"""pytest plugin: score your agent's conversations in tests, with CI gates (DESIGN.md §6.10).

    # pytest.ini
    [pytest]
    ee_suite = examples/banking_suite.yaml
    ee_calibration = calib/

    def test_transfer_flow(ee):
        conv = run_my_agent("send 100 to Ana")          # {"turns": [...], "record": {...}}
        ee.check(conv, expect={"fee_mismatch": 0})      # fails on any flagged critical check

At the end of the session the plugin prints each check's flagged rate and the gate results;
`--ee-baseline run.json` gates the non-critical checks against a previous run and `--ee-report
run.json` writes this run (use it as the next baseline).
"""

import json
import uuid
from pathlib import Path

import pytest


def pytest_addoption(parser):
    g = parser.getgroup("elastic-evals")
    g.addoption("--ee-suite", help="suite YAML (or ini ee_suite)")
    g.addoption("--ee-calibration", help="calibration directory (or ini ee_calibration)")
    g.addoption("--ee-baseline", help="previous run report to gate against")
    g.addoption("--ee-report", help="write this run's report here")
    g.addoption("--ee-tolerance", type=float, default=0.02)
    parser.addini("ee_suite", "elastic-evals suite YAML")
    parser.addini("ee_calibration", "elastic-evals calibration directory")


class Session:
    def __init__(self, config):
        self.config = config
        self.items = []
        self.bundle = None
        self.run_id = f"pytest_{uuid.uuid4().hex[:8]}"

    def opt(self, name):
        return self.config.getoption(f"--ee-{name}") or self.config.getini(f"ee_{name}")

    def engine(self):
        if self.bundle is None:
            from .bundle import load
            suite, calib = self.opt("suite"), self.opt("calibration")
            if not suite or not calib:
                pytest.skip("elastic-evals: set ee_suite and ee_calibration")
            root = Path(str(self.config.rootpath))
            self.bundle = load(root / suite, root / calib)
        return self.bundle


class EE:
    """The `ee` fixture: score conversations and assert on the result."""

    def __init__(self, session: Session, nodeid: str):
        self.session, self.nodeid = session, nodeid

    def score(self, conv: dict) -> dict:
        from .engine import Engine
        suite, cal, evaluators = self.session.engine()
        conv = dict(conv)
        conv.setdefault("id", f"{self.session.run_id}:{self.nodeid}:{len(self.session.items)}")
        conv.setdefault("lang", "und")
        conv.setdefault("record", {})
        conv["experiment"] = {"run_id": self.session.run_id, "item_id": self.nodeid}
        res = Engine(suite, cal, evaluators, audit=False).run([conv])[0]
        return res

    def check(self, conv: dict, expect: dict | None = None, assertions_expected: dict | None = None) -> dict:
        from .experiments import assertions
        suite, _, _ = self.session.engine()
        res = self.score(conv)
        checks = assertions(conv, assertions_expected or {})
        self.session.items.append({"item_id": self.nodeid, "decisions": res["decisions"], "assertions": checks,
                                   "expected": {"labels": expect or {}}, "usd": res["usd"]})
        problems = [f"critical check {q.name!r} flagged" for q in suite.questions
                    if q.critical and res["decisions"].get(q.name) == 1 and (expect or {}).get(q.name) != 1]
        problems += [f"{k}: expected {v!r}, got {res['decisions'].get(k)!r}" for k, v in (expect or {}).items()
                     if res["decisions"].get(k) != v]
        problems += [f"assertion {a['type']} failed" for a in checks if not a["pass"]]
        if problems:
            pytest.fail("elastic-evals: " + "; ".join(problems), pytrace=False)
        return res["decisions"]


def pytest_configure(config):
    config._ee_session = Session(config)


@pytest.fixture
def ee(request):
    return EE(request.config._ee_session, request.node.nodeid)


def pytest_terminal_summary(terminalreporter, config):
    s = getattr(config, "_ee_session", None)
    if not s or not s.items or s.bundle is None:
        return
    from .experiments import summarize
    suite = s.bundle[0]
    baseline = None
    if s.opt("baseline") and Path(s.opt("baseline")).exists():
        baseline = json.loads(Path(s.opt("baseline")).read_text())["summary"]
    summary = summarize(s.items, suite, baseline, config.getoption("--ee-tolerance"))
    tr = terminalreporter
    tr.section("elastic-evals")
    for check, rate in summary["rates"].items():
        if rate is not None:
            tr.write_line(f"{check:24s} flagged {100 * rate:5.1f}% of {summary['n']}")
    for g in summary["gates"]:
        tr.write_line(f"gate {g['check']:20s} {g['kind']:10s} {'PASS' if g['pass'] else 'FAIL'}")
    if s.opt("report"):
        Path(s.opt("report")).write_text(json.dumps({"run_id": s.run_id, "summary": summary, "items": s.items},
                                                   indent=1, default=str))
