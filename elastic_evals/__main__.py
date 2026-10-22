"""elastic-evals command line.

Engine (no services needed):
  calibrate   --suite S --data labelled.jsonl --out calib/ [--local-only] [--pool unlabelled.jsonl --teacher llm_x]
  report      --calib calib/
  run         --suite S --calib calib/ --data convs.jsonl --out scores.jsonl
  experiment  --suite S --calib calib/ --items items.jsonl --agent pkg.mod:fn [--baseline run.json] [--report run.json]
  synth       --spec spec.yaml --n 50 --out synth.jsonl
  gate        --suite S --data human_labelled.jsonl [--folds 5]           phase-1 gate (exit code 1 if not passed)

Services (OpenSearch, Postgres, Kafka; see deploy/):
  setup       [--pools shared] [--tenant acme --dedicated] [--admin-key] [--platform-key] [--dashboards URL]
                                                                            indices, templates, ISM, schema, saved objects
  serve       [--port 8000]                                                 API + UI
  mcp         [--url http://localhost:8000] [--read-only] [--transport stdio|streamable-http]
                                                                            MCP server for AI agents (key in EE_API_KEY)
  presidio    [--port 3000]                                                 PII analyzer for the gateway
  tracker     [--brokers localhost:9092] [--session-idle 300]               trace-complete signal
  worker      [--embed]                                                     evaluation workers
  replay      --data convs.jsonl --tenant acme [--endpoint http://localhost:4318]
  suite       --tenant T --id ID --file suite.yaml [--agent name]           register a suite version
  register    --tenant T --suite ID --calib calib/                          store a calibration (candidate)
  shadow | promote | reject  CALIBRATION_ID
  drift       --tenant T --suite ID
  annotate    --tenant T --queue QUEUE_ID [--annotator you]                 keyboard labelling in the terminal
  prices      [--load prices.json] [--export collector-prices.json]         the price book (collector reloads it)
  reprice     --tenant T --from 2026-10-01 [--to ...]                       recompute stored conversations' cost

Labelled conversations carry "labels": {question: 0|1|class}. OpenRouter calls need OPENROUTER_API_KEY
(or EVAL_ENV_FILE pointing at an env file outside the repository).
"""

import argparse
import asyncio
import importlib
import json
import logging
import os
import sys
from pathlib import Path

from tabulate import tabulate


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).open() if line.strip()]


def print_report(cal):
    sc = cal["suite_choice"]
    print(f"calibration {cal.get('id', '?')} ({cal.get('status', '?')}) on {cal['n_labelled']} labelled conversations; "
          f"hosted set: {', '.join(sc['hosted']) or 'none'}; ${1e6 * sc['usd_per_conv']:.1f} / 1M conv, "
          f"{sc['ms_per_conv']:.0f} ms / conv" + ("" if cal.get("budget_met", True) else "  ** OVER BUDGET **"))
    langs = sorted({lg for r in cal["report"].values() for lg in r["chosen"].get("by_lang", {})})
    rows = []
    for q, r in cal["report"].items():
        by_lang = r["chosen"].get("by_lang", {})
        rows.append([q, " → ".join(cal["routes"][q]), f"{100 * r['chosen']['accuracy']:.1f}",
                     f"{100 * r['floor']:.1f}", f"{100 * r['best_accuracy']:.1f}",
                     *[f"{100 * by_lang[lg]['accuracy']:.1f}" if lg in by_lang else "" for lg in langs],
                     f"{r['chosen']['usd_per_conv'] * 1e6:.1f}", f"{r['chosen']['ms_per_conv']:.0f}",
                     "yes" if r.get("meets_floor", True) else "NO", "yes" if r["meets_target"] else "NO"])
    print(tabulate(rows, ["question", "route", "acc %", "floor %", "best %", *[f"{lg} %" for lg in langs],
                          "$ / 1M conv", "ms", "meets floor", "meets target"], tablefmt="github"))


def _client(allow_hosted):
    if not allow_hosted:
        return None
    from .providers import OpenRouter
    return OpenRouter()


def _stores():
    from .registry import Registry
    from .store.opensearch import OpenSearch
    from .store.postgres import Postgres
    pg = Postgres()
    os_ = OpenSearch(prefix=os.environ.get("EE_INDEX_PREFIX", "ee"))
    return pg, os_, Registry(pg)


def cmd_calibrate(a):
    from .bundle import save
    from .calibrate import calibrate
    from .suite import Suite
    suite = Suite.load(a.suite)
    suite.policy.allow_hosted = suite.policy.allow_hosted and not a.local_only
    convs = read_jsonl(a.data)
    labels = {(cv["id"], q): v for cv in convs for q, v in cv["labels"].items()}
    client = _client(suite.policy.allow_hosted or bool(a.teacher))
    if a.pool:
        from .distill import teacher_labels
        from .calibrate import calibrate_async
        pool = read_jsonl(a.pool)

        async def go():
            teacher_suite = Suite.load(a.suite)
            judge, usd = await teacher_labels(teacher_suite, pool, a.teacher, client)
            print(f"teacher {a.teacher} labelled {len(pool)} conversations (${usd:.4f})")
            return await calibrate_async(suite, convs, labels, client if suite.policy.allow_hosted else None,
                                         extra=(pool, judge))
        cal, evaluators = asyncio.run(go())
    else:
        cal, evaluators = calibrate(suite, convs, labels, client)
    save(a.out, cal, evaluators)
    print_report(cal)


def cmd_run(a):
    from .bundle import load
    from .engine import Engine
    suite, cal, evaluators = load(a.suite, a.calib)
    results = Engine(suite, cal, evaluators).run(read_jsonl(a.data))
    with Path(a.out).open("w") as f:
        for res in results:
            for s in res["scores"]:
                f.write(json.dumps(s, ensure_ascii=False) + "\n")
    n = len(results)
    print(f"scored {n} conversations: ${sum(x['usd'] for x in results):.6f} "
          f"(+${sum(x['audit_usd'] for x in results):.6f} audit), "
          f"{sum(x['ms'] for x in results) / max(1, n):.0f} ms/conv, "
          f"{sum(any(e.startswith('llm') for e in x['evaluators_called']) for x in results)} sent to an LLM")


def cmd_experiment(a):
    from . import experiments as exps
    from .bundle import load
    suite, cal, evaluators = load(a.suite, a.calib)
    module, fn = a.agent.split(":")
    agent = getattr(importlib.import_module(module), fn)
    items = read_jsonl(a.items)
    baseline = json.loads(Path(a.baseline).read_text())["summary"] if a.baseline else None
    result = exps.run(agent, items, suite, cal, evaluators, baseline=baseline, tolerance=a.tolerance)
    s = result["summary"]
    for check, rate in s["rates"].items():
        if rate is not None:
            print(f"{check:24s} flagged {100 * rate:5.1f}%")
    for g in s["gates"]:
        print(f"gate {g['check']:20s} {g['kind']:10s} {'PASS' if g['pass'] else 'FAIL'}")
    if a.report:
        Path(a.report).write_text(json.dumps(result, indent=1, default=str, ensure_ascii=False))
    return 0 if s["passed"] else 1


def cmd_synth(a):
    from .synth import generate
    from .providers import OpenRouter
    convs, stats = generate(a.spec, a.n, OpenRouter(), a.seed)
    with Path(a.out).open("w") as f:
        for c in convs:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    print(json.dumps(stats))


def cmd_setup(a):
    pg, os_, reg = _stores()
    pg.migrate()
    pools = set(a.pools)
    if a.tenant:
        reg.ensure_tenant(a.tenant, tier="dedicated" if a.dedicated else "pooled", allow_hosted=not a.local_only)
        pools.add(reg.pool(a.tenant))
    os_.setup(pools=tuple(sorted(pools)), shards=a.shards, replicas=a.replicas)
    print(f"OpenSearch pools {sorted(pools)} and Postgres schema ready")
    if a.dashboards:
        from .dashboards import import_objects
        res = import_objects(a.dashboards, os_.prefix)
        print(f"OpenSearch Dashboards: {res['successCount']} saved objects imported")
    for role, wanted in (("admin", a.admin_key), ("platform", a.platform_key)):
        if wanted and a.tenant:
            import secrets
            from .api import hash_secret
            key_id, secret = f"ek_{secrets.token_hex(6)}", secrets.token_urlsafe(24)
            pg.exec("INSERT INTO api_keys (id, tenant_id, secret_hash, role_id) VALUES (%s, %s, %s, %s)",
                    key_id, a.tenant, hash_secret(secret), role)
            print(f"{role} API key for {a.tenant}: {key_id}.{secret}")


def cmd_gate(a):
    import yaml
    from .gates import phase1
    from .suite import Suite
    suite = Suite.load(a.suite)
    suite.policy.allow_hosted = suite.policy.allow_hosted and not a.local_only
    reasons = yaml.safe_load(Path(a.reasons).read_text()) if a.reasons else {}
    res = phase1(suite, read_jsonl(a.data), _client(suite.policy.allow_hosted), a.folds, reasons=reasons)
    rows = [[q, f"{100 * r['engine']:.1f}", f"{r['best_single']} {100 * r['best_single_accuracy']:.1f}",
             f"{100 * r['se']:.1f}", "PASS" if r["pass"] else ("documented" if r["reason"] else "FAIL")]
            for q, r in res["checks"].items()]
    print(tabulate(rows, ["check", "engine %", "best single %", "1 SE", "gate"], tablefmt="github"))
    print(f"engine: ${res['engine_usd_per_1M']:.0f} / 1M conv, {res['engine_ms']:.0f} ms, "
          f"{100 * res['engine_llm_share']:.0f}% sent to an LLM; phase-1 gate {'PASSED' if res['passed'] else 'NOT PASSED'}")
    if a.out:
        Path(a.out).write_text(json.dumps(res, indent=1))
    return 0 if res["passed"] else 1


def cmd_dashboards(a):
    from .dashboards import ndjson
    Path(a.out).write_text(ndjson(a.prefix))


def cmd_serve(a):
    import uvicorn
    uvicorn.run("elastic_evals.api:app", host=a.host, port=a.port, log_level="info")


def cmd_mcp(a):
    from .mcp_server import serve
    serve(a.transport, a.host, a.port, a.read_only, a.url)


def cmd_presidio(a):
    import uvicorn
    uvicorn.run("elastic_evals.presidio_service:app", host=a.host, port=a.port, log_level="info")


def cmd_tracker(a):
    from .tracker import Timing, run
    pg, _, _ = _stores()
    asyncio.run(run(pg, a.brokers, Timing(a.trace_idle, a.session_idle, a.max_wait), a.topic))


def cmd_worker(a):
    from .worker import Worker
    pg, os_, reg = _stores()
    asyncio.run(Worker(pg, os_, reg, batch=a.batch, embed=a.embed).run())


def cmd_replay(a):
    from .replay import replay
    n = replay(read_jsonl(a.data), a.endpoint, a.tenant, agent=a.agent, rate=a.rate)
    print(f"sent {n} spans")


def cmd_suite(a):
    _, _, reg = _stores()
    print(reg.put_suite(a.tenant, a.id, Path(a.file).read_text(), a.agent))


def cmd_register(a):
    from .bundle import load
    _, _, reg = _stores()
    calib = json.loads((Path(a.calib) / "calibration.json").read_text())
    version = calib["suite_version"]
    suite_row = reg.pg.one("SELECT yaml FROM suite_versions WHERE tenant_id=%s AND suite_id=%s AND version=%s",
                           a.tenant, a.suite, version)
    if suite_row is None:
        sys.exit(f"suite {a.suite}@{version} is not registered for {a.tenant}: run `suite` first with the same YAML")
    tmp = Path(a.calib) / ".suite.yaml"
    tmp.write_text(suite_row["yaml"])
    _, cal, evaluators = load(tmp, a.calib)
    print(reg.save_calibration(a.tenant, a.suite, cal, evaluators))


def cmd_lifecycle(a):
    _, _, reg = _stores()
    {"shadow": reg.start_shadow, "promote": reg.promote, "reject": reg.reject}[a.cmd](a.calibration)
    print(reg.calibration(a.calibration)["status"])


def cmd_drift(a):
    from .drift import check
    pg, os_, reg = _stores()
    print(json.dumps(check(pg, os_, reg, a.tenant, a.suite, a.days, a.min_n, a.threshold), indent=1, default=str))


def cmd_reprice(a):
    from .pricing import PriceBook, reprice, rows_from_db
    pg, os_, reg = _stores()
    rows = json.loads(Path(a.prices).read_text()) if a.prices else rows_from_db(pg)
    print(json.dumps(reprice(os_, reg.pool(a.tenant), a.tenant, PriceBook(rows), getattr(a, "from"), a.to)))


def cmd_prices(a):
    from .pricing import rows_from_db
    from .store.postgres import Jsonb
    pg, _, _ = _stores()
    if a.load:
        rows = json.loads(Path(a.load).read_text())
        with pg.tx() as c:
            c.execute("DELETE FROM model_prices")
            for r in rows:
                c.execute("INSERT INTO model_prices (model_pattern, start_date, tiers, priority) VALUES (%s, %s, %s, %s)",
                          (r["model_pattern"], r.get("start_date", "1970-01-01"), Jsonb(r["tiers"]), r.get("priority", 0)))
    if a.export:
        Path(a.export).write_text(json.dumps(rows_from_db(pg), indent=1))
    print(f"{len(rows_from_db(pg))} prices")


def cmd_annotate(a):
    from . import annotation
    from .conversation import build
    from .views import render
    from .worker import fetch_conversation
    pg, os_, reg = _stores()
    q = annotation.queue(pg, a.queue)
    print(f"queue {q['name']}: checks {', '.join(q['checks'])} — answer 1 (yes) / 0 (no) / s (skip) / q (quit)")
    while True:
        item = annotation.next_item(pg, a.queue, a.annotator)
        if item is None:
            print("queue empty")
            return
        cid = item["conversation_id"]
        row = pg.one("SELECT kind FROM trace_tracker WHERE tenant_id=%s AND conversation_id=%s", q["tenant_id"], cid)
        spans, content = fetch_conversation(os_, q["tenant_id"], cid, row["kind"] if row else "session", reg.pool(q["tenant_id"]))
        conv = build(cid, spans, content, q["tenant_id"])
        print("\n" + "=" * 80 + f"\n{cid}  ({item['reason']})  record: {json.dumps(conv['record'], ensure_ascii=False)}\n")
        print(render(conv["turns"]))
        labels = {}
        for check in q["checks"]:
            ans = input(f"{check}? ").strip().lower()
            if ans == "q":
                return
            if ans == "s":
                annotation.skip(pg, a.queue, cid)
                break
            labels[check] = int(ans) if ans in ("0", "1") else ans
        else:
            annotation.submit(pg, os_, reg, a.queue, cid, labels, a.annotator, conv["lang"])


def main(argv=None):
    ap = argparse.ArgumentParser(prog="elastic_evals", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("calibrate")
    c.add_argument("--suite", required=True)
    c.add_argument("--data", required=True)
    c.add_argument("--out", required=True)
    c.add_argument("--local-only", action="store_true")
    c.add_argument("--pool", help="unlabelled conversations for distillation")
    c.add_argument("--teacher", help="evaluator id of the LLM judge that labels --pool")
    r = sub.add_parser("run")
    r.add_argument("--suite", required=True)
    r.add_argument("--calib", required=True)
    r.add_argument("--data", required=True)
    r.add_argument("--out", required=True)
    p = sub.add_parser("report")
    p.add_argument("--calib", required=True)
    e = sub.add_parser("experiment")
    e.add_argument("--suite", required=True)
    e.add_argument("--calib", required=True)
    e.add_argument("--items", required=True)
    e.add_argument("--agent", required=True, help="module:function taking an item input, returning a conversation")
    e.add_argument("--baseline")
    e.add_argument("--report")
    e.add_argument("--tolerance", type=float, default=0.02)
    s = sub.add_parser("synth")
    s.add_argument("--spec", required=True)
    s.add_argument("--n", type=int, default=50)
    s.add_argument("--out", required=True)
    s.add_argument("--seed", type=int, default=0)

    st = sub.add_parser("setup")
    st.add_argument("--pools", nargs="*", default=["shared"])
    st.add_argument("--tenant")
    st.add_argument("--dedicated", action="store_true")
    st.add_argument("--local-only", action="store_true")
    st.add_argument("--shards", type=int, default=1)
    st.add_argument("--replicas", type=int, default=0)
    st.add_argument("--admin-key", action="store_true", help="print an admin key for --tenant (that tenant only)")
    st.add_argument("--platform-key", action="store_true",
                    help="print a platform key (operators: create tenants and their keys, the price book)")
    st.add_argument("--dashboards", default=os.environ.get("EE_DASHBOARDS_URL"),
                    help="OpenSearch Dashboards URL: import the saved objects")
    g = sub.add_parser("gate", help="phase-1 gate: cross-validated engine against the best single judge per check")
    g.add_argument("--suite", required=True)
    g.add_argument("--data", required=True, help="human-labelled conversations (JSONL)")
    g.add_argument("--folds", type=int, default=5)
    g.add_argument("--reasons", help="YAML: check -> documented reason it may miss the gate")
    g.add_argument("--local-only", action="store_true")
    g.add_argument("--out")
    db = sub.add_parser("dashboards", help="write the OpenSearch Dashboards saved objects as NDJSON")
    db.add_argument("--out", required=True)
    db.add_argument("--prefix", default=os.environ.get("EE_INDEX_PREFIX", "ee"))
    sv = sub.add_parser("serve")
    sv.add_argument("--host", default="0.0.0.0")
    sv.add_argument("--port", type=int, default=8000)
    mc = sub.add_parser("mcp", help="MCP server that lets an AI agent operate a running elastic-evals API")
    mc.add_argument("--url", default=os.environ.get("EE_API_URL", "http://localhost:8000"))
    mc.add_argument("--read-only", action="store_true", default=os.environ.get("EE_MCP_READ_ONLY") == "1",
                    help="do not offer the tools that change anything")
    mc.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    mc.add_argument("--host", default="127.0.0.1")
    mc.add_argument("--port", type=int, default=8765)
    ps = sub.add_parser("presidio", help="Presidio analyzer service for the gateway's redaction")
    ps.add_argument("--host", default="0.0.0.0")
    ps.add_argument("--port", type=int, default=3000)
    tr = sub.add_parser("tracker")
    tr.add_argument("--brokers", default=os.environ.get("EE_KAFKA_BROKERS", "localhost:9092"))
    tr.add_argument("--topic", default=os.environ.get("EE_KAFKA_TOPIC", "ee-spans"))
    tr.add_argument("--trace-idle", type=float, default=30)
    tr.add_argument("--session-idle", type=float, default=300)
    tr.add_argument("--max-wait", type=float, default=3600)
    w = sub.add_parser("worker")
    w.add_argument("--batch", type=int, default=32)
    w.add_argument("--embed", action="store_true")
    rp = sub.add_parser("replay")
    rp.add_argument("--data", required=True)
    rp.add_argument("--tenant", required=True)
    rp.add_argument("--endpoint", default="http://localhost:4318")
    rp.add_argument("--agent", default="bank-assistant")
    rp.add_argument("--rate", type=float, help="conversations per second")
    su = sub.add_parser("suite")
    su.add_argument("--tenant", required=True)
    su.add_argument("--id", required=True)
    su.add_argument("--file", required=True)
    su.add_argument("--agent")
    rg = sub.add_parser("register")
    rg.add_argument("--tenant", required=True)
    rg.add_argument("--suite", required=True)
    rg.add_argument("--calib", required=True)
    for name in ("shadow", "promote", "reject"):
        lc = sub.add_parser(name)
        lc.add_argument("calibration")
    d = sub.add_parser("drift")
    d.add_argument("--tenant", required=True)
    d.add_argument("--suite", required=True)
    d.add_argument("--days", type=float, default=7)
    d.add_argument("--min-n", type=int, default=50)
    d.add_argument("--threshold", type=float, default=0.05)
    rp2 = sub.add_parser("reprice", help="recompute conversation costs from stored usage")
    rp2.add_argument("--tenant", required=True)
    rp2.add_argument("--from", required=True)
    rp2.add_argument("--to")
    rp2.add_argument("--prices", help="prices JSON file (default: the model_prices table)")
    pr = sub.add_parser("prices", help="load the model_prices table from JSON and/or export the collector prices file")
    pr.add_argument("--load")
    pr.add_argument("--export")
    an = sub.add_parser("annotate")
    an.add_argument("--tenant", required=True)
    an.add_argument("--queue", required=True)
    an.add_argument("--annotator", default=os.environ.get("USER", "annotator"))

    args = ap.parse_args(argv)
    from .providers import load_env
    load_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if args.cmd == "report":
        print_report(json.loads((Path(args.calib) / "calibration.json").read_text()))
        return 0
    handler = {"calibrate": cmd_calibrate, "run": cmd_run, "experiment": cmd_experiment, "synth": cmd_synth,
               "setup": cmd_setup, "serve": cmd_serve, "mcp": cmd_mcp, "presidio": cmd_presidio, "tracker": cmd_tracker, "worker": cmd_worker,
               "replay": cmd_replay, "suite": cmd_suite, "register": cmd_register, "shadow": cmd_lifecycle,
               "promote": cmd_lifecycle, "reject": cmd_lifecycle, "drift": cmd_drift, "annotate": cmd_annotate,
               "reprice": cmd_reprice, "prices": cmd_prices,
               "dashboards": cmd_dashboards, "gate": cmd_gate}[args.cmd]
    return handler(args) or 0


if __name__ == "__main__":
    sys.exit(main())
