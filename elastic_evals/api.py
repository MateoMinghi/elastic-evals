"""HTTP API (FastAPI) for the UI, the CLI and integrations.

Auth: `Authorization: Bearer <key_id>.<secret>`; the key belongs to one tenant and one role, and
every query is filtered by that tenant (document-level security is never the only control).
With EE_AUTH=off (development only) the tenant comes from the X-EE-Tenant header and the role
is admin. Mutations are written to the audit log.

    uvicorn elastic_evals.api:app     (or: python -m elastic_evals serve)
"""

import datetime as dt
import hashlib
import json
import os
import secrets
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import annotation, drift
from . import experiments as exps
from .conversation import build as build_conversation
from .registry import PolicyViolation, Registry
from .store.opensearch import OpenSearch, tenant_filter
from .store.postgres import Jsonb, Postgres
from .worker import fetch_conversation


class Ctx(BaseModel):
    tenant: str
    actor: str
    scopes: list[str]

    def allows(self, scope: str) -> bool:
        res, act = scope.split(":")
        return any(s in ("*:*", scope, f"{res}:*") for s in self.scopes)


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def create_app(pg: Postgres | None = None, os_: OpenSearch | None = None, registry: Registry | None = None,
               auth: bool | None = None, sentry_bridge=None) -> FastAPI:
    pg = pg or Postgres()
    os_ = os_ or OpenSearch(prefix=os.environ.get("EE_INDEX_PREFIX", "ee"))
    reg = registry or Registry(pg)
    auth = (os.environ.get("EE_AUTH", "on") != "off") if auth is None else auth
    app = FastAPI(title="elastic-evals", version="0.2.0")
    app.state.pg, app.state.os, app.state.registry = pg, os_, reg

    @app.exception_handler(PolicyViolation)
    async def _policy(_, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(KeyError)
    async def _missing(_, exc):
        return JSONResponse({"detail": f"not found: {exc}"}, status_code=404)

    def ctx(request: Request) -> Ctx:
        if not auth:
            return Ctx(tenant=request.headers.get("x-ee-tenant", "dev"), actor="dev", scopes=["*:*"])
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer ") or "." not in header[7:]:
            raise HTTPException(401, "missing API key")
        key_id, secret = header[7:].strip().split(".", 1)
        row = pg.one("""SELECT k.tenant_id, k.secret_hash, r.scopes FROM api_keys k JOIN roles r ON r.id = k.role_id
                        WHERE k.id=%s AND k.revoked_at IS NULL""", key_id)
        if row is None or not secrets.compare_digest(row["secret_hash"], hash_secret(secret)):
            raise HTTPException(401, "invalid API key")
        return Ctx(tenant=row["tenant_id"], actor=f"key:{key_id}", scopes=row["scopes"])

    def need(scope):
        def dep(c: Ctx = Depends(ctx)) -> Ctx:
            if not c.allows(scope):
                raise HTTPException(403, f"missing scope {scope}")
            return c
        return dep

    def pool(c: Ctx):
        return reg.pool(c.tenant)

    # -- health ----------------------------------------------------------------------------------
    @app.get("/healthz")
    def healthz():
        pg.one("SELECT 1 AS ok")
        os_.req("GET", "/_cluster/health")
        return {"ok": True}

    # -- admin: tenants and keys -----------------------------------------------------------------
    class TenantIn(BaseModel):
        id: str
        name: str | None = None
        tier: str = "pooled"
        allow_hosted: bool = True
        retention: dict = {}

    @app.post("/v1/tenants")
    def create_tenant(body: TenantIn, c: Ctx = Depends(need("tenants:write"))):
        reg.ensure_tenant(body.id, body.name, body.tier, None, body.allow_hosted)
        pg.exec("UPDATE tenants SET retention=%s WHERE id=%s", Jsonb(body.retention), body.id)
        if body.tier == "dedicated":
            os_.setup(pools=(reg.pool(body.id),))
        pg.audit(c.actor, "tenant.create", body.id, body.id)
        return reg.tenant(body.id)

    @app.post("/v1/api-keys")
    def create_key(role: str = Body(..., embed=True), tenant: str | None = Body(None, embed=True),
                   c: Ctx = Depends(need("keys:write"))):
        tenant = tenant or c.tenant
        if tenant != c.tenant and not c.allows("*:*"):
            raise HTTPException(403, "cannot create keys for another tenant")
        key_id, secret = f"ek_{secrets.token_hex(6)}", secrets.token_urlsafe(24)
        pg.exec("INSERT INTO api_keys (id, tenant_id, secret_hash, role_id) VALUES (%s, %s, %s, %s)",
                key_id, tenant, hash_secret(secret), role)
        pg.audit(c.actor, "api_key.create", key_id, tenant, role=role)
        return {"key": f"{key_id}.{secret}", "id": key_id, "tenant": tenant, "role": role}

    @app.get("/v1/me")
    def me(c: Ctx = Depends(ctx)):
        return {**c.model_dump(), "tenant_info": reg.tenant(c.tenant) if pg.one("SELECT 1 AS x FROM tenants WHERE id=%s", c.tenant) else None}

    # -- conversations ---------------------------------------------------------------------------
    @app.get("/v1/conversations")
    def list_conversations(c: Ctx = Depends(need("traces:read")), q: str | None = None, flag: str | None = None,
                           agent: str | None = None, error: bool | None = None, lang: str | None = None,
                           experiment: str | None = None, frm: str | None = Query(None, alias="from"),
                           to: str | None = None, size: int = Query(50, le=500), search_after: str | None = None):
        filt: list[dict[str, Any]] = [tenant_filter(c.tenant)]
        if flag:
            filt.append({"term": {"flags": flag}})
        if agent:
            filt.append({"term": {"agent_name": agent}})
        if error is not None:
            filt.append({"term": {"error": error}})
        if lang:
            filt.append({"term": {"lang": lang}})
        if experiment:
            filt.append({"term": {"experiment_run_id": experiment}})
        if frm or to:
            filt.append({"range": {"start_time": {k: v for k, v in (("gte", frm), ("lte", to)) if v}}})
        must = []
        if q:
            # full text over kept and staged content, then the rollups of the matching conversations
            hits = os_.hits("payload", {"size": 1000, "_source": ["trace_id"], "query": {"bool": {"filter": [tenant_filter(c.tenant)],
                            "must": [{"multi_match": {"query": q, "fields": ["input_text", "output_text"]}}]}}}, pool(c))
            hits += os_.hits("stage", {"size": 1000, "_source": ["trace_id"], "query": {"bool": {"filter": [tenant_filter(c.tenant)],
                             "must": [{"multi_match": {"query": q, "fields": ["input_text", "output_text"]}}]}}}, pool(c))
            ids = sorted({h["trace_id"] for h in hits})
            must.append({"bool": {"should": [{"terms": {"trace_ids": ids}},
                                             {"match": {"first_user_message": q}}], "minimum_should_match": 1}})
        body = {"size": size, "query": {"bool": {"filter": filt, "must": must}},
                "sort": [{"start_time": "desc"}, {"conversation_id": "asc"}],
                "aggs": {"flags": {"terms": {"field": "flags", "size": 50}},
                         "agents": {"terms": {"field": "agent_name", "size": 50}},
                         "langs": {"terms": {"field": "lang", "size": 50}}}}
        if search_after:
            body["search_after"] = json.loads(search_after)
        res = os_.search("traces", body, pool(c))
        from .store.opensearch import dedupe
        raw = res["hits"]["hits"]
        items = dedupe(raw)
        nxt = json.dumps(raw[-1]["sort"]) if len(raw) == size else None
        aggs = {k: {b["key"]: b["doc_count"] for b in v["buckets"]} for k, v in (res.get("aggregations") or {}).items()}
        return {"items": items, "total": res["hits"]["total"]["value"], "next": nxt, "facets": aggs}

    def _rollup(c: Ctx, cid):
        hits = os_.hits("traces", {"size": 5, "query": {"bool": {"filter": [tenant_filter(c.tenant),
                                                                          {"term": {"conversation_id": cid}}]}}}, pool(c))
        return max(hits, key=lambda h: h.get("updated_at", "")) if hits else None

    @app.get("/v1/conversations/{cid}")
    def get_conversation(cid: str, c: Ctx = Depends(need("traces:read"))):
        rollup = _rollup(c, cid)
        row = pg.one("SELECT kind, status, n_spans, last_error FROM trace_tracker WHERE tenant_id=%s AND conversation_id=%s",
                     c.tenant, cid)
        kind = row["kind"] if row else ("session" if rollup and rollup.get("session_id") else "trace")
        spans, content = fetch_conversation(os_, c.tenant, cid, kind, pool(c))
        if not spans and rollup is None:
            raise HTTPException(404, "conversation not found")
        conv = build_conversation(cid, spans, content, c.tenant) if spans else None
        scores = os_.hits("scores", {"size": 1000, "query": {"bool": {"filter": [tenant_filter(c.tenant),
                                                                               {"term": {"conversation_id": cid}}]}},
                                     "sort": [{"timestamp": "desc"}]}, pool(c))
        by_span = {d["span_id"]: d for d in content}
        span_view = [dict(s, content=(by_span.get(s["span_id"]) or {}).get("raw")) for s in spans]
        trace_ids = (rollup or {}).get("trace_ids") or sorted({s["trace_id"] for s in spans})
        links = pg.all("SELECT * FROM sentry_links WHERE tenant_id=%s AND trace_id = ANY(%s)", c.tenant, trace_ids)
        if not links and sentry_bridge is not None and sentry_bridge.org and sentry_bridge.token:
            links = [dict(i, trace_id=t, source="lookup") for t in trace_ids[:5] for i in sentry_bridge.issues_for_trace(t)]
        return {"conversation": conv, "rollup": rollup, "spans": span_view, "scores": scores, "tracker": row,
                "sentry": links}

    @app.get("/v1/conversations/{cid}/similar")
    def similar(cid: str, k: int = 10, c: Ctx = Depends(need("traces:read"))):
        mine = os_.hits("embeddings", {"size": 1, "query": {"bool": {"filter": [tenant_filter(c.tenant),
                                                                              {"term": {"conversation_id": cid}}]}},
                                       "sort": [{"timestamp": "desc"}]}, pool(c))
        if not mine:
            raise HTTPException(404, "no embedding for this conversation (run the worker with --embed)")
        res = os_.search("embeddings", {"size": k + 1, "_source": {"excludes": ["vector"]}, "query": {"knn": {"vector": {
            "vector": mine[0]["vector"], "k": k + 1, "filter": {"bool": {"filter": [tenant_filter(c.tenant)]}}}}}}, pool(c))
        return [dict(h["_source"], score=h["_score"]) for h in res["hits"]["hits"] if h["_source"]["conversation_id"] != cid][:k]

    class ScoreIn(BaseModel):
        conversation_id: str
        check: str
        value: float | None = None
        label: str | None = None
        explanation: str | None = None
        supersedes: str | None = None

    @app.post("/v1/scores")
    def correct_score(body: ScoreIn, c: Ctx = Depends(need("scores:write"))):
        """A human correction: a new score that supersedes an old one (scores are never updated)."""
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        sid = hashlib.sha256(f"manual|{c.actor}|{body.conversation_id}|{body.check}|{now}".encode()).hexdigest()
        doc = {"id": sid, "timestamp": now, "tenant_id": c.tenant, "conversation_id": body.conversation_id,
               "check": body.check, "question": body.check, "value": body.value, "label": body.label,
               "explanation": body.explanation, "p": 1.0, "decision": True, "sample_reason": "annotation",
               "annotator": c.actor, "supersedes": body.supersedes,
               "evaluator": {"id": "human", "kind": "human", "tier": 4, "version": c.actor, "criteria_version": None}}
        os_.bulk("scores", [(sid, doc, None)], pool(c), refresh="wait_for")
        pg.audit(c.actor, "score.correct", body.conversation_id, c.tenant, check=body.check, supersedes=body.supersedes)
        return doc

    # -- score explorer --------------------------------------------------------------------------
    @app.get("/v1/scores/summary")
    def score_summary(c: Ctx = Depends(need("scores:read")), frm: str | None = Query(None, alias="from"),
                      to: str | None = None, agent: str | None = None, calibration_id: str | None = None,
                      experiment: str | None = None, interval: str = "day"):
        # production decisions only, unless an experiment run is asked for
        filt: list[dict[str, Any]] = [tenant_filter(c.tenant), {"term": {"decision": True}}]
        if experiment:
            filt += [{"term": {"experiment.run_id": experiment}}, {"term": {"sample_reason": "experiment"}}]
        else:
            filt.append({"term": {"sample_reason": "route"}})
        if frm or to:
            filt.append({"range": {"timestamp": {k: v for k, v in (("gte", frm), ("lte", to)) if v}}})
        if agent:
            filt.append({"term": {"trace_ctx.agent": agent}})
        if calibration_id:
            filt.append({"term": {"calibration_id": calibration_id}})
        res = os_.search("scores", {"size": 0, "query": {"bool": {"filter": filt}}, "aggs": {"check": {
            "terms": {"field": "check", "size": 100}, "aggs": {
                "flagged": {"filter": {"term": {"value": 1}}},
                "tier": {"terms": {"field": "evaluator.tier"}},
                "evaluator": {"terms": {"field": "evaluator.id"}},
                "lang": {"terms": {"field": "lang", "size": 20}, "aggs": {"flagged": {"filter": {"term": {"value": 1}}}}},
                "cost": {"sum": {"field": "cost_usd"}},
                "over_time": {"date_histogram": {"field": "timestamp", "calendar_interval": interval},
                              "aggs": {"flagged": {"filter": {"term": {"value": 1}}}}}}}}}, pool(c))
        out = {}
        for b in res["aggregations"]["check"]["buckets"]:
            n = b["doc_count"]
            out[b["key"]] = {
                "n": n, "flagged": b["flagged"]["doc_count"], "rate": b["flagged"]["doc_count"] / n if n else 0,
                "decided_by_tier": {str(t["key"]): t["doc_count"] / n for t in b["tier"]["buckets"]},
                "decided_by": {t["key"]: t["doc_count"] / n for t in b["evaluator"]["buckets"]},
                "by_lang": {lb["key"]: {"n": lb["doc_count"], "rate": lb["flagged"]["doc_count"] / lb["doc_count"]}
                            for lb in b["lang"]["buckets"]},
                "cost_usd": b["cost"]["value"],
                "over_time": [{"t": tb["key_as_string"], "n": tb["doc_count"], "flagged": tb["flagged"]["doc_count"]}
                              for tb in b["over_time"]["buckets"]]}
        return out

    # -- suites and calibrations -----------------------------------------------------------------
    @app.get("/v1/suites")
    def suites(c: Ctx = Depends(need("suites:read"))):
        rows = pg.all("""SELECT s.id, s.agent_name, s.created_at,
                         (SELECT version FROM suite_versions v WHERE v.tenant_id=s.tenant_id AND v.suite_id=s.id
                          ORDER BY created_at DESC LIMIT 1) AS latest_version,
                         (SELECT id FROM calibrations k WHERE k.tenant_id=s.tenant_id AND k.suite_id=s.id AND status='active') AS active,
                         (SELECT id FROM calibrations k WHERE k.tenant_id=s.tenant_id AND k.suite_id=s.id AND status='shadow') AS shadow
                         FROM suites s WHERE s.tenant_id=%s ORDER BY s.id""", c.tenant)
        return rows

    @app.put("/v1/suites/{suite_id}")
    def put_suite(suite_id: str, yaml: str = Body(..., embed=True), agent_name: str | None = Body(None, embed=True),
                  c: Ctx = Depends(need("suites:write"))):
        try:
            version = reg.put_suite(c.tenant, suite_id, yaml, agent_name)
        except (ValueError, TypeError, KeyError) as exc:
            raise HTTPException(422, f"invalid suite: {exc}") from exc
        pg.audit(c.actor, "suite.put", f"{suite_id}@{version}", c.tenant)
        return {"id": suite_id, "version": version}

    @app.get("/v1/suites/{suite_id}")
    def get_suite(suite_id: str, c: Ctx = Depends(need("suites:read"))):
        versions = pg.all("SELECT version, created_at, yaml FROM suite_versions WHERE tenant_id=%s AND suite_id=%s ORDER BY created_at DESC",
                          c.tenant, suite_id)
        if not versions:
            raise HTTPException(404, "suite not found")
        return {"id": suite_id, "versions": versions, "calibrations": reg.calibrations(c.tenant, suite_id)}

    @app.get("/v1/calibrations/{cal_id}")
    def get_calibration(cal_id: str, c: Ctx = Depends(need("calibrations:read"))):
        row = reg.calibration(cal_id)
        if row["tenant_id"] != c.tenant:
            raise HTTPException(404, "calibration not found")
        return row

    @app.post("/v1/suites/{suite_id}/calibrations")
    def start_calibration(suite_id: str, label_set_id: str = Body(..., embed=True), local_only: bool = Body(False, embed=True),
                          c: Ctx = Depends(need("calibrations:write"))):
        """Calibrate from a label set (conversations rebuilt from OpenSearch). Runs synchronously; for
        large label sets use the CLI or a job runner."""
        version = pg.one("SELECT version FROM suite_versions WHERE tenant_id=%s AND suite_id=%s ORDER BY created_at DESC LIMIT 1",
                         c.tenant, suite_id)
        if version is None:
            raise HTTPException(404, "suite not found")
        suite = reg.suite(c.tenant, suite_id, version["version"])
        tenant = reg.tenant(c.tenant)
        suite.policy.allow_hosted = suite.policy.allow_hosted and not local_only and tenant["allow_hosted"]
        convs, labels = annotation.labelled_conversations(pg, os_, reg, c.tenant, label_set_id)
        if len(convs) < 20:
            raise HTTPException(422, f"need at least 20 labelled conversations, have {len(convs)}")
        from .calibrate import calibrate
        client = None
        if suite.policy.allow_hosted:
            from .providers import OpenRouter
            client = OpenRouter()
        cal, evaluators = calibrate(suite, convs, labels, client)
        reg.save_calibration(c.tenant, suite_id, cal, evaluators, label_set_id, actor=c.actor)
        return {"id": cal["id"], "status": "candidate", "suite_choice": cal["suite_choice"], "report": cal["report"]}

    def _owned(cal_id, c):
        if reg.calibration(cal_id)["tenant_id"] != c.tenant:
            raise HTTPException(404, "calibration not found")

    @app.post("/v1/calibrations/{cal_id}/shadow")
    def shadow(cal_id: str, c: Ctx = Depends(need("calibrations:write"))):
        _owned(cal_id, c)
        reg.start_shadow(cal_id, c.actor)
        return reg.calibration(cal_id)

    @app.post("/v1/calibrations/{cal_id}/promote")
    def promote(cal_id: str, force: bool = False, c: Ctx = Depends(need("calibrations:write"))):
        _owned(cal_id, c)
        row = reg.calibration(cal_id)
        baseline = None
        if row["status"] == "shadow":
            cmp = drift.compare_shadow(os_, reg, c.tenant, row["suite_id"])
            if not cmp.get("promote") and not force:
                raise HTTPException(409, {"message": "candidate does not yet agree with the audit judge at least as "
                                                     "well as the active calibration (use force=true to override)",
                                          "comparison": cmp})
            baseline = {k: {"agreement": v} for k, v in (cmp.get("candidate_agreement") or {}).items()} or None
        reg.promote(cal_id, baseline, c.actor)
        return reg.calibration(cal_id)

    @app.post("/v1/calibrations/{cal_id}/reject")
    def reject(cal_id: str, c: Ctx = Depends(need("calibrations:write"))):
        _owned(cal_id, c)
        reg.reject(cal_id, c.actor)
        return reg.calibration(cal_id)

    @app.get("/v1/suites/{suite_id}/shadow-comparison")
    def shadow_comparison(suite_id: str, days: float = 7, c: Ctx = Depends(need("calibrations:read"))):
        return drift.compare_shadow(os_, reg, c.tenant, suite_id, days)

    @app.post("/v1/suites/{suite_id}/drift-check")
    def drift_check(suite_id: str, days: float = 7, min_n: int = 50, threshold: float = 0.05,
                    c: Ctx = Depends(need("calibrations:write"))):
        notify = sentry_bridge.quality_regression if sentry_bridge else None
        return drift.check(pg, os_, reg, c.tenant, suite_id, days, min_n, threshold, notify)

    @app.get("/v1/drift-alerts")
    def drift_alerts(status: str = "open", c: Ctx = Depends(need("calibrations:read"))):
        return pg.all("SELECT * FROM drift_alerts WHERE tenant_id=%s AND status=%s ORDER BY created_at DESC", c.tenant, status)

    # -- labels and annotation -------------------------------------------------------------------
    @app.post("/v1/annotation-queues")
    def create_queue(suite_id: str = Body(...), name: str = Body(...), checks: list[str] = Body(...),
                     fill_rules: list[dict] = Body([]), c: Ctx = Depends(need("annotations:write"))):
        return annotation.create_queue(pg, c.tenant, suite_id, name, checks, fill_rules)

    @app.get("/v1/annotation-queues")
    def list_queues(c: Ctx = Depends(need("annotations:read"))):
        return [annotation.queue(pg, r["id"]) for r in pg.all("SELECT id FROM annotation_queues WHERE tenant_id=%s ORDER BY created_at", c.tenant)]

    def _queue(qid, c):
        q = annotation.queue(pg, qid)
        if q["tenant_id"] != c.tenant:
            raise HTTPException(404, "queue not found")
        return q

    @app.post("/v1/annotation-queues/{qid}/items")
    def add_items(qid: str, conversation_ids: list[str] = Body(..., embed=True), c: Ctx = Depends(need("annotations:write"))):
        _queue(qid, c)
        return {"added": annotation.add_items(pg, qid, conversation_ids)}

    @app.post("/v1/annotation-queues/{qid}/fill")
    def fill(qid: str, days: float = 1, c: Ctx = Depends(need("annotations:write"))):
        _queue(qid, c)
        return annotation.fill(pg, os_, reg, qid, days)

    @app.post("/v1/annotation-queues/{qid}/next")
    def next_item(qid: str, c: Ctx = Depends(need("annotations:write"))):
        q = _queue(qid, c)
        item = annotation.next_item(pg, qid, c.actor)
        if item is None:
            return {"item": None, "queue": q}
        detail = get_conversation(item["conversation_id"], c)
        return {"item": item, "queue": q, **detail}

    @app.post("/v1/annotation-queues/{qid}/items/{cid}/labels")
    def submit(qid: str, cid: str, labels: dict[str, Any] = Body(..., embed=True), lang: str | None = Body(None, embed=True),
               c: Ctx = Depends(need("annotations:write"))):
        _queue(qid, c)
        try:
            annotation.submit(pg, os_, reg, qid, cid, labels, c.actor, lang)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"ok": True}

    @app.post("/v1/annotation-queues/{qid}/items/{cid}/skip")
    def skip(qid: str, cid: str, c: Ctx = Depends(need("annotations:write"))):
        _queue(qid, c)
        annotation.skip(pg, qid, cid)
        return {"ok": True}

    @app.get("/v1/label-sets/{lsid}/agreement")
    def label_agreement(lsid: str, calibration_id: str | None = None, c: Ctx = Depends(need("annotations:read"))):
        """Human labels against the routed decisions: kappa, accuracy, per-class precision/recall."""
        labels = annotation.labels_of(pg, lsid, ("human",))
        convs = sorted({cid for cid, _ in labels})
        filt = [tenant_filter(c.tenant), {"term": {"decision": True}}, {"terms": {"conversation_id": convs}}]
        if calibration_id:
            filt.append({"term": {"calibration_id": calibration_id}})
        hits = os_.hits("scores", {"size": 10000, "query": {"bool": {"filter": filt}}, "sort": [{"timestamp": "asc"}]}, pool(c))
        pred = {(h["conversation_id"], h["check"]): (h["label"] if h["value"] is None else int(h["value"]))
                for h in hits if h["evaluator"]["kind"] != "human"}
        per = {}
        for (cid, chk), y in labels.items():
            if (cid, chk) in pred:
                per.setdefault(chk, []).append((y, pred[(cid, chk)]))
        return {chk: annotation.agreement_stats(p) for chk, p in per.items()}

    # -- datasets and experiments ----------------------------------------------------------------
    @app.post("/v1/datasets")
    def create_dataset(name: str = Body(...), description: str | None = Body(None), items: list[dict] = Body([]),
                       c: Ctx = Depends(need("datasets:write"))):
        did = exps.create_dataset(pg, c.tenant, name, description)
        if items:
            exps.put_items(pg, did, items)
        return {"id": did}

    @app.get("/v1/datasets")
    def list_datasets(c: Ctx = Depends(need("datasets:read"))):
        return pg.all("""SELECT d.*, (SELECT count(*) FROM dataset_items i WHERE i.dataset_id=d.id AND valid_to IS NULL) AS items
                         FROM datasets d WHERE tenant_id=%s ORDER BY created_at DESC""", c.tenant)

    def _dataset(did, c):
        d = pg.one("SELECT * FROM datasets WHERE id=%s AND tenant_id=%s", did, c.tenant)
        if d is None:
            raise HTTPException(404, "dataset not found")
        return d

    @app.post("/v1/datasets/{did}/items")
    def put_items(did: str, items: list[dict] = Body([], embed=True), from_conversations: list[str] = Body([], embed=True),
                  c: Ctx = Depends(need("datasets:write"))):
        _dataset(did, c)
        for cid in from_conversations:  # snapshot production conversations as items
            detail = get_conversation(cid, c)
            conv = detail["conversation"]
            if conv:
                items.append({"id": cid, "input": {"turns": conv["turns"], "record": conv["record"], "lang": conv["lang"]},
                              "expected": {}, "source_conversation_id": cid})
        exps.put_items(pg, did, items)
        return {"items": len(exps.items_of(pg, did))}

    @app.get("/v1/datasets/{did}/items")
    def get_items(did: str, at: str | None = None, c: Ctx = Depends(need("datasets:read"))):
        _dataset(did, c)
        return exps.items_of(pg, did, at)

    @app.post("/v1/experiments")
    def record_run(dataset_id: str = Body(...), name: str = Body(...), result: dict = Body(...),
                   suite_id: str | None = Body(None), calibration_id: str | None = Body(None),
                   baseline_run_id: str | None = Body(None), c: Ctx = Depends(need("experiments:write"))):
        """Store a run produced by `elastic_evals experiment` or the pytest plugin."""
        _dataset(dataset_id, c)
        rid = exps.store_run(pg, os_, pool(c), c.tenant, dataset_id, name, result, suite_id, calibration_id, baseline_run_id)
        return {"id": rid}

    @app.get("/v1/experiments")
    def list_runs(dataset_id: str | None = None, c: Ctx = Depends(need("experiments:read"))):
        return pg.all("""SELECT id, dataset_id, name, status, summary, baseline_run_id, created_at FROM experiment_runs
                         WHERE tenant_id=%s AND (%s::text IS NULL OR dataset_id=%s) ORDER BY created_at DESC""",
                      c.tenant, dataset_id, dataset_id)

    @app.get("/v1/experiments/compare")
    def compare_runs(a: str, b: str, c: Ctx = Depends(need("experiments:read"))):
        runs = [exps.load_run(pg, r, os_, pool(c)) for r in (a, b)]
        if any(r["tenant_id"] != c.tenant for r in runs):
            raise HTTPException(404, "run not found")
        return exps.compare(*runs)

    # -- model prices (the collector's price book) -----------------------------------------------
    @app.get("/v1/model-prices")
    def prices(c: Ctx = Depends(need("prices:read"))):
        return [dict(r, start_date=str(r["start_date"])) for r in pg.all("SELECT * FROM model_prices ORDER BY priority DESC, start_date DESC")]

    @app.put("/v1/model-prices")
    def put_prices(rows: list[dict] = Body(...), c: Ctx = Depends(need("prices:write"))):
        with pg.tx() as cx:
            cx.execute("DELETE FROM model_prices")
            for r in rows:
                cx.execute("INSERT INTO model_prices (model_pattern, start_date, tiers, priority) VALUES (%s, %s, %s, %s)",
                           (r["model_pattern"], r.get("start_date", "1970-01-01"), Jsonb(r["tiers"]), r.get("priority", 0)))
        pg.audit(c.actor, "prices.put", None, c.tenant, n=len(rows))
        return {"n": len(rows)}

    @app.get("/v1/audit-log")
    def audit_log(limit: int = 100, c: Ctx = Depends(need("audit:read"))):
        return pg.all("SELECT * FROM audit_log WHERE tenant_id=%s ORDER BY at DESC LIMIT %s", c.tenant, limit)

    # -- Sentry bridge ---------------------------------------------------------------------------
    if sentry_bridge is not None:
        app.include_router(sentry_bridge.router(pg, reg, os_))

    # -- UI --------------------------------------------------------------------------------------
    ui = Path(os.environ.get("EE_UI_DIR", Path(__file__).resolve().parent.parent / "ui" / "dist"))
    if ui.exists():
        app.mount("/assets", StaticFiles(directory=ui / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            f = ui / path
            return FileResponse(f if path and f.is_file() else ui / "index.html")

    return app


class _Lazy:
    """`uvicorn elastic_evals.api:app` builds the app on first use, so importing this module is cheap."""

    def __init__(self):
        self._app = None

    async def __call__(self, scope, receive, send):
        if self._app is None:
            bridge = None
            if os.environ.get("EE_SENTRY_CLIENT_SECRET"):
                from .sentry import Bridge
                bridge = Bridge.from_env()
            self._app = create_app(sentry_bridge=bridge)
        await self._app(scope, receive, send)


app = _Lazy()
