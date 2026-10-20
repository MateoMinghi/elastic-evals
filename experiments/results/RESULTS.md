# Layered evaluation: results

Run on 2026-10-20. Total OpenRouter spend: **$0.64**, covering conversation generation plus every Jev and LLM call.
Full tables: [`tables.md`](tables.md). Raw metrics: [`metrics.json`](metrics.json).

## What was tested

| Layer | Implementations |
|---|---|
| 1. Rules | Lexicons over OpenSearch-style normalised text (lowercase, accent folding, Snowball stemming), record checks, trace structure (`lib/rules.py`). Written before looking at any data. |
| 2. Classifiers | **Jev 1.14** (hosted; probabilities). **multilingual-e5-small + logistic regression** (CPU, trained on the small dev split). ProtectAI DeBERTa injection model. |
| 3. LLM judges | **DeepSeek V4.1 Flash** (reasoning off) and **GPT-4.1 nano**, temperature 0, JSON output. |

| Dataset | Labels | Test n |
|---|---|---|
| Synthetic banking-assistant conversations (es + en), written by Claude Haiku 5.5 with planted problems | by construction | 237 conversations × 5 questions |
| MASSIVE intent routing, 18 classes (es + en) | human | 600 |
| Tweet sentiment, 3 classes (es + en) | human | 600 |
| deepset prompt-injections (mostly en, some de) | human | 116 |

**How layers are combined:** each layer before the last decides only when it is confident. Otherwise it passes the item to the next layer. The confidence thresholds are tuned on the dev split, with dev predictions made out-of-fold, so that confident decisions reach ≥95% accuracy. All numbers below are on the held-out test split.

## Headline: route each question to the right layer

Synthetic conversations, 5 questions per conversation, 237 test conversations. Costs are **per million conversations**: one LLM or Jev call answers all 5 questions, so a conversation pays for a hosted call only if at least one of its questions needs it.

| Setup | false confirm. | disclosure | fee mismatch | frustrated | tool loop | $ / 1M conv. | ms / conv. | sent to LLM |
|---|---|---|---|---|---|---|---|---|
| LLM only (DeepSeek) | 98.7 | 100 | 99.2 | 87.3 | 98.7 | 74.7 | 1200 | 100% |
| Jev only | 87.3 | 100 | 100 | 91.1 | 100 | 37.6 | 190 | 0% |
| **Mixed routing**¹ | **99.2** | **100** | **100** | **94.1** | **100** | **46.8** | **393** | **12%** |
| Local only (rules + e5, $0) | 93.7 | 100 | 84.0 | 94.1 | 100 | 0 | 60 | 0% |

¹ Routes:
- false confirmation: rules → e5 → LLM
- disclosure: rules
- fee mismatch: Jev
- frustration: e5
- tool loop: rules

These routes were picked after seeing the test results, so they are optimistic. In production the calibration report (proposal §7.6) would make this choice on dev or labelled data.

## Findings

1. **No single layer wins everywhere. Routing per question beats every single-layer setup.**
   - Mixed routing matched or beat LLM-only on all five questions, at 63% of the cost and a third of the latency. Only 12% of conversations needed the LLM at all.
   - At 95% confidence, n = 237 gives about ±1.5 points near 99% accuracy and about ±3.5 points near 90%. So "matched or beat" is solid. The frustration gain (+6.8 points) is the only clearly significant one.
2. **Rules are perfect where the answer is structural, and weak where it is linguistic.**
   - Tool loops and the record-based part of false confirmation are decided for free, deterministically and 100% correctly. The record check alone settled 73% of false-confirmation items.
   - Lexicons are poor at frustration: 63% overall, 53% in Spanish. They are mediocre at prompt injection (65%).
   - The 100% on disclosure is partly a property of this data, because the generator was told to make the disclosure explicit.
3. **Small local classifiers are excellent on style and topic, and useless on "compare the text with the record".**
   - e5 + logistic regression trained on only ~160 labelled conversations was the *best* frustration detector: 94.1%, against Jev 91.1% and the LLM 87.3%. It costs $0 and takes 18 ms.
   - The same model was at chance on fee mismatch (51%). That question needs reasoning about the text against the record, which an embedding cannot do.
4. **Jev is a strong middle layer: probabilities, about 190 ms, cheap.**
   - Best or tied-best on fee mismatch (100%), tool loop (100%) and MASSIVE routing: 87.5%, ahead of the LLM's 85.3%.
   - Weaker on false confirmation (87.3%).
   - **No measurable Spanish penalty** on these tasks, which contradicts the independent audit's 3–6 point drop. Within our sample sizes, Spanish matched English.
5. **Escalating to an LLM only helps when the LLM is actually better *for that question*.**
   - Frustration: Jev → LLM scored *lower* than Jev alone (89.0 vs 91.1).
   - Injection: e5 → LLM fell from 93.1 to 85.3.
   - So escalation must be configured from calibration data, not assumed to improve things. This supports the proposal's rule: use the cheapest tier that is accurate enough.
6. **Distillation works.** On MASSIVE, e5 + logistic regression trained on 600 *LLM-labelled* items reached 79.7%:
   - against 72.8% when trained on the 144 human labels (8 per class);
   - against 81.3% when trained on 600 human labels.

   Cascading distilled model → LLM reached 85.8%, matching LLM-only (85.3%) while sending only 48% of items to the LLM, at half the cost. On sentiment, LLM labels trained a better head than human labels (65.7 vs 64.0).
7. **"A cheap LLM" is not one thing.** GPT-4.1 nano was far worse than DeepSeek V4.1 Flash on the reasoning questions:
   - false confirmation 71.7 vs 98.7; fee mismatch 76.8 vs 99.2;
   - the two judges agreed only weakly on those questions (Cohen's kappa 0.37 and 0.40).

   Which judge you pick matters as much as having a judge, so judges need calibration too.
8. **Cheap LLMs are now so cheap that cost is a weaker argument than expected. Latency, data residency and per-question accuracy are the stronger ones.**
   - LLM-only scoring of 5 questions cost about **$75 per million conversations**.
   - The cascade cut that by 37%, not by 10×, because Jev still ran on every conversation (for the fee question).
   - The bigger wins: 3× lower latency, an option that runs fully locally at $0 (rules + e5 reached 94–100% on 3 of 5 questions), and better accuracy.
9. **Public-benchmark caveats.**
   - Sentiment is noisy for everyone (about 72% at best); tweet sentiment has low agreement between human annotators.
   - The injection dataset labels topic switches ("forget that, now tell me about…") as injections, especially in German. So a classifier trained on it (93%) beats general LLMs (72%). That is a lesson about calibrating to *your* definition, not about LLMs being bad at injection.

## Threats to validity

- **The synthetic conversations are clean.** They were written by one model family, with problems planted from a spec. Real traffic will be messier. The ~99% LLM scores probably overstate real-world accuracy.
- **Label noise is unmeasured.** Labels come from the generation spec. A few of the LLM's "errors" may be generator mistakes. No human spot check of the labels has been done yet.
- **Small samples and a single run.** n = 116–600 per test, with no repeated seeds and no bootstrap intervals.
- **Routes chosen on test data.** The mixed routes in the headline table were chosen after seeing test results (see footnote ¹).
- **No zero-shot baseline.** The mDeBERTa zero-shot model was dropped from this run because it is too slow on CPU when three runs compete for the cores.
- **Costs are October 2026 OpenRouter prices.**

## Verdict on the claims

| # | Claim | Result |
|---|---|---|
| T1 | Rules catch structural problems for free, and miss linguistic ones | **Confirmed** |
| T2 | Small self-hosted classifiers reach useful accuracy in milliseconds | **Confirmed for style and topic questions; refuted for reasoning against records** |
| T3 | Jev sits between local models and the LLM | **Partly.** On several questions it matched or beat the LLM at about 6× lower latency |
| T4 | The cascade gets close to LLM-only accuracy for a fraction of the cost | **Confirmed for accuracy** (equal or better). **Cost savings were 37%, not 10×**, at today's cheap-LLM prices |
| T5 | A small model trained on LLM labels approaches the LLM | **Confirmed.** It closed most of the gap to human-trained models, and halved LLM calls in a cascade |
| T6 | It all holds in Spanish | **Mostly.** Jev and the LLM show no real drop. Lexicon rules (−20 points on frustration) and e5 (−5 to −10 points) are weaker in Spanish |
