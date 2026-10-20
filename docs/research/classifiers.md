# Classifier and small-model evaluators for agent evals and online scoring

Research date: 2026-10-20. Sources are listed inline. Labels:
- **[primary]** read from the model card, vendor doc or paper page itself.
- **[secondary]** from a third-party article or aggregator; not checked against the primary source.
- **[vendor]** a claim made by the model's publisher; not independently verified.
- **[unverified]** could not be confirmed in this session.

---

## 1. TypeSafe AI / Jev

**What it is.** A hosted "System One" decision model. It takes a state (text) plus a set of typed questions and returns typed answers with probabilities. It does not generate text. The company is TypeSafe AI (San Francisco). Founder names reported by InfoQ: Diogo Almeida, Erik Gafni, Sasha Sheng [secondary].
- https://www.infoq.com/news/2026/10/typesafe-ai-jev-released (fetched, secondary)
- https://www.layer3labs.io/guides/jev-explained (fetched, secondary)

**Question types (API shape).** [secondary, consistent across 3 sources]
- `choice`: pick one option from up to 255. Returns the winner, a full probability distribution, and a confidence value.
- `score`: place the input on an ordered rubric. Returns tier, distribution across levels, and confidence. (One source says 2–10 levels.) Score was not tested in the Spanish audit below.
- `noul`: yes/no statement. Returns a single probability 0–1. No separate confidence field, which is a known gotcha.
- Several questions are evaluated in one parallel pass per call. A third-party example posts `state` + named `questions` to `/v1/systemone` [secondary, unverified against TypeSafe docs]. The official docs (docs.typesafe.ai) were not retrievable in this session.
- Model IDs seen: `jev-latest`, `jev-preview`, pinned `jev-1.13.0`. Version 1.14 was described as a preview with multilingual gains (not quantified) [secondary].
- https://zenmux.ai/docs/api/typesafe/systemone [secondary, search snippet only]
- https://docs.digitalocean.com/products/inference/how-to/use-system-one-api/ [secondary, search snippet only]

**Constraints.** [secondary] Text-only input (no images, audio, or video). ~32K-token context (~150K English characters). No streaming. Choice is capped at 255 options. Rate limits not published.

**Latency.** [vendor] 70–500 ms end-to-end. [user-reported, secondary] OpenChamber analysis of 12,759 launch tweets: median 76 ms, upper quartile 270 ms. A third-party benchmark measured a median of 255 ms [secondary].

**Pricing.** [vendor] $0.042 per million input tokens; output free. No free tier or trial credits documented [secondary]. At 100k requests × 1,000 input tokens, the cost is about $4.20 [arithmetic from the stated price]. A third-party benchmark reported $0.19 per 10k decisions for Jev, versus $0.065 for Microsoft Decision-1 and $0.63 for Clef 27B [secondary].

**Deployment.** Hosted API with keys, SDKs, and a playground. Early access began 2026-09-15 with a waitlist [secondary]. Routed through Vercel AI Gateway, Cloudflare AI Gateway (as `typesafe/jev`), and a LangChain `TypeSafeClassifier` integration [secondary]. No self-hosting or open weights were found. Parameter count, base model, and training data are undisclosed [secondary]. Weights are not open.

**Calibration.** Trained with "Reinforcement Learning for Calibrated Decisions" (RLCD) [vendor name]. Calibration is a training target, not a guarantee on specific prompts. Confidence reflects how concentrated the distribution is, not whether the answer is correct [secondary, layer3labs]. Teams must validate cutoffs on their own data [secondary].

**Accuracy evidence (all non-independent or small):**
- Vendor internal dashboard: 67.8% aggregate vs 74.1% for an unnamed comparator [vendor, secondary report]. No independent third-party benchmark as of 2026-09-22 [secondary].
- Author benchmark, two synthetic case sets, English and Brazilian Portuguese, 7 tasks: Jev 97.6% combined (compact), tied with Microsoft Decision-1 at 97.6%, above Clef 27B (95.8%) and OpenAI Decisions (90.5%). Locally-run open models scored higher (99.4%) [secondary: https://www.akitaonrails.com/en/2026/10/10/which-jev-like-ai-decision-classifier-model-is-best/ ]. Synthetic cases, no independent human labels; treat as directional.
- Better Stack author test, 200 YouTube comments, Claude Opus as referee (no human labels): Jev 86%, Clef 83%. Jev was about 2x faster in that test [secondary: https://betterstack.com/community/guides/ai/jev-vs-clef-decision-model.md ].

**Spanish.** No official language list. The vendor says Jev is better in English than in other languages but does not publish by how much [secondary]. The most rigorous public evidence is an independent, single-run audit, `marcosmartinez/jev-acento` (run `20260921-es-v1`, model `jev-1.13.0`, 19,200 calls, USD 0.58) [secondary-to-primary: https://github.com/marcosmartinez/jev-acento ]:
- Spanish state (vs English state, same items) lowered accuracy: XNLI 85.0 → 78.6; PAWS-X 83.4 → 77.2; MASSIVE 84.5 → 80.8; Belebele 98.2 → 95.2 (pp drops 3–6).
- Expected calibration error roughly doubled on XNLI (0.057 → 0.101) and PAWS-X (0.033 → 0.078). Automated coverage at p_max ≥ 0.9 fell from 72% to 63% on XNLI.
- Spanish text used 17–38% more input tokens than English.
- Spanish instructions gave no clear benefit; the author recommends keeping instructions and criteria in English.
- Caveats: one run, four academic datasets (not production text), MASSIVE is European Spanish, and the audit's pre-registration status is inconsistent (the reproduction doc says the pre-registration is "not frozen yet").
- Conclusion: Jev can be used on Spanish, but it must be re-validated on your own Spanish data, and calibration must be measured separately.

**Known limits** [secondary]: unreliable on counting, arithmetic, and date comparison; keep these in code. Vulnerable to prompt injection. Can emit a valid but wrong value (type-valid is not truth-valid). Out-of-distribution calibration is unknown.

---

## 2. Open small evaluator and guard models

Size, license, and language are from model cards [primary] unless marked. Output format matters most for the score shape: only models that expose a logit or probability are usable as-is for threshold cascades.

### 2a. Safety and content guards

| Model | Size | License | Languages | Output | Latency / notes |
|---|---|---|---|---|---|
| **Llama Guard 3 1B** [primary: https://huggingface.co/meta-llama/Llama-Guard-3-1B] | ~1B (pruned from 8B) | Llama 3.2 Community License (gated) | en, fr, de, hi, it, pt, es, th | Text: safe/unsafe + S1–S13 categories. No probability documented. | Small enough for CPU/edge. F1 English 0.899 vs 0.939 for 8B. |
| **Llama Guard 3 8B** | 8B | Llama 3.1 Community License [unverified] | same as 1B | Text | Better quality than 1B per Meta. |
| **Llama Guard 4 12B** [primary: https://huggingface.co/meta-llama/Llama-Guard-4-12B] | 12B dense, pruned from Llama 4 Scout | Llama 4 Community License (gated) | en plus Guard 3's languages; multilingual eval on fr, de, hi, it, pt, es, th | Text. S1–S14. Accepts text and images (multi-image). | English recall 69%, FPR 11%. Multilingual recall 43%, FPR 3%. Vendor in-house test set. |
| **ShieldGemma 2B / 9B / 27B** [primary: https://huggingface.co/google/shieldgemma-2b] | 2B (card also lists 9B, 27B) | Gemma license (gated) | English only | Generates "Yes"/"No". **P(Yes) = softmax over the Yes and No logits.** Only model here with a documented probability path. | 4 harm categories. F1 0.825 on SG-prompt (2B). Results very sensitive to policy wording. |
| **Qwen3Guard-Gen** 0.6B / 4B / 8B; **Qwen3Guard-Stream** [primary HF card; sizes from aggregator] | 0.6B–8B | Apache-2.0 [primary card; one aggregator disputes, check repo] | 119 languages and dialects [primary] | Text labels: **Safe / Controversial / Unsafe** + category; response mode adds refusal flag. No probabilities on card. | Stream variant does token-level moderation during generation. The "Controversial" class is a natural cascade trigger. Latency not reported. |
| **NVIDIA Llama Nemotron Safety Guard** (formerly NemoGuard ContentSafety 8B) [secondary: https://docs.nvidia.com/nim/ and HF card] | 8B | NVIDIA Open Model License + Llama 3.1 Community License (sources conflict on the V2 model) | V2 English; **Multilingual 8B V1 covers ar, zh, en, fr, de, hi, ja, es, th** | Safe/unsafe with categories [unverified detail] | Trained on Nemotron Content Safety Dataset V2 (formerly Aegis 2.0), CC-BY-4.0 dataset [secondary]. |
| **Llama Prompt Guard 2 86M** [primary: https://huggingface.co/meta-llama/Llama-Prompt-Guard-2-86M] | 86M (22M variant also) | Llama 4 Community License (branding and 700M-MAU clauses) | Trained on multilingual attacks; evaluated en, fr, de, hi, it, pt, es, th | Binary benign/malicious (label from logits; probability not documented). Detects injection and jailbreak. | 512-token input limit. 92.4 ms per classification on A100 at 512 tokens; 22M variant 19.3 ms [vendor]. Vendor AUC 0.998 (English), 0.995 multilingual; AgentDojo 81.2% [vendor]. |
| **ProtectAI deberta-v3-base-prompt-injection-v2** [primary: https://huggingface.co/protectai/deberta-v3-base-prompt-injection-v2] | ~0.2B | Apache-2.0 | English only | Binary 0/1 (a score is usually returned by the pipeline; not documented on card) | Does not detect jailbreaks. Not for system prompts (false positives). **Project archived, unmaintained.** |
| **OpenAI omni-moderation-latest** [primary: https://developers.openai.com/api/docs/guides/moderation] | hosted, not open | API; free endpoint | Multilingual (OpenAI reports gains on multilingual eval; no list) [secondary] | **0–1 score per category** (13 text categories, 6 applicable to images). Scores described as calibrated [secondary]. | Sends data to OpenAI. Useful as a free baseline or fallback, not self-hostable. |

Notes:
- Llama Guard, Llama Prompt Guard, and Nemotron multilingual models share one set of multilingual language lists, so language coverage is roughly: en, fr, de, hi, it, pt, es, th (Llama), plus ar, zh, ja (Nemotron, multilingual V1).
- Guards are binary or policy-based. They are better as cheap first-pass signals than as calibrated probabilities. Only ShieldGemma (logits) and OpenAI (scores) give a usable number directly.

### 2b. Hallucination / faithfulness (RAG and groundedness)

| Model | Size | License | Languages | Output | Notes |
|---|---|---|---|---|---|
| **Vectara HHEM-2.1-Open** [primary: https://huggingface.co/vectara/hallucination_evaluation_model] | ~0.1B, base flan-t5-base | Apache-2.0 | English only | **Consistency score 0–1** for (premise, hypothesis). Requires `trust_remote_code=True`. | Unlimited context per card. ~1.5 s for 2k tokens on CPU (vendor). Balanced accuracy on RAGTruth-QA 74.3% vs GPT-4 74.1% (vendor, small margin). Commercial **HHEM-2.3** adds 11 languages and is API-only [secondary]. |
| **Patronus Lynx 8B / 70B** [primary HF card for 8B: https://huggingface.co/PatronusAI/Lynx-8B-Instruct; 70B license via aggregator] | 8B, 70B (both Llama-3-Instruct fine-tunes) | **CC BY-NC 4.0 (non-commercial)** | English (primarily) | PASS/FAIL with reasoning, as JSON. Not a probability. | 8K max sequence. HaluBench overall 82.9% (8B), 87.4% (70B), GPT-4o 86.5% (vendor). **Not usable in an open-source default bundle that allows commercial use.** |
| **Granite Guardian 3.3 8B** [primary: https://huggingface.co/ibm-granite/granite-guardian-3.3-8b] | 8B | Apache-2.0 | English only | Yes/no inside `<score>` tags; optional `<think>` reasoning. Example code requests yes/no logprobs. | Criteria: harm categories, RAG groundedness, context relevance, answer relevance, function-calling hallucination. Custom criteria supported ("require testing"). |

### 2c. LLM-as-judge (small evaluator models, tier 2 candidates)

| Model | Size | License | Languages | Output | Notes |
|---|---|---|---|---|---|
| **Prometheus 2 7B** [primary: https://huggingface.co/prometheus-eval/prometheus-7b-v2.0] | 7B (8x7B also) | Apache-2.0 (generated-data terms cite OpenAI ToS) | English | Feedback then `[RESULT]` integer 1–5 (absolute) or A/B (pairwise). | Based on Mistral-7B-Instruct-v0.2. Correlation numbers only in plot images. |
| **Atla Selene-1-Mini (Llama-3.1-8B)** [primary HF card for size/context: https://huggingface.co/AtlaAI/Selene-1-Mini-Llama-3.1-8B] | 8B | Apache-2.0 (HF metadata) | Primarily English; card lists de, fr, it, pt, hi, es, th | Critique plus score; absolute, classification, pairwise | 128K context. Vendor claims beat GPT-4o on RewardBench, EvalBiasBench, AutoJ [vendor]. Also hosted API. |
| **Patronus GLIDER** [secondary: https://arxiv.org/html/2412.14140v2] | ~3.8B (docs say 3B), Phi-3.5-mini base | "Open-source" per paper; license not verified [unverified] | English | 0–1, 1–3, 1–5 scales, reasons, and highlighted spans | Explainable output is useful for debugging. License must be checked on HF. |
| **Flow Judge v0.1** [primary: https://huggingface.co/flowaicom/Flow-Judge-v0.1] | 3.8B (metadata says 4B), Phi-3.5-mini base | Apache-2.0 | English only | `<feedback>` then `<score>`; pass/fail, 3-point, or 5-point | 8,192-token max. Self-reported: pass/fail F1 0.955, 5-point Pearson 0.919 vs GPT-4o-mini 0.923. Weak on RAGTruth Data-to-Text (F1 0.341). |

### 2d. Classic classifiers (cheap, deterministic-ish, probabilities)

| Model / tool | Size | License | Languages | Output | Notes |
|---|---|---|---|---|---|
| **Microsoft Presidio** (PII) [primary: https://github.com/microsoft/presidio] | library | MIT | English by default; other languages need NLP engine and recognizer changes; regex is language-agnostic, context words are not | Entities with confidence scores | spaCy or Stanza NLP engines. Expect NER false positives and misses; tune. |
| **mDeBERTa-v3-base-xnli-multilingual-nli-2mil7** (zero-shot NLI) [primary: https://huggingface.co/MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7] | ~0.3B | MIT | Fine-tuned on 27 languages (incl. es, pt, ar, zh, ja); pretrained on 100 | Softmax over entailment / neutral / contradiction. Zero-shot labels scored as hypotheses. | Weak on hard English NLI (ANLI-all 0.537). XNLI accuracy 0.74 (sw, ur) to 0.87 (en). Good general-purpose "does this text match label X" with probabilities. |
| **cardiffnlp/twitter-roberta-base-sentiment-latest** [primary: https://huggingface.co/cardiffnlp/twitter-roberta-base-sentiment-latest] | base | CC-BY-4.0 | English only | Negative / neutral / positive probabilities | Trained on tweets; short-text domain. |
| **Frustration / escalation classifiers** | — | — | — | — | **[unverified]** No specific open frustration model was checked. Practical path: use zero-shot NLI or a Jev `score` question, or fine-tune on labeled traces. |

---

## 3. Methodology

### 3a. Calibrating evaluators against human labels

- **Chance-corrected agreement.** Use Cohen's kappa, κ = (p_o − p_e) / (1 − p_e), for categorical labels. For ordinal scales (1–5, tiers), use weighted kappa with `weights="linear"` or `"quadratic"` so near misses count less than far misses [primary: https://scikit-learn.org/stable/modules/generated/sklearn.metrics.cohen_kappa_score.html].
- **Per-class precision and recall at each threshold**, not one global accuracy. Safety and hallucination evaluators are usually imbalanced. Report precision/recall per class and per slice (language, product surface, user segment).
- **Calibration metrics.** Expected calibration error (ECE) and reliability diagrams, as the Jev Spanish audit did [secondary: https://github.com/marcosmartinez/jev-acento]. Calibrate on a held-out human set, never the threshold-tuning set.
- **Judge-vs-human baseline.** Zheng et al. (arXiv 2306.05685) report that GPT-4 judges reach >80% agreement with humans, equal to human-human agreement on MT-Bench and Chatbot Arena [primary: https://arxiv.org/abs/2306.05685 ; abstract read via search]. The same paper documents position, verbosity, and self-enhancement biases.
- **Estimating metrics from mixed labels.** Prediction-powered inference (PPI) combines a small human-labeled set with a large set labeled by an automatic rater, and gives valid confidence intervals even when the rater is biased. Stratified PPI (Fisch et al., arXiv 2406.04291, NeurIPS 2024) is the version to use when rater accuracy differs by slice [secondary: abstract via search]. A newer library, GLIDE (arXiv 2605.31278), packages PPI for agent evaluation [secondary].

### 3b. Confidence thresholds and cascades

- **Trust or Escalate** (Jung, Brahman, Choi; arXiv 2407.18370; ICLR 2025): start with a cheap judge, escalate to a stronger judge when confidence is low. Confidence is the agreement ratio across simulated annotators. A user-set risk tolerance chooses the abstention threshold with a provable human-agreement guarantee. Headline: over 80% human agreement with ~80% coverage on a Chatbot Arena subset, using Mistral-7B as the first judge [secondary: abstract and intro via search].
- **FrugalGPT** (Chen, Zaharia, Zou; arXiv 2305.05176; TMLR 2024): a learned cascade over LLMs. Reports matching GPT-4 with up to 98% lower cost on specific tasks (news classification, reading comprehension, science QA) [secondary: summaries].
- **RouteLLM** (arXiv 2406.18665): routes each query to one model. Third-party summaries report large cost cuts; the numbers are **[unverified]**.
- **A Unified Approach to Routing and Cascading** (Dekoninck et al., arXiv 2410.10347): finds that cascade routing beats routing or cascading alone, and that **quality estimators are essential** to both [secondary: abstract via search].
- **Cluster, Route, Escalate** (arXiv 2606.27457): cascaded serving that retains 97–99% of the strongest model's accuracy while cutting TPOT [secondary: abstract via search].

### 3c. Active learning and annotation sampling

- Route uncertain items to humans and confident items to the model. Uncertainty sampling is the standard baseline [secondary: arXiv 2410.15326 survey summary].
- Consistency-based flagging: samples the LLM is likely to have labeled wrongly go to humans (Wang, arXiv 2401.07367) [secondary].
- Uncertainty-based selection halved annotation for reward-model training in ActPRM (arXiv 2504.10559) [secondary]. This is training data, not eval, so the 50% figure does not transfer directly.
- No strong 2025–2026 paper on active sampling specifically for eval calibration was found in this session [gap].
- Practical recipe (from the sources above and standard practice): stratify the annotation sample by slice and by evaluator confidence band. Oversample the ambiguous band and the disagreements between tiers. Keep a random audit sample of high-confidence items to detect drift in the confident region, which otherwise goes unchecked.

### 3d. Drift and evaluator versioning

- Judge drift often comes from the judge itself: vendor snapshots behind the same alias can shift scores without a change on your side [secondary: https://tianpan.co/blog/2026-04-23-llm-judge-drift-evaluator-upgrade-phantom-regression ].
- Pin evaluator versions (`jev-1.13.0`, not `jev-latest`) and record the exact snapshot in each result [secondary: Jev article above].
- When the judge changes, run old and new side by side on a held-out, human-labeled set and compare per-slice agreement [secondary: tianpan; LangChain https://www.langchain.com/articles/llm-as-a-judge ].
- Track agreement against a fixed calibration set on a schedule. Alert on drops (vendor sources suggest weekly) [vendor-recommended; unverified cadence].
- Criteria drift: humans refine their criteria after seeing outputs, and some criteria depend on the outputs themselves [primary: Shankar et al., EvalGen, arXiv 2404.12272, https://arxiv.org/pdf/2404.12272]. Version criteria text the same way as models.

### 3e. Avoiding correlated judging

- **Same-model bias.** LLMs prefer outputs they generated, and one mechanism is familiarity or low perplexity, not only quality (Panickssery et al., arXiv 2404.13076; Wataoka et al., arXiv 2410.21819) [secondary summaries of primary papers]. A later paper argues that a judge-swap baseline removes much of the measured effect (arXiv 2601.22548) [secondary].
- **Rule for the framework:** the judge must not be the same model family, and ideally not the same weights, as the agent or the model being scored. Put the judge on a different provider or a different family than the agent under test. Keep deterministic checks outside the model stack entirely.
- Classifiers trained on different data (Jev, Llama Guard, Granite Guardian) are less correlated with a generative agent than a second instance of that agent. This is a reasoned point, not a measured result.

### 3f. Storing probabilities and re-thresholding

- Store the **raw output**, not the threshold decision: full distribution for `choice`/`score`, p(pos) for `noul`/binary, ShieldGemma's P(Yes) from logits, PASS/FAIL plus reasoning for judges.
- Each record needs: evaluator id, exact version, criteria text version, input hash, timestamp, latency, the threshold(s) applied at decision time, and the tier that produced it.
- Keep **confidence** and **probability** separate. Jev's confidence measures distribution concentration, not correctness [secondary].
- Re-thresholding is only valid against the same evaluator version and criteria version. Store both so an old decision can be re-scored.

---

## 4. Cascades and cost at scale

- **Cost reference points.** Jev: $4.20 per 100M input tokens, output free [vendor]. The layer3labs article's arithmetic for 100k requests × 1,000 input tokens and 50 output tokens: about $26 for GPT-5.6 Luna and $125 for Claude Haiku 4.5, versus about $4 for Jev [secondary article arithmetic; prices as quoted there, unverified].
- **Self-hosted small models.** The Akita benchmark measured local open models (Perplexity Decider 27B, Decision 3.0) at 99.4% on the same synthetic set, but cost and latency were measured only for hosted models [secondary].
- **Design rule from the literature.** Cascades pay off when the first tier resolves most traffic with high confidence and the escalation rate is low. The escalation rate, not the per-call price, decides total cost. Measure it on your own traffic before modeling spend.

---

## 5. Recommended design (for the framework)

### Evaluator interface

```
Result {
  metric:        string            // e.g. "safety.injection", "groundedness"
  evaluator:     { id, version, criteria_version }
  tier:          "deterministic" | "classifier" | "judge" | "human"
  label:         string | number   // decided by tier, if any
  probs:         { [label]: p }    // full distribution when available
  p_pos?:        number            // for binary metrics
  confidence?:   number            // only if the evaluator provides it; else derived from probs
  raw:           object            // logits, reasoning, PASS/FAIL, etc.
  latency_ms:    number
  input_hash:    string
  created_at:    timestamp
}
```

- Every evaluator must return a distribution or `p_pos`. A text-only judge without probabilities (Llama Guard, Lynx, Qwen3Guard-Gen labels) is a **tier-2 or tier-3 signal**: it should not be on the auto-accept path without a calibrated mapping.
- Ordinal metrics: store the distribution and expected value; kappa uses linear or quadratic weights.

### Cascade

1. **Tier 0 (100%).** Deterministic checks: schema, tool-call validation, regex and Presidio PII, length, exact-match facts. Do not ask a model to do arithmetic or date logic (Jev's own docs warn against it).
2. **Tier 1 (100%).** Classifiers with probabilities. Route each metric to its evaluator. Accept when p is above a high threshold or below a low threshold, both set per evaluator and slice on the human-labeled calibration set, targeting a chosen precision (risk-controlled, as in Trust or Escalate).
3. **Tier 2 (ambiguous band plus a random audit slice).** Small LLM judge from a different family than the agent. Only items inside the band go here, plus a random sample of confident items to detect drift.
4. **Tier 3 (human).** A stratified sample for calibration and drift tracking, plus all tier-1 vs tier-2 disagreements. Use kappa and per-class P/R to decide whether thresholds are still valid.
5. **Feedback.** Human labels update thresholds and, when needed, the evaluator choice. Version every change.

---

## 6. Shortlist for a first version

Chosen for: open license compatible with a permissive framework, output usable as a probability or a reliable label, and multilingual coverage where it matters. Verify each license before shipping.

| Need | Recommended first candidate | Alternatives | Why / caveat |
|---|---|---|---|
| Hosted typed probabilities (reference shape) | **Jev** (pinned `jev-1.13.0`) | Microsoft Decision-1, OpenAI Decisions (secondary) | Right output shape. Spanish is measurably worse; validate on your data with English instructions. Not open. |
| Safety, multilingual | **Qwen3Guard-Gen 0.6B or 4B** (Apache-2.0, 119 languages, Safe/Controversial/Unsafe) | Llama Guard 3 1B (Llama license, 8 langs) | "Controversial" is a useful escalation trigger. No probabilities on card; check logits before using as a score. |
| Safety, probability | **ShieldGemma 2B** (Gemma license, English) | OpenAI omni-moderation (hosted, 0–1 scores) | Only open option with a documented P(Yes) path. English only. |
| Prompt injection | **Llama Prompt Guard 2 86M** | ProtectAI deberta v2 (English, archived) | Multilingual, fast. Llama license branding and MAU clause. |
| Groundedness (RAG) | **Vectara HHEM-2.1-Open** (Apache-2.0, 0–1 score) | Granite Guardian 3.3 8B (Apache-2.0, yes/no + reasoning, English) | HHEM gives a number; Granite is a tier-2 check. English only. |
| Zero-shot topic / intent (multilingual, probabilities) | **mDeBERTa xnli multilingual** (MIT) | Jev `choice` for the same | Probabilities, 27 languages, cheap. Weak on hard English NLI. |
| PII | **Presidio** (MIT) | — | Tune per language; English by default. |
| Tier-2 judge | **Flow Judge v0.1** (Apache-2.0, 3.8B, structured score) or **Prometheus 2 7B** (Apache-2.0, 1–5) | Atla Selene-1-Mini (Apache-2.0 per HF metadata, 128K) | All English-first. Selene has the most languages listed. Avoid **Lynx** in a commercial-permissive default (CC BY-NC). |
| Sentiment / frustration | Zero-shot NLI or Jev `score` | cardiffnlp sentiment (CC-BY-4.0, English) | Frustration not verified as a standalone model. |

**Licensing flags to resolve before v1:** Llama licenses (MAU clause, branding), Gemma terms, NVIDIA Open Model License conflicts, Lynx (NC), Selene and GLIDER (verify on HF), Qwen3Guard (aggregator dispute; verify the repo).

**Gaps in this research:**
- Official TypeSafe docs (docs.typesafe.ai) were not retrievable; API shape is from third-party sources.
- Jev language coverage beyond the Spanish audit is not documented.
- Llama Guard 4 language list and license-independent output probabilities were not confirmed on the card.
- No evidence was found for a standalone open frustration classifier.
- Cascade cost numbers are mostly vendor or secondary; no independent production study was found.

---

## Source list

Primary model and tool pages:
- https://huggingface.co/meta-llama/Llama-Guard-3-1B
- https://huggingface.co/meta-llama/Llama-Guard-4-12B
- https://huggingface.co/google/shieldgemma-2b
- https://huggingface.co/Qwen/Qwen3Guard-Gen-0.6B
- https://docs.nvidia.com/nim/llama-3-1-nemoguard-8b-contentsafety/latest/index.html (secondary for naming)
- https://huggingface.co/meta-llama/Llama-Prompt-Guard-2-86M
- https://huggingface.co/protectai/deberta-v3-base-prompt-injection-v2
- https://developers.openai.com/api/docs/guides/moderation
- https://huggingface.co/vectara/hallucination_evaluation_model
- https://huggingface.co/PatronusAI/Lynx-8B-Instruct
- https://huggingface.co/ibm-granite/granite-guardian-3.3-8b
- https://huggingface.co/prometheus-eval/prometheus-7b-v2.0
- https://huggingface.co/AtlaAI/Selene-1-Mini-Llama-3.1-8B
- https://arxiv.org/html/2412.14140v2 (GLIDER, secondary)
- https://huggingface.co/flowaicom/Flow-Judge-v0.1
- https://github.com/microsoft/presidio
- https://huggingface.co/MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7
- https://huggingface.co/cardiffnlp/twitter-roberta-base-sentiment-latest

Jev and related:
- https://www.infoq.com/news/2026/10/typesafe-ai-jev-released (secondary)
- https://www.layer3labs.io/guides/jev-explained (secondary)
- https://www.akitaonrails.com/en/2026/10/10/which-jev-like-ai-decision-classifier-model-is-best/ (author benchmark)
- https://betterstack.com/community/guides/ai/jev-vs-clef-decision-model.md (author test)
- https://github.com/marcosmartinez/jev-acento (Spanish audit)
- https://zenmux.ai/docs/api/typesafe/systemone (secondary, snippet only)

Methodology papers:
- Zheng et al., Judging LLM-as-a-Judge, arXiv 2306.05685
- Shankar et al., Who Validates the Validators, arXiv 2404.12272
- Fisch et al., Stratified PPI, arXiv 2406.04291
- GLIDE library, arXiv 2605.31278 (secondary)
- Jung, Brahman, Choi, Trust or Escalate, arXiv 2407.18370
- Chen et al., FrugalGPT, arXiv 2305.05176
- RouteLLM, arXiv 2406.18665 (numbers unverified)
- Dekoninck et al., A Unified Approach to Routing and Cascading, arXiv 2410.10347
- Cluster, Route, Escalate, arXiv 2606.27457 (secondary)
- Panickssery et al., Self-Preference Bias, arXiv 2404.13076 (secondary)
- Wataoka et al., arXiv 2410.21819 (secondary)
- arXiv 2601.22548 (secondary)
- Rouzegar and Makrehchi, arXiv 2406.12114 (secondary)
- Wang, arXiv 2401.07367 (secondary)
- ActPRM, arXiv 2504.10559 (secondary)
- Cohen's kappa, scikit-learn: https://scikit-learn.org/stable/modules/generated/sklearn.metrics.cohen_kappa_score.html
- Drift: https://tianpan.co/blog/2026-04-23-llm-judge-drift-evaluator-upgrade-phantom-regression (practitioner)
- LangChain calibration: https://www.langchain.com/articles/llm-as-a-judge (practitioner)
