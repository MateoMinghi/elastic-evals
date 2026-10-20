# Layered-evaluation experiments

These experiments test the cascade from `docs/PROPOSAL.md` §7 on real models:
1. **rules:** lexicons, record checks, trace structure;
2. **classifiers:** Jev (hosted), or small local models on CPU;
3. **LLM judges.**

Results are in [`results/RESULTS.md`](results/RESULTS.md).

## Run

```bash
python3 -m venv .venv
.venv/bin/pip install --index-url https://download.pytorch.org/whl/cpu torch
.venv/bin/pip install sentence-transformers transformers setfit datasets scikit-learn pandas httpx snowballstemmer unidecode tabulate

# the OpenRouter key lives in an env file OUTSIDE the repo
export EVAL_ENV_FILE=/path/outside/repo/.env      # contains OPENROUTER_API_KEY=...
export EVAL_BUDGET_USD=5                          # hard cap on paid calls (default 5)

.venv/bin/python prep_public.py        # MASSIVE, tweet sentiment, prompt injections -> data/
.venv/bin/python gen_synthetic.py      # synthetic banking conversations with planted labels
.venv/bin/python run_layers.py         # every layer on every item -> data/preds/
.venv/bin/python analyze.py            # single layers, cascades, distillation -> results/
```

Every OpenRouter response is cached in `.cache/`, and spend is recorded in `.cache/ledger.jsonl`, so re-runs are free.

Options:
- `EVAL_OFFLINE=1` uses only cached responses.
- `run_layers.py --local-only` skips all paid calls.

## Files

| File | What it does |
|---|---|
| `gen_synthetic.py` | Builds a domain record in code and asks an LLM to write a conversation with planted problems. The labels come from the spec; the tool-loop label is recomputed from the generated tool calls. |
| `prep_public.py` | Downloads public, human-labelled datasets and builds dev / test / pool splits. |
| `lib/rules.py` | Layer 1: normalisation in the style of OpenSearch analyzers, lexicons, record checks, trace-structure checks. |
| `lib/tasks.py` | The question wording shared by Jev and the LLM judges, plus the per-question text views used by local models. |
| `run_layers.py` | Runs rules, Jev, e5+logreg, mDeBERTa zero-shot, the ProtectAI injection model and two LLM judges. |
| `analyze.py` | Scores single layers, all layer combinations (thresholds tuned on dev), distillation, and Spanish vs English. |
