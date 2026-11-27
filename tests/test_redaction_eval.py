"""Per-language measurement of the redaction analyzer, with a scripted analyzer (no Presidio needed)."""

from elastic_evals.redaction_eval import match, measure, verdict
from elastic_evals.stats import MIN_N


def _gold(*spans):
    return [{"type": t, "start": a, "end": b} for t, a, b in spans]


def test_match_requires_same_type_and_overlap():
    gold = _gold(("PERSON", 0, 10))
    assert match(gold, [{"type": "PERSON", "start": 1, "end": 10}])[0] == [(0, 0)]
    pairs, extra, missed = match(gold, [{"type": "LOCATION", "start": 0, "end": 10}])
    assert pairs == [] and len(extra) == 1 and len(missed) == 1
    pairs, _, _ = match(gold, [{"type": "PERSON", "start": 8, "end": 30}])  # IoU too low
    assert pairs == []


def test_a_one_to_one_match_does_not_credit_one_gold_twice():
    gold = _gold(("PERSON", 0, 10))
    pred = [{"type": "PERSON", "start": 0, "end": 10, "score": 0.9}, {"type": "PERSON", "start": 0, "end": 9, "score": 0.5}]
    pairs, extra, _ = match(gold, pred)
    assert len(pairs) == 1 and len(extra) == 1


def _corpus(n_es=60, n_en=60):
    rows = []
    for i in range(n_es):
        rows.append({"text": f"es {i}", "lang": "es", "entities": _gold(("PERSON", 0, 5))})
    for i in range(n_en):
        rows.append({"text": f"en {i}", "lang": "en", "entities": _gold(("PERSON", 0, 5))})
    return rows


def test_spanish_names_missed_are_reported_per_language():
    def analyze(text, lang):
        found = lang == "en" or int(text.split()[1]) % 10 < 4  # Spanish: finds 40% of names
        return [{"type": "PERSON", "start": 0, "end": 5, "score": 0.9}] if found else []
    rep = measure(_corpus(), analyze)
    es, en = rep["languages"]["es"]["PERSON"], rep["languages"]["en"]["PERSON"]
    assert en["recall"] == 1.0 and es["recall"] == 0.4 and es["reliable"] and en["reliable"]
    assert es["precision"] == 1.0 and es["recall_ci"][1] < 0.55
    bad = verdict(rep, min_precision=0.8, min_recall=0.8)
    assert [(b["lang"], b["entity"], b["metric"]) for b in bad if b["entity"] == "PERSON"] == [("es", "PERSON", "recall")]


def test_small_samples_are_marked_unreliable_and_never_fail():
    rep = measure(_corpus(n_es=5, n_en=60), lambda t, lang: [])  # finds nothing at all
    assert not rep["languages"]["es"]["PERSON"]["reliable"] and 5 < MIN_N
    assert all(b["lang"] != "es" for b in verdict(rep, 0.8, 0.8))
    assert any(b["lang"] == "en" for b in verdict(rep, 0.8, 0.8))


def test_false_positives_lower_precision_and_analyzer_errors_are_counted():
    def analyze(text, lang):
        if text.endswith("0"):
            raise RuntimeError("down")
        return [{"type": "PERSON", "start": 0, "end": 5}, {"type": "PERSON", "start": 20, "end": 25}]
    rep = measure(_corpus(n_es=40, n_en=0), analyze)
    c = rep["languages"]["es"]["PERSON"]
    assert rep["analyzer_errors"] == 4 and c["precision"] == 0.5 and c["recall"] == 0.9
