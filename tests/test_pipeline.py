"""End to end: agent spans -> gateway collector -> Kafka -> ingest collector -> OpenSearch;
tracker -> worker -> scores, rollups, retention. Needs OpenSearch, Postgres, Kafka and a built
collector (collector/_build/eecol)."""

import asyncio
import time
import uuid
from pathlib import Path

import pytest
import yaml

from elastic_evals.calibrate import calibrate
from elastic_evals.conversation import build as build_conversation
from elastic_evals.engine import Engine
from elastic_evals.registry import ArtifactStore, Registry
from elastic_evals.replay import replay
from elastic_evals.suite import Suite
from elastic_evals.views import text_of
from elastic_evals.tracker import Timing
from elastic_evals.tracker import run as run_tracker
from elastic_evals.worker import Worker, fetch_conversation

from .conftest import KAFKA, load

pytestmark = pytest.mark.infra
ROOT = Path(__file__).resolve().parent.parent
EECOL = ROOT / "collector" / "_build" / "eecol"
DATA = ROOT / "experiments" / "data"


RULES_SUITE = yaml.safe_load((ROOT / "examples/banking_suite.yaml").read_text())
RULES_SUITE["evaluators"] = [{"id": "rules", "type": "rules"}]
RULES_SUITE["policy"] = {"allow_hosted": False, "target_accuracy": 0.95}
RULES_SUITE["questions"]["tool_failures"] = {"ask": "Did any tool call fail?", "view": "trajectory", "rule": "tool_errors",
                                             "scope": "trajectory"}
RULES_SUITE["questions"]["step_failed"] = {"ask": "Did this tool call fail?", "view": "tools", "rule": "step_failed",
                                           "scope": "observation", "select": {"type": "TOOL"}}


def with_step_labels(convs):
    """Tool status and the labels of the trajectory and observation checks, from the tool results."""
    out = []
    for c in convs:
        c = dict(c, labels=dict(c.get("labels", {})), turns=[dict(t) for t in c["turns"]])
        failed_any = 0
        for i, t in enumerate(c["turns"]):
            if t["role"] == "tool":
                failed = str(t.get("result", "")).lower().startswith("error")
                t["status"] = "ERROR" if failed else "OK"
                c["labels"][f"step_failed@t{i}"] = int(failed)
                failed_any |= failed
        c["labels"]["tool_failures"] = int(failed_any)
        out.append(c)
    return out
RULES_YAML = yaml.safe_dump(RULES_SUITE, allow_unicode=True)


def test_pipeline_end_to_end(opensearch, postgres, collectors, tmp_path):
    reg = Registry(postgres, ArtifactStore(f"file://{tmp_path}/artifacts"))
    reg.ensure_tenant("pipe", tier="dedicated", index_pool="pipe", allow_hosted=False)
    reg.put_suite("pipe", "banking", RULES_YAML, agent_name="bank-assistant")
    suite = Suite.from_yaml(RULES_YAML)
    train = with_step_labels(load("synthetic.jsonl", 80))
    cal, evs = calibrate(suite, train, {(c["id"], q): v for c in train for q, v in c["labels"].items() if "@" not in q})
    assert cal["observation"]["routes"] == {"step_failed": ["rules"]}
    reg.save_calibration("pipe", "banking", cal, evs)
    reg.promote(cal["id"])

    convs = with_step_labels(load("synthetic_c.jsonl", 40))
    n_spans = replay(convs, collectors["otlp"], tenant="pipe")

    async def drive():
        stop = asyncio.Event()
        tracker = asyncio.create_task(run_tracker(postgres, KAFKA, Timing(trace_idle_s=1, session_idle_s=2),
                                                  topic=collectors["topic"], group=f"t-{uuid.uuid4().hex[:6]}", stop=stop))
        worker = Worker(postgres, opensearch, reg, retry_s=1)
        # a Sentry issue linked before evaluation (the usual order) must survive the rollup write
        first, until = None, time.time() + 30
        while first is None and time.time() < until:
            opensearch.refresh("obs")
            hits = opensearch.hits("obs", {"size": 1, "query": {"term": {"session_id": convs[0]["id"]}}}, "pipe")
            first = hits[0]["trace_id"] if hits else None
            await asyncio.sleep(0.2)
        assert first, "no span of the first conversation was indexed"
        postgres.exec("INSERT INTO sentry_links (tenant_id, trace_id, sentry_issue_id, status) VALUES ('pipe', %s, '77', 'unresolved')",
                      first)
        deadline = time.time() + 90
        while time.time() < deadline:
            await worker.run_once()
            done = postgres.one("SELECT count(*) FILTER (WHERE status='evaluated') AS done, count(*) AS n "
                                "FROM trace_tracker WHERE tenant_id='pipe'")
            if done["n"] == len(convs) and done["done"] == len(convs):
                break
            await asyncio.sleep(0.5)
        stop.set()
        await tracker

    asyncio.run(drive())
    errors = postgres.all("SELECT conversation_id, status, last_error FROM trace_tracker "
                          "WHERE status <> 'evaluated' AND tenant_id='pipe'")
    assert not errors, errors
    opensearch.refresh()

    linked = opensearch.hits("traces", {"query": {"term": {"conversation_id": convs[0]["id"]}}}, "pipe")[0]
    assert linked["sentry_issue_ids"] == ["77"] and linked["sentry_status"] == "unresolved"

    # every span landed once, in the tenant's dedicated pool
    assert opensearch.count("obs", pool="pipe") == n_spans
    assert opensearch.count("obs", {"term": {"tenant_id": "pipe"}}, pool="shared") == 0

    # conversations rebuilt from spans are identical to what the agent said
    for c in convs:
        spans, stage = fetch_conversation(opensearch, "pipe", c["id"], "session", "pipe")
        rebuilt = build_conversation(c["id"], spans, stage, "pipe")
        # what evaluators see is identical (non-string tool results travel as their text)
        assert text_of(rebuilt) == text_of(c), c["id"]
        assert [t["role"] for t in rebuilt["turns"]] == [t["role"] for t in c["turns"]]
        assert rebuilt["record"] == c["record"] and rebuilt["lang"] == c["lang"]

    # scores equal a direct engine run on the original conversations
    direct = {r["conversation_id"]: r["decisions"] for r in Engine(suite, cal, evs).run(convs)}
    traces = {t["conversation_id"]: t for t in opensearch.hits("traces", {"size": 100}, "pipe")}
    assert len(traces) == len(convs)
    for cid, t in traces.items():
        got = {chk: s["value"] for chk, s in t["scores"].items()}
        assert got == direct[cid], cid
        assert t["flags"] == sorted(k for k, v in direct[cid].items() if v == 1)
        assert t["n_turns"] == sum(x["role"] != "tool" for x in next(c for c in convs if c["id"] == cid)["turns"])
    # conversation and trajectory checks: one decision each; the observation check: one per tool call
    n_tools = sum(t["role"] == "tool" for c in convs for t in c["turns"])
    n_decisions = opensearch.count("scores", {"term": {"decision": True}}, pool="pipe")
    assert n_decisions == len(convs) * (len(suite.questions) - 1) + n_tools
    steps = opensearch.hits("scores", {"size": 1000, "query": {"bool": {"filter": [
        {"term": {"check": "step_failed"}}, {"term": {"decision": True}}]}}}, "pipe")
    tool_spans = {s["span_id"] for s in opensearch.hits("obs", {"size": 5000, "query": {"term": {"type": "TOOL"}}}, "pipe")}
    assert len(steps) == n_tools and {s["observation_id"] for s in steps} == tool_spans
    for c in convs:
        t = traces[c["id"]]
        assert t["scores"]["step_failed"]["steps"] == sum(x["role"] == "tool" for x in c["turns"])
        assert t["scores"]["step_failed"]["value"] == c["labels"]["tool_failures"] == t["scores"]["tool_failures"]["value"]

    # retention: content is copied to payload only for kept conversations
    kept = [t for t in traces.values() if t["content_kept"]]
    assert kept and len(kept) < len(traces) + 1
    for t in kept:
        assert opensearch.count("payload", {"terms": {"trace_id": t["trace_ids"]}}, "pipe") > 0
    dropped = [t for t in traces.values() if not t["content_kept"]]
    for t in dropped:
        assert opensearch.count("payload", {"terms": {"trace_id": t["trace_ids"]}}, "pipe") == 0
    assert opensearch.count("metrics", pool="pipe") > 0

    # cost was priced at ingest? the demo model has no price: cost stays 0, tokens are counted
    assert all(t["input_tokens"] > 0 for t in traces.values())
