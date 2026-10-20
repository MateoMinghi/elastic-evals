"""End-to-end test of the elastic_evals engine.

Calibrate on labelled data only, then score held-out conversations:
  A-test   same generator as the calibration data (Claude Haiku 5.5)
  B        a messier set from a different model family (Gemini 3.1 Flash Lite), never seen in calibration
  B-rest   B minus 100 conversations that are added to calibration ("recalibrate with a few in-domain labels")

Every run is compared with each single evaluator on the same conversations, with
bootstrap 95% intervals for the engine-minus-baseline difference in mean accuracy.
"""

import json
import sys
from pathlib import Path

import numpy as np
from tabulate import tabulate

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
from elastic_evals.calibrate import calibrate  # noqa: E402
from elastic_evals.engine import Engine  # noqa: E402
from elastic_evals.providers import OpenRouter, load_env, spent_usd  # noqa: E402
from elastic_evals.suite import Suite  # noqa: E402

SUITE = ROOT.parent / "examples" / "banking_suite.yaml"
OUT = ROOT / "results"


def load(name):
    return [json.loads(line) for line in (ROOT / "data" / name).open()]


def labels_of(convs):
    return {(c["id"], q): v for c in convs for q, v in c["labels"].items()}


def summarize(results, gold, questions):
    acc = {q: float(np.mean([s["value"] == gold[(r["conversation_id"], q)]
                             for r in results for s in r["scores"] if s["decision"] and s["question"] == q]))
           for q in questions}
    per_conv = np.array([np.mean([s["value"] == gold[(r["conversation_id"], s["question"])]
                                  for s in r["scores"] if s["decision"]]) for r in results])
    hosted = {"jev", "llm"}
    return {
        "acc": acc, "mean_acc": float(np.mean(list(acc.values()))), "per_conv": per_conv,
        "usd_per_1M_conv": 1e6 * float(np.mean([r["usd"] for r in results])),
        "ms_per_conv": float(np.mean([r["ms"] for r in results])),
        "llm_share": float(np.mean([any(e.startswith("llm") for e in r["evaluators_called"]) for r in results])),
        "hosted_share": float(np.mean([any(s["evaluator"]["kind"] in hosted for s in r["scores"] if s["decision"])
                                       for r in results])),
    }


def bootstrap_diff(a, b, n=2000, seed=0):
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(a), (n, len(a)))
    d = (a[idx] - b[idx]).mean(axis=1)
    return float(np.percentile(d, 2.5)), float(np.percentile(d, 97.5))


def single(suite, cal, evaluators, ev_id):
    fixed = dict(cal, routes={q.name: [ev_id] for q in suite.questions})
    return Engine(suite, fixed, evaluators)


def evaluate(name, suite, calib_convs, tests, client, rows, md):
    cal, evaluators = calibrate(suite, calib_convs, labels_of(calib_convs), client)
    (OUT / f"calibration-{name}.json").write_text(json.dumps(cal, indent=1, default=str))
    qs = [q.name for q in suite.questions]
    md.append(f"\n### {name}: calibrated on {len(calib_convs)} labelled conversations\n")
    md.append(tabulate([[q, " → ".join(cal["routes"][q]), f"{100 * cal['report'][q]['chosen']['accuracy']:.1f}",
                         f"{100 * cal['report'][q]['best_accuracy']:.1f}",
                         "yes" if cal["report"][q]["meets_target"] else "**no**"] for q in qs],
                       ["question", "chosen route", "dev acc %", "best dev acc %", "meets target"], tablefmt="github"))
    for test_name, convs in tests.items():
        gold = labels_of(convs)
        eng = summarize(Engine(suite, cal, evaluators).run(convs), gold, qs)
        table = [["**engine**", *[f"{100 * eng['acc'][q]:.1f}" for q in qs], f"**{100 * eng['mean_acc']:.1f}**", "",
                  f"{eng['usd_per_1M_conv']:.1f}", f"{eng['ms_per_conv']:.0f}", f"{100 * eng['llm_share']:.0f}%"]]
        baselines = {}
        for ev in evaluators:
            if ev.type == "local_head" and not ev.heads:
                continue
            b = summarize(single(suite, cal, evaluators, ev.id).run(convs), gold, qs)
            baselines[ev.id] = b
            lo, hi = bootstrap_diff(eng["per_conv"], b["per_conv"])
            table.append([ev.id, *[f"{100 * b['acc'][q]:.1f}" for q in qs], f"{100 * b['mean_acc']:.1f}",
                          f"{100 * lo:+.1f} … {100 * hi:+.1f}", f"{b['usd_per_1M_conv']:.1f}",
                          f"{b['ms_per_conv']:.0f}", f"{100 * b['llm_share']:.0f}%"])
        md.append(f"\n**{test_name}** ({len(convs)} conversations)\n")
        md.append(tabulate(table, ["", *qs, "mean %", "engine − this (95% CI, points)", "$ / 1M conv", "ms / conv",
                                   "conv. sent to an LLM"], tablefmt="github"))
        rows.append({"run": name, "test": test_name, "engine": {k: v for k, v in eng.items() if k != "per_conv"},
                     "baselines": {k: {kk: vv for kk, vv in v.items() if kk != "per_conv"} for k, v in baselines.items()},
                     "routes": cal["routes"]})


def main():
    load_env()
    a = load("synthetic.jsonl")
    for i, c in enumerate(a):
        c["split"] = "dev" if i % 5 in (0, 1) else "test"
    b = load("synthetic_b.jsonl")
    a_dev, a_test = [c for c in a if c["split"] == "dev"], [c for c in a if c["split"] == "test"]
    b_cal, b_rest = b[:100], b[100:]
    client = OpenRouter(concurrency=8)
    rows, md = [], ["# Engine evaluation\n", f"Suite: `{SUITE.name}`. Targets and tolerance from the suite policy."]

    hosted = Suite.load(SUITE)
    evaluate("hosted", hosted, a_dev, {"A-test": a_test, "B (unseen generator)": b}, client, rows, md)
    evaluate("hosted + 100 B labels", hosted, a_dev + b_cal, {"B-rest": b_rest}, client, rows, md)
    local = Suite.load(SUITE)
    local.policy.allow_hosted = False
    evaluate("local-only", local, a_dev, {"A-test": a_test, "B (unseen generator)": b}, client, rows, md)
    evaluate("local-only + 100 B labels", local, a_dev + b_cal, {"B-rest": b_rest}, client, rows, md)

    md.append(f"\n\nTotal OpenRouter spend so far: ${spent_usd():.2f}")
    (OUT / "ENGINE.md").write_text("\n".join(md) + "\n")
    (OUT / "engine.json").write_text(json.dumps(rows, indent=1, default=float))
    print("\n".join(md))


if __name__ == "__main__":
    main()
