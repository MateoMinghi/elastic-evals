"""Calibration: learn thresholds and choose each question's route from labelled conversations.

For every (question, evaluator) the threshold is the widest confidence band whose decisions
reach policy.target_accuracy on the labelled set. Local heads are trained here and scored
out-of-fold, so their thresholds are never tuned on their own training data.

Route choice is joint, because one hosted call answers every question of a conversation:
  1. For each question, the best achievable accuracy over all chains (rules -> local -> jev ->
     one LLM judge) sets a floor: best minus max(policy.tolerance, one standard error);
     critical questions use one standard error only, so noise-level gaps don't pick the route.
     Each language slice listed in policy.languages gets its own floor the same way.
  2. Each candidate set of hosted evaluators (Jev or not x at most one LLM judge) is tried:
     every question takes its cheapest chain inside that set that clears its floors, and the
     whole suite is simulated per conversation to get the real cost of shared calls.
  3. Among the sets within the latency and cost budgets (policy.max_ms_per_conv,
     policy.max_usd_per_1M_conv), the one with the fewest questions below their floor wins,
     then the cheapest, then the fastest. If no set fits the budgets, the smallest overrun wins
     and the report says so.

Binary questions: p is the probability that the problem is present; a band (lo, hi) is
confident when p <= lo or p >= hi. Multi-class questions: p is {label: probability}; a band
(0, hi) is confident when the top probability is >= hi.

Labels may be partial: a conversation without a label for a question is skipped when that
question's accuracy is measured, but still counts for cost and latency.
"""

import asyncio
import datetime as dt
import hashlib
import itertools
from collections import defaultdict

import numpy as np

from .evaluators import LocalHeadEvaluator, build
from .providers import OpenRouter
from .suite import Suite


def decide(p):
    if p is None:
        return None
    if isinstance(p, dict):
        return max(p, key=p.get) if p else None
    return int(p >= 0.5)


def confident(p, th):
    if p is None or th is None:
        return False
    lo, hi = th
    if isinstance(p, dict):
        return bool(p) and max(p.values()) >= hi
    return p <= lo or p >= hi


def fallback(q):
    """Decision when the last layer of a route has no answer: 'no problem' for binary checks."""
    return None if q.multiclass else 0


def final(p, q):
    d = decide(p)
    return fallback(q) if d is None else d


def tune_threshold(ps, ys, target, multiclass=False):
    """Largest-coverage band whose confident decisions reach `target` accuracy."""
    best, best_key = None, (-1.0, -1.0)
    los = [0.0] if multiclass else np.arange(0, 0.51, 0.05)
    his = np.arange(0.0, 1.0001, 0.05) if multiclass else np.arange(0.5, 1.0001, 0.05)
    for lo in los:
        for hi in his:
            th = (round(float(lo), 2), round(float(hi), 2))
            conf = [(p, y) for p, y in zip(ps, ys) if confident(p, th)]
            if not conf:
                continue
            acc = float(np.mean([decide(p) == y for p, y in conf]))
            key = (len(conf) / len(ps), acc)
            if acc >= target and key > best_key:
                best, best_key = th, key
    return best


def floor_of(best, n, tolerance, critical):
    smoothed = (best * n + 1) / (n + 2)  # avoids a zero standard error at 0% or 100%
    se = float(np.sqrt(smoothed * (1 - smoothed) / max(n, 1)))
    return best - (se if critical else max(tolerance, se))


async def collect(evaluators, convs, questions, labels=None, extra=None):
    """Run every evaluator on every conversation. Returns preds[e][(cid,q)], cost[e][cid], latency[e][cid].
    extra = (conversations, labels): additional training data for local heads (distillation)."""
    preds, cost, lat = defaultdict(dict), defaultdict(dict), defaultdict(dict)
    for ev in evaluators:
        if not ev.hosted and not isinstance(ev, LocalHeadEvaluator):
            for c in convs:
                out, cst, lt = ev.evaluate(c, questions)
                preds[ev.id].update({(c["id"], q): p for q, p in out.items()})
                cost[ev.id][c["id"]], lat[ev.id][c["id"]] = cst, lt
        elif isinstance(ev, LocalHeadEvaluator):
            if labels is not None:
                preds[ev.id].update(ev.fit(convs, labels, questions, extra))   # out-of-fold
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
    cheap = [e for e in allowed if e.rank < 3]
    llms = [e for e in allowed if e.rank >= 3]
    for r in range(len(cheap) + 1):
        for subset in itertools.combinations(cheap, r):
            for tail in [None] + llms:
                chain = list(subset) + ([tail] if tail else [])
                if chain:
                    yield chain


def _th(thresholds, cid):
    """Thresholds for one conversation: a dict, or (cross-fitting) a function of the conversation."""
    return thresholds(cid) if callable(thresholds) else thresholds


def walk(chain, q, cid, preds, thresholds):
    """Evaluators a question visits on one conversation, and the decision it ends with."""
    th = _th(thresholds, cid)
    for k, ev in enumerate(chain):
        p = preds[ev.id].get((cid, q.name))
        if k == len(chain) - 1 or confident(p, th[q.name][ev.id]):
            return chain[: k + 1], final(p, q)
    return chain, fallback(q)


def simulate_suite(routes, questions, convs, labels, preds, cost, lat, thresholds):
    """Per-conversation simulation: every evaluator visited by any question is paid for once."""
    acc, spend, latency = defaultdict(list), [], []
    for c in convs:
        cid, visited = c["id"], {}
        for q in questions:
            path, d = walk(routes[q.name], q, cid, preds, thresholds)
            if (cid, q.name) in labels:
                acc[q.name].append(d == labels[(cid, q.name)])
            visited.update({e.id: e for e in path})
        spend.append(sum(cost[e].get(cid, 0.0) for e in visited))
        latency.append(sum(lat[e].get(cid, 0.0) for e in visited))
    return ({q: float(np.mean(v)) for q, v in acc.items()}, float(np.mean(spend)), 1000 * float(np.mean(latency)))


def simulate(chain, q, convs, labels, preds, cost, lat, thresholds):
    correct, spend, latency, decided_by, by_lang = [], [], [], defaultdict(int), defaultdict(list)
    for c in convs:
        cid, cs, lt = c["id"], 0.0, 0.0
        th = _th(thresholds, cid)
        for k, ev in enumerate(chain):
            p = preds[ev.id].get((cid, q.name))
            cs += cost[ev.id].get(cid, 0.0)
            lt += lat[ev.id].get(cid, 0.0)
            if k == len(chain) - 1 or confident(p, th[q.name][ev.id]):
                if (cid, q.name) in labels:
                    ok = final(p, q) == labels[(cid, q.name)]
                    correct.append(ok)
                    by_lang[c.get("lang", "und")].append(ok)
                decided_by[ev.id] += 1
                break
        spend.append(cs)
        latency.append(lt)
    n = len(convs)
    return {"chain": [e.id for e in chain], "accuracy": float(np.mean(correct)) if correct else 0.0,
            "n": len(correct),
            "by_lang": {lg: {"accuracy": float(np.mean(v)), "n": len(v)} for lg, v in sorted(by_lang.items())},
            "usd_per_conv": float(np.mean(spend)), "ms_per_conv": 1000 * float(np.mean(latency)),
            "decided_by": {k: v / n for k, v in decided_by.items()}}


def _calibration_id(suite, convs, created):
    h = hashlib.sha256((suite.version + "|" + ",".join(sorted(c["id"] for c in convs))).encode()).hexdigest()[:8]
    return f"cal_{created:%Y%m%d%H%M%S}_{h}"


def _overrun(c, policy):
    over = 0.0
    if policy.max_ms_per_conv:
        over += max(0.0, c["ms_per_conv"] / policy.max_ms_per_conv - 1)
    if policy.max_usd_per_1M_conv:
        over += max(0.0, 1e6 * c["usd_per_conv"] / policy.max_usd_per_1M_conv - 1)
    return over


async def calibrate_async(suite: Suite, convs, labels, client: OpenRouter | None, extra=None):
    main, obs = suite.split()
    if obs is not None:
        # observation checks: every selected step is an item, calibrated like a conversation
        from . import trajectory
        items = [it for c in convs for it in trajectory.items(c, obs.questions)]
        item_labels = trajectory.item_labels(convs, labels)
        if not item_labels:
            raise ValueError("observation checks need labels such as 'check@<span_id>'")
        sub, sub_evs = await calibrate_async(obs, items, item_labels, client)
        if main.questions:
            cal, evaluators = await calibrate_async(main, convs, labels, client, extra)
        else:
            cal, evaluators = _empty_calibration(main, convs), build(main, client)
        cal["observation"] = sub
        for ev in evaluators:  # one set of local heads, keyed by check name
            twin = next((e for e in sub_evs if e.id == ev.id and isinstance(e, LocalHeadEvaluator)), None)
            if twin is not None and isinstance(ev, LocalHeadEvaluator):
                ev.heads.update(twin.heads)
        return cal, evaluators
    evaluators = build(suite, client)
    questions = suite.questions
    policy = suite.policy
    preds, cost, lat = await collect(evaluators, convs, questions, labels, extra)

    # thresholds per strictness level: a chain may require its cheap layers to be more accurate
    # than the policy target when the question's floor demands it
    levels = sorted({policy.target_accuracy, 0.97, 0.99, 1.0} - {x for x in (0.97, 0.99) if x < policy.target_accuracy})
    def tune(subset):
        out = {lv: {q.name: {} for q in questions} for lv in levels}
        for q in questions:
            for ev in evaluators:
                pairs = [(preds[ev.id].get((c["id"], q.name)), labels[(c["id"], q.name)]) for c in subset
                         if (c["id"], q.name) in labels]
                pairs = [(p, y) for p, y in pairs if p is not None]
                for lv in levels:
                    out[lv][q.name][ev.id] = tune_threshold(*zip(*pairs), lv, q.multiclass) if pairs else None
        return out

    by_level = tune(convs)  # what the runtime uses
    # what routes are judged by: with cross-fitting, each conversation is decided with thresholds
    # tuned on the other folds, so a route's accuracy is not flattered by thresholds fitted to it
    folds = policy.crossfit_folds if policy.crossfit_folds and len(convs) >= 2 * policy.crossfit_folds else 0
    if folds:
        fold = {c["id"]: int(hashlib.sha256(c["id"].encode()).hexdigest()[:8], 16) % folds for c in convs}
        per_fold = [tune([c for c in convs if fold[c["id"]] != k]) for k in range(folds)]
        judge_th = {lv: (lambda lv: lambda cid: per_fold[fold[cid]][lv])(lv) for lv in levels}
    else:
        judge_th = by_level

    # 1. every chain per question, and each question's floors (overall and per required language)
    rows, floors, lang_floors = {}, {}, {}
    for q in questions:
        usable = [e for e in evaluators if any((c["id"], q.name) in preds[e.id] for c in convs)]
        rows[q.name] = []
        for ch in chains(usable, q):
            for lv in (levels if len(ch) > 1 else levels[:1]):
                r = simulate(ch, q, convs, labels, preds, cost, lat, judge_th[lv])
                rows[q.name].append((ch, dict(r, strictness=lv)))
        if not rows[q.name]:
            raise ValueError(f"no evaluator can answer question {q.name!r}")
        best = max(rows[q.name], key=lambda x: x[1]["accuracy"])[1]
        floors[q.name] = (best["accuracy"], floor_of(best["accuracy"], best["n"], policy.tolerance, q.critical))
        lang_floors[q.name] = {}
        for lg in policy.languages or []:
            accs = [(r["by_lang"][lg]["accuracy"], r["by_lang"][lg]["n"]) for _, r in rows[q.name] if lg in r["by_lang"]]
            if accs:
                b, n = max(accs)
                lang_floors[q.name][lg] = (b, floor_of(b, n, policy.tolerance, q.critical))

    def clears(q, r):
        if r["accuracy"] < floors[q.name][1] - 1e-9:
            return False
        return all(r["by_lang"].get(lg, {"accuracy": 1.0})["accuracy"] >= f - 1e-9
                   for lg, (_, f) in lang_floors[q.name].items())

    # 2. candidate hosted sets: Jev or not x at most one LLM judge
    jevs = [None] + [e for e in evaluators if e.hosted and e.rank < 3]
    llms = [None] + [e for e in evaluators if e.rank >= 3]
    configs = []
    for jev, llm in itertools.product(jevs, llms):
        hosted = {e.id for e in (jev, llm) if e is not None}
        routes, short, levels_used = {}, [], {}
        for q in questions:
            inside = [(ch, r) for ch, r in rows[q.name] if {e.id for e in ch if e.hosted} <= hosted]
            if not inside:
                break
            ok = [(ch, r) for ch, r in inside if clears(q, r)]
            if not ok:
                short.append(q.name)
                ok = [max(inside, key=lambda x: x[1]["accuracy"])]
            ch, r = min(ok, key=lambda x: (x[1]["usd_per_conv"], x[1]["ms_per_conv"], len(x[0]), -x[1]["strictness"]))
            routes[q.name], levels_used[q.name] = ch, r["strictness"]
        if len(routes) < len(questions):
            continue
        th = {q: by_level[levels_used[q]][q] for q in routes}
        judged = (lambda lu: lambda cid: {q: judge_th[lu[q]](cid)[q] for q in lu})(dict(levels_used)) if folds else th
        acc, usd, ms = simulate_suite(routes, questions, convs, labels, preds, cost, lat, judged)
        c = {"hosted": sorted(hosted), "short": len(short), "below_floor": short, "usd_per_conv": usd,
             "ms_per_conv": ms, "accuracy": acc, "strictness": dict(levels_used), "thresholds": th,
             "routes": {q: [e.id for e in ch] for q, ch in routes.items()}}
        c["budget_overrun"] = _overrun(c, policy)
        configs.append(c)
    if not configs:
        raise ValueError("no hosted set can answer every question")
    within = [c for c in configs if c["budget_overrun"] == 0]
    pool = within or [min(configs, key=lambda c: (c["budget_overrun"], c["short"]))]
    choice = min(pool, key=lambda c: (c["short"], round(c["usd_per_conv"], 9), c["ms_per_conv"]))

    # 3. report
    routes, thresholds = choice["routes"], choice["thresholds"]
    report = {}
    for q in questions:
        chosen = next(r for ch, r in rows[q.name]
                      if [e.id for e in ch] == routes[q.name] and r["strictness"] == choice["strictness"][q.name])
        report[q.name] = {
            "chosen": dict(chosen, accuracy=choice["accuracy"][q.name]),
            "best_accuracy": floors[q.name][0], "floor": floors[q.name][1],
            "lang_floors": {lg: f for lg, (_, f) in lang_floors[q.name].items()},
            "meets_target": choice["accuracy"][q.name] >= policy.target_accuracy,
            "meets_floor": q.name not in choice["below_floor"],
            "candidates": sorted((r for _, r in rows[q.name]), key=lambda r: -r["accuracy"])[:25]}

    # baseline for the drift monitor: how often the chosen routes agree with the audit judge on the
    # labelled set; live agreement on the audit sample is compared with this (DESIGN.md §6.7)
    audit_agreement = {}
    if policy.audit_judge and policy.audit_judge in preds:
        auditor = next(e for e in evaluators if e.id == policy.audit_judge)
        by_id = {e.id: e for e in evaluators}
        for q in questions:
            chain = [by_id[e] for e in routes[q.name]]
            pairs = []
            for c in convs:
                pa = preds[auditor.id].get((c["id"], q.name))
                if pa is None:
                    continue
                _, d = walk(chain, q, c["id"], preds, thresholds)
                pairs.append((d == final(pa, q), c.get("lang", "und")))
            if pairs:
                langs = {lg: [ok for ok, l2 in pairs if l2 == lg] for lg in {lg for _, lg in pairs}}
                audit_agreement[q.name] = {"agreement": float(np.mean([ok for ok, _ in pairs])), "n": len(pairs),
                                           "by_lang": {lg: {"agreement": float(np.mean(v)), "n": len(v)}
                                                       for lg, v in sorted(langs.items())}}

    created = dt.datetime.now(dt.timezone.utc)
    return {
        "id": _calibration_id(suite, convs, created),
        "created_at": created.isoformat(),
        "status": "candidate",
        "suite": suite.name,
        "suite_version": suite.version,
        "criteria_versions": {q.name: q.criteria_version for q in questions},
        "policy": vars(policy),
        "n_labelled": len(convs),
        "crossfit_folds": folds,
        "n_distilled": len(extra[0]) if extra else 0,
        "languages": sorted({c.get("lang", "und") for c in convs}),
        "evaluator_versions": {e.id: e.version for e in evaluators},
        "routes": routes,
        "thresholds": thresholds,
        "suite_choice": {k: v for k, v in choice.items() if k not in ("routes", "thresholds")},
        "budget_met": choice["budget_overrun"] == 0,
        "audit_agreement": audit_agreement,
        "configs": sorted(({k: v for k, v in c.items() if k not in ("routes", "thresholds")} for c in configs),
                          key=lambda c: (c["budget_overrun"] > 0, c["short"], c["usd_per_conv"])),
        "report": report,
    }, evaluators


def _empty_calibration(suite, convs):
    created = dt.datetime.now(dt.timezone.utc)
    return {"id": _calibration_id(suite, convs, created), "created_at": created.isoformat(), "status": "candidate",
            "suite": suite.name, "suite_version": suite.version, "criteria_versions": {}, "policy": vars(suite.policy),
            "n_labelled": len(convs),
        "crossfit_folds": 0, "languages": [], "evaluator_versions": {}, "routes": {}, "thresholds": {},
            "suite_choice": {"hosted": [], "short": 0, "below_floor": [], "usd_per_conv": 0.0, "ms_per_conv": 0.0,
                             "accuracy": {}, "strictness": {}, "budget_overrun": 0.0},
            "budget_met": True, "audit_agreement": {}, "configs": [], "report": {}}


def calibrate(suite: Suite, convs, labels, client: OpenRouter | None = None, extra=None):
    """Returns (calibration dict, evaluators with trained local heads)."""
    return asyncio.run(calibrate_async(suite, convs, labels, client, extra))
