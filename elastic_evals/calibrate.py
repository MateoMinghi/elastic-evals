"""Calibration: learn thresholds and choose each question's route from labelled conversations.

For every (question, evaluator) the threshold is the widest confidence band whose decisions
reach policy.target_accuracy on the labelled set. Local heads are trained here and scored
out-of-fold, so their thresholds are never tuned on their own training data.

Route choice: every chain of evaluators in cost order (rules -> local -> jev -> one LLM judge)
is simulated on the labelled set. Among chains within policy.tolerance of the best accuracy
(exactly the best, for critical questions), the cheapest one wins, then the fastest.
"""

import asyncio
import itertools
from collections import defaultdict

import numpy as np

from .evaluators import LocalHeadEvaluator, build
from .providers import OpenRouter
from .suite import Suite


def decide(p):
    return None if p is None else int(p >= 0.5)


def confident(p, th):
    if p is None or th is None:
        return False
    lo, hi = th
    return p <= lo or p >= hi


def tune_threshold(ps, ys, target):
    """Largest-coverage (lo, hi) band whose confident decisions reach `target` accuracy."""
    best, best_key = None, (-1.0, -1.0)
    for lo in np.arange(0, 0.51, 0.05):
        for hi in np.arange(0.5, 1.0001, 0.05):
            th = (round(float(lo), 2), round(float(hi), 2))
            conf = [(p, y) for p, y in zip(ps, ys) if confident(p, th)]
            if not conf:
                continue
            acc = float(np.mean([decide(p) == y for p, y in conf]))
            key = (len(conf) / len(ps), acc)
            if acc >= target and key > best_key:
                best, best_key = th, key
    return best


async def collect(evaluators, convs, questions, labels=None):
    """Run every evaluator on every conversation. Returns preds[e][(cid,q)], cost[e][cid], latency[e][cid]."""
    preds, cost, lat = defaultdict(dict), defaultdict(dict), defaultdict(dict)
    for ev in evaluators:
        if ev.type == "rules":
            for c in convs:
                out, cst, lt = ev.evaluate(c, questions)
                preds[ev.id].update({(c["id"], q): p for q, p in out.items()})
                cost[ev.id][c["id"]], lat[ev.id][c["id"]] = cst, lt
        elif isinstance(ev, LocalHeadEvaluator):
            if labels is not None:
                preds[ev.id].update(ev.fit(convs, labels, questions))   # out-of-fold
                _, per_item = ev.evaluate_batch(convs, questions)        # timing only
            else:
                out, per_item = ev.evaluate_batch(convs, questions)
                for cid, ps in out.items():
                    preds[ev.id].update({(cid, q): p for q, p in ps.items()})
            for c in convs:
                cost[ev.id][c["id"]], lat[ev.id][c["id"]] = 0.0, per_item
        else:
            results = await asyncio.gather(*(ev.evaluate(c, questions) for c in convs))
            for c, (out, cst, lt) in zip(convs, results):
                preds[ev.id].update({(c["id"], q): p for q, p in out.items()})
                cost[ev.id][c["id"]], lat[ev.id][c["id"]] = cst, lt
    return preds, cost, lat


def chains(evaluators, question):
    allowed = [e for e in evaluators if question.layers is None or e.id in question.layers]
    cheap = [e for e in allowed if e.type != "llm"]
    llms = [e for e in allowed if e.type == "llm"]
    for r in range(len(cheap) + 1):
        for subset in itertools.combinations(cheap, r):
            for tail in [None] + llms:
                chain = list(subset) + ([tail] if tail else [])
                if chain:
                    yield chain


def simulate(chain, q, convs, labels, preds, cost, lat, thresholds):
    correct, spend, latency, decided_by = [], [], [], defaultdict(int)
    for c in convs:
        cid, cs, lt = c["id"], 0.0, 0.0
        for k, ev in enumerate(chain):
            p = preds[ev.id].get((cid, q))
            cs += cost[ev.id].get(cid, 0.0)
            lt += lat[ev.id].get(cid, 0.0)
            if k == len(chain) - 1 or confident(p, thresholds[q][ev.id]):
                correct.append((decide(p) if p is not None else 0) == labels[(cid, q)])
                decided_by[ev.id] += 1
                break
        spend.append(cs)
        latency.append(lt)
    n = len(convs)
    return {"chain": [e.id for e in chain], "accuracy": float(np.mean(correct)),
            "usd_per_conv": float(np.mean(spend)), "ms_per_conv": 1000 * float(np.mean(latency)),
            "decided_by": {k: v / n for k, v in decided_by.items()}}


async def calibrate_async(suite: Suite, convs, labels, client: OpenRouter | None):
    evaluators = build(suite, client)
    questions = suite.questions
    preds, cost, lat = await collect(evaluators, convs, questions, labels)

    thresholds = {q.name: {} for q in questions}
    for q in questions:
        for ev in evaluators:
            pairs = [(preds[ev.id].get((c["id"], q.name)), labels[(c["id"], q.name)]) for c in convs]
            pairs = [(p, y) for p, y in pairs if p is not None]
            thresholds[q.name][ev.id] = tune_threshold(*zip(*pairs), suite.policy.target_accuracy) if pairs else None

    routes, report = {}, {}
    for q in questions:
        usable = [e for e in evaluators if any((c["id"], q.name) in preds[e.id] for c in convs)]
        rows = [simulate(ch, q.name, convs, labels, preds, cost, lat, thresholds) for ch in chains(usable, q)]
        best = max(r["accuracy"] for r in rows)
        floor = best if q.critical else best - suite.policy.tolerance
        eligible = [r for r in rows if r["accuracy"] >= floor - 1e-9]
        choice = min(eligible, key=lambda r: (round(r["usd_per_conv"], 9), r["ms_per_conv"], len(r["chain"])))
        routes[q.name] = choice["chain"]
        report[q.name] = {"chosen": choice, "best_accuracy": best,
                          "meets_target": choice["accuracy"] >= suite.policy.target_accuracy,
                          "candidates": sorted(rows, key=lambda r: -r["accuracy"])}

    return {
        "suite": suite.name,
        "policy": vars(suite.policy),
        "n_labelled": len(convs),
        "routes": routes,
        "thresholds": thresholds,
        "report": report,
    }, evaluators


def calibrate(suite: Suite, convs, labels, client: OpenRouter | None = None):
    """Returns (calibration dict, evaluators with trained local heads)."""
    return asyncio.run(calibrate_async(suite, convs, labels, client))
