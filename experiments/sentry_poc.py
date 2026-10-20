"""Phase-5 gate (DESIGN.md §12): the Sentry bridge against a real self-hosted Sentry.

"Click-through both ways on a demo; self-hosted proof of concept." Needs a running self-hosted
Sentry (Relay on :3001, web on :9100) with a project and an internal integration whose webhook
points at http://127.0.0.1:8001/v1/sentry/webhook; `deploy/sentry-poc/` sets one up, and its
bootstrap writes the JSON this script reads (--sentry). OpenSearch, Postgres, Kafka and the built
collector must be running locally.

    python experiments/sentry_poc.py --sentry sentry_poc.json

1. A demo agent runs a synthetic banking conversation through our collectors (OTel spans), then
   a transfer turn whose tool raises; the Sentry SDK captures it with our `before_send` hook.
2. Sentry creates the issue and sends `issue.created` to our webhook bridge, which links the issue
   to the trace (Postgres + traces-* rollup) and creates an External Issue on it.
3. Checks, through each system's own API: the event carries the trace id and our deep link and no
   conversation content; Sentry shows the External Issue pointing at our conversation; our API
   returns the issue with a Sentry permalink; resolving in Sentry updates our rollup.
4. A browser (Playwright) clicks through both ways: our conversation page -> the Sentry issue, and
   the Sentry issue's External Issue link -> our conversation page. Screenshots in results/.
5. A drift alert becomes a fingerprinted Sentry issue.

Writes results/SENTRY_POC.md and results/sentry_poc.json.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
sys.path.insert(0, str(REPO))
os.environ.setdefault("HF_HUB_OFFLINE", "1")

PREFIX, TENANT, API_PORT = "sentrypoc", "bank", 8001
TOPIC = f"sentrypoc-{int(time.time())}"  # fresh per run: a new tracker group would replay older runs
PG_BASE = os.environ.get("EE_POSTGRES_BASE", "postgresql://postgres@localhost:5432")
SECRET = "my card is 4111 1111 1111 1111"


def wait(pred, timeout, what, every=1.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            v = pred()
        except httpx.TransportError:
            v = None
        if v:
            return v
        time.sleep(every)
    raise TimeoutError(f"timed out after {timeout}s waiting for {what}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sentry", required=True, help="JSON written by the Sentry bootstrap")
    a = ap.parse_args()
    sc = json.loads(Path(a.sentry).read_text())
    sapi = httpx.Client(base_url=sc["url"] + "/api/0", headers={"Authorization": f"Bearer {sc['token']}"}, timeout=30)

    from elastic_evals.calibrate import calibrate
    from elastic_evals.registry import ArtifactStore, Registry
    from elastic_evals.replay import emit, tracer_provider
    from elastic_evals.store.opensearch import OpenSearch
    from elastic_evals.store.postgres import Postgres, create_database
    from elastic_evals.suite import Suite
    from elastic_evals.tracker import Timing
    from elastic_evals.tracker import run as run_tracker
    from elastic_evals.worker import Worker

    # fresh stores, a local-only calibration (no hosted calls), tenant "bank"
    os_ = OpenSearch(prefix=PREFIX)
    os_.teardown()
    os_.setup(pools=("shared",))
    import psycopg
    with psycopg.connect(PG_BASE + "/postgres", autocommit=True) as c:
        c.execute('DROP DATABASE IF EXISTS "ee_sentrypoc" WITH (FORCE)')
    create_database(PG_BASE + "/ee_sentrypoc")
    pg = Postgres(PG_BASE + "/ee_sentrypoc")
    pg.migrate()
    reg = Registry(pg, ArtifactStore(f"file://{ROOT}/.artifacts"))
    reg.ensure_tenant(TENANT, allow_hosted=False)
    yaml_text = (REPO / "examples/banking_suite.yaml").read_text()
    reg.put_suite(TENANT, "banking", yaml_text, agent_name="bank-assistant")
    suite = Suite.from_yaml(yaml_text)
    suite.policy.allow_hosted = False
    a_set = [json.loads(line) for line in (ROOT / "data/synthetic.jsonl").open()]
    dev = [c for i, c in enumerate(a_set) if i % 5 in (0, 1)]
    cal, evs = calibrate(suite, dev, {(c["id"], q): v for c in dev for q, v in c["labels"].items()})
    reg.save_calibration(TENANT, "banking", cal, evs)
    reg.promote(cal["id"])

    # collectors and our API with the bridge
    env = dict(os.environ, EE_KAFKA_TOPIC=TOPIC, EE_INDEX_PREFIX=PREFIX, EE_INGEST_GROUP=f"sp-{int(time.time())}",
               EE_PRICES_FILE=str(REPO / "deploy/collector/prices.json"))
    logs = ROOT / ".e2e-logs"
    logs.mkdir(exist_ok=True)
    eecol = str(REPO / "collector/_build/eecol")
    procs = [subprocess.Popen([eecol, "--config", str(REPO / f"deploy/collector/{n}.yaml")], env=env,
                              stdout=open(logs / f"sentrypoc-{n}.log", "w"), stderr=subprocess.STDOUT)
             for n in ("gateway", "ingest")]
    api_env = dict(env, EE_POSTGRES_DSN=PG_BASE + "/ee_sentrypoc", EE_AUTH="off",
                   EE_SENTRY_CLIENT_SECRET=sc["client_secret"], EE_SENTRY_TOKEN=sc["app_token"],
                   EE_SENTRY_URL=sc["url"], EE_SENTRY_ORG=sc["org"], EE_SENTRY_INSTALLATION=sc["installation"],
                   EE_UI_URL=f"http://localhost:{API_PORT}")
    procs.append(subprocess.Popen([sys.executable, "-c", "import logging, uvicorn; logging.basicConfig(level='INFO'); "
                                   f"uvicorn.run('elastic_evals.api:app', port={API_PORT})"],
                                  env=api_env, cwd=REPO, stdout=open(logs / "sentrypoc-api.log", "w"), stderr=subprocess.STDOUT))
    import atexit
    atexit.register(lambda: [p.terminate() for p in procs])
    wait(lambda: httpx.get(f"http://localhost:{API_PORT}/healthz").status_code == 200, 60, "our API")
    for p in procs:
        if p.poll() is not None:
            sys.exit(f"a process exited at start (port in use?): see {logs}")
    api = httpx.Client(base_url=f"http://localhost:{API_PORT}", headers={"X-EE-Tenant": TENANT}, timeout=30)

    # 1. the demo agent: a conversation, then a transfer turn whose tool raises
    import sentry_sdk

    from elastic_evals.sentry import Bridge, before_send
    provider = tracer_provider("http://localhost:4318", TENANT, service="bank-agent")
    tracer = provider.get_tracer("demo-agent")
    sentry_sdk.init(dsn=sc["dsn"], before_send=before_send(ui_url=f"http://localhost:{API_PORT}", tenant_id=TENANT,
                                                               tracer_provider=provider),
                    send_default_pii=False, default_integrations=False, environment="poc")
    c_set = [json.loads(line) for line in (ROOT / "data/synthetic_c.jsonl").open()]
    conv = dict(c_set[0], id=f"poc-{int(time.time())}")
    emit(tracer, conv)
    with tracer.start_as_current_span("invoke_agent bank-assistant", attributes={
            "gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": "bank-assistant",
            "gen_ai.conversation.id": conv["id"], "ee.lang": conv.get("lang", "und"),
            "gen_ai.input.messages": json.dumps([{"role": "user", "parts": [{"type": "text", "content": SECRET}]}])}) as root:
        trace_id = format(root.get_span_context().trace_id, "032x")
        with tracer.start_as_current_span("execute_tool transfer", attributes={
                "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "transfer"}):
            try:
                raise ConnectionError("core banking transfer service unavailable (503)")
            except ConnectionError:
                event_id = sentry_sdk.capture_exception()
    # 5 (sent now). A drift alert becomes a fingerprinted Sentry issue. Sent after the exception because
    # Sentry post-processes an event once the next one arrives (Snuba's commit log records the last
    # processed offset): with real traffic that is immediate, in a one-event demo it would stall. The
    # pause keeps the exception ahead of it (Relay does not keep the order of near-simultaneous events).
    sentry_sdk.flush(10)
    time.sleep(3)
    Bridge("unused").quality_regression({"tenant_id": TENANT, "suite_id": "banking", "check_name": "user_frustrated",
                                         "lang": "es", "baseline": 0.88, "observed": 0.71, "n": 120, "calibration_id": cal["id"]})
    sentry_sdk.flush(10)
    captured_at = time.time() - 3
    provider.force_flush()
    print("conversation", conv["id"], "trace", trace_id, "sentry event", event_id)
    res = {"conversation_id": conv["id"], "trace_id": trace_id, "event_id": event_id, "checks": {}}
    checks = res["checks"]

    # our side: ingest, track, evaluate
    async def drive():
        stop = asyncio.Event()
        tracker = asyncio.create_task(run_tracker(pg, "localhost:9092", Timing(trace_idle_s=1, session_idle_s=3),
                                                  topic=TOPIC, group=f"sp-tr-{int(time.time())}", stop=stop))
        worker = Worker(pg, os_, reg, retry_s=2, batch=10)
        t0 = time.time()
        while time.time() - t0 < 180:
            await worker.run_once()
            if pg.one("SELECT count(*) AS n FROM trace_tracker WHERE status='evaluated' AND conversation_id=%s", conv["id"])["n"]:
                break
            await asyncio.sleep(0.5)
        stop.set()
        await tracker
    asyncio.run(drive())
    checks["conversation evaluated"] = bool(pg.one("SELECT count(*) AS n FROM trace_tracker WHERE status='evaluated' "
                                                   "AND conversation_id=%s", conv["id"])["n"])

    # 2. Sentry: the event, the issue, the webhook, the link
    ev = wait(lambda: (lambda r: r.json() if r.status_code == 200 else None)(
        sapi.get(f"/projects/{sc['org']}/{sc['project']}/events/{event_id.replace('-', '')}/")), 180, "the Sentry event", 3)
    tags = {t["key"]: t["value"] for t in ev["tags"]}
    issue_id = str(ev["groupID"])
    checks["event tagged with the trace id"] = tags.get("trace_id") == trace_id
    checks["event tagged with the conversation id"] = tags.get("gen_ai.conversation.id") == conv["id"]
    checks["event carries our deep link"] = (ev.get("contexts", {}).get("elastic_evals", {}).get("url")
                                             == f"http://localhost:{API_PORT}/conversations/{conv['id']}")
    checks["no conversation content in Sentry"] = SECRET not in json.dumps(ev)
    link = wait(lambda: pg.one("SELECT * FROM sentry_links WHERE tenant_id=%s AND trace_id=%s", TENANT, trace_id), 600,
                "a webhook to link the trace", 2)
    res["link_s_after_capture"] = round(time.time() - captured_at, 1)
    checks["webhook linked issue to trace"] = link["sentry_issue_id"] == issue_id
    ext = wait(lambda: sapi.get(f"/issues/{issue_id}/external-issues/").json(), 60, "the External Issue", 2)
    checks["External Issue on the Sentry issue opens our conversations for that issue"] = any(
        f"sentry_issue={issue_id}" in e.get("webUrl", "") for e in ext)
    res["external_issue"] = ext
    detail = api.get(f"/v1/conversations/{conv['id']}").json()
    ours = [s for s in detail["sentry"] if str(s["sentry_issue_id"]) == issue_id]
    checks["our API returns the issue with a Sentry permalink"] = bool(ours and ours[0].get("permalink"))
    os_.refresh("traces")
    roll = os_.hits("traces", {"size": 1, "query": {"term": {"conversation_id": conv["id"]}}})
    checks["traces-* rollup carries the issue id"] = bool(roll and issue_id in (roll[0].get("sentry_issue_ids") or []))

    # 4. click-through, both ways, in a browser
    from playwright.sync_api import sync_playwright
    shots = ROOT / "results" / "sentry_poc"
    shots.mkdir(exist_ok=True)
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("EE_CHROMIUM") or None)
        page = b.new_page(viewport={"width": 1400, "height": 1000})
        page.goto(f"{sc['url']}/auth/login/")
        page.locator("input[name=username]").first.fill(sc["user"])
        page.locator("input[name=password]").first.fill(sc["password"])
        page.get_by_role("button", name="Sign In").click()
        page.wait_for_load_state("networkidle")
        # ours -> Sentry
        page.goto(f"http://localhost:{API_PORT}/")
        page.evaluate(f"localStorage.setItem('ee.tenant', '{TENANT}')")
        page.goto(f"http://localhost:{API_PORT}/conversations/{conv['id']}")
        page.get_by_text(f"issue {issue_id}").wait_for(timeout=30000)
        page.screenshot(path=str(shots / "1-ours-conversation.png"), full_page=True)
        page.get_by_role("link", name="open").first.click()
        page.wait_for_url(f"**/issues/{issue_id}/**", timeout=30000)
        page.get_by_text("core banking transfer service unavailable").first.wait_for(timeout=60000)
        # Sentry 25.7's classic issue page swallows clicks on integration External Issue links
        # (preventDefault once an issue is linked); the new issue UI renders them as plain links
        time.sleep(3)
        if page.get_by_text("Try New UI").count():
            page.get_by_text("Try New UI").first.click()
            time.sleep(5)
        if page.get_by_text("Maybe later").count():  # first-visit tour
            page.get_by_text("Maybe later").click()
            time.sleep(1)
        page.screenshot(path=str(shots / "2-sentry-issue.png"))
        checks["click: our conversation -> Sentry issue"] = True
        # Sentry -> ours, through the External Issue link: every conversation linked to the issue
        ext_link = page.locator(f"a[href*='sentry_issue={issue_id}']").first
        ext_link.wait_for(timeout=60000)
        href = ext_link.get_attribute("href")
        with page.context.expect_page(timeout=15000) as popup:  # target=_blank
            ext_link.click()
        ours_page = popup.value
        ours_page.evaluate(f"localStorage.setItem('ee.tenant', '{TENANT}')")
        ours_page.reload()
        rows = ours_page.locator("tr.click")
        rows.first.wait_for(timeout=30000)
        res["conversations_listed_for_issue"] = rows.count()
        ours_page.screenshot(path=str(shots / "3-ours-from-sentry.png"), full_page=True)
        rows.first.click()
        ours_page.get_by_text(f"issue {issue_id}").wait_for(timeout=30000)
        ours_page.get_by_text(conv["id"]).first.wait_for(timeout=30000)
        checks["click: Sentry issue -> our conversations for that issue -> the conversation"] = conv["id"] in ours_page.url
        res["external_issue_href"] = href
        b.close()

    # 3b. resolve in Sentry -> our rollup follows
    sapi.put(f"/organizations/{sc['org']}/issues/{issue_id}/", json={"status": "resolved"}).raise_for_status()
    wait(lambda: pg.one("SELECT status FROM sentry_links WHERE sentry_issue_id=%s", issue_id)["status"] == "resolved", 120,
         "issue.resolved webhook", 2)
    os_.refresh("traces")
    roll = os_.hits("traces", {"size": 1, "query": {"term": {"conversation_id": conv["id"]}}})
    checks["resolving in Sentry updates our rollup"] = roll[0].get("sentry_status") == "resolved"

    # 5. the drift alert sent at step 1
    drift = wait(lambda: sapi.get(f"/projects/{sc['org']}/{sc['project']}/issues/",
                                  params={"query": "ee.check:user_frustrated", "statsPeriod": "24h"}).json(), 180,
                 "the drift issue", 3)
    checks["drift alert is a Sentry issue, tagged by check"] = bool(drift) and drift[0]["level"] == "warning"
    res["drift_issue"] = {"id": drift[0]["id"], "title": drift[0]["title"]} if drift else None
    res["issue_id"] = issue_id

    for p in procs:
        p.terminate()
    res["passed"] = all(checks.values())
    (ROOT / "results" / "sentry_poc.json").write_text(json.dumps(res, indent=1))
    lines = ["# Sentry bridge against a self-hosted Sentry (phase-5 gate)", "",
             "Self-hosted Sentry 25.7.0 (web, worker, ingest and post-process consumers, Snuba, ClickHouse, Relay) "
             "running next to the elastic-evals pipeline; set up by `deploy/sentry-poc/`. "
             "One demo conversation through our collectors; its transfer tool raises and the Sentry SDK captures it "
             "with `before_send`. Script: `experiments/sentry_poc.py`.", "",
             "| check | result |", "|---|---|"]
    lines += [f"| {k} | {'✓' if v else '✗'} |" for k, v in checks.items()]
    lines += ["", f"Gate: **{'passed' if res['passed'] else 'not passed'}**. The issue was linked to our trace "
              f"{res['link_s_after_capture']:.0f} s after the exception was captured.", "",
              "**What the real Sentry exposed** (each fixed, with a test, before this run):", "",
              "- `issue.created` fires once per issue; later conversations hitting the same issue arrive only as "
              "`error.created`, which the bridge ignored. A regression (a resolved issue seen again) arrives as "
              "`issue.unresolved`, not always with `error.created`. The bridge now links on all three.",
              "- Sentry keeps one External Issue per issue and integration, so it pointed at the first trace only. "
              "It now opens our conversation list filtered to the issue (`/conversations?sentry_issue=…`).",
              "- Sentry links by trace id; our API looked conversations up by conversation id only. It now resolves "
              "a trace id to its conversation.",
              "- The issue usually exists before the conversation is evaluated, and the worker rewrote the rollup "
              "without the link. The worker now carries Sentry links into every rollup it writes.",
              "- Errors are raised in child spans (tool calls), which do not carry `gen_ai.conversation.id`; "
              "`before_send` now gets it from a span processor that indexes each trace's conversation and agent.",
              "- Stored links had no Sentry URL for the UI to open; the API now builds the issue permalink.", "",
              "Self-hosted specifics, not ours: Sentry post-processes an event (and sends its webhooks) once the next "
              "event arrives; Relay needs keep-alive off on uWSGI without nginx; the classic issue page swallows "
              "clicks on integration External Issue links (the new issue UI opens them).", "",
              "Screenshots: [our conversation with its Sentry issue](sentry_poc/1-ours-conversation.png), "
              "[the Sentry issue opened from it](sentry_poc/2-sentry-issue.png), "
              "[our conversations for the issue, opened from its External Issue link](sentry_poc/3-ours-from-sentry.png)."]
    (ROOT / "results" / "SENTRY_POC.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0 if res["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
