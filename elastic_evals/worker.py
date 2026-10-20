"""Evaluation worker (DESIGN.md §5.2 step 5, §6.5, §7).

Loop: claim conversations the tracker marked due -> read their spans and staged content from
OpenSearch -> build the conversation -> score it with the active calibration of every suite that
covers its agent (and, in shadow, with a candidate calibration) -> write scores, the traces-*
rollup and metrics -> take the retention decision (copy kept content from stage to payload) ->
mark the conversation evaluated.

Several workers can run side by side: claims use FOR UPDATE SKIP LOCKED, and every write is
idempotent (deterministic ids).
"""

import asyncio
import datetime as dt
import logging
import os
import socket
from collections import defaultdict

from .conversation import build as build_conversation
from .conversation import rollup
from .engine import Engine, unit_hash
from .registry import PolicyViolation, Registry
from .store.opensearch import OpenSearch
from .store.postgres import Postgres

log = logging.getLogger("elastic_evals.worker")

MAX_ATTEMPTS = 3
DEFAULT_KEEP_RATE = 0.05  # random share of unremarkable conversations whose content is kept


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


CLAIM = """
UPDATE trace_tracker t SET status='claimed', claimed_by=%(worker)s, claimed_at=now(), attempts=t.attempts+1
FROM (SELECT tenant_id, conversation_id FROM trace_tracker
      WHERE (status='open' AND due_at <= now())
         OR (status='claimed' AND claimed_at < now() - interval '10 minutes')
      ORDER BY due_at LIMIT %(limit)s FOR UPDATE SKIP LOCKED) d
WHERE t.tenant_id=d.tenant_id AND t.conversation_id=d.conversation_id
RETURNING t.*
"""


def retention_decision(conv, spans, scores, keep_rate: float):
    """DESIGN.md §7: keep content for errors, Sentry-linked traces, tier-0 failures, flagged
    conversations, the audit sample, experiments, and a random share of the rest."""
    reasons = []
    if any(s.get("status") == "ERROR" for s in spans):
        reasons.append("error")
    if any(s.get("sentry_event_id") for s in spans):
        reasons.append("sentry")
    decided = [s for s in scores if s["decision"]]
    if any(s["evaluator"]["tier"] == 0 and s["value"] == 1 for s in decided):
        reasons.append("tier0_failure")
    if any(s["value"] == 1 for s in decided):
        reasons.append("flagged")
    if any(s["decision"] and s["p"] is not None and 0.3 < s["p"] < 0.7 and s["value"] is not None for s in decided):
        reasons.append("low_confidence")
    if any(s["sample_reason"] == "random_audit" for s in scores):
        reasons.append("audit")
    if conv["experiment"]["run_id"]:
        reasons.append("experiment")
    if not reasons and unit_hash(conv["id"] + "|keep") < keep_rate:
        reasons.append("random")
    return bool(reasons), reasons


def fetch_conversation(os_: OpenSearch, tenant: str, conversation_id: str, kind: str, pool: str):
    if kind == "session":
        res = os_.search("obs", {"size": 0, "query": {"bool": {"filter": [
            {"term": {"tenant_id": tenant}}, {"term": {"session_id": conversation_id}}]}},
            "aggs": {"t": {"terms": {"field": "trace_id", "size": 10000}}}}, pool)
        trace_ids = sorted(b["key"] for b in res["aggregations"]["t"]["buckets"])
    else:
        trace_ids = [conversation_id]
    if not trace_ids:
        return [], []
    filt = [{"term": {"tenant_id": tenant}}, {"terms": {"trace_id": trace_ids}}]
    routing = ",".join(trace_ids) if len(trace_ids) <= 64 else None
    spans = os_.hits("obs", {"query": {"bool": {"filter": filt}}, "size": 10000, "sort": [{"start_time": "asc"}]},
                     pool, routing)
    if not spans:
        return [], []
    # staged content expires after a few days; content the retention decision kept lives in payload
    content = os_.hits("payload", {"query": {"bool": {"filter": filt}}, "size": 10000}, pool, routing)
    content += os_.hits("stage", {"query": {"bool": {"filter": filt}}, "size": 10000}, pool, routing)
    return spans, list({d["span_id"]: d for d in content}.values())


class Worker:
    def __init__(self, pg: Postgres, os_: OpenSearch, registry: Registry | None = None, worker_id: str | None = None,
                 batch: int = 32, embed: bool = False, client=None, retry_s: float = 30):
        self.pg, self.os, self.registry = pg, os_, registry or Registry(pg)
        self.retry_s = retry_s
        self.id = worker_id or f"{socket.gethostname()}-{os.getpid()}"
        self.batch, self.embed, self.client = batch, embed, client

    # -- reading ---------------------------------------------------------------------------------
    def spans_of(self, tenant, row, pool):
        """All spans and staged content of a conversation. For a session, find its traces first:
        usually only root spans carry the session id."""
        return fetch_conversation(self.os, tenant, row["conversation_id"], row["kind"], pool)



    # -- one batch -------------------------------------------------------------------------------
    def claim(self, limit=None):
        with self.pg.tx() as c:
            return c.execute(CLAIM, {"worker": self.id, "limit": limit or self.batch}).fetchall()

    def finish(self, row, ok: bool, error: str | None = None):
        if ok:
            self.pg.exec("""UPDATE trace_tracker SET status = CASE WHEN n_spans > %s THEN 'open' ELSE 'evaluated' END,
                            evaluated_at=now(), last_error=NULL WHERE tenant_id=%s AND conversation_id=%s""",
                         row["n_spans"], row["tenant_id"], row["conversation_id"])
        else:
            retry = row["attempts"] < MAX_ATTEMPTS
            self.pg.exec("""UPDATE trace_tracker SET status=%s, last_error=%s,
                            due_at = now() + make_interval(secs => %s * attempts)
                            WHERE tenant_id=%s AND conversation_id=%s""",
                         "open" if retry else "error", (error or "")[:2000], self.retry_s, row["tenant_id"],
                         row["conversation_id"])

    async def process(self, rows):
        """Score a batch of claimed tracker rows. Returns per-conversation outcomes."""
        groups = defaultdict(list)   # (tenant, calibration id, role) -> [conv]
        loaded, outcomes = {}, {}
        for row in rows:
            key = (row["tenant_id"], row["conversation_id"])
            try:
                pool = self.registry.pool(row["tenant_id"])
                spans, stage = self.spans_of(row["tenant_id"], row, pool)
                lagging = len(spans) < row["n_spans"] and row["attempts"] < MAX_ATTEMPTS
                if not spans or lagging:  # ingest has not indexed everything the tracker saw yet
                    outcomes[key] = ("retry", f"{len(spans)} of {row['n_spans']} spans indexed")
                    continue
                conv = build_conversation(row["conversation_id"], spans, stage, row["tenant_id"])
                if conv["internal"]:
                    outcomes[key] = ("skip", "internal")
                    continue
                loaded[key] = (row, conv, spans, stage, pool)
                for suite_id in self.registry.suites_for(row["tenant_id"], conv["trace_ctx"]["agent"]):
                    for role in ("active", "shadow"):
                        cal = self.registry.by_status(row["tenant_id"], suite_id, role)
                        if cal:
                            groups[(row["tenant_id"], cal["id"], role)].append(conv)
            except Exception as exc:  # noqa: BLE001 - one bad conversation must not stop the batch
                log.exception("reading %s failed", key)
                outcomes[key] = ("error", repr(exc))

        scores = defaultdict(list)  # key -> score records
        eval_cost = defaultdict(float)
        for (tenant, cal_id, role), convs in groups.items():
            try:
                suite, cal, evaluators = self.registry.engine(cal_id, self.client)
                # a shadow candidate is scored but not served, and not audited twice
                results = await Engine(suite, cal, evaluators, audit=(role == "active")).run_async(convs)
            except PolicyViolation as exc:
                for c in convs:
                    outcomes[(tenant, c["id"])] = ("error", str(exc))
                continue
            except Exception as exc:  # noqa: BLE001
                log.exception("scoring with %s failed", cal_id)
                for c in convs:
                    outcomes[(tenant, c["id"])] = ("error", repr(exc))
                continue
            stamp = now_utc().isoformat()
            for c, r in zip(convs, results):
                for s in r["scores"]:
                    s["timestamp"] = stamp
                    if role == "shadow":
                        s["decision"], s["sample_reason"] = False, "candidate"
                scores[(tenant, c["id"])] += r["scores"]
                if role == "active":
                    eval_cost[(tenant, c["id"])] += r["usd"] + r["audit_usd"]

        self.write(loaded, scores, eval_cost, outcomes)
        return outcomes

    # -- writing ---------------------------------------------------------------------------------
    def write(self, loaded, scores, eval_cost, outcomes):
        by_pool = defaultdict(lambda: defaultdict(list))
        metrics = defaultdict(lambda: {"n": 0, "flagged": 0, "cost_usd": 0.0})
        hour = now_utc().replace(minute=0, second=0, microsecond=0).isoformat()
        for key, (row, conv, spans, stage, pool) in loaded.items():
            if key in outcomes:
                continue
            tenant = row["tenant_id"]
            sc = scores.get(key, [])
            tenant_cfg = self.registry.tenant(tenant)
            keep_rate = (tenant_cfg.get("retention") or {}).get("keep_rate", DEFAULT_KEEP_RATE)
            keep, reasons = retention_decision(conv, spans, sc, keep_rate)
            decided = {s["check"]: s for s in sc if s["decision"] and not s.get("observation_id")}
            steps = {}  # observation checks: one decision per step, rolled up per check
            for s_ in sc:
                if s_["decision"] and s_.get("observation_id"):
                    steps.setdefault(s_["check"], []).append(s_)
            doc = rollup(conv, spans)
            score_map = {c: {"value": s["value"], "label": s["label"], "p": s["p"], "evaluator": s["evaluator"]["id"],
                             "tier": s["evaluator"]["tier"]} for c, s in decided.items()}
            for c, ss in steps.items():
                flagged = [x["observation_id"] for x in ss if x["value"] == 1]
                score_map[c] = {"value": int(bool(flagged)), "steps": len(ss), "flagged_steps": flagged,
                                "evaluator": ss[0]["evaluator"]["id"], "tier": max(x["evaluator"]["tier"] for x in ss)}
            doc.update({
                "updated_at": now_utc().isoformat(), "evaluated_at": now_utc().isoformat(),
                "eval_status": "scored" if score_map else "unscored",
                "calibration_id": next(iter({s["calibration_id"] for s in decided.values()}
                                            | {x["calibration_id"] for ss in steps.values() for x in ss}), None),
                "eval_cost_usd": eval_cost.get(key, 0.0),
                "flags": sorted(c for c, v in score_map.items() if v["value"] == 1),
                "scores": score_map,
                "content_kept": keep, "retention_reasons": reasons,
            })
            # the rollup is rewritten whole: carry the Sentry links, which often arrive before evaluation
            links = self.pg.all("SELECT sentry_issue_id, status FROM sentry_links WHERE tenant_id=%s AND trace_id = ANY(%s) "
                                "ORDER BY updated_at", tenant, conv["trace_ids"])
            if links:
                doc["sentry_issue_ids"] = sorted({lk["sentry_issue_id"] for lk in links})
                doc["sentry_status"] = links[-1]["status"]
            doc = {k: v for k, v in doc.items() if v is not None}
            by_pool[pool]["traces"].append((conv["id"], doc, None))
            by_pool[pool]["scores"] += [(s["id"], s, None) for s in sc]
            if keep:
                by_pool[pool]["payload"] += [(d["_id"], {k: v for k, v in d.items() if k != "_id"}, d["trace_id"])
                                             for d in stage]
            for c, v in score_map.items():
                m = metrics[(pool, tenant, conv["trace_ctx"]["agent"] or "", c)]
                m["n"] += 1
                m["flagged"] += int(v["value"] == 1)
                m["cost_usd"] += sum(x["cost_usd"] or 0.0 for x in sc if x["check"] == c and x["decision"])
            if self.embed:
                by_pool[pool]["embeddings"].append(self._embedding(conv))
        for (pool, tenant, agent, check), m in metrics.items():
            by_pool[pool]["metrics"].append((None, {"ts": hour, "tenant_id": tenant, "agent_name": agent,
                                                    "check": check, **m}, None))
        for pool, kinds in by_pool.items():
            for kind in ("payload", "scores", "embeddings", "metrics"):
                if kinds.get(kind):
                    self.os.bulk(kind, kinds[kind], pool, op="create" if kind in ("payload", "scores") else "index")
            if kinds.get("traces"):  # rollups last: a list view never shows scores that are not written yet
                self.os.bulk("traces", kinds["traces"], pool, op="index")

    _encoder = None

    def _embedding(self, conv):
        from .views import view
        if Worker._encoder is None:
            from sentence_transformers import SentenceTransformer
            Worker._encoder = SentenceTransformer("intfloat/multilingual-e5-small", device="cpu")
        vec = Worker._encoder.encode(["passage: " + view(conv, "user|assistant")[:2000]], normalize_embeddings=True)[0]
        return (f"{conv['id']}@{conv['version']}", {"tenant_id": conv["tenant_id"], "conversation_id": conv["id"],
                "unit": "conversation", "model": "intfloat/multilingual-e5-small", "lang": conv["lang"],
                "timestamp": now_utc().isoformat(), "vector": [float(x) for x in vec]}, None)

    async def run_once(self):
        rows = await asyncio.to_thread(self.claim)
        if not rows:
            return 0
        outcomes = await self.process(rows)
        for row in rows:
            out = outcomes.get((row["tenant_id"], row["conversation_id"]))
            if out and out[0] in ("error", "retry"):
                self.finish(row, False, out[1])
            else:
                self.finish(row, True)
        return len(rows)

    async def run(self, stop: asyncio.Event | None = None, idle_sleep: float = 1.0):
        while not (stop and stop.is_set()):
            n = await self.run_once()
            if n == 0:
                await asyncio.sleep(idle_sleep)
