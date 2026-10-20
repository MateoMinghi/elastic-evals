"""Experiments, gates, the pytest plugin and the CLI: no services needed."""

import json
import random

import pytest
import yaml

from elastic_evals import experiments as exps
from elastic_evals.__main__ import main as cli
from elastic_evals.bundle import save
from elastic_evals.calibrate import calibrate
from elastic_evals.suite import Suite
from elastic_evals.synth import make_item, valid_turns

from .conftest import ROOT, load

pytest_plugins = ["pytester"]

SUITE = yaml.safe_load((ROOT / "examples/banking_suite.yaml").read_text())
SUITE["evaluators"] = [{"id": "rules", "type": "rules"}]
SUITE["policy"] = {"allow_hosted": False, "target_accuracy": 0.95}


@pytest.fixture(scope="module")
def calib(tmp_path_factory):
    d = tmp_path_factory.mktemp("calib")
    (d / "suite.yaml").write_text(yaml.safe_dump(SUITE, allow_unicode=True))
    suite = Suite.load(d / "suite.yaml")
    train = load("synthetic.jsonl", 100)
    cal, evs = calibrate(suite, train, {(c["id"], q): v for c in train for q, v in c["labels"].items()})
    save(d / "cal", cal, evs)
    return d, suite, cal, evs


def test_assertions_vocabulary():
    conv = {"turns": [{"role": "tool", "name": "a", "args": {"x": 1}}, {"role": "tool", "name": "b", "args": {}}]}
    out = {a["type"]: a for a in exps.assertions(conv, {
        "tool_sequence": ["a", "b"], "tool_call_f1": 0.6, "tool_calls": [{"name": "a", "args": {"x": 1}}],
        "not_tools": ["delete"]})}
    assert out["trajectory:tool-sequence"]["pass"]
    assert out["tool-call-f1"]["value"] == pytest.approx(2 / 3) and out["tool-call-f1"]["pass"]
    assert out["not-tool:delete"]["pass"]
    assert exps.tool_call_f1([], []) == 1.0


def test_gates(calib):
    _, suite, cal, evs = calib
    convs = load("synthetic.jsonl", 140)[100:]
    items = [{"id": c["id"], "input": {"turns": c["turns"], "record": c["record"], "lang": c["lang"]},
              "expected": {"labels": c["labels"]}} for c in convs]
    agent = lambda inp: dict(inp)  # noqa: E731 - echo agent
    base = exps.run(agent, items, suite, cal, evs)
    gates = {g["check"]: g for g in base["summary"]["gates"]}
    # the data has planted false confirmations: the absolute gate on a critical check fails
    assert not gates["false_confirmation"]["pass"] and gates["false_confirmation"]["flagged"]
    assert "user_frustrated" not in gates  # no baseline: reported only
    assert 0.5 < base["summary"]["evaluator_accuracy"]["tool_loop"] <= 1
    # relative gate against a baseline with a lower rate fails, against itself passes
    lower = {"rates": {k: (v or 0) / 2 for k, v in base["summary"]["rates"].items()}}
    worse = exps.run(agent, items, suite, cal, evs, baseline=lower, tolerance=0.0)
    assert not {g["check"]: g for g in worse["summary"]["gates"]}["user_frustrated"]["pass"]
    same = exps.run(agent, items, suite, cal, evs, baseline=base["summary"])
    assert {g["check"]: g for g in same["summary"]["gates"]}["user_frustrated"]["pass"]
    assert exps.compare(base, same)["changed_items"] == []


def test_pytest_plugin(pytester, calib, pytestconfig):
    d, _, _, _ = calib
    ok, bad = None, None
    for c in load("synthetic.jsonl", 100):
        if not any(c["labels"].values()) and ok is None:
            ok = c
        if c["labels"]["false_confirmation"] and c["labels"]["fee_mismatch"] == 0 and bad is None:
            bad = c
    pytester.makeini(f"[pytest]\nee_suite = {d / 'suite.yaml'}\nee_calibration = {d / 'cal'}\n")
    pytester.makepyfile(f"""
import json
OK = json.loads({json.dumps(json.dumps(ok, ensure_ascii=False))})
BAD = json.loads({json.dumps(json.dumps(bad, ensure_ascii=False))})

def test_good_flow(ee):
    ee.check(OK)

def test_false_confirmation(ee):
    ee.check(BAD)

def test_expected_values(ee):
    assert ee.check(OK, expect={{"tool_loop": 0}})["tool_loop"] == 0
""")
    report = pytester.path / "run.json"
    # installed packages register the plugin through the pytest11 entry point; otherwise load it explicitly
    plugin_args = [] if pytestconfig.pluginmanager.has_plugin("elastic_evals") else ["-p", "elastic_evals.pytest_plugin"]
    res = pytester.runpytest(*plugin_args, f"--ee-report={report}")
    res.assert_outcomes(passed=2, failed=1)
    res.stdout.fnmatch_lines(["*critical check 'false_confirmation' flagged*", "*elastic-evals*", "*gate false_confirmation*FAIL*"])
    run = json.loads(report.read_text())
    assert run["summary"]["n"] == 3


def test_cli_experiment_exit_code(calib, tmp_path, monkeypatch):
    d, suite, cal, evs = calib
    from elastic_evals.engine import Engine
    pool = load("synthetic.jsonl", 100)
    clean = {r["conversation_id"] for r in Engine(suite, cal, evs).run(pool) if not any(r["decisions"].values())}
    items = tmp_path / "items.jsonl"
    convs = [c for c in pool if c["id"] in clean][:5]
    items.write_text("\n".join(json.dumps({"id": c["id"], "input": {"turns": c["turns"], "record": c["record"],
                                                                    "lang": c["lang"]}}) for c in convs))
    (tmp_path / "echo_agent.py").write_text("def agent(inp):\n    return dict(inp)\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    report = tmp_path / "run.json"
    assert cli(["experiment", "--suite", str(d / "suite.yaml"), "--calib", str(d / "cal"), "--items", str(items),
                "--agent", "echo_agent:agent", "--report", str(report)]) == 0
    assert json.loads(report.read_text())["summary"]["passed"]
    flagged = [c for c in pool if c["id"] not in clean][:3]
    items.write_text("\n".join(json.dumps({"id": c["id"], "input": {"turns": c["turns"], "record": c["record"],
                                                                     "lang": c["lang"]}}) for c in flagged))
    assert cli(["experiment", "--suite", str(d / "suite.yaml"), "--calib", str(d / "cal"), "--items", str(items),
                "--agent", "echo_agent:agent"]) in (0, 1)


def test_synth_spec_items():
    spec = yaml.safe_load((ROOT / "examples/banking_synth.yaml").read_text())
    rng = random.Random(0)
    items = [make_item(spec, rng, i) for i in range(300)]
    for it in items:
        if it["labels"]["false_confirmation"]:
            assert it["record"]["status"] != "confirmed"
        if it["record"]["operation"] != "transfer":
            assert it["labels"]["fee_mismatch"] == 0
    rate = sum(it["labels"]["user_frustrated"] for it in items) / len(items)
    assert 0.2 < rate < 0.4
    assert valid_turns([{"role": "user", "text": "hi"}, {"role": "assistant", "text": "hello"}])
    assert not valid_turns([{"role": "user", "text": "hi"}, {"role": "tool", "args": {}}])


def test_sentry_before_send_links_event_and_span():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.trace import StatusCode

    from elastic_evals.sentry import before_send, verify
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("t")
    hook = before_send(ui_url="https://ui.test/", tenant_id="acme")
    with tracer.start_as_current_span("invoke_agent", attributes={"gen_ai.conversation.id": "c-1",
                                                                  "gen_ai.agent.name": "bank"}) as span:
        trace_id = format(span.get_span_context().trace_id, "032x")
        event = hook({"event_id": "e1", "message": "boom"}, {})
    assert event["tags"] == {"trace_id": trace_id, "gen_ai.conversation.id": "c-1", "gen_ai.agent.name": "bank",
                             "ee.tenant_id": "acme"}
    assert event["contexts"]["elastic_evals"]["url"] == "https://ui.test/conversations/c-1"
    done = exporter.get_finished_spans()[0]
    assert done.attributes["ee.sentry.event_id"] == "e1" and done.status.status_code == StatusCode.ERROR
    assert "message" in event and "breadcrumbs" not in event  # nothing added beyond ids and links
    assert hook({"event_id": "e2"}, {}) == {"event_id": "e2"}  # no active span: untouched
    import hashlib
    import hmac
    sig = hmac.new(b"s", b"{}", hashlib.sha256).hexdigest()
    assert verify("s", b"{}", sig) and not verify("s", b"{}", "x") and not verify("s", b"{}", None)
