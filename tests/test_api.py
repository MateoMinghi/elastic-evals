"""API tests against a small real deployment: conversations go through the collectors, Kafka,
OpenSearch, the tracker and the worker once per module, then every API surface is exercised."""

import asyncio
import hashlib
import hmac
import json
import time
import uuid

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from elastic_evals import experiments as exps
from elastic_evals.api import create_app, hash_secret
from elastic_evals.calibrate import calibrate
from elastic_evals.registry import ArtifactStore, Registry
from elastic_evals.replay import replay
from elastic_evals.sentry import Bridge
from elastic_evals.suite import Suite
from elastic_evals.tracker import Timing
from elastic_evals.tracker import run as run_tracker
from elastic_evals.worker import Worker

from .conftest import KAFKA, ROOT, load

pytestmark = pytest.mark.infra

SUITE = yaml.safe_load((ROOT / "examples/banking_suite.yaml").read_text())
SUITE["evaluators"] = [{"id": "rules", "type": "rules"}]
SUITE["policy"] = {"allow_hosted": False, "target_accuracy": 0.95}
SUITE_YAML = yaml.safe_dump(SUITE, allow_unicode=True)
N = 30


class FakeSentry:
    def __init__(self):
        self.calls = []

    def handler(self, request: httpx.Request):
        self.calls.append((request.method, request.url.path, json.loads(request.content or b"{}")))
        if request.url.path.endswith("/events/latest/"):
            return httpx.Response(200, json={"eventID": "ev1", "tags": [{"key": "trace_id", "value": self.trace},
                                                                        {"key": "ee.tenant_id", "value": "acme"}]})
        if "/organizations/" in request.url.path:
            return httpx.Response(200, json=[])
        return httpx.Response(201, json={"id": "ext1"})


@pytest.fixture(scope="module")
def deployment(opensearch, postgres, collectors, tmp_path_factory):
    reg = Registry(postgres, ArtifactStore(f"file://{tmp_path_factory.mktemp('art')}"))
    reg.ensure_tenant("acme", tier="dedicated", index_pool="acme", allow_hosted=False)
    reg.ensure_tenant("other", allow_hosted=False)
    reg.put_suite("acme", "banking", SUITE_YAML, agent_name="bank-assistant")
    suite = Suite.from_yaml(SUITE_YAML)
    train = load("synthetic.jsonl", 80)
    cal, evs = calibrate(suite, train, {(c["id"], q): v for c in train for q, v in c["labels"].items()})
    reg.save_calibration("acme", "banking", cal, evs)
    reg.promote(cal["id"])
    convs = load("synthetic_c.jsonl", N)
    replay(convs, collectors["otlp"], tenant="acme")

    async def drive():
        stop = asyncio.Event()
        task = asyncio.create_task(run_tracker(postgres, KAFKA, Timing(trace_idle_s=1, session_idle_s=2),
                                               topic=collectors["topic"], group=f"t-{uuid.uuid4().hex[:6]}", stop=stop))
        worker = Worker(postgres, opensearch, reg, retry_s=1, embed=True)
        end = time.time() + 120
        while time.time() < end:
            await worker.run_once()
            row = postgres.one("SELECT count(*) FILTER (WHERE status='evaluated') AS done FROM trace_tracker WHERE tenant_id='acme'")
            if row["done"] >= N:
                break
            await asyncio.sleep(0.5)
        stop.set()
        await task

    asyncio.run(drive())
    opensearch.refresh()
    fake = FakeSentry()
    bridge = Bridge("whsecret", token="tok", base_url="https://sentry.test", org="org", ui_url="https://ui.test",
                    installation="inst-1", http=httpx.Client(transport=httpx.MockTransport(fake.handler)))
    app = create_app(postgres, opensearch, reg, auth=True, sentry_bridge=bridge)
    keys = {}
    for name, tenant, role in [("admin", "acme", "admin"), ("viewer", "acme", "viewer"), ("annot", "acme", "annotator"),
                               ("other", "other", "admin"), ("platform", "acme", "platform")]:
        secret = uuid.uuid4().hex
        postgres.exec("INSERT INTO api_keys (id, tenant_id, secret_hash, role_id) VALUES (%s, %s, %s, %s)",
                      f"k_{name}", tenant, hash_secret(secret), role)
        keys[name] = {"Authorization": f"Bearer k_{name}.{secret}"}
    return {"client": TestClient(app), "keys": keys, "convs": convs, "cal": cal, "reg": reg, "suite": suite,
            "evs": evs, "fake": fake, "pg": postgres, "os": opensearch}


def test_auth_scopes_and_isolation(deployment):
    c, k = deployment["client"], deployment["keys"]
    assert c.get("/v1/conversations").status_code == 401
    assert c.get("/v1/conversations", headers={"Authorization": "Bearer k_admin.wrong"}).status_code == 401
    assert c.get("/v1/conversations", headers=k["viewer"]).status_code == 200
    assert c.put("/v1/suites/x", json={"yaml": SUITE_YAML}, headers=k["viewer"]).status_code == 403
    # another tenant sees nothing of acme's
    other = c.get("/v1/conversations", headers=k["other"]).json()
    assert other["total"] == 0
    cid = deployment["convs"][0]["id"]
    assert c.get(f"/v1/conversations/{cid}", headers=k["other"]).status_code == 404
    assert c.get(f"/v1/calibrations/{deployment['cal']['id']}", headers=k["other"]).status_code == 404
    # a tenant admin administers its own tenant only; global scopes belong to the platform role
    assert c.post("/v1/tenants", json={"id": "evil"}, headers=k["other"]).status_code == 403
    assert c.post("/v1/api-keys", json={"role": "admin", "tenant": "acme"}, headers=k["other"]).status_code == 403
    assert c.post("/v1/api-keys", json={"role": "platform"}, headers=k["other"]).status_code == 403
    assert c.put("/v1/model-prices", json=[], headers=k["other"]).status_code == 403
    assert c.post("/v1/api-keys", json={"role": "viewer"}, headers=k["other"]).json()["tenant"] == "other"
    assert c.post("/v1/tenants", json={"id": "newco"}, headers=k["platform"]).status_code == 200
    assert c.post("/v1/api-keys", json={"role": "admin", "tenant": "newco"}, headers=k["platform"]).json()["tenant"] == "newco"


def test_list_search_and_detail(deployment):
    c, k, convs = deployment["client"], deployment["keys"]["viewer"], deployment["convs"]
    res = c.get("/v1/conversations", params={"size": 10}, headers=k).json()
    assert res["total"] == N and len(res["items"]) == 10 and res["next"]
    page2 = c.get("/v1/conversations", params={"size": 10, "search_after": res["next"]}, headers=k).json()
    assert not {i["conversation_id"] for i in page2["items"]} & {i["conversation_id"] for i in res["items"]}
    flag, count = next(iter(res["facets"]["flags"].items()))
    assert c.get("/v1/conversations", params={"flag": flag}, headers=k).json()["total"] == count
    # full text over content
    word = convs[3]["turns"][0]["text"].split()[1]
    hits = c.get("/v1/conversations", params={"q": word}, headers=k).json()
    assert convs[3]["id"] in {i["conversation_id"] for i in hits["items"]}
    d = c.get(f"/v1/conversations/{convs[3]['id']}", headers=k).json()
    assert len(d["conversation"]["turns"]) == len(convs[3]["turns"])
    assert d["rollup"]["scores"] and len(d["scores"]) >= len(deployment["suite"].questions)
    assert any(s["content"] for s in d["spans"])
    sim = c.get(f"/v1/conversations/{convs[3]['id']}/similar", params={"k": 3}, headers=k).json()
    assert len(sim) == 3 and all(s["conversation_id"] != convs[3]["id"] for s in sim)


def test_score_summary_and_correction(deployment):
    c, k = deployment["client"], deployment["keys"]
    summary = c.get("/v1/scores/summary", headers=k["viewer"]).json()
    assert set(summary) == {q.name for q in deployment["suite"].questions}
    assert all(v["n"] == N and v["decided_by"] == {"rules": 1.0} for v in summary.values())
    cid = deployment["convs"][0]["id"]
    doc = c.post("/v1/scores", json={"conversation_id": cid, "check": "user_frustrated", "value": 1,
                                     "supersedes": "abc"}, headers=k["admin"]).json()
    assert doc["supersedes"] == "abc" and doc["evaluator"]["kind"] == "human"


def test_calibration_lifecycle(deployment):
    c, k, reg = deployment["client"], deployment["keys"]["admin"], deployment["reg"]
    suites = c.get("/v1/suites", headers=k).json()
    assert suites[0]["active"] == deployment["cal"]["id"]
    # a second calibration on other data goes candidate -> shadow
    train = load("synthetic.jsonl", 120)[40:]
    cal2, evs2 = calibrate(deployment["suite"], train, {(x["id"], q): v for x in train for q, v in x["labels"].items()})
    reg.save_calibration("acme", "banking", cal2, evs2)
    assert c.post(f"/v1/calibrations/{cal2['id']}/shadow", headers=k).json()["status"] == "shadow"
    # no audited traffic yet (local-only: no audit judge): promotion needs force
    r = c.post(f"/v1/calibrations/{cal2['id']}/promote", headers=k)
    assert r.status_code == 409
    r = c.post(f"/v1/calibrations/{cal2['id']}/promote", params={"force": True}, headers=k)
    assert r.json()["status"] == "active"
    assert reg.calibration(deployment["cal"]["id"])["status"] == "retired"
    # put the original back for the other tests
    deployment["pg"].exec("UPDATE calibrations SET status='retired' WHERE id=%s", cal2["id"])
    deployment["pg"].exec("UPDATE calibrations SET status='active' WHERE id=%s", deployment["cal"]["id"])
    report = c.get(f"/v1/calibrations/{deployment['cal']['id']}", headers=k).json()
    assert report["report"]["routes"] and report["hosted_set"] == []
    assert c.post("/v1/suites/banking/drift-check", headers=k).json()["calibration_id"] == deployment["cal"]["id"]


def test_annotation_flow_and_calibration_from_labels(deployment):
    c, k = deployment["client"], deployment["keys"]
    q = c.post("/v1/annotation-queues", json={"suite_id": "banking", "name": "weekly", "checks": ["user_frustrated", "fee_mismatch"],
                                             "fill_rules": [{"type": "stratified", "per_lang": 15}]}, headers=k["admin"]).json()
    added = c.post(f"/v1/annotation-queues/{q['id']}/fill", headers=k["annot"]).json()
    assert added["stratified"] >= 15
    labels_by_id = {x["id"]: x["labels"] for x in deployment["convs"]}
    done = 0
    while True:
        nxt = c.post(f"/v1/annotation-queues/{q['id']}/next", headers=k["annot"]).json()
        if not nxt["item"]:
            break
        cid = nxt["item"]["conversation_id"]
        assert nxt["conversation"]["turns"]
        truth = labels_by_id[cid]
        r = c.post(f"/v1/annotation-queues/{q['id']}/items/{cid}/labels",
                   json={"labels": {"user_frustrated": truth["user_frustrated"], "fee_mismatch": truth["fee_mismatch"]}},
                   headers=k["annot"])
        assert r.status_code == 200
        done += 1
    assert done == added["stratified"]
    bad = c.post(f"/v1/annotation-queues/{q['id']}/items/x/labels", json={"labels": {"tool_loop": 1}}, headers=k["annot"])
    assert bad.status_code == 422
    agree = c.get(f"/v1/label-sets/{q['label_set_id']}/agreement", headers=k["annot"]).json()
    assert agree["fee_mismatch"]["n"] == done and 0 <= agree["fee_mismatch"]["accuracy"] <= 1
    # synthetic labels for every conversation, then calibrate from the label set through the API
    from elastic_evals.annotation import add_labels, create_label_set
    lsid = create_label_set(deployment["pg"], "acme", "banking", "synthetic")
    for x in deployment["convs"]:
        add_labels(deployment["pg"], lsid, x["id"], x["labels"], "synthetic", lang=x["lang"])
    res = c.post("/v1/suites/banking/calibrations", json={"label_set_id": lsid}, headers=k["admin"])
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "candidate" and set(res.json()["report"]) == set(labels_by_id[deployment["convs"][0]["id"]])


def test_datasets_and_experiments(deployment):
    c, k = deployment["client"], deployment["keys"]["admin"]
    ids = [x["id"] for x in deployment["convs"][:6]]
    did = c.post("/v1/datasets", json={"name": f"regressions-{uuid.uuid4().hex[:4]}"}, headers=k).json()["id"]
    assert c.post(f"/v1/datasets/{did}/items", json={"from_conversations": ids}, headers=k).json()["items"] == 6
    items = c.get(f"/v1/datasets/{did}/items", headers=k).json()
    items[0]["expected"] = {"not_tools": ["delete_account"]}

    def echo_agent(inp):  # replays the recorded conversation: a stand-in for the agent under test
        return {"turns": inp["turns"], "record": inp["record"], "lang": inp["lang"]}

    suite, cal, evs = deployment["suite"], deployment["cal"], deployment["evs"]
    base = exps.run(echo_agent, items, suite, cal, evs)
    rid_a = c.post("/v1/experiments", json={"dataset_id": did, "name": "baseline", "result": base}, headers=k).json()["id"]
    second = exps.run(echo_agent, items, suite, cal, evs, baseline=base["summary"])
    assert all(g["pass"] for g in second["summary"]["gates"] if g["kind"] == "relative")
    rid_b = c.post("/v1/experiments", json={"dataset_id": did, "name": "candidate", "result": second,
                                           "baseline_run_id": rid_a}, headers=k).json()["id"]
    runs = c.get("/v1/experiments", params={"dataset_id": did}, headers=k).json()
    assert {r["id"] for r in runs} == {rid_a, rid_b}
    cmp = c.get("/v1/experiments/compare", params={"a": rid_a, "b": rid_b}, headers=k).json()
    assert cmp["changed_items"] == []
    # experiment results land in production indices too: comparing them is a query
    assert c.get("/v1/scores/summary", params={"calibration_id": cal["id"]}, headers=k).status_code == 200


def _signed(body: dict):
    raw = json.dumps(body).encode()
    return raw, hmac.new(b"whsecret", raw, hashlib.sha256).hexdigest()


def test_sentry_webhooks(deployment):
    c, fake = deployment["client"], deployment["fake"]
    cid = deployment["convs"][1]["id"]
    detail = c.get(f"/v1/conversations/{cid}", headers=deployment["keys"]["viewer"]).json()
    trace = detail["rollup"]["trace_ids"][0]
    fake.trace = trace
    raw, sig = _signed({"action": "created", "data": {"issue": {"id": "123", "status": "unresolved", "project": {"slug": "bank"}}}})
    assert c.post("/v1/sentry/webhook", content=raw, headers={"sentry-hook-signature": "bad", "sentry-hook-resource": "issue"}).status_code == 401
    r = c.post("/v1/sentry/webhook", content=raw, headers={"sentry-hook-signature": sig, "sentry-hook-resource": "issue"})
    assert r.json() == {"linked": True}
    assert any(p.endswith("/external-issues/") and b["webUrl"].endswith("/conversations?sentry_issue=123") for _, p, b in fake.calls)
    raw, sig = _signed({"action": "resolved", "data": {"issue": {"id": "123"}}})
    assert c.post("/v1/sentry/webhook", content=raw, headers={"sentry-hook-signature": sig,
                                                              "sentry-hook-resource": "issue"}).json() == {"updated": 1}
    detail = c.get(f"/v1/conversations/{cid}", headers=deployment["keys"]["viewer"]).json()
    assert detail["rollup"]["sentry_issue_ids"] == ["123"] and detail["rollup"]["sentry_status"] == "resolved"
    assert detail["sentry"][0]["status"] == "resolved"
    assert detail["sentry"][0]["permalink"] == "https://sentry.test/organizations/org/issues/123/"
    # a later event of the same issue in another conversation arrives as error.created
    cid2 = deployment["convs"][3]["id"]
    trace2 = c.get(f"/v1/conversations/{cid2}", headers=deployment["keys"]["viewer"]).json()["rollup"]["trace_ids"][0]
    raw, sig = _signed({"action": "created", "data": {"error": {"issue_id": "123", "event_id": "e2", "project": 2,
                                                                "tags": [["trace_id", trace2], ["ee.tenant_id", "acme"]]}}})
    assert c.post("/v1/sentry/webhook", content=raw, headers={"sentry-hook-signature": sig,
                                                              "sentry-hook-resource": "error"}).json() == {"linked": True}
    by_trace = c.get(f"/v1/conversations/{trace2}", headers=deployment["keys"]["viewer"]).json()  # Sentry links by trace
    assert by_trace["rollup"]["conversation_id"] == cid2 and by_trace["sentry"][0]["sentry_issue_id"] == "123"
    linked = c.get("/v1/conversations", params={"sentry_issue": "123"}, headers=deployment["keys"]["viewer"]).json()
    assert {i["conversation_id"] for i in linked["items"]} == {cid, cid2}
    # a regression reopens the issue: status follows, and the reopening event's conversation is linked
    raw, sig = _signed({"action": "unresolved", "data": {"issue": {"id": "123"}}})
    assert c.post("/v1/sentry/webhook", content=raw, headers={"sentry-hook-signature": sig,
                                                              "sentry-hook-resource": "issue"}).json() == {"updated": 2, "linked": True}
    assert c.get(f"/v1/conversations/{cid}", headers=deployment["keys"]["viewer"]).json()["rollup"]["sentry_status"] == "unresolved"
    assert c.get("/v1/conversations", params={"size": 50}, headers=deployment["keys"]["viewer"]).json()["total"] == N
    log = c.get("/v1/audit-log", headers=deployment["keys"]["admin"]).json()
    assert {"calibration.promote", "annotation.submit"} <= {e["action"] for e in log}


def test_prices_export_and_reprice(deployment):
    c, k, os_ = deployment["client"], deployment["keys"]["admin"], deployment["os"]
    cid = deployment["convs"][2]["id"]
    before = c.get(f"/v1/conversations/{cid}", headers=k).json()["rollup"]
    assert before.get("cost_usd", 0) == 0  # the demo model had no price at ingest
    rows = [{"model_pattern": "^demo-model$", "start_date": "2020-01-01", "tiers": [{"input": 1.0, "output": 2.0}]}]
    assert c.put("/v1/model-prices", json=rows, headers=k).status_code == 403  # the price book is global
    assert c.put("/v1/model-prices", json=rows, headers=deployment["keys"]["platform"]).json() == {"n": 1}
    exported = c.get("/v1/model-prices/export", headers=k).json()
    assert exported[0]["model_pattern"] == "^demo-model$" and exported[0]["tiers"] == rows[0]["tiers"]
    out = c.post("/v1/reprice", json={"from": "2000-01-01"}, headers=k).json()
    assert out == {"conversations": N, "repriced": N}
    os_.refresh("traces")
    after = c.get(f"/v1/conversations/{cid}", headers=k).json()["rollup"]
    spans = [s for s in c.get(f"/v1/conversations/{cid}", headers=k).json()["spans"] if s.get("usage")]
    want = sum(s["usage"]["input_tokens"] * 1.0 + s["usage"]["output_tokens"] * 2.0 for s in spans) / 1e6
    assert after["cost_usd"] == pytest.approx(want) and after["price_ids"] and after["repriced_at"]
    assert c.post("/v1/reprice", json={"from": "2000-01-01"}, headers=k).json()["repriced"] == 0  # idempotent
