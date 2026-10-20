"""Phase-2 gate (DESIGN.md §12): sustained load at 2-3x the target peak, against the budgets.

Budgets and the target peak are in DESIGN.md §12 ("Phase-2 budgets"). The load is paced, not a
burst: S sender processes emit synthetic conversations as OTel spans at a fixed total rate through
gateway collector -> Kafka -> ingest collector -> a multi-node OpenSearch cluster (3 primaries,
1 replica), while the tracker and workers evaluate (local-only: rules + e5 heads) and the UI's
queries run against the cluster. Measured:

- ingest latency: for every 20th conversation, the time from its scheduled send to its root span
  being searchable in obs-* (polled every 0.5 s). Scheduled, not actual, send time: a sender that
  falls behind makes the latency look worse, never better. It includes the agent SDK's batch delay;
- loss: spans sent against spans indexed (after the load drains);
- GB per million spans: obs primary store after a force merge;
- query latency during the load: the API calls the UI makes, and the aggregations the OpenSearch
  Dashboards visualizations make.

Writes results/PHASE2.md and results/phase2.json.

    python experiments/phase2_gate.py --urls http://127.0.0.1:9201,http://127.0.0.1:9202,http://127.0.0.1:9203 \\
        --spans-per-s 2900 --minutes 5
"""

import argparse
import json
import multiprocessing as mp
import os
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
sys.path.insert(0, str(REPO))
os.environ.setdefault("HF_HUB_OFFLINE", "1")

PREFIX, TENANT, SAMPLE = "p2", "load", 20
PG_BASE = os.environ.get("EE_POSTGRES_BASE", "postgresql://postgres@localhost:5432")
SPANS_PER_CONV = 12.7  # mean over the synthetic sets (LOAD.md: 63,643 spans for 5,000 conversations)

# DESIGN.md §12, phase-2 budgets
BUDGET = {"ingest_p95_s": 30.0, "obs_gb_per_million_spans": 1.0, "ui_p95_ms": 1000.0, "search_p95_ms": 3000.0,
          "dashboards_p95_ms": 2000.0}


def load_all():
    convs = []
    for name in ("synthetic.jsonl", "synthetic_b.jsonl", "synthetic_c.jsonl"):
        convs += [json.loads(line) for line in (ROOT / "data" / name).open()]
    return convs


def sender(args):
    idx, total, n, rate, endpoint, t0 = args
    from elastic_evals.replay import emit, tracer_provider
    base = load_all()
    provider = tracer_provider(endpoint, TENANT)
    tracer = provider.get_tracer("phase2")
    spans = 0
    for k in range(idx, n, total):  # conversation k is due at t0 + k / rate
        wait = t0 + k / rate - time.time()
        if wait > 0:
            time.sleep(wait)
        spans += emit(tracer, dict(base[k % len(base)], id=f"p2-{k:07d}"))
    provider.force_flush()
    provider.shutdown()
    return spans


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))] if xs else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--urls", required=True)
    ap.add_argument("--spans-per-s", type=float, default=2900)
    ap.add_argument("--minutes", type=float, default=5)
    ap.add_argument("--senders", type=int, default=4)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--partitions", type=int, default=6)
    ap.add_argument("--rules-only", action="store_true", help="workers evaluate the rule checks only")
    a = ap.parse_args()
    urls = a.urls.split(",")
    rate = a.spans_per_s / SPANS_PER_CONV  # conversations per second
    n = int(rate * a.minutes * 60)

    import httpx

    from elastic_evals.calibrate import calibrate
    from elastic_evals.registry import ArtifactStore, Registry
    from elastic_evals.store.opensearch import OpenSearch
    from elastic_evals.store.postgres import Postgres, create_database
    from elastic_evals.suite import Suite

    os_ = OpenSearch(urls[0], prefix=PREFIX)
    os_.teardown()
    os_.setup(pools=("shared",), shards=3, replicas=1)
    import psycopg
    with psycopg.connect(PG_BASE + "/postgres", autocommit=True) as c:
        c.execute('DROP DATABASE IF EXISTS "ee_p2" WITH (FORCE)')
    create_database(PG_BASE + "/ee_p2")
    pg = Postgres(PG_BASE + "/ee_p2", max_size=20)
    pg.migrate()
    artifacts = f"file://{ROOT}/.artifacts"
    reg = Registry(pg, ArtifactStore(artifacts))
    reg.ensure_tenant(TENANT, allow_hosted=False)
    import yaml
    spec = yaml.safe_load((REPO / "examples/banking_suite.yaml").read_text())
    if a.rules_only:  # local heads served off this box (ONNX Runtime / TEI): the workers here run rules
        spec["evaluators"] = [e for e in spec["evaluators"] if e.get("type") == "rules"]
    yaml_text = yaml.safe_dump(spec, allow_unicode=True)
    reg.put_suite(TENANT, "banking", yaml_text, agent_name="bank-assistant")
    suite = Suite.from_yaml(yaml_text)
    suite.policy.allow_hosted = False
    a_set = [json.loads(line) for line in (ROOT / "data/synthetic.jsonl").open()]
    dev = [c for i, c in enumerate(a_set) if i % 5 in (0, 1)]
    cal, evs = calibrate(suite, dev, {(c["id"], q): v for c in dev for q, v in c["labels"].items()})
    reg.save_calibration(TENANT, "banking", cal, evs)
    reg.promote(cal["id"])

    topic = f"p2-{int(time.time())}"
    # as deploy/docker-compose.yml: ingest commits offsets only after OpenSearch accepts a batch, so
    # it writes one bulk request at a time per partition; partitions are its write concurrency
    import asyncio

    from aiokafka.admin import AIOKafkaAdminClient, NewTopic

    async def create_topic():
        admin = AIOKafkaAdminClient(bootstrap_servers="localhost:9092")
        await admin.start()
        try:
            await admin.create_topics([NewTopic(topic, num_partitions=a.partitions, replication_factor=1)])
        finally:
            await admin.close()
    asyncio.run(create_topic())

    async def delete_topic():  # a run leaves ~1.5 GB in Kafka
        admin = AIOKafkaAdminClient(bootstrap_servers="localhost:9092")
        await admin.start()
        try:
            await admin.delete_topics([topic])
        finally:
            await admin.close()
    env = dict(os.environ, EE_KAFKA_TOPIC=topic, EE_INDEX_PREFIX=PREFIX, EE_INGEST_GROUP=f"{topic}-ingest",
               EE_OPENSEARCH_URL=urls[0], EE_POSTGRES_DSN=PG_BASE + "/ee_p2", EE_ARTIFACTS=artifacts,
               EE_PRICES_FILE=str(REPO / "deploy/collector/prices.json"))
    logs = ROOT / ".e2e-logs"
    logs.mkdir(exist_ok=True)
    eecol = str(REPO / "collector/_build/eecol")
    procs = [subprocess.Popen([eecol, "--config", str(REPO / f"deploy/collector/{c}.yaml")], env=env,
                              stdout=open(logs / f"p2-{c}.log", "w"), stderr=subprocess.STDOUT) for c in ("gateway", "ingest")]
    procs.append(subprocess.Popen([sys.executable, "-m", "elastic_evals", "tracker", "--trace-idle", "2", "--session-idle", "5"],
                                  env=env, cwd=REPO, stdout=open(logs / "p2-tracker.log", "w"), stderr=subprocess.STDOUT))
    procs += [subprocess.Popen([sys.executable, "-m", "elastic_evals", "worker", "--batch", "64"], env=env, cwd=REPO,
                               stdout=open(logs / f"p2-worker{i}.log", "w"), stderr=subprocess.STDOUT) for i in range(a.workers)]
    import atexit
    atexit.register(lambda: [p.terminate() for p in procs])
    time.sleep(6)
    for p in procs:
        if p.poll() is not None:
            sys.exit(f"a process exited at start (port in use?): see {logs}")

    res = {"target_spans_per_s": a.spans_per_s, "conversations": n, "minutes": a.minutes, "senders": a.senders,
           "partitions": a.partitions, "rules_only": a.rules_only, "workers": a.workers,
           "nodes": len(urls), "budget": BUDGET}
    t0 = time.time() + 2
    stop = threading.Event()

    # ingest latency: poll for sampled root spans
    latency, pending = [], {k: t0 + k / rate for k in range(0, n, SAMPLE)}

    def poll():
        http = httpx.Client(timeout=10)
        while not stop.is_set() or pending:
            due = [k for k, t in pending.items() if t <= time.time()]
            if due:
                body = {"size": len(due), "_source": ["session_id"],
                        "query": {"bool": {"filter": [{"terms": {"session_id": [f"p2-{k:07d}" for k in due]}},
                                                      {"term": {"type": "AGENT"}}]}}}
                try:
                    hits = http.post(f"{urls[0]}/{PREFIX}-obs-*/_search", json=body).json()["hits"]["hits"]
                except (httpx.HTTPError, KeyError):
                    hits = []
                now = time.time()
                for h in hits:
                    k = int(h["_source"]["session_id"][3:])
                    if k in pending:
                        latency.append(now - pending.pop(k))
            if stop.is_set() and time.time() > t_end + 600:
                break
            time.sleep(0.5)

    # query latency during the load
    from fastapi.testclient import TestClient

    from elastic_evals.api import create_app
    api = TestClient(create_app(pg, os_, reg, auth=False))
    h = {"X-EE-Tenant": TENANT}
    queries = {
        "list (50, sorted, facets)": ("ui", lambda: api.get("/v1/conversations", params={"size": 50}, headers=h)),
        "list filtered by flag": ("ui", lambda: api.get("/v1/conversations", params={"flag": "user_frustrated"}, headers=h)),
        "conversation detail": ("ui", lambda: api.get("/v1/conversations/p2-0000040", headers=h)),
        "score summary (7 days)": ("ui", lambda: api.get("/v1/scores/summary", headers=h)),
        "full-text search": ("search", lambda: api.get("/v1/conversations", params={"q": "transferencia"}, headers=h)),
        "Dashboards: LLM cost per day": ("dashboards", lambda: os_.search("obs", {"size": 0, "aggs": {"d": {
            "date_histogram": {"field": "start_time", "fixed_interval": "1h"}, "aggs": {"c": {"sum": {"field": "cost_usd"}}}}}})),
        "Dashboards: errors by type": ("dashboards", lambda: os_.search("obs", {"size": 0, "query": {"term": {"status": "ERROR"}},
                                                                               "aggs": {"t": {"terms": {"field": "error_type", "size": 20}}}})),
        "Dashboards: latency p50/p95 by model": ("dashboards", lambda: os_.search("obs", {"size": 0, "query": {"term": {"type": "GENERATION"}},
            "aggs": {"m": {"terms": {"field": "request_model", "size": 20}, "aggs": {"p": {"percentiles": {
                "field": "duration_ms", "percents": [50, 95]}}}}}})),
    }
    qlat = {name: [] for name in queries}

    def query_loop():
        while time.time() < t0 + 30 and not stop.is_set():  # let data arrive first
            time.sleep(1)
        while not stop.is_set():
            for name, (_, fn) in queries.items():
                t = time.perf_counter()
                try:
                    r = fn()
                    ok = getattr(r, "status_code", 200) in (200, 404)
                except Exception:  # noqa: BLE001
                    ok = False
                if ok:
                    qlat[name].append(1000 * (time.perf_counter() - t))
            time.sleep(1)

    t_end = t0 + a.minutes * 60
    threads = [threading.Thread(target=poll, daemon=True), threading.Thread(target=query_loop, daemon=True)]
    for t in threads:
        t.start()
    with mp.get_context("spawn").Pool(a.senders) as pool:
        spans = sum(pool.map(sender, [(i, a.senders, n, rate, "http://localhost:4318", t0) for i in range(a.senders)]))
    res["spans"] = spans
    res["send_s"] = time.time() - t0
    res["achieved_send_spans_per_s"] = spans / res["send_s"]
    stop.set()

    # drain: every span indexed?
    while time.time() < t_end + 600:
        os_.refresh("obs")
        indexed = os_.count("obs")
        if indexed >= spans:
            break
        time.sleep(3)
    res["indexed"] = indexed
    res["drain_s"] = time.time() - res["send_s"] - t0
    threads[0].join(timeout=30)
    res["latency_samples"] = len(latency)
    res["latency_unseen"] = len(pending)
    res["ingest_latency_s"] = {"p50": pct(latency, 0.5), "p95": pct(latency, 0.95), "p99": pct(latency, 0.99),
                               "max": max(latency) if latency else None}
    res["query_latency_ms"] = {name: {"kind": queries[name][0], "n": len(v), "p50": statistics.median(v) if v else None,
                                      "p95": pct(v, 0.95)} for name, v in qlat.items()}
    res["evaluated"] = pg.one("SELECT count(*) FILTER (WHERE status='evaluated') AS n FROM trace_tracker")["n"]
    for p in procs:
        p.terminate()
    asyncio.run(delete_topic())

    os_.refresh()
    httpx.post(f"{urls[0]}/{PREFIX}-obs-*,{PREFIX}-stage-*/_forcemerge", params={"max_num_segments": 1}, timeout=1800)
    stats = os_.req("GET", f"/{PREFIX}-obs-*,{PREFIX}-stage-*/_stats/store,docs")["indices"]
    per = {}
    for idx, s in stats.items():
        kind = idx.split("-")[1]
        d = per.setdefault(kind, {"docs": 0, "bytes": 0})
        d["docs"] += s["primaries"]["docs"]["count"]
        d["bytes"] += s["primaries"]["store"]["size_in_bytes"]
    res["storage"] = per
    res["obs_gb_per_million_spans"] = per["obs"]["bytes"] / max(1, per["obs"]["docs"]) * 1e6 / 1e9
    res["stage_gb_per_million_spans"] = per["stage"]["bytes"] / max(1, per["stage"]["docs"]) * 1e6 / 1e9

    def worst(kind):
        v = [q["p95"] for q in res["query_latency_ms"].values() if q["kind"] == kind and q["p95"] is not None]
        return max(v) if v else None
    lat95 = res["ingest_latency_s"]["p95"]
    res["gate"] = {
        "load reached the target rate": res["achieved_send_spans_per_s"] >= 0.95 * a.spans_per_s,
        "no span lost": indexed == spans,
        f"ingest latency p95 <= {BUDGET['ingest_p95_s']:.0f} s": lat95 is not None and lat95 <= BUDGET["ingest_p95_s"]
        and res["latency_unseen"] == 0,
        f"obs <= {BUDGET['obs_gb_per_million_spans']} GB per million spans": res["obs_gb_per_million_spans"] <= BUDGET["obs_gb_per_million_spans"],
        f"UI queries p95 <= {BUDGET['ui_p95_ms']:.0f} ms under load": (worst("ui") or 1e9) <= BUDGET["ui_p95_ms"],
        f"full-text search p95 <= {BUDGET['search_p95_ms']:.0f} ms under load": (worst("search") or 1e9) <= BUDGET["search_p95_ms"],
        f"Dashboards aggregations p95 <= {BUDGET['dashboards_p95_ms']:.0f} ms under load": (worst("dashboards") or 1e9) <= BUDGET["dashboards_p95_ms"],
    }
    res["passed"] = all(res["gate"].values())
    (ROOT / "results" / "phase2.json").write_text(json.dumps(res, indent=1))

    il = res["ingest_latency_s"]
    lines = [f"# Phase-2 gate: {a.spans_per_s:.0f} spans/s for {a.minutes:g} minutes on a {len(urls)}-node cluster", "",
             f"Target peak and budgets: DESIGN.md §12. Load: {n} conversations ({spans} spans) from {a.senders} paced sender "
             f"processes through gateway → Kafka ({a.partitions} partitions) → ingest → OpenSearch ({len(urls)} nodes, 3 primaries + 1 replica per index), "
             f"while the tracker and {a.workers} workers evaluate ({'rule checks only' if a.rules_only else 'local-only: rules and e5 heads'}) and the UI's queries run. Everything shares one "
             f"{os.cpu_count()}-core VM. Script: `experiments/phase2_gate.py`.", "",
             f"- Sent at **{res['achieved_send_spans_per_s']:.0f} spans/s** (target {a.spans_per_s:.0f}); indexed {indexed} of {spans}; "
             f"drained {res['drain_s']:.0f} s after the last send",
             f"- Ingest latency, scheduled send → searchable ({len(latency)} sampled conversations, agent SDK batching included): "
             f"p50 **{il['p50']:.1f} s**, p95 **{il['p95']:.1f} s**, p99 {il['p99']:.1f} s, max {il['max']:.1f} s"
             if latency else "- Ingest latency: no samples",
             f"- Storage after force merge: obs **{res['obs_gb_per_million_spans']:.2f} GB** per million spans (primary), "
             f"stage {res['stage_gb_per_million_spans']:.2f} GB; replicas double both",
             f"- Evaluated during the load: {res['evaluated']} conversations, so the UI queries ran on that many rollups", "",
             "| query, during the load | n | p50 ms | p95 ms |", "|---|---|---|---|"]
    for name, q in res["query_latency_ms"].items():
        if q["p50"] is not None:
            lines.append(f"| {name} | {q['n']} | {q['p50']:.0f} | {q['p95']:.0f} |")
    lines += ["", "| gate | result |", "|---|---|"] + [f"| {k} | {'✓' if v else '✗'} |" for k, v in res["gate"].items()]
    lines += ["", f"Phase-2 gate: **{'passed' if res['passed'] else 'not passed'}**"]
    (ROOT / "results" / "PHASE2.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0 if res["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
