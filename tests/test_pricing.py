import datetime as dt
import json

import pytest

from elastic_evals.pricing import PriceBook

from .conftest import ROOT

AT = dt.datetime(2026, 10, 20, tzinfo=dt.timezone.utc)
BOOK = PriceBook([
    {"model_pattern": "^gpt-x", "start_date": "2026-01-01", "tiers": [{"above_input_tokens": 0, "input": 1.0, "output": 4.0, "cache_read": 0.1}]},
    {"model_pattern": "^gpt-x", "start_date": "2020-01-01", "tiers": [{"above_input_tokens": 0, "input": 9, "output": 9}]},
    {"model_pattern": "^big", "tiers": [{"above_input_tokens": 0, "input": 1, "output": 1},
                                         {"above_input_tokens": 200000, "input": 2, "output": 2}]},
])


def test_same_rules_as_the_collector():
    # the case the Go exporter test asserts: (600*1 + 400*0.1 + 100*4) / 1e6 with the newest price
    usd, pid = BOOK.cost("gpt-x-2026", {"input_tokens": 1000, "cache_read_tokens": 400, "output_tokens": 100}, AT)
    assert usd == pytest.approx(0.00104) and pid.endswith("2026-01-01")
    assert BOOK.cost("gpt-x", {"input_tokens": 10}, dt.datetime(2021, 1, 1, tzinfo=dt.timezone.utc))[0] == pytest.approx(9e-5)
    assert BOOK.cost("big", {"input_tokens": 300000}, AT)[0] == pytest.approx(0.6)
    assert BOOK.cost("unknown", {"input_tokens": 1}, AT) == (None, None)


def test_deploy_prices_file_parses():
    book = PriceBook(json.loads((ROOT / "deploy/collector/prices.json").read_text()))
    assert book.cost("anthropic/claude-haiku-5.5", {"input_tokens": 1_000_000}, AT)[0] == pytest.approx(1.0)
