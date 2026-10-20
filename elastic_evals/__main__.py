"""CLI.

  python -m elastic_evals calibrate --suite suite.yaml --data labelled.jsonl --out calib/
  python -m elastic_evals run       --suite suite.yaml --calib calib/ --data convs.jsonl --out scores.jsonl
  python -m elastic_evals report    --calib calib/

Labelled conversations carry "labels": {question: 0|1}. Add --local-only to forbid hosted
evaluators (no conversation text leaves the machine). OpenRouter calls need OPENROUTER_API_KEY
(or EVAL_ENV_FILE pointing at an env file outside the repository).
"""

import argparse
import json
import pickle
import sys
from pathlib import Path

from tabulate import tabulate

from .calibrate import calibrate
from .engine import Engine
from .evaluators import LocalHeadEvaluator, build
from .providers import OpenRouter, load_env
from .suite import Suite


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).open() if line.strip()]


def print_report(cal):
    sc = cal["suite_choice"]
    print(f"calibration {cal.get('id', '?')} ({cal.get('status', '?')}) on {cal['n_labelled']} labelled conversations; "
          f"hosted set: {', '.join(sc['hosted']) or 'none'}; ${1e6 * sc['usd_per_conv']:.1f} / 1M conv, "
          f"{sc['ms_per_conv']:.0f} ms / conv" + ("" if cal.get("budget_met", True) else "  ** OVER BUDGET **"))
    langs = sorted({lg for r in cal["report"].values() for lg in r["chosen"].get("by_lang", {})})
    rows = []
    for q, r in cal["report"].items():
        by_lang = r["chosen"].get("by_lang", {})
        rows.append([q, " → ".join(cal["routes"][q]), f"{100 * r['chosen']['accuracy']:.1f}",
                     f"{100 * r['floor']:.1f}", f"{100 * r['best_accuracy']:.1f}",
                     *[f"{100 * by_lang[lg]['accuracy']:.1f}" if lg in by_lang else "" for lg in langs],
                     f"{r['chosen']['usd_per_conv'] * 1e6:.1f}", f"{r['chosen']['ms_per_conv']:.0f}",
                     "yes" if r.get("meets_floor", True) else "NO", "yes" if r["meets_target"] else "NO"])
    print(tabulate(rows, ["question", "route", "acc %", "floor %", "best %", *[f"{lg} %" for lg in langs],
                          "$ / 1M conv", "ms", "meets floor", "meets target"], tablefmt="github"))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="elastic_evals")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("calibrate")
    c.add_argument("--suite", required=True)
    c.add_argument("--data", required=True)
    c.add_argument("--out", required=True)
    c.add_argument("--local-only", action="store_true")
    r = sub.add_parser("run")
    r.add_argument("--suite", required=True)
    r.add_argument("--calib", required=True)
    r.add_argument("--data", required=True)
    r.add_argument("--out", required=True)
    p = sub.add_parser("report")
    p.add_argument("--calib", required=True)
    args = ap.parse_args(argv)
    load_env()

    if args.cmd == "report":
        print_report(json.loads((Path(args.calib) / "calibration.json").read_text()))
        return

    suite = Suite.load(args.suite)
    if args.cmd == "calibrate":
        suite.policy.allow_hosted = not args.local_only
        convs = read_jsonl(args.data)
        labels = {(cv["id"], q): v for cv in convs for q, v in cv["labels"].items()}
        cal, evaluators = calibrate(suite, convs, labels, OpenRouter() if suite.policy.allow_hosted else None)
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "calibration.json").write_text(json.dumps(cal, indent=1, default=str))
        heads = {e.id: e.heads for e in evaluators if isinstance(e, LocalHeadEvaluator)}
        (out / "heads.pkl").write_bytes(pickle.dumps(heads))
        print_report(cal)
        return

    cal = json.loads((Path(args.calib) / "calibration.json").read_text())
    suite.policy.allow_hosted = cal["policy"]["allow_hosted"]
    evaluators = build(suite, OpenRouter() if suite.policy.allow_hosted else None)
    heads = pickle.loads((Path(args.calib) / "heads.pkl").read_bytes())
    for e in evaluators:
        if isinstance(e, LocalHeadEvaluator):
            e.heads = heads.get(e.id, {})
    results = Engine(suite, cal, evaluators).run(read_jsonl(args.data))
    with Path(args.out).open("w") as f:
        for res in results:
            for s in res["scores"]:
                f.write(json.dumps(s, ensure_ascii=False) + "\n")
    n = len(results)
    print(f"scored {n} conversations: ${sum(x['usd'] for x in results):.6f} "
          f"(+${sum(x['audit_usd'] for x in results):.6f} audit), "
          f"{sum(x['ms'] for x in results) / max(1, n):.0f} ms/conv, "
          f"{sum(any(e.startswith('llm') for e in x['evaluators_called']) for x in results)} sent to an LLM")


if __name__ == "__main__":
    sys.exit(main())
