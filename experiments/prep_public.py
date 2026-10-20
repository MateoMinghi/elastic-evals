"""Download public, human-labelled datasets and build dev/test/pool splits.

  massive    MASSIVE scenario classification (18 classes), es + en   -> intent routing
  sentiment  tweet_sentiment_multilingual (neg/neu/pos), es + en      -> frustration proxy
  injection  deepset/prompt-injections (binary, mostly en, some de)   -> prompt injection

dev  = small labelled set (what a team would hand-label): trains classifiers, tunes thresholds
test = held-out evaluation set
pool = unlabelled-in-practice items, used for the distillation experiment (LLM labels them)
"""

import io
import json
from pathlib import Path

import httpx
import pandas as pd

OUT = Path(__file__).resolve().parent / "data"
HF = "https://huggingface.co/api/datasets/{repo}/parquet/{config}/{split}/0.parquet"
SEED = 7


def parquet(repo, config, split) -> pd.DataFrame:
    url = HF.format(repo=repo, config=config, split=split)
    res = httpx.get(url, follow_redirects=True, timeout=120)
    res.raise_for_status()
    return pd.read_parquet(io.BytesIO(res.content))


def raw_jsonl_gz(repo, path) -> pd.DataFrame:
    url = f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"
    res = httpx.get(url, follow_redirects=True, timeout=120)
    res.raise_for_status()
    return pd.read_json(io.BytesIO(res.content), lines=True, compression="gzip")


def per_class(df, n, label="label"):
    return pd.concat(g.sample(min(n, len(g)), random_state=SEED) for _, g in df.groupby(label))


def write(name, rows):
    path = OUT / f"{name}.jsonl"
    with path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"{name}: {len(rows)}")


def records(df, task, lang, split, text_col, label_col):
    return [
        {"id": f"{task}-{lang}-{split}-{i}", "task": task, "lang": lang, "split": split,
         "text": str(r[text_col]), "label": str(r[label_col])}
        for i, r in df.reset_index(drop=True).iterrows()
    ]


def main():
    OUT.mkdir(parents=True, exist_ok=True)

    rows = []
    for lang in ["es", "en"]:
        # the auto-converted parquet for this repo is refused by the CDN; read the raw files
        tr = raw_jsonl_gz("mteb/amazon_massive_scenario", f"train/{lang}.json.gz")
        te = raw_jsonl_gz("mteb/amazon_massive_scenario", f"test/{lang}.json.gz")
        lab = "label_text" if "label_text" in tr.columns else "label"
        dev = per_class(tr, 8, lab)
        pool = tr.drop(dev.index).sample(600, random_state=SEED)
        test = te.sample(300, random_state=SEED)
        rows += records(dev, "massive", lang, "dev", "text", lab)
        rows += records(pool, "massive", lang, "pool", "text", lab)
        rows += records(test, "massive", lang, "test", "text", lab)
    write("massive", rows)

    rows = []
    names = {0: "negative", 1: "neutral", 2: "positive"}
    for lang, cfg in [("es", "spanish"), ("en", "english")]:
        tr = parquet("cardiffnlp/tweet_sentiment_multilingual", cfg, "train")
        te = parquet("cardiffnlp/tweet_sentiment_multilingual", cfg, "test")
        for df in (tr, te):
            df["label_text"] = df["label"].map(names)
        dev = per_class(tr, 16, "label_text")
        pool = tr.drop(dev.index).sample(min(600, len(tr) - len(dev)), random_state=SEED)
        test = te.sample(300, random_state=SEED)
        rows += records(dev, "sentiment", lang, "dev", "text", "label_text")
        rows += records(pool, "sentiment", lang, "pool", "text", "label_text")
        rows += records(test, "sentiment", lang, "test", "text", "label_text")
    write("sentiment", rows)

    tr = parquet("deepset/prompt-injections", "default", "train")
    te = parquet("deepset/prompt-injections", "default", "test")
    dev = per_class(tr, 60, "label")
    rows = records(dev, "injection", "mixed", "dev", "text", "label")
    rows += records(te, "injection", "mixed", "test", "text", "label")
    write("injection", rows)


if __name__ == "__main__":
    main()
