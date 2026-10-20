import copy

import pytest

from elastic_evals.calibrate import calibrate
from elastic_evals.engine import Engine, drift_report, score_id
from elastic_evals.evaluators import build
from elastic_evals.suite import Suite

from .fakes import SUITE_YAML, labels_of, make_convs


@pytest.fixture(scope="module")
def suite():
    return Suite.from_yaml(SUITE_YAML)


@pytest.fixture(scope="module")
def calibrated(suite):
    convs = make_convs()
    cal, evs = calibrate(suite, convs, labels_of(convs))
    return cal, evs


def test_structural_check_is_settled_by_rules(calibrated):
    cal, _ = calibrated
    assert cal["routes"]["structural"] == ["rules"]
    assert cal["report"]["structural"]["chosen"]["accuracy"] == 1.0


def test_one_judge_per_suite_and_cheapest_set_wins(calibrated):
    cal, _ = calibrated
    hosted = cal["suite_choice"]["hosted"]
    assert sum(h.startswith("judge") for h in hosted) <= 1
    # the style check needs a judge to clear its floor; judge_slow is cheaper per call
    assert "judge_slow" in cal["routes"]["style"]
    assert cal["report"]["style"]["meets_floor"]
    assert cal["id"].startswith("cal_") and cal["status"] == "candidate"
    assert set(cal["evaluator_versions"]) >= {"rules", "clf", "judge_fast", "judge_slow"}


def test_latency_budget_changes_the_choice(suite):
    s = copy.deepcopy(suite)
    s.policy.max_ms_per_conv = 1000
    convs = make_convs()
    cal, _ = calibrate(s, convs, labels_of(convs))
    assert "judge_slow" not in cal["suite_choice"]["hosted"]
    assert cal["budget_met"]
    assert cal["suite_choice"]["ms_per_conv"] <= 1000


def test_tight_budget_is_kept_and_shortfall_reported(suite):
    s = copy.deepcopy(suite)
    s.policy.max_usd_per_1M_conv = 0.001  # only the free layers fit
    convs = make_convs()
    cal, _ = calibrate(s, convs, labels_of(convs))
    assert cal["budget_met"] and cal["suite_choice"]["hosted"] == []
    assert cal["suite_choice"]["below_floor"] == ["style"]
    assert not cal["report"]["style"]["meets_floor"]


def test_local_only_drops_hosted(suite):
    s = copy.deepcopy(suite)
    s.policy.allow_hosted = False
    convs = make_convs()
    cal, evs = calibrate(s, convs, labels_of(convs))
    assert all(not e.hosted for e in evs)
    assert cal["suite_choice"]["hosted"] == []
    assert cal["report"]["structural"]["meets_target"]


def test_language_slices_are_reported_and_enforced(suite):
    s = copy.deepcopy(suite)
    s.policy.languages = ["en", "es"]
    convs = make_convs()
    cal, _ = calibrate(s, convs, labels_of(convs))
    by_lang = cal["report"]["style"]["chosen"]["by_lang"]
    assert set(by_lang) == {"en", "es"}
    assert set(cal["report"]["style"]["lang_floors"]) == {"en", "es"}


def test_partial_labels(suite):
    convs = make_convs()
    labels = labels_of(convs)
    for c in convs[::3]:
        labels.pop((c["id"], "style"))
    cal, _ = calibrate(suite, convs, labels)
    assert cal["report"]["style"]["chosen"]["n"] == len(convs) - len(convs[::3])


def test_runtime_records_and_escalation(suite, calibrated):
    cal, evs = calibrated
    test = make_convs(200, seed=1)
    results = Engine(suite, cal, evs).run(test)
    gold = labels_of(test)
    for r in results:
        decided = [s for s in r["scores"] if s["decision"]]
        assert {s["check"] for s in decided} == {"structural", "style"}
        for s in r["scores"]:
            assert len(s["id"]) == 64 and s["calibration_id"] == cal["id"]
            assert s["evaluator"]["version"] and s["evaluator"]["criteria_version"] == "1"
    acc = sum(r["decisions"]["style"] == gold[(r["conversation_id"], "style")] for r in results) / len(results)
    assert acc >= 0.93
    # the rule settles about half of the style items, so the judge is not called on all conversations
    judged = sum(any(e.startswith("judge") for e in r["evaluators_called"]) for r in results)
    assert judged < len(results)
    # shadow scores: the judge answered 'structural' too, though rules had decided it
    assert any(s["sample_reason"] == "shadow" for r in results for s in r["scores"])
    # audit at 50%: roughly half the conversations
    audited = sum(any(s["sample_reason"] == "random_audit" for s in r["scores"]) for r in results)
    assert 60 < audited < 140
    drift = drift_report(results)
    assert drift["structural"]["agreement"] > 0.9


def test_score_ids_are_deterministic(suite, calibrated):
    cal, evs = calibrated
    test = make_convs(20, seed=2)
    a = Engine(suite, cal, evs).run(test)
    b = Engine(suite, cal, evs).run(test)
    assert [s["id"] for r in a for s in r["scores"]] == [s["id"] for r in b for s in r["scores"]]
    assert score_id("c", "v", "x", "q", "1", "route") != score_id("c", "v", "x", "q", "2", "route")


MC_YAML = """
name: routing
judge_preamble: 'Classify. JSON keys: '
questions:
  intent:
    ask: What does the user want?
    view: user
    rule: tests.test_engine:intent_rule
    classes: {balance: check balance, transfer: send money, card: card problem}
evaluators:
  - {id: rules, type: rules}
  - {id: judge, type: fake_judge, usd: 0.0001, ms: 800}
policy: {target_accuracy: 0.95}
"""


def intent_rule(conv):
    return conv["fake"]["rules"]["intent"]


def test_multiclass_checks():
    import random
    rng = random.Random(3)
    s = Suite.from_yaml(MC_YAML)
    classes = ["balance", "transfer", "card"]
    convs = []
    for i in range(240):
        y = rng.choice(classes)
        # the rule is sure and right on 'balance' (keyword), unsure otherwise
        rule = {c: float(c == y) for c in classes} if y == "balance" else {c: 1 / 3 for c in classes}
        judge_y = y if rng.random() < 0.97 else rng.choice(classes)
        convs.append({"id": f"m{i}", "lang": "en", "turns": [], "labels": {"intent": y},
                      "fake": {"rules": {"intent": rule}, "judge": {"intent": {c: float(c == judge_y) for c in classes}}}})
    cal, evs = calibrate(s, convs, labels_of(convs))
    assert cal["routes"]["intent"] == ["rules", "judge"]
    res = Engine(s, cal, evs).run(convs[:50])
    for r in res:
        sc = next(x for x in r["scores"] if x["decision"])
        assert sc["value"] is None and sc["label"] in classes and set(sc["probs"]) == set(classes)
    acc = sum(r["decisions"]["intent"] == c["labels"]["intent"] for r, c in zip(res, convs[:50])) / 50
    assert acc > 0.9


def test_unknown_policy_field_rejected():
    with pytest.raises(ValueError):
        Suite.from_yaml(SUITE_YAML.replace("audit_rate: 0.5", "audit_rat: 0.5"))


def test_build_respects_local_only(suite):
    s = copy.deepcopy(suite)
    s.policy.allow_hosted = False
    assert [e.id for e in build(s, None)] == ["rules"]
