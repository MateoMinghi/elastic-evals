"""Harness optimisation (evolve.py): no services, a rules-only suite, a fake agent and a scripted proposer."""

import json

import pytest
import yaml

from elastic_evals import evolve
from elastic_evals.__main__ import main as cli
from elastic_evals.bundle import save
from elastic_evals.calibrate import calibrate
from elastic_evals.suite import Suite

from .conftest import ROOT, load

SUITE = yaml.safe_load((ROOT / "examples/banking_suite.yaml").read_text())
SUITE["evaluators"] = [{"id": "rules", "type": "rules"}]
SUITE["policy"] = {"allow_hosted": False, "target_accuracy": 0.95}


def build(harness_dir):
    """An agent that replays a recorded conversation, shaped by the harness's policy.yaml."""
    policy = yaml.safe_load((harness_dir / "policy.yaml").read_text())

    def agent(inp):
        turns = [dict(t) for t in inp["turns"]]
        if policy.get("disclose"):
            first = next(t for t in turns if t["role"] == "assistant")
            first["text"] = ("Soy un asistente virtual. " if inp.get("lang") == "es" else "I am a virtual assistant. ") + first["text"]
        if policy.get("dedupe_tools"):
            out = []
            for t in turns:
                if not (t["role"] == "tool" and out and out[-1]["role"] == "tool"
                        and (out[-1]["name"], out[-1].get("args")) == (t["name"], t.get("args"))):
                    out.append(t)
            turns = out
        if policy.get("retry_everything"):
            first = next((i for i, t in enumerate(turns) if t["role"] == "tool"), None)
            if first is not None:
                turns[first + 1:first + 1] = [dict(turns[first]), dict(turns[first])]
        return {"turns": turns, "record": inp["record"], "lang": inp["lang"]}
    return agent


class Scripted:
    """Proposes a fixed sequence of edits and keeps the requests it received."""

    def __init__(self):
        self.requests = []
        self.script = [
            {"rationale": "escape the harness", "files": {"../escape.yaml": "x", "agent.py": "import os", "/etc/x.md": "x"}},
            {"rationale": "retry tools more", "files": {"policy.yaml": "retry_everything: true\n"}},
            {"rationale": "the agent never says it is an AI", "files": {"policy.yaml": "disclose: true\n"}},
            {"rationale": "repeated identical tool calls", "files": {"policy.yaml": "disclose: true\ndedupe_tools: true\n"}},
        ]

    async def __call__(self, request):
        self.requests.append(request)
        return self.script.pop(0) if self.script else {"rationale": "nothing left", "files": {}}


@pytest.fixture(scope="module")
def setup(tmp_path_factory):
    d = tmp_path_factory.mktemp("evolve")
    (d / "suite.yaml").write_text(yaml.safe_dump(SUITE, allow_unicode=True))
    suite = Suite.load(d / "suite.yaml")
    train = load("synthetic.jsonl", 100)
    cal, evs = calibrate(suite, train, {(c["id"], q): v for c in train for q, v in c["labels"].items()})
    save(d / "cal", cal, evs)
    items = [{"id": c["id"], "input": {"turns": c["turns"], "record": c["record"], "lang": c["lang"]}}
             for c in load("synthetic.jsonl", 260)[100:]]
    seed = d / "seed"
    seed.mkdir()
    (seed / "policy.yaml").write_text("disclose: false\n")
    (seed / "agent.py").write_text("# kernel: not part of the mutable surface\n")
    return d, suite, cal, evs, items, seed


def test_split_is_deterministic_and_stable():
    items = [{"id": f"i{i}"} for i in range(400)]
    a = evolve.split(items)
    b = evolve.split(items[:200])
    assert sum(map(len, a)) == 400 and all(a)
    for part_a, part_b in zip(a, b):
        assert {i["id"] for i in part_b} <= {i["id"] for i in part_a}


def test_paired_and_regressions(setup):
    _, suite, *_ = setup
    assert evolve.paired({"a": 0.5, "b": 0.5}, {"a": 1.0, "b": 1.0}) == (0.5, 0.0)
    parent = {"rates": {"tool_loop": 0.10, "false_confirmation": 0.10}, "failed_assertions": 0}
    child = {"rates": {"tool_loop": 0.11, "false_confirmation": 0.11}, "failed_assertions": 1}
    # a non-critical check may move within tolerance; a critical one may not rise at all
    assert evolve.regressions(parent, child, suite, 0.02) == ["false_confirmation", "assertions"]


def test_evolve_loop(setup, tmp_path):
    d, suite, cal, evs, items, seed = setup
    proposer = Scripted()
    rep = evolve.evolve(build, seed, items, suite, cal, evs, proposer, tmp_path / "evo", iterations=4,
                        minibatch=100, log=lambda _: None)
    archive = [json.loads(line) for line in (tmp_path / "evo/archive.jsonl").read_text().splitlines()]
    by_id = {e["id"]: e for e in archive}

    # writes outside the mutable surface are refused and nothing escapes the candidate directory
    assert sorted(by_id["c001"]["refused"]) == ["../escape.yaml", "/etc/x.md", "agent.py"]
    assert not by_id["c001"]["accepted"] and not (tmp_path / "evo/candidates/escape.yaml").exists()
    assert (tmp_path / "evo/candidates/c001/agent.py").read_text() == (seed / "agent.py").read_text()
    # a change that makes the agent loop on tools is rejected before or at validation
    assert not by_id["c002"]["accepted"] and "rejected" in by_id["c002"]["outcome"]
    # the useful changes are accepted; the seed, now dominated, is no longer picked as a parent
    assert by_id["c003"]["accepted"] and by_id["c004"]["accepted"]
    assert rep["best"] == "c004" and rep["lineage"] == ["c000", "c003", "c004"]
    assert rep["val"]["best"] > rep["val"]["seed"]
    assert rep["test"]["best"]["mean"] > rep["test"]["seed"]["mean"]
    assert rep["test"]["best"]["rates"]["disclosure_missing"] < rep["test"]["seed"]["rates"]["disclosure_missing"]

    # the proposer learns from train items only, and sees each check's spec but not its rule or route
    val_test = {it["id"] for part in evolve.split(items)[1:] for it in part}
    seen = {f["item_id"] for r in proposer.requests for f in r["failures"]}
    assert seen and not seen & val_test
    assert set(proposer.requests[0]["files"]) == {"policy.yaml"}
    assert "rule" not in json.dumps(proposer.requests[0]["checks"])
    assert proposer.requests[-1]["history"][0]["outcome"].startswith("rejected: no change")


def test_cli_evolve(setup, tmp_path, monkeypatch):
    d, suite, cal, evs, items, seed = setup
    (tmp_path / "evo_fixture.py").write_text(
        "from tests.test_evolve import build, Scripted\n_p = Scripted()\n\n\ndef propose(request):\n    return _p.script.pop(0)\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    (tmp_path / "items.jsonl").write_text("\n".join(json.dumps(it) for it in items))
    assert cli(["evolve", "--suite", str(d / "suite.yaml"), "--calib", str(d / "cal"), "--items", str(tmp_path / "items.jsonl"),
                "--harness", str(seed), "--agent", "evo_fixture:build", "--proposer", "evo_fixture:propose",
                "--out", str(tmp_path / "evo"), "--iterations", "1", "--minibatch", "20"]) == 0
    assert json.loads((tmp_path / "evo/report.json").read_text())["attempts"] == 1
    assert cli(["evolve", "--suite", str(d / "suite.yaml"), "--calib", str(d / "cal"), "--items", str(tmp_path / "items.jsonl"),
                "--harness", str(seed), "--agent", "evo_fixture:build", "--out", str(tmp_path / "evo2")]) == 2
