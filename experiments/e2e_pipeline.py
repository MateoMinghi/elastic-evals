"""The validated engine, run through the whole production path.

Same calibration data (158 set-A conversations) and same holdout (300 set-C conversations) as
ENGINE_SUMMARY.md, but the holdout now travels as OpenTelemetry spans: replay -> gateway
collector -> Kafka -> ingest collector -> OpenSearch -> tracker -> evaluation worker -> scores.
Hosted calls are served from the cache (EVAL_OFFLINE=1): any change in the text the evaluators
see would be a cache miss and fail the run, so this also proves the path is lossless.

Needs OpenSearch, Postgres and Kafka running and collector/_build/eecol built.
Writes results/PIPELINE.md and results/pipeline.json.
"""

import asyncio
import json
import os
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
sys.path.insert(0, str(REPO))
os.environ.setdefault("EVAL_OFFLINE", "1")
os.environ.setdefault("EVAL_CACHE_DIR", str(ROOT / ".cache"))
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from elastic_evals.bundle import load as load_bundle  # noqa: E402
from elastic_evals.bundle import save as save_bundle  # noqa: E402
from elastic_evals.calibrate import calibrate  # noqa: E402
from elastic_evals.drift import check as drift_check  # noqa: E402
from elastic_evals.providers import OpenRouter  # noqa: E402
from elastic_evals.registry import ArtifactStore, Registry  # noqa: E402
from elastic_evals.replay import replay  # noqa: E402
from elastic_evals.store.opensearch import OpenSearch  # noqa: E402
from elastic_evals.store.postgres import Postgres, create_database  # noqa: E402
from elastic_evals.suite import Suite  # noqa: E402
from elastic_evals.tracker import Timing  # noqa: E402
from elastic_evals.tracker import run as run_tracker  # noqa: E402
from elastic_evals.worker import Worker  # noqa: E402

PREFIX, TOPIC, KAFKA = "e2e", "e2e-spans", os.environ.get("EE_KAFKA", "localhost:9092")
PG_BASE = os.environ.get("EE_POSTGRES_BASE", "postgresql://postgres@localhost:5432")


def load(name):
    return [json.loads(line) for line in (ROOT / "data" / name).open()]


def main():
    # fresh stores
    os_ = OpenSearch(prefix=PREFIX)
    os_.teardown()
    os_.setup(pools=("shared",))
    import psycopg
    with psycopg.connect(PG_BASE + "/postgres", autocommit=True) as c:
        c.execute('DROP DATABASE IF EXISTS "ee_e2e" WITH (FORCE)')
    create_database(PG_BASE + "/ee_e2e")
    pg = Postgres(PG_BASE + "/ee_e2e")
    pg.migrate()
    reg = Registry(pg, ArtifactStore(f"file://{ROOT}/.artifacts"))

    # calibrate exactly as in the engine study, then register and promote
    yaml_text = (REPO / "examples/banking_suite.yaml").read_text()
    suite = Suite.from_yaml(yaml_text)
    a = load("synthetic.jsonl")
    dev = [c for i, c in enumerate(a) if i % 5 in (0, 1)]
    client = OpenRouter()
    calib_dir = ROOT / ".artifacts" / "e2e-calibration"
    if (calib_dir / "calibration.json").exists():  # calibration is deterministic; reuse it across runs
        _, cal, evs = load_bundle(REPO / "examples/banking_suite.yaml", calib_dir, client)
    else:
        cal, evs = calibrate(suite, dev, {(c["id"], q): v for c in dev for q, v in c["labels"].items()}, client)
        save_bundle(calib_dir, cal, evs)
    reg.ensure_tenant("bank")
    reg.put_suite("bank", "banking", yaml_text, agent_name="bank-assistant")
    reg.save_calibration("bank", "banking", cal, evs)
    reg.promote(cal["id"])
    print("calibrated", cal["id"], cal["suite_choice"]["hosted"], cal["routes"])

    # collectors
    env = dict(os.environ, EE_KAFKA_TOPIC=TOPIC, EE_INDEX_PREFIX=PREFIX, EE_INGEST_GROUP=f"e2e-{int(time.time())}",
               EE_PRICES_FILE=str(REPO / "deploy/collector/prices.json"))
    eecol = str(REPO / "collector/_build/eecol")
    logs = ROOT / ".e2e-logs"
    logs.mkdir(exist_ok=True)
    procs = [subprocess.Popen([eecol, "--config", str(REPO / f"deploy/collector/{n}.yaml")], env=env,
                              stdout=open(logs / f"{n}.log", "w"), stderr=subprocess.STDOUT) for n in ("gateway", "ingest")]
    import atexit
    atexit.register(lambda: [p.terminate() for p in procs])  # never leave collectors holding the ports
    time.sleep(4)
    for p in procs:
        if p.poll() is not None:
            sys.exit(f"collector exited at start (port in use?): see {logs}")

    c_set = load("synthetic_c.jsonl")
    labels = {c["id"]: c["labels"] for c in c_set}
    timings = {}
    t0 = time.time()
    n_spans = replay(c_set, "http://localhost:4318", tenant="bank")
    t_sent = time.time() - t0
    print(f"replayed {len(c_set)} conversations, {n_spans} spans in {t_sent:.1f}s")
    t1 = time.time()
    while os_.count("obs") < n_spans and time.time() - t1 < 120:
        time.sleep(0.5)
        os_.refresh("obs")
    timings["ingest_s"] = time.time() - t0
    print(f"indexed {os_.count('obs')} / {n_spans} spans after {timings['ingest_s']:.1f}s")

    async def drive():
        stop = asyncio.Event()
        tracker = asyncio.create_task(run_tracker(pg, KAFKA, Timing(trace_idle_s=1, session_idle_s=3), topic=TOPIC,
                                                  group=f"e2e-tracker-{int(time.time())}", stop=stop))
        worker = Worker(pg, os_, reg, retry_s=2, batch=50, client=client)
        start = time.time()
        while time.time() - start < 900:
            await worker.run_once()
            row = pg.one("SELECT count(*) FILTER (WHERE status='evaluated') AS done, count(*) AS n, "
                         "count(*) FILTER (WHERE status='error') AS err FROM trace_tracker")
            if row["done"] + row["err"] >= len(c_set):
                break
            await asyncio.sleep(0.2)
        timings["evaluate_s"] = time.time() - start
        stop.set()
        await tracker

    asyncio.run(drive())
    for p in procs:
        p.terminate()
    errors = pg.all("SELECT conversation_id, last_error FROM trace_tracker WHERE status <> 'evaluated'")
    os_.refresh()

    traces = {t["conversation_id"]: t for t in os_.hits("traces", {"size": 1000})}
    checks = [q.name for q in suite.questions]
    acc = {q: float(np.mean([traces[cid]["scores"][q]["value"] == labels[cid][q] for cid in traces])) for q in checks}
    eval_cost = float(np.mean([t["eval_cost_usd"] for t in traces.values()]))
    hosted_share = float(np.mean([any(s["tier"] >= 2 for s in t["scores"].values()) for t in traces.values()]))
    llm_share = float(np.mean([any(s["tier"] == 3 for s in t["scores"].values()) for t in traces.values()]))
    kept = sum(t["content_kept"] for t in traces.values())
    reasons = defaultdict(int)
    for t in traces.values():
        for r in t["retention_reasons"]:
            reasons[r] += 1
    drift = drift_check(pg, os_, reg, "bank", "banking", min_n=1)
    sizes = {i["index"]: (int(i["docs.count"]), i["store.size"])
             for i in os_.req("GET", f"/_cat/indices/{PREFIX}-*", format="json", bytes="kb")}

    engine_md = {"false_confirmation": 94.3, "disclosure_missing": 99.3, "fee_mismatch": 93.3,
                 "user_frustrated": 80.7, "tool_loop": 100.0}
    res = {"calibration": cal["id"], "hosted": cal["suite_choice"]["hosted"], "routes": cal["routes"],
           "conversations": len(c_set), "evaluated": len(traces), "errors": errors, "spans": n_spans,
           "obs_docs": os_.count("obs"), "stage_docs": os_.count("stage"), "payload_docs": os_.count("payload"),
           "score_docs": os_.count("scores"), "accuracy": acc, "mean_accuracy": float(np.mean(list(acc.values()))),
           "engine_study_accuracy": engine_md, "usd_per_1M_conv": 1e6 * eval_cost,
           "sent_to_llm": llm_share, "sent_to_hosted": hosted_share, "content_kept": kept, "retention_reasons": reasons,
           "replay_s": t_sent, "ingest_s": timings["ingest_s"], "evaluate_s": timings["evaluate_s"], "drift": {k: v for k, v in drift.items() if k != "alerts"},
           "indices_kb": sizes}
    (ROOT / "results" / "pipeline.json").write_text(json.dumps(res, indent=1, default=str))

    lines = ["# The engine through the production path", "",
             "Calibration: 158 set-A conversations (as in ENGINE_SUMMARY.md). Holdout: the 300 set-C conversations, "
             "replayed as OpenTelemetry spans through gateway collector → Kafka → ingest collector → OpenSearch → "
             "tracker → evaluation worker. Hosted answers come from the cache, so any change in what evaluators see "
             "would have failed the run.", "",
             f"- Conversations evaluated: **{len(traces)} / {len(c_set)}**, errors: {len(errors)}",
             f"- Spans: {n_spans} sent, {res['obs_docs']} `obs` documents, {res['stage_docs']} staged content documents",
             f"- Hosted set chosen by calibration: {', '.join(res['hosted'])}",
             f"- Timing (one machine, 4 cores): spans indexed {res['ingest_s']:.1f}s after the replay started; "
             f"all conversations scored {res['evaluate_s']:.0f}s later (session idle timeout 3s; hosted answers from cache)",
             "- Evaluation cost includes the 2% audit sample",
             f"- Evaluation cost: ${res['usd_per_1M_conv']:.0f} per 1M conversations; sent to an LLM: {100 * llm_share:.0f}%",
             f"- Content kept after evaluation (eval-aware retention): {kept} / {len(traces)} "
             f"({', '.join(f'{k}: {v}' for k, v in sorted(reasons.items()))})", "",
             "| check | through the pipeline % | engine study % |", "|---|---|---|"]
    lines += [f"| {q} | {100 * acc[q]:.1f} | {engine_md[q]:.1f} |" for q in checks]
    lines += [f"| **mean** | **{100 * res['mean_accuracy']:.1f}** | **{np.mean(list(engine_md.values())):.1f}** |", "",
              "Drift monitor on the audit sample (agreement of routed decisions with the audit judge):", "",
              "| check | n | live agreement % | calibration baseline % |", "|---|---|---|---|"]
    for q, v in sorted(drift.get("agreement", {}).items()):
        base = (drift["baseline"].get(q) or {}).get("agreement")
        lines.append(f"| {q} | {v['n']} | {100 * v['agreement']:.1f} | {'' if base is None else f'{100 * base:.1f}'} |")
    (ROOT / "results" / "PIPELINE.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
