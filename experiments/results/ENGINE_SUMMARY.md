# The engine: does the thesis hold?

**Yes, with the design changes below.** `elastic_evals` was calibrated automatically on 158 labelled conversations and then tested on conversations from a model family it had never seen. It:
- matched the best single LLM judge on accuracy;
- cost 34–39% less than the DeepSeek or Haiku judge alone (about the same as GPT-6 Luna alone, but 3.7× faster);
- ran 2–4× faster;
- sent only about a third of conversations to an LLM;
- flagged the question where it was weakest with its own drift monitor.

Full tables: [`ENGINE.md`](ENGINE.md). Raw numbers: [`engine.json`](engine.json). Total OpenRouter spend for the whole study: **$1.55**.

## Setup

- **Suite:** [`examples/banking_suite.yaml`](../../examples/banking_suite.yaml). Five checks on banking-assistant conversations:
  - false confirmation (critical)
  - missing AI disclosure
  - fee mismatch (critical)
  - frustrated user
  - tool loop
- **Evaluators available to the engine:**
  - rules
  - multilingual-e5-small + logistic heads (CPU)
  - Jev 1.14
  - LLM judges: DeepSeek V4.1 Flash, GPT-6 Luna, Claude Haiku 5.5
- **Calibration:** 158 labelled conversations from set A, written by Claude Haiku 5.5. The engine chooses routes, thresholds and the hosted evaluators by itself. No hand tuning.
- **Test sets.** Labels are planted by construction; sets B and C are messier on purpose (typos, late disclosures, fees written as words, hard negatives).

| Set | Generator | Size | Role |
|---|---|---|---|
| A-test | Claude Haiku 5.5 | 237 | Same generator as the calibration data |
| B | Gemini 3.1 Flash Lite | 300 | Different model family. Used while iterating, so no longer a clean test |
| C | Qwen 3.8 Flash | 300 | **Final holdout.** Generated after the last design change, scored exactly once |

## Results (mean accuracy over the 5 checks)

| | A-test | B | **C (holdout)** | $ / 1M conv. (C) | ms / conv. (C) | sent to an LLM (C) |
|---|---|---|---|---|---|---|
| **engine** | **98.3** | 94.1 | **93.5** | 83 | 564 | 36% |
| DeepSeek V4.1 Flash | 96.8 | **95.3** | 92.8 | 135 | 1211 | 100% |
| GPT-6 Luna | 95.5 | 94.8 | 93.1 | 81 | 2064 | 100% |
| Claude Haiku 5.5 | 96.3 | 94.1 | 92.8 | 125 | 920 | 100% |
| Jev 1.14 | 95.7 | 93.3 | 92.3 | 37 | 198 | 0% |
| rules | 84.8 | 83.9 | 85.0 | 0 | 2 | 0% |
| e5 + logistic heads | 82.1 | 76.3 | 74.9 | 0 | ~180 | 0% |
| **engine, local-only** | 94.3 | 88.9 | 87.3 | 0 | 49 | 0% |

**How the engine compared, with bootstrap 95% intervals:**

| Set | Against the LLM judges | Against Jev | Against rules / e5 |
|---|---|---|---|
| A-test | Significantly better than every one | Significantly better | Significantly better |
| B | Statistically tied (DeepSeek: −2.5 … +0.2 points) | Tied | Significantly better |
| **C (holdout)** | Statistically tied, with the highest point estimate | **Significantly better** (+0.1 … +2.5) | Significantly better |

## What the engine chose by itself

From the 158 labelled conversations, calibration picked **Jev + Claude Haiku 5.5** as the suite's hosted evaluators. Its dev estimate was $72 per 1M conversations at 639 ms, against $125 and 1081 ms for Haiku alone. The routes:

| check | route | how it plays out |
|---|---|---|
| false confirmation | rules → e5 → Jev → Haiku | rules settle the record-checkable cases, the small model takes clear cases, Jev most of the rest, Haiku about 13% |
| missing disclosure | rules | |
| fee mismatch | Jev → Haiku | Haiku on about 2% |
| frustrated user | e5 → Jev → Haiku | |
| tool loop | rules | LLMs are bad at counting repeated calls (Luna 87%, Haiku 95%); the rule is exact |

The **local-only** policy (no data leaves the deployment) gets a calibration report that names the 3 checks that can't reach 95% without hosted models: false confirmation, fee mismatch, frustration. That's a deployment decision a bank can make knowingly.

## What had to change to get here (iteration log)

1. **v1 picked the best route per question independently.** Each question ended up with a different LLM (Haiku for one, Luna for another, Jev for a third). Every conversation then paid for three hosted calls: $212 per 1M and 3.4 s, worse than any single judge.
   - **Fix:** choose the hosted evaluators *jointly for the suite*, and cost them per conversation, because one call answers every question.
2. **"Critical means exactly the best accuracy" chased noise** (99.4% vs 98.7% on 158 items).
   - **Fix:** a question's floor is the best accuracy minus one smoothed standard error, so noise-level gaps don't decide routes.
3. **Cheap layers were trusted at a fixed 95% confidence target**, so any cascade fell below the floor of a critical question and calibration fell back to "LLM on everything".
   - **Fix:** each chain may require stricter confidence from its cheap layers (97%, 99%, 100%). Rules then settle only what they're certain of. For false confirmation that's mainly the record check.
4. **The drift monitor caught a real bug on set B.** The disclosure rule only read the first two assistant messages, and audit agreement dropped from 100% to 81%.
   - **Fix:** scan every message. Disclosure on B went from 80% to 96%.
   - Recalibrating with 100 labelled B conversations also recovered B (97.1% on the rest of B in v1).

## What this does not prove yet

- **The conversations are synthetic** and the labels come from the generation spec. Some "errors" on B and C may be generator mistakes. Fee mismatch on C is about 93% for *every* evaluator, which hints at label noise or genuinely ambiguous items.
- **Frustration is the weak check everywhere** on messy data (77–86% for every evaluator), and the drift monitor flags it.

   It's subjective. The fix is human labels from real traffic, not a better router.
- **Recalibrating on mixed data chose GPT-6 Luna alone** (one question short of its floor): the best accuracy on B-rest (96.6%), but 2.1 s per conversation.

   The policy needs a latency budget alongside accuracy and cost.
- **Costs are October 2026 OpenRouter prices.** Local-model latency was measured on a shared 4-core CPU.

## Use it

```bash
python -m elastic_evals calibrate --suite examples/banking_suite.yaml --data labelled.jsonl --out calib/   # add --local-only to forbid hosted models
python -m elastic_evals report    --calib calib/
python -m elastic_evals run       --suite examples/banking_suite.yaml --calib calib/ --data conversations.jsonl --out scores.jsonl
```
