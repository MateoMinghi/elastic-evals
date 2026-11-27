"""Measure the redaction analyzer per language and entity type, on labelled text.

The gateway drops or masks content on the strength of a Presidio analyzer whose quality differs a lot
by language (a small spaCy model finds far fewer Spanish names than English ones). Nothing in the
pipeline measured that. This does, against any analyzer that speaks Presidio's REST contract.

    python -m elastic_evals measure-redaction --data labelled_text.jsonl --endpoint http://localhost:3000 \\
        --min-precision 0.8 --min-recall 0.8 [--out report.json]

Each line of `--data`: {"text": "...", "lang": "es", "entities": [{"type": "PERSON", "start": 5, "end": 12}]}
(offsets in Unicode code points, as Presidio reports them). An analyzer span is a hit when it has the
same type as a gold span and overlaps it by IoU >= 0.5.

Precision answers "of what it masked, how much was really that entity" (low: it mangles ordinary text,
so scores get worse); recall answers "of the real entities, how many it found" (low: personal data
leaks into storage). Both are reported with a 95% interval, and a cell with fewer than MIN_N gold
entities is marked unreliable rather than trusted. Exit status 1 when a reliable cell is below a threshold.
"""

import json
from collections import defaultdict
from pathlib import Path

import httpx

from .stats import MIN_N, wilson


def _iou(a, b):
    inter = max(0, min(a[1], b[1]) - max(a[0], b[0]))
    union = (a[1] - a[0]) + (b[1] - b[0]) - inter
    return inter / union if union else 0.0


def match(gold: list[dict], pred: list[dict], min_iou: float = 0.5):
    """Greedy one-to-one matching of same-type spans by IoU. Returns (tp pairs, unmatched pred, unmatched gold)."""
    used, pairs = set(), []
    for pi, p in sorted(enumerate(pred), key=lambda x: -(x[1].get("score") or 0)):
        best, best_iou = None, min_iou
        for gi, g in enumerate(gold):
            if gi in used or g["type"] != p["type"]:
                continue
            v = _iou((g["start"], g["end"]), (p["start"], p["end"]))
            if v >= best_iou:
                best, best_iou = gi, v
        if best is not None:
            used.add(best)
            pairs.append((best, pi))
    matched_pred = {pi for _, pi in pairs}
    return (pairs, [p for i, p in enumerate(pred) if i not in matched_pred],
            [g for i, g in enumerate(gold) if i not in used])


def analyzer_over_http(endpoint: str, entities=None, score_threshold=0.5, timeout=30.0):
    client = httpx.Client(base_url=endpoint.rstrip("/"), timeout=timeout)

    def analyze(text, lang):
        body = {"text": text, "language": lang, "score_threshold": score_threshold}
        if entities:
            body["entities"] = entities
        r = client.post("/analyze", json=body)
        r.raise_for_status()
        return [{"type": e["entity_type"], "start": e["start"], "end": e["end"], "score": e.get("score")} for e in r.json()]
    return analyze


def measure(rows: list[dict], analyze, min_iou: float = 0.5) -> dict:
    """Per (language, entity type) and per language overall: gold, predicted, hits, precision, recall, F1, intervals."""
    cells = defaultdict(lambda: {"gold": 0, "pred": 0, "tp": 0})
    errors = 0
    for row in rows:
        lang, gold = row.get("lang", "und"), row.get("entities") or []
        try:
            pred = analyze(row["text"], lang)
        except Exception:  # noqa: BLE001 - a failing call counts as nothing found, and is reported
            pred, errors = [], errors + 1
        pairs, extra, missed = match(gold, pred, min_iou)
        types = {g["type"] for g in gold} | {p["type"] for p in pred}
        for t in types:
            for key in ((lang, t), (lang, "ALL")):
                c = cells[key]
                c["gold"] += sum(g["type"] == t for g in gold)
                c["pred"] += sum(p["type"] == t for p in pred)
                c["tp"] += sum(gold[gi]["type"] == t for gi, _ in pairs)
    out = {}
    for (lang, t), c in sorted(cells.items()):
        p = c["tp"] / c["pred"] if c["pred"] else None
        r = c["tp"] / c["gold"] if c["gold"] else None
        f1 = 2 * p * r / (p + r) if p and r else (0.0 if c["gold"] and c["pred"] else None)
        out.setdefault(lang, {})[t] = {
            **c, "precision": p, "recall": r, "f1": f1,
            "precision_ci": wilson(c["tp"], c["pred"]), "recall_ci": wilson(c["tp"], c["gold"]),
            "reliable": c["gold"] >= MIN_N}
    return {"languages": out, "analyzer_errors": errors, "n_texts": len(rows)}


def verdict(report: dict, min_precision: float, min_recall: float) -> list[dict]:
    """Reliable cells below a threshold. The upper end of the interval must also miss it: a cell is
    only failed when it is unlikely to be fine, so a lucky-bad small sample does not block anything."""
    bad = []
    for lang, types in report["languages"].items():
        for t, c in types.items():
            if not c["reliable"]:
                continue
            for metric, floor in (("precision", min_precision), ("recall", min_recall)):
                ci = c[f"{metric}_ci"]
                if c[metric] is not None and ci and ci[1] < floor:
                    bad.append({"lang": lang, "entity": t, "metric": metric, "value": c[metric], "floor": floor})
    return bad


def format_report(report: dict) -> str:
    from tabulate import tabulate
    rows = []
    for lang, types in report["languages"].items():
        for t, c in sorted(types.items(), key=lambda kv: (kv[0] != "ALL", kv[0])):
            def f(v, ci):
                return "–" if v is None else f"{100 * v:.0f}% [{100 * ci[0]:.0f}-{100 * ci[1]:.0f}]"
            rows.append([lang, t, c["gold"], c["pred"], f(c["precision"], c["precision_ci"]) if c["pred"] else "–",
                         f(c["recall"], c["recall_ci"]) if c["gold"] else "–", "" if c["reliable"] else "too few"])
    return tabulate(rows, ["lang", "entity", "gold", "found", "precision [95%]", "recall [95%]", f"n < {MIN_N}?"],
                    tablefmt="github")


def run(args) -> int:
    import logging
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one INFO line per text drowns the report
    rows = [json.loads(line) for line in Path(args.data).open() if line.strip()]
    analyze = analyzer_over_http(args.endpoint, args.entities.split(",") if args.entities else None, args.score_threshold)
    report = measure(rows, analyze)
    print(format_report(report))
    if report["analyzer_errors"]:
        print(f"\n{report['analyzer_errors']} of {report['n_texts']} texts could not be analyzed (counted as nothing found)")
    bad = verdict(report, args.min_precision, args.min_recall)
    for b in bad:
        print(f"FAIL {b['lang']} {b['entity']}: {b['metric']} {100 * b['value']:.0f}% is below {100 * b['floor']:.0f}%")
    if args.out:
        Path(args.out).write_text(json.dumps({**report, "failures": bad}, indent=2))
    return 1 if bad else 0
