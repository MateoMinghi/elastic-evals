"""Load test of the storage path (DESIGN.md §12 phase 2), on one machine.

Replays N conversations (the 995 synthetic ones, cycled with fresh ids) as OTel spans from
several processes through gateway collector -> Kafka -> ingest collector -> OpenSearch, then
scores them with a local-only calibration (rules + e5 heads, no hosted calls), and measures:
ingest throughput, bytes per span on disk (after a force merge), evaluation throughput, and
list / dashboard query latency. Writes results/LOAD.md and results/load.json.

Everything (OpenSearch, Kafka, Postgres, both collectors, the workers and the load generator)
shares one 4-core VM, so absolute numbers are a floor, not a capacity plan.

    python experiments/load_test.py --conversations 5000 --senders 3 --workers 2
"""

import argparse
import asyncio
import json
import multiprocessing as mp
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
sys.path.insert(0, str(REPO))
os.environ.setdefault("HF_HUB_OFFLINE", "1")

PREFIX, TOPIC = "load", "load-spans"
PG_BASE = os.environ.get("EE_POSTGRES_BASE", "postgresql://postgres@localhost:5432")


def load_all():
    convs = []
    for name in ("synthetic.jsonl", "synthetic_b.jsonl", "synthetic_c.jsonl"):
        convs += [json.loads(line) for line in (ROOT / "data" / name).open()]
    return convs


def sender(args):
    idx, n, total, endpoint = args
    from elastic_evals.replay import replay
    base = load_all()
    convs = []
    for i in range(idx, n, total):
        c = dict(base[i % len(base)])
        c["id"] = f"load-{i:07d}"
        convs.append(c)
    return replay(convs, endpoint, tenant="load")


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--conversations", type=int, default=5000)
    ap.add_argument("--senders", type=int, default=3)
    ap.add_argument("--workers", type=int, default=2)
    a = ap.parse_args()

    from elastic_evals.bundle import save
    from elastic_evals.calibrate import calibrate
    from elastic_evals.registry import ArtifactStore, Registry
    from elastic_evals.store.opensearch import OpenSearch
    from elastic_evals.store.postgres import Postgres, create_database
    from elastic_evals.suite import Suite
    from elastic_evals.tracker import Timing
    from elastic_evals.tracker import run as run_tracker
    from elastic_evals.worker import Worker

    os_ = OpenSearch(prefix=PREFIX)
    os_.teardown()
    os_.setup(pools=("shared",))
    os_.req("PUT", f"/{PREFIX}-*/_settings", {"index": {"refresh_interval": "5s"}})
    import psycopg
    with psycopg.connect(PG_BASE + "/postgres", autocommit=True) as c:
        c.execute('DROP DATABASE IF EXISTS "ee_load" WITH (FORCE)')
    create_database(PG_BASE + "/ee_load")
    pg = Postgres(PG_BASE + "/ee_load", max_size=20)
    pg.migrate()
    reg = Registry(pg, ArtifactStore(f"file://{ROOT}/.artifacts"))
    reg.ensure_tenant("load", allow_hosted=False)
    yaml_text = (REPO / "examples/banking_suite.yaml").read_text()
    reg.put_suite("load", "banking", yaml_text, agent_name="bank-assistant")
    suite = Suite.from_yaml(yaml_text)
    suite.policy.allow_hosted = False
    a_set = [json.loads(line) for line in (ROOT / "data/synthetic.jsonl").open()]
    dev = [c for i, c in enumerate(a_set) if i % 5 in (0, 1)]
    cal, evs = calibrate(suite, dev, {(c["id"], q): v for c in dev for q, v in c["labels"].items()})
    reg.save_calibration("load", "banking", cal, evs)
    reg.promote(cal["id"])
    save(ROOT / ".artifacts" / "load-calibration", cal, evs)

    env = dict(os.environ, EE_KAFKA_TOPIC=TOPIC, EE_INDEX_PREFIX=PREFIX, EE_INGEST_GROUP=f"load-{int(time.time())}")
    eecol = str(REPO / "collector/_build/eecol")
    logs = ROOT / ".e2e-logs"
    logs.mkdir(exist_ok=True)
    procs = [subprocess.Popen([eecol, "--config", str(REPO / f"deploy/collector/{n}.yaml")], env=env,
                              stdout=open(logs / f"load-{n}.log", "w"), stderr=subprocess.STDOUT)
             for n in ("gateway", "ingest")]
    time.sleep(4)
    res = {"conversations": a.conversations, "senders": a.senders, "workers": a.workers, "cpus": os.cpu_count()}

    # 1. ingest
    t0 = time.time()
    with mp.get_context("spawn").Pool(a.senders) as pool:
        spans = sum(pool.map(sender, [(i, a.conversations, a.senders, "http://localhost:4318") for i in range(a.senders)]))
    res["spans"], res["send_s"] = spans, time.time() - t0
    curve = []
    while True:
        os_.refresh("obs")
        n = os_.count("obs")
        curve.append((round(time.time() - t0, 1), n))
        if n >= spans or time.time() - t0 > 1800:
            break
        time.sleep(2)
    res["ingest_s"] = time.time() - t0
    res["indexed"] = n
    res["ingest_spans_per_s"] = n / res["ingest_s"]
    res["ingest_curve"] = curve

    # 2. evaluation (rules + e5, local-only)
    async def drive():
        stop = asyncio.Event()
        tracker = asyncio.create_task(run_tracker(pg, "localhost:9092", Timing(trace_idle_s=1, session_idle_s=2),
                                                  topic=TOPIC, group=f"load-tr-{int(time.time())}", stop=stop))
        workers = [Worker(pg, os_, reg, worker_id=f"w{i}", batch=64, retry_s=2) for i in range(a.workers)]
        start = time.time()
        while time.time() - start < 3600:
            await asyncio.gather(*(w.run_once() for w in workers))
            row = pg.one("SELECT count(*) FILTER (WHERE status='evaluated') AS done FROM trace_tracker")
            if row["done"] >= a.conversations:
                break
        stop.set()
        await tracker
        return time.time() - start

    res["evaluate_s"] = asyncio.run(drive())
    res["evaluated"] = pg.one("SELECT count(*) FILTER (WHERE status='evaluated') AS n FROM trace_tracker")["n"]
    res["conversations_per_s"] = res["evaluated"] / res["evaluate_s"]
    for p in procs:
        p.terminate()

    # 3. storage after a force merge
    os_.refresh()
    os_.req("POST", f"/{PREFIX}-*/_forcemerge", max_num_segments=1)
    os_.req("POST", f"/{PREFIX}-*/_refresh")
    stats = os_.req("GET", f"/{PREFIX}-*/_stats/store,docs")["indices"]
    per = {}
    for idx, s in stats.items():
        kind = idx.split("-")[1]
        per[kind] = {"docs": s["primaries"]["docs"]["count"], "bytes": s["primaries"]["store"]["size_in_bytes"]}
    res["storage"] = per
    obs_b = per["obs"]["bytes"] / max(1, per["obs"]["docs"])
    stage_b = per["stage"]["bytes"] / max(1, per["stage"]["docs"])
    res["gb_per_million_spans"] = {"obs": obs_b * 1e6 / 1e9, "stage": stage_b * 1e6 / 1e9,
                                   "scores_per_million_conversations": per["scores"]["bytes"] / max(1, per["traces"]["docs"]) * 1e6 / 1e9}

    # 4. query latency (warm), as the UI issues them
    from fastapi.testclient import TestClient
    from elastic_evals.api import create_app
    client = TestClient(create_app(pg, os_, reg, auth=False))
    h = {"X-EE-Tenant": "load"}
    lat = {}
    queries = {
        "list (50, sorted, facets)": lambda: client.get("/v1/conversations", params={"size": 50}, headers=h),
        "list filtered by flag": lambda: client.get("/v1/conversations", params={"flag": "user_frustrated"}, headers=h),
        "full-text search": lambda: client.get("/v1/conversations", params={"q": "transferencia"}, headers=h),
        "score summary (7 days)": lambda: client.get("/v1/scores/summary", headers=h),
        "conversation detail": lambda: client.get("/v1/conversations/load-0000042", headers=h),
    }
    for name, fn in queries.items():
        fn()
        times = []
        for _ in range(30):
            t = time.perf_counter()
            r = fn()
            times.append(1000 * (time.perf_counter() - t))
            assert r.status_code == 200, r.text
        lat[name] = {"p50_ms": statistics.median(times), "p95_ms": pct(times, 0.95)}
    res["query_latency"] = lat
    (ROOT / "results" / "load.json").write_text(json.dumps(res, indent=1))

    lines = ["# Load test (one 4-core VM)", "",
             "Everything shares one 4-core VM: OpenSearch (2 GB heap), Kafka, Postgres, the gateway and ingest "
             "collectors, the tracker, the workers and the load generator. Numbers are a floor for a single node, "
             "not a capacity plan.", "",
             f"- Load: {res['conversations']} conversations, {res['spans']} spans, from {res['senders']} sender processes",
             f"- Ingest: all {res['indexed']} spans indexed after {res['ingest_s']:.0f}s → **{res['ingest_spans_per_s']:.0f} spans/s** end to end "
             f"(sending took {res['send_s']:.0f}s)",
             f"- Evaluation (rules + e5 heads, local-only, {res['workers']} workers): {res['evaluated']} conversations in "
             f"{res['evaluate_s']:.0f}s → **{res['conversations_per_s']:.1f} conversations/s**",
             f"- Disk after force merge: `obs` **{1000 * res['gb_per_million_spans']['obs']:.0f} MB per million spans**, "
             f"`stage` (full content) {1000 * res['gb_per_million_spans']['stage']:.0f} MB per million spans, "
             f"`scores` {1000 * res['gb_per_million_spans']['scores_per_million_conversations']:.0f} MB per million conversations", "",
             "| query (through the API) | p50 ms | p95 ms |", "|---|---|---|"]
    lines += [f"| {k} | {v['p50_ms']:.0f} | {v['p95_ms']:.0f} |" for k, v in lat.items()]
    lines += ["", "| index | documents | MB |", "|---|---|---|"]
    lines += [f"| {k} | {v['docs']} | {v['bytes'] / 1e6:.1f} |" for k, v in sorted(per.items())]
    (ROOT / "results" / "LOAD.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
