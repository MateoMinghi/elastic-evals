# elastic-evals: design v1.0

Status: **design of record**, 2026-10-20. Supersedes [`PROPOSAL.md`](PROPOSAL.md), which is kept as history.
Evidence: [`experiments/`](../experiments/). Research: [`docs/research/`](research/). Prototype: [`elastic_evals/`](../elastic_evals/).

---

## 0. Summary

**What it is.** elastic-evals is open-source observability and evaluation for production AI agents:
- Agents are instrumented once with OpenTelemetry.
- Events go to **OpenSearch**; configuration lives in **Postgres**.
- Every conversation is scored by a **calibrated cascade of evaluators**: rules → small local models → hosted classifiers (Jev) → one LLM judge → humans.
- It is linked to **Sentry** in both directions.

**Why it should exist.** We surveyed about 20 evaluation and observability tools (§2.1). None of them offers the following:
- OpenSearch as the store;
- a cascade of cheap evaluators whose routes are chosen by calibration against labels;
- a fully local mode for regulated tenants;
- a drift monitor for the evaluators themselves;
- integration with an error tracker.

The tests below show the cascade is worth building.

**What we tested** (§2.2; $1.55 of model calls in total):
- Every layer on its own, on 4 public datasets and on 3 synthetic banking-agent datasets written by 3 different model families.
- A working engine (`elastic_evals`) that calibrates itself from 158 labelled conversations.

On a holdout from a model family never used in development, the engine:
- **matched the best single LLM judge on accuracy** (93.5% against 92.8–93.1%);
- cost **34–39% less** than DeepSeek or Haiku alone;
- was **2–4× faster**;
- sent **64% of conversations to no LLM at all**.

**What we learned that changed the plan:**
- **Cheap LLMs are already cheap** (about $75–135 per million conversations for 5 checks). The case for the cascade is *accuracy per check, latency, a local-only option and drift detection*, more than cost.
- **Routes must be chosen for the whole suite and escalated per conversation.** One hosted call answers every check of a conversation.
- **Every layer, LLM judges included, must be calibrated.** The ranking of evaluators differs per check.

**Decided:**
- OpenSearch (not Elasticsearch);
- Go for the Collector and ingest pipeline, Python for the engine, API and SDK, TypeScript for the UI (§11);
- engine first, then storage, then UI;
- OpenTelemetry GenAI semantic conventions on the wire.

**Next gate:** calibrate on ~200 real, human-labelled agent conversations, and load-test OpenSearch (§12).

---

## 1. Goals and non-goals

**Goals (v1)**
1. Accept OTel traces from any agent framework (GenAI semconv, OpenInference, OpenLLMetry, Langfuse SDK attributes).
2. Score every conversation with a suite of checks along calibrated routes, at a known accuracy, cost and latency per check.
3. Let teams choose which layers each check may use, and let tenants forbid hosted models entirely.
4. Detect when the cheap layers stop fitting new traffic (drift), and recalibrate.
5. Store spans, content, scores and embeddings in OpenSearch, per tenant, with tiered and eval-aware retention.
6. Offline experiments on datasets from a CLI or pytest, with CI gates.
7. Annotation queues whose labels feed calibration.
8. Links to Sentry in both directions.
9. A minimal UI: trace viewer, search, score explorer with calibration reports, experiment comparison, annotation.

**Non-goals (v1)**
- Prompt management or a playground.
- Operational dashboards of our own (we ship OpenSearch Dashboards saved objects).
- Langfuse API compatibility.
- Serving large models: LLM judges and Jev are called over HTTP.
- Elasticsearch support.

---

## 2. What we learned

### 2.1 The landscape: existing evaluation tools

Detailed reports: [`research/oss-evals.md`](research/oss-evals.md), [`research/langfuse.md`](research/langfuse.md).

| Tool | License | Store | What we borrow | Weakness for our case |
|---|---|---|---|---|
| **Langfuse** (v4; acquired by ClickHouse, Jan 2026) | MIT core; `ee/` gated | ClickHouse + Postgres + Redis + S3 | Data model (below); evaluator/rule/job schema; deterministic hash sampling; deterministic score ids; Jev as a "decision model" evaluator | Evaluation is observation-level only (trace-level deprecated); RBAC, retention, audit and masking are behind the EE license; no cascade |
| **Comet Opik** | Apache-2.0 | ClickHouse + MySQL + Redis Streams | Online rules (sampling + variable mapping + several scores); agent-as-judge with tools (`read`/`jq`/`search`) and a USD cap per evaluation; thread rules with an idle cooldown; **annotation queues filled automatically from score conditions** | No calibration and no routing |
| **Arize Phoenix** | **ELv2** (not OSI open source) | Postgres | Score = (name, score, label, explanation, annotator kind HUMAN/LLM/CODE, direction); judges forced to call a tool for structured output; evaluator runs are traced | License; online evaluation is commercial |
| **MLflow GenAI** | Apache-2.0 | SQL | Assessments on traces; session-level online scoring with a settle window; judge versioning and alignment to human feedback | Online scoring drops traces when its queue is full, has no retries, and only scores traces under 1 h old |
| **DeepEval** | Apache-2.0 | none / commercial cloud | Richest agent metric catalogue: task completion, tool/argument correctness, plan adherence, step efficiency; Jev integration | Production monitoring is commercial |
| **Ragas** | Apache-2.0 | none | Multi-turn message schema with tool calls; tool-call F1; topic adherence | Offline only |
| **promptfoo** | MIT (now OpenAI) | local | Deterministic-first assertion vocabulary (`trajectory:tool-sequence`, `tool-call-f1`, weights, `not-` negation); best CI ergonomics | Offline only |
| **Inspect AI** | MIT | log files | Score `Value` typing (scalar/list/dict); metrics declared per scorer; `edit_score` provenance; Inspect Scout validates scanners against labelled sets | Not a production system |
| **Laminar** | Apache-2.0 (Signals gated) | ClickHouse + Quickwit | Each evaluation datapoint is a trace with EVALUATOR spans; SQL over traces | ClickHouse |
| **Braintrust** (closed) / **autoevals** (MIT) | — / MIT | Object storage + WAL | `LLMClassifier` with `choice_scores`; scorer scopes span / trace / group | Closed core |
| **TruLens, Agenta, Evidently, Giskard** | MIT / Apache | various | Selectors over span attributes (TruLens); cheap statistical descriptors (Evidently) | — |
| **OpenSearch Agent Traces** (3.6+) | Apache-2.0 | OpenSearch | Collector-edge GenAI normalizer; `otel-v1-apm-span-*` conventions | Alpha; traces only, no evaluations |
| **Helicone, OpenAI Evals** | — | — | — | Maintenance mode (Mar 2026); hosted Evals shuts down 2026-11-30 |

**Gaps none of them fill:**
1. **Scale strategies.** Uniform N% sampling everywhere; no priority sampling of errors and low scores; no per-evaluator budgets or tiering.
2. **Classifier-based online scoring.** Only hosted Jev exists, with no threshold calibration, no validation against humans, and no drift monitoring.
3. **A cascade or routing** across evaluator tiers as a first-class concept.
4. **Error-tracker integration.**
5. **Evaluator version and provenance on scores**, which only Inspect and MLflow track.
6. **OpenSearch as the evaluation store.**

These six gaps are the product (§3).

**Langfuse concepts we adopt** (from its v4 source, migrations and docs):
- **One wide document per span:** trace attributes (user, session, tags, release, environment) are copied onto every span; spans are immutable; experiment ids are span attributes.
- **A core/full split:** a truncated "core" record for lists and dashboards, and full content for single-record views.
- **Ingestion:** raw events go to blob storage first, then a queue.
- **Evaluation model:** Evaluator → immutable EvaluatorVersion → Rule (filter, sampling, delay, variable mapping) → JobExecution.
- **Reproducible sampling:** sampling by `sha256(span_id)`, and deterministic score ids.
- **No recursion:** evaluator traces go to internal environments that are never evaluated.
- **Accounting:** exclusive token-usage buckets; model prices matched by regex, with tiers.
- **Score analytics:** Cohen's kappa, Pearson, confusion matrix.

**Langfuse pain points we avoid:**
- trace-level aggregates that are too expensive on the wide table (no pre-aggregation);
- unbounded UI queries with retries;
- pricing fixed at ingestion with no way to re-price;
- five moving parts;
- governance features behind a license.

### 2.2 Experiments

Reports: [`experiments/results/RESULTS.md`](../experiments/results/RESULTS.md) (single layers) and [`experiments/results/ENGINE_SUMMARY.md`](../experiments/results/ENGINE_SUMMARY.md) (engine).

**Data:**

| Set | Content | Labels | Size |
|---|---|---|---|
| A | Synthetic banking-assistant conversations (es + en), written by Claude Haiku 5.5. Problems planted from a spec: false confirmation, missing AI disclosure, fee mismatch, frustrated user, tool loop | by construction | 395 |
| B | The same brief, made messy (typos, late disclosures, fees written as words, hard negatives), written by Gemini 3.1 Flash Lite | by construction | 300 |
| C | The messy brief, written by Qwen 3.8 Flash. **Final holdout**, scored once | by construction | 300 |
| MASSIVE | Intent routing, 18 classes, es + en | human | 600 test |
| Tweet sentiment | 3 classes, es + en | human | 600 test |
| deepset prompt-injections | Binary, mostly en, some de | human | 116 test |

**Evaluators tested:**
- **Rules:** lexicons over OpenSearch-style normalised text, record checks, trace structure.
- **Local models:** multilingual-e5-small with logistic heads (CPU); mDeBERTa zero-shot (dropped as too slow on CPU); ProtectAI injection DeBERTa.
- **Hosted classifier:** Jev 1.14 via OpenRouter's Decisions API.
- **LLM judges:** DeepSeek V4.1 Flash, GPT-4.1 nano, GPT-6 Luna, Claude Haiku 5.5.

**Part 1 — single layers** (set A test, 237 conversations, accuracy %):

| check | rules | e5 + logistic | Jev | DeepSeek | GPT-6 Luna | Haiku 5.5 | GPT-4.1 nano |
|---|---|---|---|---|---|---|---|
| false confirmation | 77.2 | 80.2 | 87.3 | 98.7 | 99.6 | 99.2 | 71.7 |
| missing disclosure | 100 | 99.6 | 100 | 100 | 100 | 99.6 | 97.0 |
| fee mismatch | 84.0 | 51.1 | 100 | 99.2 | 100 | 100 | 76.8 |
| frustrated user | 62.9 | **94.1** | 91.1 | 87.3 | 90.7 | 87.8 | 85.2 |
| tool loop | **100** | 85.7 | 100 | 98.7 | 87.3 | 94.9 | 87.3 |

On the public data, Jev led on intent routing (87.5% against 84–86% for the LLMs). Everyone sat around 72% on sentiment. On injection, an e5 head trained on the dataset (93%) beat every LLM (58–83%), because that dataset counts topic switches as injections.

**Part 2 — the engine.** Calibrated on 158 set A conversations. Mean accuracy over the 5 checks:

| | A-test | B | **C (holdout)** | $ / 1M conv. (C) | ms / conv. (C) | sent to an LLM (C) |
|---|---|---|---|---|---|---|
| **engine** (picked Jev + Haiku itself) | **98.3** | 94.1 | **93.5** | 83 | 564 | 36% |
| DeepSeek V4.1 Flash | 96.8 | 95.3 | 92.8 | 135 | 1211 | 100% |
| GPT-6 Luna | 95.5 | 94.8 | 93.1 | 81 | 2064 | 100% |
| Claude Haiku 5.5 | 96.3 | 94.1 | 92.8 | 125 | 920 | 100% |
| Jev | 95.7 | 93.3 | 92.3 | 37 | 198 | 0% |
| engine, local-only | 94.3 | 88.9 | 87.3 | 0 | 49 | 0% |

On C, the bootstrap 95% intervals show the engine is statistically tied with every LLM judge, significantly better than Jev alone, and far better than rules or e5 alone.

**Lessons.** Each one is tied to a design decision.

| # | Lesson | Evidence | Design consequence |
|---|---|---|---|
| L1 | No single evaluator wins every check | Rules perfect on tool loops, worst on frustration; e5 best on frustration, at chance on fee; LLMs bad at counting tool calls | Routes are **per check**, chosen by calibration (§6.3) |
| L2 | Structural checks belong to rules | The record check settled 73% of false-confirmation items for free; tool-loop rule 100% | Rules and record checks are tier 0; critical checks are never overruled by models (§6.4) |
| L3 | Small encoders handle style and topic, not reasoning against records | e5: frustration 94%, fee mismatch 51% | A local head is offered for a check only if calibration shows it helps |
| L4 | Escalating can make results *worse* | Frustration: Jev → LLM 89.0% against Jev alone 91.1%; injection: e5 → LLM 85% against e5 alone 93% | Escalation is never assumed; it is chosen only if it wins on labelled data |
| L5 | Choosing a judge per check is expensive | Engine v1 picked 3 hosted models: $212 / 1M and 3.4 s, worse than any single judge | **Joint suite-level selection** of hosted evaluators, **per-conversation escalation** (§6.3, §6.5) |
| L6 | A fixed confidence target breaks cascades on critical checks | At 95% confidence, cheap layers dragged critical checks below their floor, so the engine fell back to "LLM on everything" | **Strictness levels** per chain, and floors set statistically (§6.3) |
| L7 | The audit sample catches drift | A broken disclosure rule on set B: audit agreement fell from 100% to 81%; after the fix, disclosure went from 80% to 96% | The audit sample is the **drift monitor**; drift triggers recalibration (§6.7) |
| L8 | Judges differ a lot and need calibration too | GPT-4.1 nano 72% against DeepSeek 99% on false confirmation; the two judges agreed only weakly (kappa 0.37) | Judges are evaluators like any other, chosen per suite by calibration; never assume "an LLM" is ground truth |
| L9 | LLM labels can train small models | MASSIVE: e5 trained on 600 LLM labels 79.7% against 600 human labels 81.3%; distilled model → LLM matched LLM-only with 48% fewer LLM calls | **Distillation loop** (§6.8) |
| L10 | Cheap LLMs make cost a secondary argument | LLM-only: $75–135 per 1M conversations | Lead with accuracy per check, latency, local-only and drift; budgets and latency limits are policy (§6.2) |
| L11 | Spanish: hosted models held up, local ones weakened | Jev and the LLMs showed no clear drop; rules lost 20 points on frustration, e5 lost 5–10 | Calibrate and report **per language slice**; local heads are trained per tenant language |
| L12 | Evaluations depend on *your* definition | The injection dataset's definition favoured a head trained on it; ProtectAI's model scored 67% | Ship evaluator *adapters*, not verdicts; calibrate on the tenant's own labels |
| L13 | Synthetic conversations with planted labels are cheap and useful | 995 conversations for $0.96; they exposed real routing bugs | **Synthetic test-set generation** is a product feature for cold start and CI, never a replacement for real labels (§6.9) |
| L14 | Recalibrating can pick a slow judge | With B labels added, calibration chose GPT-6 Luna alone: best accuracy, 2.1 s per conversation | Add a **latency budget** to the policy (§6.2) |

**What remains unproven:**
- Behaviour on real traffic with human labels. All agent data so far is synthetic, labelled by construction, and probably cleaner than production.
- Subjective checks. Frustration stays at 77–86% for every evaluator on messy data.
- OpenSearch at billions of spans.
- Self-hosted Sentry over OTLP.
- Trace-level and trajectory evaluations. The engine scores whole conversations today.

### 2.3 Concept ledger: everything we wanted to include, after the tests

| Concept | Status | Why |
|---|---|---|
| Three automated layers (rules, classifiers, LLM) plus humans | **Kept, refined** | L1–L4. Humans are the calibration ground truth, not a review step |
| User chooses which layers a check may use | **Kept** | `layers:` per check in the suite file; calibration picks within the allowed set |
| Tenant policy "data never leaves" | **Kept, tested** | Local-only engine: 87–94%. The calibration report names the checks that cannot meet target locally |
| "Tokenizer" / lexical rules | **Kept for structural and lexical checks** | L2. Weak for linguistic judgments (L1); rules are versioned and monitored for drift (L7) |
| Jev | **Kept, optional** | Strong middle layer (L1, L11); hosted only, so excluded under local-only. Other decision models use the same API (Microsoft Decision-1, Perplexity, Cloudflare Clef…) and are untested |
| Small self-hosted encoders | **Kept** | L3, L9 |
| Open guard models (Qwen3Guard, Llama Guard, HHEM, Prompt Guard 2) | **Candidates, untested** | Shortlisted by research; the same adapter and calibration apply. ProtectAI DeBERTa is not a default (L12) |
| Zero-shot NLI for cold start | **Optional, untested** | Too slow on CPU in our run; synthetic labels (L13) cover cold start better |
| LLM judges | **Kept; one per suite; calibrated** | L5, L8. Different family from the agent; the audit judge differs from the routing judge |
| Cascade escalating on uncertainty | **Kept, changed to per conversation** | L5 |
| Fixed confidence thresholds | **Replaced** by strictness levels and statistical floors | L6 |
| Critical checks deterministic and never overruled | **Kept** | L2 |
| Raw probabilities stored, never just decisions | **Kept** | Re-thresholding and recalibration need them |
| Distillation (LLM labels → small heads) | **Kept; in engine v1.1** | L9 |
| Random audit sample | **Kept, now the drift monitor** | L7 |
| Cost as the headline argument | **Demoted** | L10 |
| Eval-aware retention | **Kept, untested** | Follows from tiers 0 and 1 running on 100% before the retention decision |
| Trace-complete signal and trajectory evaluations | **Kept, untested** | Langfuse dropped trace-level evaluation; a gap we fill |
| OpenSearch as the event store | **Kept, decided; needs a load test** | §5, §12 |
| Elasticsearch | **Dropped** | Licensing; one backend |
| Langfuse concepts (§2.1) | **Kept** | Proven at scale; MIT |
| Sentry integration | **Kept, narrow; untested** | §8 |
| OTel `gen_ai.evaluation.result` event for scores | **Changed** | Sentry drops span events on OTLP, and the event lacks evaluator kind and version. Scores are documents; we can still *export* the event |
| Synthetic labelled test sets | **New** | L13 |
| Prompt management / playground | **Non-goal** | Others do it well |

---

## 3. Positioning

elastic-evals fills the gaps in §2.1:
1. **OpenSearch-native.** Full-text and k-NN search over agent traffic, the security plugin included at no cost, and reuse of the cluster that banks and enterprises already run.
2. **Calibrated evaluator cascade.** Each check gets the cheapest route that is statistically as accurate as the best, chosen from labels, with known accuracy, cost and latency.
3. **A local-only mode** with an honest report of what it cannot do.
4. **A drift monitor for evaluators**, and recalibration.
5. **Eval-aware retention and priority sampling** at very high volume.
6. **A Sentry bridge.**
7. **Governance in the open-source core:** RBAC, retention, audit, masking.

---

## 4. Architecture

```
 Agents (OTel SDK, gen_ai.* semconv)              Sentry SDK (errors; OTLPIntegration)
        │ traceparent / baggage                           │ same W3C trace_id
        ▼                                                 ▼
 ┌────────────────────────────────┐               ┌───────────────┐
 │ OTel Collector, 2 tiers        │               │    Sentry     │◄────────────┐
 │ 1: load-balance by trace_id    │               └──────┬────────┘             │
 │ 2: redact PII · tag tenant ·   │                      │ issue / alert webhooks│ External Issue links,
 │    normalize semconv · route   │                      ▼                       │ quality-regression events
 └──────────────┬─────────────────┘               ┌───────────────┐             │
                ▼                                 │ Sentry bridge │─────────────┘
         Kafka: spans topic                       └──────┬────────┘
                │                                        │ issue status
     ┌──────────┴───────────────┐                        ▼
     ▼                          ▼
 Ingest workers            Evaluation workers (elastic_evals engine)
 normalize, price,         tier 0 rules · tier 1 local heads · hosted classifier · one LLM judge
 write obs/payload docs,   routes + thresholds from the active calibration; per-conversation
 large blobs → S3,         escalation; shadow + audit scores; drift monitor
 trace-complete tracker    retention decision (keep content?)
     │                          │
     ▼                          ▼
 ┌──────────────────────────────────────────────┐   ┌────────────────────────────────────┐
 │ OpenSearch                                   │   │ Postgres                           │
 │ obs-* · payload-* · scores-* · traces-* ·    │   │ tenants, RBAC · suites + versions ·│
 │ embeddings-* · metrics-* (per tenant / pool) │   │ evaluators · calibrations + routes │
 └──────────────────────────────────────────────┘   │ datasets, items, runs · annotation │
                ▲                                   │ queues + labels · model prices ·   │
                │                                   │ trace tracker · sentry links       │
       API (FastAPI) ◄── UI (React) · CLI · pytest  └────────────────────────────────────┘
                                                      S3: payload blobs, calibration artifacts
                                                          (local heads), audio with consent
```

**Where data lives:**
- **OpenSearch:** everything high-volume and append-only.
- **Postgres:** everything configuration, transactional or versioned.
- **S3:** large blobs and model artifacts.

---

## 5. Ingestion, conventions and storage

### 5.1 Semantic conventions

- **Wire format:** OTel GenAI semantic conventions (`gen_ai.*`, repo `semantic-conventions-genai`). They are still at *Development* stability, so we **pin a version** and normalise OpenInference, OpenLLMetry and `langfuse.*` at the Collector or ingest, using a priority-ordered mapper as Langfuse does.
- **Key attributes:**
  - `gen_ai.operation.name` (chat, embeddings, retrieval, create_agent, invoke_agent, invoke_workflow, execute_tool);
  - `gen_ai.agent.*`, `gen_ai.workflow.name`;
  - `gen_ai.conversation.id`, our session key;
  - `gen_ai.tool.name`, `gen_ai.tool.call.arguments`, `gen_ai.tool.call.result`;
  - `gen_ai.usage.*`, including cache and reasoning tokens;
  - `error.type`.
- **Content:** `gen_ai.input.messages`, `gen_ai.output.messages` and `gen_ai.system_instructions`, captured opt-in and redacted at the Collector.
- **Our namespace, `ee.*`:**
  - `ee.tenant_id`
  - `ee.experiment.run_id`, `ee.experiment.item_id`
  - `ee.sentry.event_id`
  - `ee.record.*` (references to domain records used by record checks)
  - `ee.eval_flag`

  These are always attributes, never span events: Sentry drops span events on OTLP ingest.
- **Borrowed from OpenInference:** the `GUARDRAIL` and `EVALUATOR` span kinds, `llm.cost.*`, and prompt-template version attributes.

### 5.2 Pipeline

1. **Agents** use an `always_on` sampler. Every sampling decision is made downstream, where errors and scores are visible.
2. **Collector tier 1** load-balances by `trace_id`. **Tier 2** runs redaction (a Presidio-backed processor), tenant tagging, the semconv normalizer and routing.
3. **Kafka** buffers the stream, absorbs spikes, allows replay, and lets ingest and evaluation consume the same stream.
4. **Ingest workers** (a second Collector deployment in Go: a Kafka receiver plus our OpenSearch exporter, §11):
   - normalise attributes and compute cost from `model_prices` (with a re-pricing job);
   - write `obs` (truncated) and `payload` (full content) documents, with large blobs in S3;
   - update the trace-complete tracker.
5. **Evaluation workers** run the engine (§6) on completed conversations, *before* the retention decision (§7).

### 5.3 OpenSearch layout

Based on [`research/opensearch.md`](research/opensearch.md).

| Index | One document per | Notes |
|---|---|---|
| `obs-*` | span | Typed columns (ids, parent, trace/session, name, type, model, exclusive token buckets, cost, latency, status, `date_nanos`); trace attributes copied on; input/output truncated to ~200 chars; metadata as `flat_object`. Strict mapping, `zstd`, routing by `trace_id`, **never updated** |
| `payload-*` | span with kept content | Full messages: `match_only_text` for search, raw JSON with `index: false`. Shorter retention |
| `scores-*` | score | The §6.6 record, with trace context copied on (agent, flow, model, tags, experiment ids) so it can be filtered without joins. A correction is a new score that `supersedes` the old one |
| `traces-*` | completed conversation or trace | Rollup written on completion: cost, tokens, latency, error flag, tool sequence, latest score per check, Sentry issue ids. **The source for list views.** Avoids Langfuse's expensive trace-level aggregates |
| `embeddings-*` | turn or conversation | `knn_vector` (on-disk, quantized; default codec, because k-NN rejects `zstd`). Used for "similar failures", dataset mining and the local heads |
| `metrics-*` | time bucket | Append-only, with a star-tree index for fixed dashboards |

**Design rules:**
- **Time-based indices behind a write alias, not data streams.** Data streams reject custom `_id` and `delete_by_query`, so we would lose idempotent ingest (`op_type: create` with a deterministic `_id`) and cheap GDPR deletes. The cost: duplicates are possible across a rollover boundary, so the read path removes them by id.
- **Roll over by size** (30–50 GB per primary shard), not by day, to stay below shard limits.
- **Promote any charted field to a typed column.** `flat_object` cannot be aggregated.
- **No joins at query time.** Copy context onto score documents; experiment comparisons (thousands of rows) are joined in the application. PPL `join` exists in 3.9 but is capped at 50k/10k rows.
- **Tiers:** hot 0–14 days → force-merge, snapshot, `convert_index_to_remote` (searchable snapshot on warm nodes) for 90–180 days → snapshot archive. Scores and metrics outlive payloads. Remote-backed storage must be chosen when the cluster is created.
- **Known weaknesses, accepted:**
  - aggregation speed and disk footprint compared with ClickHouse (ClickHouse's study of Elasticsearch: about 5× smaller and 1.7–6× faster);
  - approximate percentiles and cardinality;
  - immature OTel exporter and Data Prepper paths.

  The mitigations are rollups, eval-aware retention and a load test before committing (§12). Keep a ClickHouse adapter as a possible future analytics sink.

### 5.4 Postgres

- `tenants`, `projects`, `memberships`, `roles` (scopes `resource:action`), `api_keys`
- `suites` → `suite_versions` (the YAML, immutable)
- `evaluators` → `evaluator_versions` (immutable: model id pinned, e.g. `typesafe/jev-1.14`, never `-latest`)
- `calibrations`:
  - suite version, label set, policy;
  - routes, thresholds and strictness per check;
  - the chosen hosted set;
  - the per-check report (accuracy, floor, cost, latency, per language slice);
  - the artifact URI for local heads in S3;
  - status: candidate → shadow → active → retired
- `label_sets` → `labels` (conversation, check, value, source human / synthetic / judge, annotator)
- `datasets` → `dataset_items` (with valid-from/valid-to so items have versions) → `experiment_runs`
- `annotation_queues` → `annotation_items`
- `model_prices` (regex, tiers, start date)
- `trace_tracker` (trace, last span, due time): the trace-complete signal, replaceable by a stream processor
- `sentry_links` (trace ↔ Sentry issue, status)
- `audit_log`

---

## 6. The evaluation engine

The engine is the core of the product, and it is already prototyped in `elastic_evals`.

### 6.1 Concepts

- **Conversation:** the unit of scoring.
  - Today it is `{id, lang, turns (user / assistant / tool), record}`, built from a trace or session.
  - `record` holds the domain records the checks compare against: the system of record, fetched by reference.
- **Check (question):** a binary judgment about a conversation. Each check has:
  - the wording for hosted evaluators (`ask`);
  - the text `view` local models see;
  - an optional `rule`;
  - `invert` (asked positively, scored as a problem);
  - `critical`;
  - optional `layers` (the user's restriction).

  Multi-class checks (routing, intents) are planned: the prototype handles binary checks; the experiments ran multi-class.
- **Suite:** a versioned YAML file with the checks, the allowed evaluators, the judge preamble and the policy. Example: [`examples/banking_suite.yaml`](../examples/banking_suite.yaml).
- **Evaluator:** one of four types, ordered by cost:

| Type | Runs | What it is good at (L1–L3) |
|---|---|---|
| `rules` | in process, ~0 ms, $0 | Record checks, trace structure (loops, retries, step budgets), lexical presence (disclosures, forbidden promises) |
| `local_head` | CPU, a few ms per check | Style and topic: frustration, intent, sentiment, injection-by-definition |
| `jev` (and other decision models) | hosted, ~190 ms, ~$37 / 1M conversations | Typed judgments with probabilities: fee consistency, intent routing |
| `llm` | hosted, 0.9–2.4 s | Reasoning against records, nuanced judgments. One judge per suite |

### 6.2 Policy

Per suite, overridable per tenant:

| Field | Meaning |
|---|---|
| `allow_hosted` | `false` = local-only: rules and local heads only, no text leaves the deployment |
| `target_accuracy` | Minimum accuracy for a cheap layer's *confident* decisions (default 0.95) |
| `tolerance` | How far below a check's best accuracy a cheaper route may be (default 1 point; critical checks use one standard error only) |
| `audit_rate`, `audit_judge` | Share of conversations re-scored by an independent judge (default 2%); the drift monitor |
| `max_ms_per_conv` | **New (L14):** latency budget the chosen set must respect |
| `max_usd_per_1M_conv` | **New:** cost budget |
| `languages` | Slices that must each meet target (L11) |

### 6.3 Calibration (`elastic_evals calibrate`)

Input: a suite, labelled conversations, the policy. Output: a calibration (§5.4), *candidate* until promoted.

1. **Collect.** Run every allowed evaluator on every labelled conversation:
   - local heads are trained here and scored **out-of-fold**, so thresholds are never tuned on their own training data;
   - hosted calls are cached.
2. **Thresholds at several strictness levels.** For each (check, evaluator) and each level in {target, 0.97, 0.99, 1.0}, find the widest confidence band whose decisions reach that accuracy (L6).
3. **Floors.** For each check, the best accuracy over all chains sets its floor: best − max(tolerance, SE), using a smoothed standard error so that 100% doesn't demand perfection. Critical checks use best − SE.
4. **Joint selection (L5).** Try each candidate hosted set: Jev or not, × at most one LLM judge.
   - Every check takes its cheapest chain inside that set (rules → local → Jev → judge, at any strictness) that clears its floor.
   - The whole suite is then simulated **per conversation**, paying each hosted call once.
   - The winner has the fewest checks below their floor, then the lowest cost, then the lowest latency, subject to `max_ms_per_conv` and `max_usd_per_1M_conv`.
5. **Report.** Per check: route, accuracy, floor, best achievable, meets target or not, share decided by each layer, cost and latency, per language slice; plus every alternative considered. Under local-only, the report states which checks cannot meet target.
6. **Promote.**
   - A candidate first runs in **shadow** on live traffic: scored but not served.
   - It is promoted when it agrees with the audit judge at least as well as the active calibration.
   - Versions are immutable; old scores stay attributable.

### 6.4 Invariants

- **Critical checks are anchored in tier 0.** A failed record check is final; a model may add a flag, never clear one.
- **No correlated judging.** The judge is from a different model family than the agent, and the audit judge differs from the routing judge (in the tests: routing Haiku, audit DeepSeek).
- **Store raw outputs and probabilities**, never just decisions.
- **Pin evaluator versions and criteria versions**; record both on every score.
- **Evaluator traces never get evaluated:** internal environment, as in Langfuse.

### 6.5 Runtime (`elastic_evals run`, evaluation workers)

For each conversation:
1. **Evaluators run in cost order.**
2. **A hosted evaluator is called only if** at least one unresolved check has it next on its route. **One call answers every check** of the suite; the answers that weren't needed are stored as free **shadow scores** (L5).
3. **Each check stops** at the first layer that is confident at its calibrated strictness, or at the end of its route.
4. **Audit:** `sha256(conversation_id)` below `audit_rate` → the audit judge scores every check (`sample_reason = random_audit`).
5. **Trigger:** per conversation, on the trace-complete signal: the root span has ended, then an idle timeout configurable per agent (calls and missions need minutes). Sessions (`gen_ai.conversation.id`) use a longer cooldown and are re-evaluated when new turns arrive (MLflow-style).
6. **Scopes:**
   - **conversation** (shipped in the prototype);
   - **observation:** span-level checks such as "this tool call was valid";
   - **trace / trajectory:** task completion, tool-sequence F1, plan adherence, step efficiency (DeepEval / Ragas metrics), using Opik-style agent-as-judge tools for long traces;
   - **experiment item.**

### 6.6 Score record

```jsonc
{
  "id": "sha256(calibration|evaluator_version|conversation|check|criteria_version)",   // deterministic, idempotent
  "tenant_id": "...", "conversation_id": "...", "trace_id": "...", "observation_id": null, "session_id": "...",
  "check": "fee_mismatch", "value": 1, "p": 0.97, "probs": {"yes": 0.97, "no": 0.03},
  "label": null, "explanation": null,
  "decision": true,                       // false for shadow and audit scores
  "sample_reason": "route",               // route | shadow | random_audit | experiment | annotation
  "evaluator": {"id": "jev", "kind": "jev", "tier": 2, "version": "typesafe/jev-1.14", "criteria_version": "3"},
  "calibration_id": "cal_2026_10_20_01", "route": ["jev", "llm_haiku"], "strictness": 1.0,
  "cost_usd": 0.0000372, "latency_ms": 190, "lang": "es",
  "experiment": {"run_id": null, "item_id": null},
  "trace_ctx": {"agent": "...", "flow": "...", "model": "...", "tags": []},
  "supersedes": null
}
```

Every field above `trace_ctx` is emitted by the prototype or derivable from it. Export to OTel's `gen_ai.evaluation.result` is optional and lossy.

### 6.7 Drift monitor and recalibration

- **Agreement with the audit judge, per check and per language slice,** compared with the agreement at calibration time.
  - In the tests, the drift monitor flagged the broken disclosure rule (100% → 81%) and frustration (78–86% on new data).
  - The audit sample is only a monitor: at 1M conversations a day, a 2% audit is 20k audited conversations a day, enough to detect drops of a few points per check.
- **A drop beyond a threshold** opens a recalibration task: sample disagreements into the annotation queue, fix the rule or add labels, recalibrate, run the candidate in shadow, promote.
  - In the tests (engine v1), recalibrating with 100 labels from set B took the engine from 91.7% on B to 97.1% on the remaining 200 B conversations.
- **Evaluator drift:** a new evaluator version is shadowed against the old one on the labelled set before use.

### 6.8 Distillation (engine v1.1)

1. LLM judge and human labels on a sample →
2. train or refresh local heads per check and language →
3. calibration decides whether the head enters the route (L9) →
4. the judge falls back to the uncertain band plus audit.

LLM cost then follows uncertainty, not traffic.

### 6.9 Labels, annotation, synthetic sets

- **Label sources, kept apart:** `human` (annotation queues), `synthetic` (planted by construction), `judge` (LLM labels, used for distillation only, never as calibration truth on their own).
- **Annotation queues** fill automatically from conditions (Opik): audit disagreements, tier disagreements, low-confidence items, stratified random samples per slice.
- **Agreement metrics:** Cohen's kappa (weighted for ordinal checks), per-class precision and recall, expected calibration error. Use stratified prediction-powered inference (Fisch et al. 2024) for confidence intervals from mixed human and automatic labels.
- **Synthetic generator** (L13), shipped as `elastic_evals synth` (planned):
  - a domain record built in code, plus an LLM-written conversation with planted problems and hard negatives;
  - generated by a model family different from the judges;
  - validated structurally.

  It is used for cold start and CI regression sets, and is always followed by real labels.

### 6.10 Offline experiments and CI

- **Datasets** with versioned items, created from traces in bulk, through k-NN "similar failures", or by synthesis.
- **`elastic_evals experiment` and a pytest plugin** run an agent over a dataset. Spans get the `ee.experiment.*` tags, and the same suite and calibration score them.
- **Gates:** absolute for critical checks (zero false confirmations); relative to a baseline run for the rest. Assertion vocabulary from promptfoo (`trajectory:tool-sequence`, `tool-call-f1`, `not-`).
- **Results** go into the same indices, so comparing an experiment with production is a query.

---

## 7. Retention and sampling (eval-aware)

| Data | Kept | Default retention |
|---|---|---|
| `obs` metadata, `scores`, `traces` rollups | 100% | hot 14 days → warm 90–180 days → archive |
| `payload` (full content) | errors; Sentry-linked traces; tier-0 failures; flagged or low-score conversations; annotation and dataset items; audit sample; random N% | short, per tenant (e.g. 30 days hot) |
| Audio and large blobs | consent only | S3 lifecycle per tenant; traces keep a reference |

The retention decision is taken **after** the engine runs. Evaluators see all content while it is in the stream, and only the conversations that matter keep it. This is the main lever on OpenSearch cost (§5.3), and the "priority sampling" missing from every tool in §2.1.

---

## 8. Sentry integration

Based on [`research/sentry.md`](research/sentry.md). Not yet tested in practice.

- **Division of work:** Sentry handles exceptions, alerting and the issue workflow. elastic-evals handles traces, content, scores and evaluation. They share **one W3C trace id** (Sentry trace ids are the same 32-hex value as OTel's).
- **Propagation:** the Sentry SDK's `OTLPIntegration` puts the active OTel trace and span ids on every Sentry event.
  - Set `OTEL_PROPAGATORS=tracecontext,baggage` and `setup_propagator=False`. Sentry's propagator otherwise writes `sentry-trace` (unverified).
  - Don't set `traces_sample_rate`.
- **Sentry → our trace:**
  - In `before_send`, tag the event with `trace_id`, `gen_ai.conversation.id`, `gen_ai.agent.name` and a deep-link context.
  - In the same hook, write `ee.sentry.event_id` on the active span **and mark it ERROR**. Otherwise a handled exception leaves the span OK and its content may be dropped.
  - The **bridge** receives HMAC-signed issue and alert webhooks and calls the External Issue API (`POST /api/0/sentry-app-installations/{uuid}/external-issues/`). That puts a clickable "View agent trace" on the issue.
- **Our trace → Sentry:**
  - Read `ee.sentry.event_id`, falling back to a cached `issues?query=trace:<id>` lookup (partly undocumented, so degrade gracefully).
  - The bridge copies issue status back into `traces-*` and `sentry_links`.
- **Quality regressions into Sentry:** only aggregated threshold breaches from the drift monitor and the check rates, using `capture_message` with a stable fingerprint per check, agent and tenant. Sentry's alerting and on-call routing then cover quality. Cron check-ins cover scheduled calibration jobs.
- **Never sent to Sentry:**
  - prompt, response or tool content (Sentry does not scrub the gen_ai content attributes by default);
  - full-volume traces;
  - individual scores;
  - span events.

  As a backstop: redaction at the Collector, `send_default_pii=false`, and Sentry scrubbing rules on `$span.data.*`.
- **Sampling consistency:** `always_on` at the SDK. The Collector's tail sampler keeps `status=ERROR`, `ee.sentry.event_id` and flagged traces, with a larger `decision_wait` and the decision cache enabled. Our retention decision (§7) is the final word.
- **Self-hosted Sentry:** start errors-only. OTLP ingest and AI Agents Insights on self-hosted are unproven and need a proof of concept. Self-hosted needs 16+ GB RAM, more with spans.

---

## 9. Tenancy, security, privacy

- **Two tenancy tiers:**
  - **Regulated tenants:** dedicated indices, roles, retention, snapshot repository, and optionally their own cluster.
  - **Small tenants:** pooled indices with `tenant_id` routing, document-level security from a JWT attribute, a mandatory filter in the application, and Workload Management groups.
  - Document-level security is read-side only, so it is never the only control.
- **Local-only policy** (§6.2): no conversation text leaves the deployment. It is calibrated and reported honestly (in the tests, 3 of 5 checks could not reach 95% locally).
- **Redact at the Collector:** OpenSearch field masking is hash-only and breaks search.
- **GDPR:** per-tenant time-based indices, plus `delete_by_query` per user; payloads have short retention.
- **Eval datasets built from real conversations** need per-tenant consent and stay separated by tenant.
- **Audit:** the OpenSearch security plugin's audit log, plus our API audit log.
- **RBAC, retention, audit and masking stay in the open-source core.**

---

## 10. UI (v1)

The part Langfuse gives away for free, and the part we must build. Five screens; the rest is OpenSearch Dashboards.

1. **Trace / conversation viewer:** span tree and timeline, content, scores per check, with route, layer, probability and shadow/audit scores alongside; Sentry links.
2. **Search:** full-text and filtered search over conversations, plus k-NN "similar conversations".
3. **Score explorer and calibration report:**
   - per check: rates, slices, language;
   - the active route and the share decided per layer;
   - drift agreement over time;
   - candidate against active calibration.
4. **Experiment comparison:** runs side by side per item, diffs, gate status.
5. **Annotation queue:** keyboard-driven labelling; labels feed calibration.

---

## 11. Technology

**Three languages, each where it is clearly the best fit: Go for the pipeline, Python for evaluation, TypeScript for the UI. No Rust for now.**

| Component | Language / pick | Why |
|---|---|---|
| Collector distribution (receive, redact, tag tenant, normalise semconv, route to Kafka) | **Go**, built with the OpenTelemetry Collector Builder from `otelcol-contrib` plus our own processors | Collector components can only be written in Go; one static binary |
| Ingest pipeline (Kafka → OpenSearch) | **Go**: a second Collector deployment, a Kafka receiver plus our own OpenSearch exporter (obs/payload split, truncation, deterministic ids, routing, pricing) | Same codebase and data model as the Collector; fast enough for the span transform; no separate worker service to build. The upstream `opensearchexporter` is alpha and doesn't match our index layout |
| Evaluation engine, calibration, evaluation workers | **Python** (asyncio, Pydantic) | The ML ecosystem (sentence-transformers, scikit-learn, ONNX Runtime, LLM SDKs); the validated prototype; mostly waiting on hosted calls; scales out through Kafka consumer groups |
| API | **Python** (FastAPI) | Shares models and calibration code with the engine |
| CLI, SDK, pytest plugin | **Python** | Evaluator authors expect Python (DeepEval, Ragas, Inspect, pytest). Agents need no SDK from us: they emit OTel |
| Embeddings and local heads at serving time | **Off the shelf**: Hugging Face `text-embeddings-inference` or ONNX Runtime | Fast serving without writing Rust ourselves |
| Hosted evaluators | OpenRouter (chat + Decisions API) or direct provider APIs | Cached, budget-capped client; pinned model versions |
| UI | **TypeScript** + React | Standard; the PayMe team already works in TypeScript |
| Queue | Kafka (Redpanda for small installs) | |
| Event store / config store / blobs | OpenSearch 3.x / Postgres / S3-compatible | §5.3, §5.4 |
| Deployment | Docker Compose for development; Helm for production | |

**Why not Rust.** Rust pays off when you build a storage or query engine; Laminar and Braintrust use it for that. OpenSearch does that job for us. Our own hot path is transforming spans in the ingest pipeline, which Go handles, and Rust cannot write Collector components. It is also the steepest learning curve for a Python and TypeScript team. If the phase-2 load test shows ingest CPU is the bottleneck, that one stateless exporter can be rewritten in Rust later.

**Why not all-Go.** It would mean giving up the Python ML and evaluation ecosystem, and users want to write evaluators in Python.

**Why not all-Python.** CPU cost at billions of spans, and Collector extensions must be Go anyway.

**Fallback if the team wants to delay Go.** Run the stock Collector, write the ingest exporter in Python first, and move it to Go after the load test. Not recommended: the Collector customisation (tenant tagging, normaliser, index layout) needs Go from day one.

---|---|---|
| Engine, SDK, workers, API | Python (FastAPI, Pydantic) | The prototype is Python; the ML ecosystem; the PayMe engine is Python |
| Local heads | sentence-transformers (multilingual-e5-small), scikit-learn; ONNX Runtime for serving | CPU; SetFit for few-shot refreshes |
| Hosted evaluators | OpenRouter (chat + Decisions API) or direct provider APIs | Cached, budget-capped client; pinned model versions |
| Collector | `otelcol-contrib`: `loadbalancing`, `tail_sampling`, `redaction`, `transform` | The redaction processor is beta |
| Queue | Kafka (Redpanda for small installs) | |
| Event store | OpenSearch 3.x | §5.3 |
| Config store | Postgres | |
| Blobs | S3-compatible | |
| UI | React + TypeScript | |
| Deployment | Docker Compose for development; Helm for production | |

---

## 12. Roadmap

Each phase ends with a measurable gate.

| Phase | Scope | Exit gate |
|---|---|---|
| **0. Done** | Research; layer experiments; `elastic_evals` engine (suites, calibration, per-conversation runtime, audit/drift, local-only, CLI); synthetic sets A/B/C | On holdout C: matches the best single judge at lower cost and latency (✓) |
| **1. Real-traffic validation** | Collect ~200 real, anonymised agent conversations; label them through a simple annotation CLI; calibrate; add `max_ms_per_conv`; multi-class checks; per-language reporting | Engine within 1 SE of the best judge per check on real labels, or a documented reason per check |
| **2. Storage path + load test** | Collector config, Kafka, ingest workers, OpenSearch templates and ISM, Postgres schema, trace-complete tracker, `traces-*` rollups; replay synthetic traces at 2–3× target peak | Ingest latency, GB per million spans and dashboard times within budget, or switch the analytics sink |
| **3. Engine as a service** | Evaluation workers on the stream; scores into `scores-*`; calibration lifecycle (candidate → shadow → active); drift alerts; eval-aware retention | Drift detection and recalibration work end to end on replayed traffic |
| **4. Humans and offline** | Annotation queues with automatic filling; distillation loop; datasets and experiments; pytest plugin and CI gates; `synth` | A distilled head replaces judge calls on at least one check without breaching its floor |
| **5. Sentry bridge** | Propagation guide; `before_send` helper; webhook bridge; External Issue links; quality-regression events | Click-through both ways on a demo; self-hosted proof of concept |
| **6. UI and governance** | The 5 screens; two-tier tenancy; audit log; Helm chart | First external tenant |

---

## 13. Risks and open questions

| Risk / question | Mitigation |
|---|---|
| Results don't transfer from synthetic to real traffic | Phase 1 gate; calibration on real labels; the drift monitor |
| Subjective checks (frustration) plateau around 80–85% | Better definitions with examples, human labels, per-tenant heads; publish per-check accuracy rather than hide it |
| OpenSearch cost and speed at billions of spans | Eval-aware retention, rollups, star-tree metrics, a load test before commitment; possible ClickHouse analytics sink |
| GenAI semconv still changing | Pin the version, normalise at ingest |
| Hosted evaluator drift and deprecation (e.g. model expiry dates on OpenRouter) | Pinned versions; shadow re-validation; calibration chooses the alternatives |
| Jev and other decision models are hosted only | Optional; excluded under local-only |
| Evaluator model licenses (Llama, Gemma, CC-BY-NC Lynx) | Default to Apache/MIT models; license check per adapter |
| Self-hosted Sentry over OTLP is unproven | Errors-only first; proof of concept in phase 5 |
| Tail sampling on long agent runs | Our retention decision is final; larger `decision_wait` |
| Building the UI is the bulk of the work | Five screens; OpenSearch Dashboards for the rest |

---

## Appendix A: Evidence index

- Single-layer experiments: [`experiments/results/RESULTS.md`](../experiments/results/RESULTS.md), [`tables.md`](../experiments/results/tables.md)
- Engine: [`experiments/results/ENGINE_SUMMARY.md`](../experiments/results/ENGINE_SUMMARY.md), [`ENGINE.md`](../experiments/results/ENGINE.md), the calibration JSONs in `experiments/results/`
- Code:
  - `elastic_evals/`: the engine
  - `experiments/`: generators, runners, analysis
  - `examples/banking_suite.yaml`: an example suite
- Research: [Langfuse](research/langfuse.md) · [other evaluation tools](research/oss-evals.md) · [OpenSearch](research/opensearch.md) · [Sentry](research/sentry.md) · [classifier evaluators](research/classifiers.md)

## Appendix B: Classifier and judge shortlist

From [`research/classifiers.md`](research/classifiers.md); only Jev was tested.

| Need | First choice | Notes |
|---|---|---|
| Typed hosted classifier | Jev 1.14 (pinned) | Tested: strong on fee and routing, no Spanish drop seen |
| Multilingual safety | Qwen3Guard-Gen 0.6B / 4B (Apache-2.0) | "Controversial" is a natural escalation trigger |
| Prompt injection | Llama Prompt Guard 2 86M | Gated download; calibrate against your own definition (L12) |
| Groundedness | Vectara HHEM-2.1-Open (Apache-2.0) | English |
| Zero-shot labels | mDeBERTa XNLI (MIT) | Slow on CPU for many labels |
| PII | Microsoft Presidio (MIT) | Tune per language |
| Small judge | Flow Judge (Apache-2.0) / Prometheus 2 | English-first |

## Appendix C: Glossary

| Term | Meaning |
|---|---|
| **Check** | A binary judgment about a conversation |
| **Suite** | A versioned set of checks plus policy |
| **Route** | The ordered layers a check may use |
| **Strictness** | The accuracy a cheap layer must reach on its confident decisions |
| **Floor** | The minimum acceptable accuracy for a check (best minus tolerance or SE) |
| **Shadow score** | A free answer from a hosted call made for another check |
| **Audit** | A random re-score by an independent judge; the drift monitor |
| **Calibration** | A versioned artifact holding routes, thresholds, local heads and the report |
| **Local-only** | A policy under which no conversation text leaves the deployment |
