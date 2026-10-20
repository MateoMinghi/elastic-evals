"""Does cross-fitting the thresholds make calibration choose better routes? (DESIGN.md §6.3)

Without cross-fitting, a route's accuracy is measured with thresholds tuned on the very same
labelled conversations, and calibration then takes the cheapest route that looks "close enough"
to the best: an optimistic, selection-biased estimate. With cross-fitting, each conversation is
decided with thresholds tuned on the other folds.

Two protocols, both offline from the cache: (1) calibrate on 158 set-A conversations, score the
300-conversation holdout C; (2) the phase-1 gate, 5-fold cross-validation on C itself.
Writes results/CROSSFIT.md and results/crossfit.json.
"""

import asyncio
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
from elastic_evals.engine import Engine  # noqa: E402
from elastic_evals.gates import phase1_async  # noqa: E402
from elastic_evals.providers import OpenRouter  # noqa: E402
from elastic_evals.suite import Suite  # noqa: E402


def load(name):
    return [json.loads(line) for line in (ROOT / "data" / name).open()]


def main():
    a = load("synthetic.jsonl")
    dev = [c for i, c in enumerate(a) if i % 5 in (0, 1)]
    c_set = load("synthetic_c.jsonl")
    client = OpenRouter()
    out = {}
    for folds in (0, 5):
        suite = Suite.load(ROOT.parent / "examples" / "banking_suite.yaml")
        suite.policy.crossfit_folds = folds
        cal, evs = calibrate(suite, dev, {(c["id"], q): v for c in dev for q, v in c["labels"].items()}, client)
        res = Engine(suite, cal, evs, audit=False).run(c_set)
        qs = [q.name for q in suite.questions]
        acc = {q: float(np.mean([r["decisions"][q] == c["labels"][q] for r, c in zip(res, c_set)])) for q in qs}
        rank = {e.id: e.rank for e in evs}
        gate = asyncio.run(phase1_async(suite, c_set, client, folds=5))
        out[f"crossfit={folds}"] = {
            "a_to_c": {"accuracy": acc, "mean": float(np.mean(list(acc.values()))), "routes": cal["routes"],
                       "hosted": cal["suite_choice"]["hosted"],
                       "usd_per_1M": 1e6 * float(np.mean([r["usd"] for r in res])),
                       "llm_share": float(np.mean([any(rank.get(e) == 3 for e in r["evaluators_called"]) for r in res]))},
            "gate_c": gate}
        print(folds, out[f"crossfit={folds}"]["a_to_c"]["mean"], gate["passed"], flush=True)
    (ROOT / "results" / "crossfit.json").write_text(json.dumps(out, indent=1))
    qs = list(out["crossfit=0"]["a_to_c"]["accuracy"])
    lines = ["# Cross-fitted thresholds in calibration", "",
             "Calibration judges candidate routes by their accuracy on the labelled set. Without cross-fitting, the "
             "confidence thresholds are tuned on the same conversations they are judged on; with it (5 folds), each "
             "conversation is decided with thresholds tuned on the other folds. The runtime always uses thresholds "
             "tuned on all labels. Hosted answers from the cache.", "",
             "**Calibrated on 158 set-A conversations, scored on the 300-conversation holdout C**", "",
             "| thresholds | " + " | ".join(qs) + " | mean % | $ / 1M conv | sent to an LLM | hosted set |", "|---" * (len(qs) + 5) + "|"]
    for k, v in out.items():
        r = v["a_to_c"]
        lines.append(f"| {k} | " + " | ".join(f"{100 * r['accuracy'][q]:.1f}" for q in qs) +
                     f" | **{100 * r['mean']:.1f}** | {r['usd_per_1M']:.0f} | {100 * r['llm_share']:.0f}% | {', '.join(r['hosted'])} |")
    lines += ["", "**Phase-1 gate: 5-fold cross-validation on C (engine against the best single evaluator, 1 SE)**", "",
              "| thresholds | " + " | ".join(qs) + " | gate | $ / 1M conv | sent to an LLM |", "|---" * (len(qs) + 4) + "|"]
    for k, v in out.items():
        g = v["gate_c"]
        cells = [f"{100 * g['checks'][q]['engine']:.1f} vs {100 * g['checks'][q]['best_single_accuracy']:.1f}"
                 + ("" if g["checks"][q]["pass"] else " ✗") for q in qs]
        lines.append(f"| {k} | " + " | ".join(cells) + f" | {'PASSED' if g['passed'] else 'not passed'} | "
                     f"{g['engine_usd_per_1M']:.0f} | {100 * g['engine_llm_share']:.0f}% |")
    (ROOT / "results" / "CROSSFIT.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
