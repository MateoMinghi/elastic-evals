"""Roadmap gates as commands (DESIGN.md §12).

Phase 1: "engine within 1 SE of the best judge per check on real labels, or a documented reason
per check". `phase1()` cross-validates the whole calibration: for each of K folds it calibrates on
the other folds, scores the held-out fold with the engine and with every single evaluator, then
compares per check the engine's held-out accuracy with the best single evaluator's.

    python -m elastic_evals gate --suite suite.yaml --data human_labelled.jsonl --folds 5 [--reasons reasons.yaml]

`reasons.yaml` maps a check to the documented reason it may fail the gate.
"""

import asyncio
import math
import random

from .calibrate import calibrate_async
from .engine import Engine


def _se(p, n):
    p = (p * n + 1) / (n + 2)
    return math.sqrt(p * (1 - p) / max(n, 1))


async def phase1_async(suite, convs, client=None, folds=5, seed=0, reasons=None):
    reasons = reasons or {}
    order = list(range(len(convs)))
    random.Random(seed).shuffle(order)
    fold_of = {convs[i]["id"]: k % folds for k, i in enumerate(order)}
    qs = [q for q in suite.questions if q.scope != "observation"]
    correct = {"engine": {q.name: [] for q in qs}}
    cost, ms, llm = [], [], []
    for k in range(folds):
        train = [c for c in convs if fold_of[c["id"]] != k]
        test = [c for c in convs if fold_of[c["id"]] == k]
        labels = {(c["id"], q): v for c in train for q, v in c["labels"].items() if "@" not in q}
        cal, evs = await calibrate_async(suite, train, labels, client)
        results = await Engine(suite, cal, evs, audit=False).run_async(test)
        rank = {e.id: e.rank for e in evs}
        for c, r in zip(test, results):
            for q in qs:
                if q.name in c["labels"]:
                    correct["engine"][q.name].append(r["decisions"].get(q.name) == c["labels"][q.name])
            cost.append(r["usd"])
            ms.append(r["ms"])
            llm.append(any(rank.get(e) == 3 for e in r["evaluators_called"]))
        for ev in evs:  # every single evaluator alone, on the same held-out fold
            fixed = dict(cal, routes={q.name: [ev.id] for q in qs}, observation=None)
            single = await Engine(suite, fixed, evs, audit=False).run_async(test)
            for c, r in zip(test, single):
                for q in qs:
                    if q.name in c["labels"] and (ev.rank > 0 or q.rule):
                        correct.setdefault(ev.id, {}).setdefault(q.name, []).append(
                            r["decisions"].get(q.name) == c["labels"][q.name])
    report, passed = {}, True
    for q in qs:
        eng = correct["engine"][q.name]
        acc = sum(eng) / len(eng) if eng else 0.0
        singles = {e: sum(v[q.name]) / len(v[q.name]) for e, v in correct.items()
                   if e != "engine" and q.name in v and v[q.name]}
        best_id = max(singles, key=singles.get)
        best = singles[best_id]
        se = _se(best, len(eng))
        ok = acc >= best - se - 1e-9
        documented = q.name in reasons
        report[q.name] = {"engine": acc, "best_single": best_id, "best_single_accuracy": best, "se": se, "n": len(eng),
                          "pass": ok, "reason": reasons.get(q.name), "singles": singles}
        passed &= ok or documented
    n = max(1, len(cost))
    return {"passed": passed, "folds": folds, "n": len(convs), "checks": report,
            "engine_usd_per_1M": 1e6 * sum(cost) / n, "engine_ms": sum(ms) / n, "engine_llm_share": sum(llm) / n}


def phase1(suite, convs, client=None, folds=5, seed=0, reasons=None):
    return asyncio.run(phase1_async(suite, convs, client, folds, seed, reasons))
