"""Datasets, offline experiments and CI gates (DESIGN.md §6.10).

An experiment runs an agent over dataset items, scores each produced conversation with the same
suite and calibration as production, checks trajectory assertions, and applies gates:

  critical checks   absolute: no item may be flagged ("zero false confirmations")
  other checks      relative: the flagged rate may not exceed the baseline run's by more than
                    `tolerance` (no baseline: reported, not gated)
  assertions        promptfoo vocabulary on the item's `expected`:
                      tool_sequence        trajectory:tool-sequence, exact order of tool names
                      tool_call_f1         tool-call-f1 >= value, over (name, args) pairs
                      not_tools            not-: these tools must not be called
                      labels               expected check values (measures the evaluators)

Cost of the agent under test (not of the evaluation, which is `cost_usd` in the summary). The agent
reports what it used on the conversation it returns:

  usage       {input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens},
              or a list of those, one per LLM call, each with its own `model`. input_tokens include
              the cached ones and output_tokens the reasoning ones, as in the OTel GenAI usage.
  model       the model the calls used when they do not say (else trace_ctx.model, else the sweep's)
  cost_usd    a cost the provider reported (OpenRouter's usage.cost); otherwise the price book prices it

and the summary reports, per run, the two numbers model choice turns on:

  cost per task          spend per item, so it counts how many tokens a model needs (reasoning,
                         retries, longer answers), not only its rate; and cost per *successful* task,
                         spend divided by items with no flagged check and no failed assertion
  cost per 1M tokens     effective (spend / tokens actually used, after cache discounts), and the
                         list price blended 7:2:1 cache-hit:input:output (Artificial Analysis' ratio)

`sweep` runs the same items against several models and ranks them (DESIGN.md §6.10).

Runs work without any service (pytest, CLI). With Postgres and OpenSearch they are stored in
experiment_runs and scores-* (sample_reason "experiment"), so comparing an experiment with
production is a query.
"""

import asyncio
import datetime as dt
import json
import re
import time
import uuid
from collections import Counter

import numpy as np

from .engine import Engine

TOKEN_KINDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")


def tool_calls(conv):
    return [(t["name"], json.dumps(t.get("args", {}), sort_keys=True)) for t in conv["turns"] if t["role"] == "tool"]


def tool_call_f1(expected, got):
    e, g = Counter(expected), Counter(got)
    tp = sum((e & g).values())
    if not e and not g:
        return 1.0
    p = tp / sum(g.values()) if g else 0.0
    r = tp / sum(e.values()) if e else 0.0
    return 2 * p * r / (p + r) if p + r else 0.0


def assertions(conv, expected: dict) -> list[dict]:
    out = []
    calls = tool_calls(conv)
    if "tool_sequence" in expected:
        got = [n for n, _ in calls]
        out.append({"type": "trajectory:tool-sequence", "pass": got == expected["tool_sequence"],
                    "expected": expected["tool_sequence"], "got": got})
    if "tool_call_f1" in expected:
        want = [(c["name"], json.dumps(c.get("args", {}), sort_keys=True)) for c in expected.get("tool_calls", [])]
        f1 = tool_call_f1(want, calls)
        out.append({"type": "tool-call-f1", "pass": f1 >= expected["tool_call_f1"], "value": f1,
                    "threshold": expected["tool_call_f1"]})
    for name in expected.get("not_tools", []):
        out.append({"type": f"not-tool:{name}", "pass": all(n != name for n, _ in calls)})
    return out


def summarize(items, suite, baseline: dict | None = None, tolerance: float = 0.02) -> dict:
    checks = {q.name: q for q in suite.questions}
    n = len(items)
    rates, acc = {}, {}
    for name, q in checks.items():
        decided = [it["decisions"].get(name) for it in items]
        if q.multiclass:
            rates[name] = None
        else:
            rates[name] = sum(d == 1 for d in decided) / n if n else 0.0
        truth = [(it["expected"].get("labels", {}).get(name), it["decisions"].get(name)) for it in items
                 if name in (it["expected"].get("labels") or {})]
        if truth:
            acc[name] = sum(str(a) == str(b) for a, b in truth) / len(truth)
    gates = []
    for name, q in checks.items():
        if q.multiclass:
            continue
        flagged = [it["item_id"] for it in items if it["decisions"].get(name) == 1]
        if q.critical:
            gates.append({"check": name, "kind": "absolute", "pass": not flagged, "flagged": flagged})
        elif baseline and name in (baseline.get("rates") or {}):
            limit = baseline["rates"][name] + tolerance
            gates.append({"check": name, "kind": "relative", "pass": rates[name] <= limit + 1e-12,
                          "rate": rates[name], "baseline": baseline["rates"][name], "limit": limit})
    failed_assertions = [(it["item_id"], a["type"]) for it in items for a in it["assertions"] if not a["pass"]]
    gates.append({"check": "assertions", "kind": "assertions", "pass": not failed_assertions, "failed": failed_assertions})
    ok = [succeeded(it, suite) for it in items]
    rate = sum(ok) / n if n else 0.0
    m = len({it.get("base_id", it["item_id"]) for it in items})  # repeats of an item are not independent samples
    return {"n": n, "rates": rates, "evaluator_accuracy": acc, "gates": gates, "passed": all(g["pass"] for g in gates),
            "success_rate": rate, "success_ci": wilson(rate * m, m),
            "cost_usd": sum(it["usd"] for it in items),  # the evaluation's cost; the agent's is under "agent"
            "agent": agent_summary(items, ok)}


def succeeded(item, suite) -> bool:
    """An item succeeds when no binary check is flagged and every assertion passes."""
    return (not any(item["decisions"].get(q.name) == 1 for q in suite.questions if not q.multiclass)
            and all(a["pass"] for a in item["assertions"]))


def wilson(k, n, z=1.96):
    """95% Wilson interval of a proportion; honest on the small datasets experiments use."""
    if not n:
        return None
    p = k / n
    centre, half = p + z * z / (2 * n), z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return [float(max(0.0, (centre - half) / (1 + z * z / n))), float(min(1.0, (centre + half) / (1 + z * z / n)))]


# -- the agent's own cost ------------------------------------------------------------------------
def agent_usage(conv, prices=None, default_model=None, at=None) -> dict:
    """Tokens, model and cost of the agent's LLM calls in one produced conversation."""
    calls = conv.get("usage") or []
    calls = [calls] if isinstance(calls, dict) else [c for c in calls if isinstance(c, dict)]
    fallback = conv.get("model") or (conv.get("trace_ctx") or {}).get("model") or default_model
    tokens = {k: sum(int(c.get(k) or 0) for c in calls) for k in TOKEN_KINDS}
    models = sorted({c.get("model") or fallback for c in calls if c.get("model") or fallback})
    usd = conv.get("cost_usd")
    if usd is None and calls and prices is not None:
        at = at or dt.datetime.now(dt.timezone.utc)
        priced = [prices.cost(c.get("model") or fallback, c, at)[0] for c in calls]
        usd = None if any(p is None for p in priced) else sum(priced)
    return {"models": models, "tokens": tokens, "usd": None if usd is None else float(usd)}


def agent_summary(items, ok=None) -> dict | None:
    """Cost per task, cost per successful task, cost per 1M tokens and latency of the agent."""
    rows = [it["agent"] for it in items if it.get("agent")]
    if not rows:
        return None
    ok = ok if ok is not None else [True] * len(items)
    priced = [(it["agent"], o) for it, o in zip(items, ok) if it.get("agent") and it["agent"]["usd"] is not None]
    tokens = {k: sum(a["tokens"][k] for a in rows) for k in TOKEN_KINDS}
    total = tokens["input_tokens"] + tokens["output_tokens"]
    out = {"models": sorted({m for a in rows for m in a["models"]}), "tokens": tokens,
           "tokens_per_task": total / len(rows), "priced": len(priced), "unpriced": len(rows) - len(priced),
           "usd": None, "cost_per_task": None, "cost_per_success": None, "usd_per_1M_tokens": None}
    if priced:
        usd = sum(a["usd"] for a, _ in priced)
        priced_tokens = sum(a["tokens"]["input_tokens"] + a["tokens"]["output_tokens"] for a, _ in priced)
        successes = sum(o for _, o in priced)
        out.update(usd=usd, cost_per_task=usd / len(priced),
                   cost_per_success=usd / successes if successes else None,
                   usd_per_1M_tokens=1e6 * usd / priced_tokens if priced_tokens else None)
    ms = [a["ms"] for a in rows if a.get("ms") is not None]
    if ms:
        out.update(p50_ms=float(np.percentile(ms, 50)), p95_ms=float(np.percentile(ms, 95)))
    return out


async def run_async(agent, items, suite, calibration, evaluators, run_id=None, baseline=None, tolerance=0.02,
                    concurrency=4, prices=None, model=None, repeats=1):
    """agent(item_input) -> conversation dict (sync or async). items: [{"id", "input", "expected"}].

    prices: a PriceBook for the agent's usage; model: the model the agent runs on, when its
    conversations do not say; repeats: run each item k times (item ids get "#r"), since agents
    are not deterministic."""
    run_id = run_id or f"run_{dt.datetime.now(dt.timezone.utc):%Y%m%d%H%M%S}_{uuid.uuid4().hex[:6]}"
    sem = asyncio.Semaphore(concurrency)
    work = [(it, r, it["id"] if repeats == 1 else f"{it['id']}#{r}") for it in items for r in range(repeats)]

    async def produce(item, item_id):
        async with sem:
            t0 = time.perf_counter()
            out = agent(item["input"])
            if asyncio.iscoroutine(out):
                out = await out
            ms = 1000 * (time.perf_counter() - t0)
            conv = dict(out)
            conv.setdefault("id", f"{run_id}:{item_id}")
            conv.setdefault("lang", item["input"].get("lang", "und"))
            conv.setdefault("record", item["input"].get("record", {}))
            conv["experiment"] = {"run_id": run_id, "item_id": item_id}
            used = dict(agent_usage(conv, prices, model), ms=conv.get("latency_ms", ms))
            if used["models"] and not (conv.get("trace_ctx") or {}).get("model"):  # scores-* filter by the agent's model
                conv["trace_ctx"] = {**(conv.get("trace_ctx") or {}), "model": used["models"][0]}
            return conv, used

    produced = await asyncio.gather(*(produce(it, iid) for it, _, iid in work))
    convs = [c for c, _ in produced]
    results = await Engine(suite, calibration, evaluators, audit=False).run_async(convs)
    out = []
    for (item, rep, item_id), (conv, used), r in zip(work, produced, results):
        for s in r["scores"]:
            if s["decision"]:
                s["sample_reason"] = "experiment"
        out.append({"item_id": item_id, "base_id": item["id"], "repeat": rep, "conversation": conv,
                    "decisions": r["decisions"], "scores": r["scores"],
                    "assertions": assertions(conv, item.get("expected") or {}), "expected": item.get("expected") or {},
                    "usd": r["usd"], "agent": used})
    summary = summarize(out, suite, baseline, tolerance)
    if repeats > 1:  # pass^k: the share of items that succeed on every one of the k attempts
        by_item = {}
        for it in out:
            by_item.setdefault(it["base_id"], []).append(succeeded(it, suite))
        summary["repeats"] = repeats
        summary["pass_all_k"] = sum(all(v) for v in by_item.values()) / len(by_item) if by_item else 0.0
    return {"run_id": run_id, "model": model, "items": out, "summary": summary}


def run(agent, items, suite, calibration, evaluators, **kw):
    return asyncio.run(run_async(agent, items, suite, calibration, evaluators, **kw))


def compare(a: dict, b: dict) -> dict:
    """Per-item differences between two runs of the same dataset (b against a)."""
    ia = {it["item_id"]: it for it in a["items"]}
    rows = []
    for it in b["items"]:
        old = ia.get(it["item_id"])
        if old is None:
            continue
        changed = {c: (old["decisions"].get(c), v) for c, v in it["decisions"].items() if old["decisions"].get(c) != v}
        if changed:
            rows.append({"item_id": it["item_id"], "changed": changed})
    agent = {k: ((a["summary"].get("agent") or {}).get(k), (b["summary"].get("agent") or {}).get(k))
             for k in ("cost_per_task", "cost_per_success", "usd_per_1M_tokens", "tokens_per_task")}
    return {"a": a["run_id"], "b": b["run_id"], "rates": {c: (a["summary"]["rates"].get(c), r)
                                                          for c, r in b["summary"]["rates"].items()},
            "success_rate": (a["summary"].get("success_rate"), b["summary"].get("success_rate")),
            "agent": agent, "changed_items": rows}


# -- which model should the agent run on -----------------------------------------------------------
def paired_delta(base_items, items, suite, n_boot=2000, seed=0) -> dict | None:
    """Success rate of `items` minus the baseline's on the same dataset items, with a paired
    bootstrap 95% interval (repeats are averaged per item first)."""
    def per_item(rows):
        acc = {}
        for it in rows:
            acc.setdefault(it.get("base_id", it["item_id"]), []).append(succeeded(it, suite))
        return {k: sum(v) / len(v) for k, v in acc.items()}
    a, b = per_item(base_items), per_item(items)
    keys = sorted(set(a) & set(b))
    if not keys:
        return None
    d = np.array([b[k] - a[k] for k in keys])
    boot = np.random.default_rng(seed).choice(d, size=(n_boot, len(d)), replace=True).mean(axis=1)
    lo, hi = np.percentile(boot, [2.5, 97.5])
    return {"delta": float(d.mean()), "ci": [float(lo), float(hi)], "n": len(keys),
            "significant": bool(lo > 0 or hi < 0)}


def family(model: str) -> set[str]:
    """Vendor and family of a model id, to spot a judge grading its own family."""
    m = model.lower()
    name = m.split("/")[-1]
    token = (re.match(r"[a-z]+", name) or [name])[0]
    known = {"claude": "anthropic", "gpt": "openai", "o": "openai", "gemini": "google", "gemma": "google",
             "llama": "meta", "grok": "xai"}
    return {known.get(token, token)} | ({m.split("/")[0]} if "/" in m else set())


def pareto(rows) -> set[str]:
    """Qualified models no other qualified model beats on both success rate and cost per task."""
    pts = [(r["model"], r["success_rate"], r["cost_per_task"]) for r in rows
           if r["qualified"] and r["cost_per_task"] is not None]
    return {m for m, s, c in pts if not any((s2 >= s and c2 <= c) and (s2 > s or c2 < c) for _, s2, c2 in pts)}


async def sweep_async(make_agent, models, items, suite, calibration, evaluators, prices=None, repeats=1,
                      baseline=None, concurrency=4, tolerance=0.02) -> dict:
    """Run the same items with the agent on each model and rank the models.

    make_agent(model) -> agent. A model whose critical checks flag any item is disqualified; the
    rest rank by success rate, then by cost per task. Each model is compared with `baseline`
    (default: the first) item by item."""
    baseline = baseline or models[0]
    if baseline not in models:
        raise ValueError(f"baseline {baseline!r} is not one of the models")
    stamp = f"{dt.datetime.now(dt.timezone.utc):%Y%m%d%H%M%S}_{uuid.uuid4().hex[:6]}"
    runs = {}
    for m in models:
        runs[m] = await run_async(make_agent(m), items, suite, calibration, evaluators, concurrency=concurrency,
                                  prices=prices, model=m, repeats=repeats, tolerance=tolerance,
                                  run_id=f"sweep_{stamp}_{re.sub(r'[^A-Za-z0-9.-]+', '-', m)}")
    at = dt.datetime.now(dt.timezone.utc)
    rows = []
    for m in models:
        s, a = runs[m]["summary"], runs[m]["summary"]["agent"] or {}
        critical = [g for g in s["gates"] if g["kind"] == "absolute"]
        rows.append({
            "model": m, "run_id": runs[m]["run_id"], "n": s["n"],
            "success_rate": s["success_rate"], "success_ci": s["success_ci"], "pass_all_k": s.get("pass_all_k"),
            "qualified": all(g["pass"] for g in critical),
            "critical_flagged": {g["check"]: len(g["flagged"]) for g in critical if g["flagged"]},
            "rates": s["rates"],
            "cost_per_task": a.get("cost_per_task"), "cost_per_success": a.get("cost_per_success"),
            "usd_per_1M_tokens": a.get("usd_per_1M_tokens"),
            "list_usd_per_1M_tokens": prices.blended(m, at) if prices is not None else None,
            "tokens_per_task": a.get("tokens_per_task"), "tokens": a.get("tokens"), "unpriced": a.get("unpriced"),
            "p50_ms": a.get("p50_ms"), "p95_ms": a.get("p95_ms"),
            "eval_usd": s["cost_usd"],
            "vs_baseline": None if m == baseline else paired_delta(runs[baseline]["items"], runs[m]["items"], suite),
        })
    front = pareto(rows)
    for r in rows:
        r["pareto"] = r["model"] in front
    rows.sort(key=lambda r: (not r["qualified"], -r["success_rate"],
                             r["cost_per_task"] if r["cost_per_task"] is not None else float("inf")))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    warnings = []
    used = {e for route in calibration["routes"].values() for e in route}
    judges = [e for e in evaluators if e.id in used and getattr(e, "model", None)]
    for m in models:
        for j in judges:
            if family(m) & family(j.model):
                warnings.append(f"{m}: evaluator {j.id} ({j.model}) is from the same family and may favour it; "
                                "check its accuracy on this model's conversations, or calibrate without it")
    for r in rows:
        if r["unpriced"]:
            warnings.append(f"{r['model']}: {r['unpriced']} of {r['n']} conversations carry no usage or no matching "
                            "price; cost figures cover the rest")
    return {"baseline": baseline, "repeats": repeats, "models": rows, "warnings": warnings, "runs": runs}


def sweep(make_agent, models, items, suite, calibration, evaluators, **kw):
    return asyncio.run(sweep_async(make_agent, models, items, suite, calibration, evaluators, **kw))


# -- storage -------------------------------------------------------------------------------------
def create_dataset(pg, tenant, name, description=None) -> str:
    did = f"ds_{uuid.uuid4().hex[:12]}"
    pg.exec("INSERT INTO datasets (id, tenant_id, name, description) VALUES (%s, %s, %s, %s)", did, tenant, name, description)
    return did


def put_items(pg, dataset_id, items):
    """Insert or version items: a changed item closes its current version and opens a new one."""
    from .store.postgres import Jsonb
    with pg.tx() as c:
        for it in items:
            cur = c.execute("SELECT input, expected FROM dataset_items WHERE dataset_id=%s AND id=%s AND valid_to IS NULL",
                            (dataset_id, it["id"])).fetchone()
            if cur and cur["input"] == it["input"] and cur["expected"] == it.get("expected"):
                continue
            if cur:
                c.execute("UPDATE dataset_items SET valid_to=now() WHERE dataset_id=%s AND id=%s AND valid_to IS NULL",
                          (dataset_id, it["id"]))
            c.execute("""INSERT INTO dataset_items (id, dataset_id, input, expected, source_conversation_id)
                         VALUES (%s, %s, %s, %s, %s)""", (it["id"], dataset_id, Jsonb(it["input"]),
                                                          Jsonb(it.get("expected")), it.get("source_conversation_id")))


def items_of(pg, dataset_id, at=None):
    rows = pg.all("""SELECT id, input, expected, source_conversation_id FROM dataset_items WHERE dataset_id=%s
                     AND valid_from <= coalesce(%s, now()) AND (valid_to IS NULL OR valid_to > coalesce(%s, now()))
                     ORDER BY id""", dataset_id, at, at)
    return [dict(r, expected=r["expected"] or {}) for r in rows]


def store_run(pg, os_, pool, tenant, dataset_id, name, result, suite_id=None, calibration_id=None, baseline_run_id=None):
    from .store.postgres import Jsonb
    s = result["summary"]
    pg.exec("""INSERT INTO experiment_runs (id, tenant_id, dataset_id, name, suite_id, calibration_id, baseline_run_id,
               summary, status, finished_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
               ON CONFLICT (id) DO UPDATE SET summary=EXCLUDED.summary, status=EXCLUDED.status, finished_at=now()""",
            result["run_id"], tenant, dataset_id, name, suite_id, calibration_id, baseline_run_id,
            Jsonb(json.loads(json.dumps(s, default=str))), "passed" if s["passed"] else "failed")
    if os_ is not None:
        stamp = dt.datetime.now(dt.timezone.utc).isoformat()
        docs = []
        for it in result["items"]:
            for sc in it["scores"]:
                docs.append((sc["id"], dict(sc, tenant_id=tenant, timestamp=stamp), None))
        os_.bulk("scores", docs, pool, op="index")
    return result["run_id"]


def load_run(pg, run_id, os_=None, pool=None):
    row = pg.one("SELECT * FROM experiment_runs WHERE id=%s", run_id)
    if row is None:
        raise KeyError(run_id)
    if os_ is not None:
        hits = os_.hits("scores", {"size": 10000, "query": {"bool": {"filter": [
            {"term": {"experiment.run_id": run_id}}, {"term": {"sample_reason": "experiment"}}]}}}, pool)
        items = {}
        for h in hits:
            it = items.setdefault(h["experiment"]["item_id"], {"item_id": h["experiment"]["item_id"], "decisions": {}})
            it["decisions"][h["check"]] = h["label"] if h["value"] is None else int(h["value"])
        row["items"] = sorted(items.values(), key=lambda x: x["item_id"])
    row["run_id"] = row["id"]
    return row
