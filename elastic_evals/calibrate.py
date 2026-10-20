"""Calibration: learn thresholds and choose each question's route from labelled conversations.

For every (question, evaluator) the threshold is the widest confidence band whose decisions
reach policy.target_accuracy on the labelled set. Local heads are trained here and scored
out-of-fold, so their thresholds are never tuned on their own training data.

Route choice is joint, because one hosted call answers every question of a conversation:
  1. For each question, the best achievable accuracy over all chains (rules -> local -> jev ->
     one LLM judge) sets a floor: best minus max(policy.tolerance, one standard error);
     critical questions use one standard error only, so noise-level gaps don't pick the route.
  2. Each candidate set of hosted evaluators (Jev or not x at most one LLM judge) is tried:
     every question takes its cheapest chain inside that set that clears its floor, and the
     whole suite is simulated per conversation to get the real cost of shared calls.
  3. The set with the fewest questions below their floor wins, then the cheapest, then the fastest.
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


def walk(chain, q, cid, preds, thresholds):
    """Evaluators a question visits on one conversation, and the decision it ends with."""
    for k, ev in enumerate(chain):
        p = preds[ev.id].get((cid, q))
        if k == len(chain) - 1 or confident(p, thresholds[q][ev.id]):
            return chain[: k + 1], (decide(p) if p is not None else 0)
    return chain, 0


def simulate_suite(routes, convs, labels, preds, cost, lat, thresholds):
    """Per-conversation simulation: every evaluator visited by any question is paid for once."""
    acc, spend, latency = defaultdict(list), [], []
    for c in convs:
        cid, visited = c["id"], {}
        for q, chain in routes.items():
            path, d = walk(chain, q, cid, preds, thresholds)
            acc[q].append(d == labels[(cid, q)])
            visited.update({e.id: e for e in path})
        spend.append(sum(cost[e].get(cid, 0.0) for e in visited))
        latency.append(sum(lat[e].get(cid, 0.0) for e in visited))
    return ({q: float(np.mean(v)) for q, v in acc.items()}, float(np.mean(spend)), 1000 * float(np.mean(latency)))


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

    # thresholds per strictness level: a chain may require its cheap layers to be more accurate
    # than the policy target when the question's floor demands it
    levels = sorted({suite.policy.target_accuracy, 0.97, 0.99, 1.0} - {x for x in (0.97, 0.99) if x < suite.policy.target_accuracy})
    by_level = {lv: {q.name: {} for q in questions} for lv in levels}
    for q in questions:
        for ev in evaluators:
            pairs = [(preds[ev.id].get((c["id"], q.name)), labels[(c["id"], q.name)]) for c in convs]
            pairs = [(p, y) for p, y in pairs if p is not None]
            for lv in levels:
                by_level[lv][q.name][ev.id] = tune_threshold(*zip(*pairs), lv) if pairs else None

    # 1. every chain per question, and each question's floor
    n = len(convs)
    rows, floors = {}, {}
    for q in questions:
        usable = [e for e in evaluators if any((c["id"], q.name) in preds[e.id] for c in convs)]
        rows[q.name] = []
        for ch in chains(usable, q):
            for lv in (levels if len(ch) > 1 else levels[:1]):
                r = simulate(ch, q.name, convs, labels, preds, cost, lat, by_level[lv])
                rows[q.name].append((ch, dict(r, strictness=lv)))
        best = max(r["accuracy"] for _, r in rows[q.name])
        smoothed = (best * n + 1) / (n + 2)  # avoids a zero standard error at 0% or 100%
        se = float(np.sqrt(smoothed * (1 - smoothed) / n))
        floors[q.name] = (best, best - (se if q.critical else max(suite.policy.tolerance, se)))

    # 2. candidate hosted sets: Jev or not x at most one LLM judge
    jevs = [None] + [e for e in evaluators if e.type == "jev"]
    llms = [None] + [e for e in evaluators if e.type == "llm"]
    configs = []
    for jev, llm in itertools.product(jevs, llms):
        hosted = {e.id for e in (jev, llm) if e is not None}
        routes, short, levels_used = {}, 0, {}
        for q in questions:
            inside = [(ch, r) for ch, r in rows[q.name] if {e.id for e in ch if e.hosted} <= hosted]
            if not inside:
                short += 1
                continue
            ok = [(ch, r) for ch, r in inside if r["accuracy"] >= floors[q.name][1] - 1e-9]
            if not ok:
                short += 1
                ok = [max(inside, key=lambda x: x[1]["accuracy"])]
            ch, r = min(ok, key=lambda x: (x[1]["usd_per_conv"], x[1]["ms_per_conv"], len(x[0]), -x[1]["strictness"]))
            routes[q.name], levels_used[q.name] = ch, r["strictness"]
        if len(routes) < len(questions):
            continue
        th = {q: by_level[levels_used[q]][q] for q in routes}
        acc, usd, ms = simulate_suite(routes, convs, labels, preds, cost, lat, th)
        configs.append({"hosted": sorted(hosted), "short": short, "usd_per_conv": usd, "ms_per_conv": ms,
                        "accuracy": acc, "strictness": dict(levels_used), "thresholds": th,
                        "routes": {q: [e.id for e in ch] for q, ch in routes.items()}})
    choice = min(configs, key=lambda c: (c["short"], round(c["usd_per_conv"], 9), c["ms_per_conv"]))

    # 3. report
    routes, thresholds = choice["routes"], choice["thresholds"]
    report = {}
    for q in questions:
        chosen = next(r for ch, r in rows[q.name]
                      if [e.id for e in ch] == routes[q.name] and r["strictness"] == choice["strictness"][q.name])
        report[q.name] = {"chosen": dict(chosen, accuracy=choice["accuracy"][q.name]),
                          "best_accuracy": floors[q.name][0], "floor": floors[q.name][1],
                          "meets_target": choice["accuracy"][q.name] >= suite.policy.target_accuracy,
                          "candidates": sorted((r for _, r in rows[q.name]), key=lambda r: -r["accuracy"])}

    return {
        "suite": suite.name,
        "policy": vars(suite.policy),
        "n_labelled": len(convs),
        "routes": routes,
        "thresholds": thresholds,
        "suite_choice": {k: v for k, v in choice.items() if k not in ("routes", "thresholds")},
        "configs": sorted(({k: v for k, v in c.items() if k not in ("routes", "thresholds")} for c in configs),
                          key=lambda c: (c["short"], c["usd_per_conv"])),
        "report": report,
    }, evaluators


def calibrate(suite: Suite, convs, labels, client: OpenRouter | None = None):
    """Returns (calibration dict, evaluators with trained local heads)."""
    return asyncio.run(calibrate_async(suite, convs, labels, client))
