"""End to end: agent spans -> gateway collector -> Kafka -> ingest collector -> OpenSearch;
tracker -> worker -> scores, rollups, retention. Needs OpenSearch, Postgres, Kafka and a built
collector (collector/_build/eecol)."""

import asyncio
import json
import os
import socket
import subprocess
import time
import uuid
from pathlib import Path

import httpx
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
RULES_YAML = yaml.safe_dump(RULES_SUITE, allow_unicode=True)


def test_pipeline_end_to_end(opensearch, postgres, collectors, tmp_path):
    reg = Registry(postgres, ArtifactStore(f"file://{tmp_path}/artifacts"))
    reg.ensure_tenant("pipe", tier="dedicated", index_pool="pipe", allow_hosted=False)
    reg.put_suite("pipe", "banking", RULES_YAML, agent_name="bank-assistant")
    suite = Suite.from_yaml(RULES_YAML)
    train = load("synthetic.jsonl", 80)
    cal, evs = calibrate(suite, train, {(c["id"], q): v for c in train for q, v in c["labels"].items()})
    reg.save_calibration("pipe", "banking", cal, evs)
    reg.promote(cal["id"])

    convs = load("synthetic_c.jsonl", 40)
    n_spans = replay(convs, collectors["otlp"], tenant="pipe")

    async def drive():
        stop = asyncio.Event()
        tracker = asyncio.create_task(run_tracker(postgres, KAFKA, Timing(trace_idle_s=1, session_idle_s=2),
                                                  topic=collectors["topic"], group=f"t-{uuid.uuid4().hex[:6]}", stop=stop))
        worker = Worker(postgres, opensearch, reg, retry_s=1)
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
    n_scores = opensearch.count("scores", pool="pipe")
    assert n_scores == len(convs) * len(suite.questions)

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
