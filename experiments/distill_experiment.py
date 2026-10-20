"""Phase-4 gate: does a distilled head replace judge calls on at least one check without breaching
its floor? (DESIGN.md §6.8, §12)

  labelled  158 set-A conversations (labels by construction, standing in for human labels)
  pool      the 300 set-B conversations, unlabelled; the routing judge (Claude Haiku 5.5) labels them
  test      the 300 set-C conversations (holdout)

Compared: the engine calibrated on the labelled set only, and the engine whose local heads were
also trained on the judge's labels; both hosted and local-only. Hosted answers from the cache.
Writes results/DISTILL.md and results/distill.json.
"""

import json
import os
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
os.environ.setdefault("EVAL_OFFLINE", "1")
os.environ.setdefault("EVAL_CACHE_DIR", str(ROOT / ".cache"))
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from elastic_evals.calibrate import calibrate  # noqa: E402
from elastic_evals.distill import distill  # noqa: E402
from elastic_evals.engine import Engine  # noqa: E402
from elastic_evals.providers import OpenRouter  # noqa: E402
from elastic_evals.suite import Suite  # noqa: E402

TEACHER = "llm_haiku"


def load(name):
    return [json.loads(line) for line in (ROOT / "data" / name).open()]


def evaluate(suite, cal, evs, test):
    res = Engine(suite, cal, evs, audit=False).run(test)
    gold = {(c["id"], q): v for c in test for q, v in c["labels"].items()}
    qs = [q.name for q in suite.questions]
    acc = {q: float(np.mean([r["decisions"][q] == gold[(r["conversation_id"], q)] for r in res])) for q in qs}
    return {"accuracy": acc, "mean": float(np.mean(list(acc.values()))),
            "usd_per_1M": 1e6 * float(np.mean([r["usd"] for r in res])),
            "ms": float(np.mean([r["ms"] for r in res])),
            "llm_share": float(np.mean([any(e.startswith("llm") for e in r["evaluators_called"]) for r in res])),
            "routes": cal["routes"], "hosted": cal["suite_choice"]["hosted"],
            "decided_by_local": {q: cal["report"][q]["chosen"]["decided_by"].get("e5", 0.0) for q in qs},
            "below_floor": cal["suite_choice"]["below_floor"]}


def main():
    a = load("synthetic.jsonl")
    dev = [c for i, c in enumerate(a) if i % 5 in (0, 1)]
    labels = {(c["id"], q): v for c in dev for q, v in c["labels"].items()}
    pool = [{k: v for k, v in c.items() if k != "labels"} for c in load("synthetic_b.jsonl")]
    b_gold = {(c["id"], q): v for c in load("synthetic_b.jsonl") for q, v in c["labels"].items()}
    test = load("synthetic_c.jsonl")
    client = OpenRouter()
    out = {}
    for policy in ("hosted", "local-only"):
        suite = Suite.load(ROOT.parent / "examples" / "banking_suite.yaml")
        suite.policy.allow_hosted = policy == "hosted"
        cal, evs = calibrate(suite, dev, labels, client if policy == "hosted" else None)
        out[f"{policy}: labelled only"] = evaluate(suite, cal, evs, test)
        # the teacher is hosted; a local-only tenant would run a self-hosted judge as teacher instead
        suite_t = Suite.load(ROOT.parent / "examples" / "banking_suite.yaml")
        dcal, devs, judge = distill(suite_t, dev, labels, pool, TEACHER, client)
        if policy == "local-only":
            suite_t.policy.allow_hosted = False
            from elastic_evals.calibrate import calibrate_async
            import asyncio
            dcal, devs = asyncio.run(calibrate_async(suite_t, dev, labels, None, extra=(pool, judge)))
        out[f"{policy}: + distilled from {TEACHER} on 300 unlabelled"] = evaluate(suite_t, dcal, devs, test)
        out["teacher_agreement_on_pool"] = {q.name: float(np.mean([judge[(c["id"], q.name)] == b_gold[(c["id"], q.name)]
                                                                   for c in pool if (c["id"], q.name) in judge]))
                                            for q in suite.questions}
    (ROOT / "results" / "distill.json").write_text(json.dumps(out, indent=1))

    qs = list(next(iter(out.values()))["accuracy"])
    lines = ["# Distillation: LLM labels train the local heads", "",
             "Labelled: 158 set-A conversations. Unlabelled pool: 300 set-B conversations, labelled by "
             f"`{TEACHER}` (the suite's routing judge). Test: the 300 set-C holdout. Hosted answers from cache.", "",
             "| run | " + " | ".join(qs) + " | mean % | $ / 1M conv | ms / conv | sent to an LLM |",
             "|---" * (len(qs) + 5) + "|"]
    for name, r in out.items():
        if name == "teacher_agreement_on_pool":
            continue
        lines.append(f"| {name} | " + " | ".join(f"{100 * r['accuracy'][q]:.1f}" for q in qs) +
                     f" | **{100 * r['mean']:.1f}** | {r['usd_per_1M']:.0f} | {r['ms']:.0f} | {100 * r['llm_share']:.0f}% |")
    lines += ["", "Routes:", ""]
    for name, r in out.items():
        if name == "teacher_agreement_on_pool":
            continue
        lines.append(f"- **{name}**: " + "; ".join(f"{q}: {' → '.join(r['routes'][q])}" for q in qs))
    lines += ["", "Teacher agreement with the planted labels on the pool: " +
              ", ".join(f"{q} {100 * v:.1f}%" for q, v in out["teacher_agreement_on_pool"].items())]
    (ROOT / "results" / "DISTILL.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
