from elastic_evals.__main__ import _lang_cell
from elastic_evals.stats import MIN_N, wilson


def test_small_language_slices_are_marked():
    lo, hi = wilson(28, 30)
    assert hi - lo > 0.1  # 93% on 30 labels could be anywhere in about +-10 points
    assert _lang_cell({"accuracy": 28 / 30, "n": 30, "ci": [lo, hi], "low_n": 30 < MIN_N}).endswith("(n=30)")
    assert "*" in _lang_cell({"accuracy": 1.0, "n": 8, "ci": wilson(8, 8), "low_n": True})
    assert "*" not in _lang_cell({"accuracy": 0.9, "n": 120, "ci": wilson(108, 120), "low_n": False})
