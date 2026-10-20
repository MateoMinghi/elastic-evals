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

Runs work without any service (pytest, CLI). With Postgres and OpenSearch they are stored in
experiment_runs and scores-* (sample_reason "experiment"), so comparing an experiment with
production is a query.
"""

import asyncio
import datetime as dt
import json
import uuid
from collections import Counter

from .engine import Engine


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
    return {"n": n, "rates": rates, "evaluator_accuracy": acc, "gates": gates, "passed": all(g["pass"] for g in gates),
            "cost_usd": sum(it["usd"] for it in items)}


async def run_async(agent, items, suite, calibration, evaluators, run_id=None, baseline=None, tolerance=0.02,
                    concurrency=4):
    """agent(item_input) -> conversation dict (sync or async). items: [{"id", "input", "expected"}]."""
    run_id = run_id or f"run_{dt.datetime.now(dt.timezone.utc):%Y%m%d%H%M%S}_{uuid.uuid4().hex[:6]}"
    sem = asyncio.Semaphore(concurrency)

    async def produce(item):
        async with sem:
            out = agent(item["input"])
            if asyncio.iscoroutine(out):
                out = await out
            conv = dict(out)
            conv.setdefault("id", f"{run_id}:{item['id']}")
            conv.setdefault("lang", item["input"].get("lang", "und"))
            conv.setdefault("record", item["input"].get("record", {}))
            conv["experiment"] = {"run_id": run_id, "item_id": item["id"]}
            return conv

    convs = await asyncio.gather(*(produce(it) for it in items))
    results = await Engine(suite, calibration, evaluators, audit=False).run_async(convs)
    out = []
    for item, conv, r in zip(items, convs, results):
        for s in r["scores"]:
            if s["decision"]:
                s["sample_reason"] = "experiment"
        out.append({"item_id": item["id"], "conversation": conv, "decisions": r["decisions"], "scores": r["scores"],
                    "assertions": assertions(conv, item.get("expected") or {}), "expected": item.get("expected") or {},
                    "usd": r["usd"]})
    return {"run_id": run_id, "items": out, "summary": summarize(out, suite, baseline, tolerance)}


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
    return {"a": a["run_id"], "b": b["run_id"], "rates": {c: (a["summary"]["rates"].get(c), r)
                                                          for c, r in b["summary"]["rates"].items()},
            "changed_items": rows}


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
