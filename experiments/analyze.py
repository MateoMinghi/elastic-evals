"""Analyse stored layer outputs: single layers, layer combinations (cascades), distillation, es vs en.

Cascade rule: layers run in order. A non-final layer decides only when it is confident
(binary: p <= lo or p >= hi; multiclass: max p >= tau); otherwise the item escalates.
The final enabled layer always decides. lo/hi/tau are tuned on the DEV split only, as the
largest coverage whose dev accuracy on confident items is >= TARGET. Metrics are on TEST.
"""

import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, cohen_kappa_score, f1_score
from tabulate import tabulate

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_layers import PREDS, load, synthetic_items  # noqa: E402

RESULTS = Path(__file__).resolve().parent / "results"
TARGET = 0.95
LLM = "llm_ds"


# ------------------------------------------------------------------ loading

def truth():
    gold = {}
    for it in synthetic_items():
        for q, v in it["labels"].items():
            gold[(it["id"], q)] = v
    for task in ["massive", "sentiment"]:
        for it in load(task):
            gold[(it["id"], task)] = it["label"]
    for it in load("injection"):
        gold[(it["id"], "injection")] = int(it["label"])
    return gold


def preds(task):
    path = PREDS / f"{task}.jsonl"
    rows = [json.loads(line) for line in path.open()] if path.exists() else []
    table = defaultdict(dict)  # (question) -> evaluator -> id -> row
    for r in rows:
        table[r["question"]].setdefault(r["evaluator"], {})[r["id"]] = r
    return table


# ------------------------------------------------------------------ decisions

def is_multi(p):
    return isinstance(p, dict)


def decide(p):
    if p is None:
        return None
    if is_multi(p):
        return max(p, key=p.get) if p else None
    return int(p >= 0.5)


def confident(p, th):
    if p is None:
        return False
    if is_multi(p):
        return bool(p) and max(p.values()) >= th
    lo, hi = th
    return p <= lo or p >= hi


def tune(rows, gold, multi):
    """Pick the threshold with max dev coverage whose confident-subset accuracy >= TARGET."""
    grid = ([round(x, 2) for x in np.arange(0.3, 1.0001, 0.05)] if multi else
            [(round(lo, 2), round(hi, 2)) for lo in np.arange(0, 0.51, 0.05) for hi in np.arange(0.5, 1.0001, 0.05)])
    best, best_key = None, (-1, -1)
    for th in grid:
        conf = [r for r in rows if confident(r["p"], th)]
        if not conf:
            continue
        acc = np.mean([decide(r["p"]) == gold[(r["id"], r["question"])] for r in conf])
        key = (len(conf) / len(rows), acc)
        if acc >= TARGET and key > best_key:
            best, best_key = th, key
    return best  # None -> this layer is never trusted to stop the cascade


def cascade(layers, table, ids, gold, question, thresholds):
    """Return per-item (decision, cost, latency, layer_used)."""
    out = {}
    for i in ids:
        cost = lat = 0.0
        dec, used = None, None
        for k, layer in enumerate(layers):
            r = table[layer].get(i)
            if r is None:
                continue
            cost += r["cost_usd"] or 0
            lat += r["latency_s"] or 0
            last = k == len(layers) - 1
            th = thresholds.get(layer)
            if last or (th is not None and confident(r["p"], th)):
                dec, used = decide(r["p"]), layer
                break
        out[i] = (dec, cost, lat, used)
    return out


def scores(ids, dec, gold, question, multi):
    y = [gold[(i, question)] for i in ids]
    fallback = "__invalid__" if multi else 0
    p = [dec[i] if dec[i] is not None else fallback for i in ids]
    avg = "macro" if multi else "binary"
    return {
        "n": len(ids),
        "acc": accuracy_score(y, p),
        "f1": f1_score(y, p, average=avg, zero_division=0),
        "kappa": cohen_kappa_score(y, p),
    }


# ------------------------------------------------------------------ distillation

def distill(task, table):
    """Train e5 + logistic regression on LLM labels of the unlabelled pool; add as evaluator 'e5_distilled'."""
    items = {it["id"]: it for it in load(task)}
    rows = {}
    for lang in ["es", "en"]:
        x = np.load(PREDS / f"{task}-{lang}-e5.npy")
        ids = json.load((PREDS / f"{task}-{lang}-e5.ids.json").open())
        llm = table[task].get(LLM, {})
        train = [(k, decide(llm[i]["p"])) for k, i in enumerate(ids)
                 if items[i]["split"] == "pool" and i in llm and decide(llm[i]["p"])]
        clf = LogisticRegression(max_iter=2000, class_weight="balanced", C=4.0)
        clf.fit(x[[k for k, _ in train]], [lab for _, lab in train])
        proba = clf.predict_proba(x)
        lat = next(iter(table[task]["e5_logreg"].values()))["latency_s"]
        for k, i in enumerate(ids):
            if items[i]["split"] != "pool":
                rows[i] = {"id": i, "lang": lang, "split": items[i]["split"], "question": task,
                           "evaluator": "e5_distilled", "p": dict(zip(clf.classes_, map(float, proba[k]))),
                           "latency_s": lat, "cost_usd": 0.0}
    table[task]["e5_distilled"] = rows


# ------------------------------------------------------------------ report

LAYER_NAMES = {"rules": "rules", "jev": "Jev", "e5_logreg": "e5+logreg", "nli_zeroshot": "mDeBERTa zero-shot",
               "pi_deberta": "ProtectAI injection", "llm_ds": "LLM DeepSeek-V4.1-Flash", "llm_nano": "LLM GPT-4.1-nano",
               "e5_logreg_goldpool": "e5+logreg (600 human labels)", "e5_distilled": "e5+logreg distilled from LLM"}


def fmt(x, pct=True):
    return f"{100 * x:.1f}" if pct else f"{x:.2f}"


def main():
    RESULTS.mkdir(exist_ok=True)
    gold = truth()
    tasks = {"synthetic": None, "massive": None, "sentiment": None, "injection": None}
    tables = {t: preds(t) for t in tasks}
    for t in ["massive", "sentiment"]:
        if tables[t].get(t, {}).get(LLM):
            distill(t, tables[t])

    md, metrics = [], {"target_confident_accuracy": TARGET, "single": [], "cascade": [], "by_lang": []}
    single_rows, casc_rows, lang_rows = [], [], []
    for task, qtable in tables.items():
        for question, table in qtable.items():
            any_rows = next(iter(table.values()))
            multi = is_multi(next(iter(any_rows.values()))["p"]) if any_rows else False
            test_ids = sorted({i for ev in table.values() for i, r in ev.items() if r["split"] == "test"})
            # --- single layers
            for ev, rows in table.items():
                ids = [i for i in test_ids if i in rows]
                if not ids:
                    continue
                dec = {i: decide(rows[i]["p"]) for i in ids}
                s = scores(ids, dec, gold, question, multi)
                cost = 1000 * np.mean([rows[i]["cost_usd"] or 0 for i in ids])
                lat = 1000 * np.mean([rows[i]["latency_s"] or 0 for i in ids])
                metrics["single"].append({"task": task, "question": question, "evaluator": ev, **s,
                                          "usd_per_1k": cost, "ms_per_item": lat})
                single_rows.append([question, LAYER_NAMES.get(ev, ev), s["n"], fmt(s["acc"]), fmt(s["f1"]),
                                    fmt(s["kappa"], False), f"{cost:.4f}", f"{lat:.0f}"])
                for lang in ["es", "en", "mixed"]:
                    lids = [i for i in ids if rows[i]["lang"] == lang]
                    if lids and lang != "mixed":
                        ls = scores(lids, dec, gold, question, multi)
                        metrics["by_lang"].append({"question": question, "evaluator": ev, "lang": lang, **ls})
            # --- cascades
            classifiers = [c for c in ["jev", "e5_logreg", "e5_distilled", "pi_deberta"] if c in table]
            thresholds = {}
            for ev, rows in table.items():
                dev = [r for r in rows.values() if r["split"] == "dev"]
                thresholds[ev] = tune(dev, gold, multi) if dev else None
            stacks = []
            for c in classifiers:
                stacks += [[c], [c, LLM]]
                if "rules" in table:
                    stacks += [["rules", c], ["rules", c, LLM]]
            stacks += [[LLM]] + ([["rules"], ["rules", LLM]] if "rules" in table else [])
            seen = set()
            for layers in stacks:
                if tuple(layers) in seen or any(layer not in table for layer in layers):
                    continue
                seen.add(tuple(layers))
                ids = [i for i in test_ids if all(i in table[layer] for layer in layers)]
                res = cascade(layers, table, ids, gold, question, thresholds)
                s = scores(ids, {i: res[i][0] for i in ids}, gold, question, multi)
                share = {layer: np.mean([res[i][3] == layer for i in ids]) for layer in layers}
                cost = 1000 * np.mean([res[i][1] for i in ids])
                lat = 1000 * np.mean([res[i][2] for i in ids])
                name = " → ".join(LAYER_NAMES.get(layer, layer) for layer in layers)
                metrics["cascade"].append({"task": task, "question": question, "layers": layers, **s,
                                           "decided_by": share, "usd_per_1k": cost, "ms_per_item": lat,
                                           "thresholds": {layer: thresholds.get(layer) for layer in layers}})
                casc_rows.append([question, name, s["n"], fmt(s["acc"]), fmt(s["f1"]), fmt(s["kappa"], False),
                                  " / ".join(f"{100 * share[layer]:.0f}%" for layer in layers),
                                  f"{cost:.4f}", f"{lat:.0f}"])
            # --- es vs en for the main evaluators
            for ev in ["rules", "jev", "e5_logreg", "llm_ds"]:
                byl = {m["lang"]: m for m in metrics["by_lang"] if m["question"] == question and m["evaluator"] == ev}
                if "es" in byl and "en" in byl:
                    lang_rows.append([question, LAYER_NAMES[ev], fmt(byl["es"]["acc"]), fmt(byl["en"]["acc"]),
                                      f"{100 * (byl['es']['acc'] - byl['en']['acc']):+.1f}"])
            # --- judge agreement
            if "llm_ds" in table and "llm_nano" in table:
                ids = [i for i in test_ids if i in table["llm_ds"] and i in table["llm_nano"]]
                a = [str(decide(table["llm_ds"][i]["p"])) for i in ids]
                b = [str(decide(table["llm_nano"][i]["p"])) for i in ids]
                metrics.setdefault("judge_agreement", {})[question] = cohen_kappa_score(a, b)

    json.dump(metrics, (RESULTS / "metrics.json").open("w"), indent=1, default=float)
    hdr = ["question", "evaluator", "n", "acc %", "F1 %", "kappa", "$ / 1k items", "ms / item"]
    md.append("## Single layers (test split)\n")
    md.append(tabulate(single_rows, hdr, tablefmt="github"))
    md.append("\n\n## Layer combinations (test split)\n")
    md.append(f"Non-final layers stop the cascade only when confident; thresholds tuned on dev for "
              f"≥{int(TARGET * 100)}% accuracy on confident items. 'decided by' = share of items each layer decided.\n")
    md.append(tabulate(casc_rows, ["question", "layers", "n", "acc %", "F1 %", "kappa", "decided by", "$ / 1k",
                                   "ms / item"], tablefmt="github"))
    md.append("\n\n## Spanish vs English accuracy (test split)\n")
    md.append(tabulate(lang_rows, ["question", "evaluator", "es %", "en %", "es − en"], tablefmt="github"))
    if metrics.get("judge_agreement"):
        md.append("\n\n## Agreement between the two LLM judges (Cohen's kappa)\n")
        md.append(tabulate([[q, fmt(k, False)] for q, k in metrics["judge_agreement"].items()],
                           ["question", "kappa"], tablefmt="github"))
    (RESULTS / "tables.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))


if __name__ == "__main__":
    main()
