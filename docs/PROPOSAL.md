# elastic-evals: proposal

Status: draft for discussion · 2026-10-20

## 1. What we are building

An open-source **observability and evaluation system for production AI agents**:

- **Instrumentation:** agents are instrumented once with OpenTelemetry.
- **Storage:** OpenSearch holds the event data and Postgres holds configuration.
- **Evaluation:** every turn is scored by a cascade of evaluators: deterministic checks, cheap classifiers (Jev and open models), sampled LLM judges, and human review. A calibration loop keeps the cheap evaluators honest.
- **Sentry:** linked by a shared trace ID, so you can jump from an error to the agent trace and from a bad trace to the error, in both directions.

It borrows Langfuse's concepts (data model, evaluator rules, datasets and experiments, annotation queues). It is aimed at teams that:

- run agents at very high volume,
- already operate OpenSearch, and
- need per-tenant isolation (banks and other regulated customers).

The inspiration is the "Observability, traces and evals" section of the PayMe agent architecture. That design used Langfuse for LLM traces and OpenSearch for operational telemetry. This project collapses the two into one OpenSearch-native system and builds the eval workflow ourselves.

## 2. Is the idea sound? Assessment

**Yes, with the trade-offs stated plainly.**

| Idea | Verdict | Why |
|---|---|---|
| Borrow Langfuse concepts | **Yes** | Its v4 model (one wide, append-only document per span, with trace attributes copied onto it) is exactly the shape OpenSearch handles well. Its evaluator, rule and job model is mature and worth copying. Core is MIT. |
| OpenSearch as the event store | **Yes, for the right reasons** | Wins on: full-text search over conversations, k-NN (similar failures, dataset mining), open security (document-level security, audit), and running on infrastructure banks already have. Loses to ClickHouse on aggregation speed and disk footprint: ClickHouse's own study found Elasticsearch ~5× larger on disk; there is no public OpenSearch-vs-ClickHouse benchmark. So "it handles massive data" is a design goal we must engineer for (§6), not a free property. |
| Jev and other classifiers as evaluators | **Yes, our strongest differentiator** | At very high volume only classifiers are cheap enough to score 100% of turns. Langfuse already supports Jev ("decision model" evaluators, Sept 2026). No open-source tool offers the full cascade: thresholds calibrated against human labels, escalation by confidence, and drift monitoring of the evaluators themselves. |
| Sentry integration | **Yes, kept narrow** | Sentry and OTel share the same W3C trace ID, so linking works without extra plumbing. Sentry should own *errors and alerts*; we own *traces, content and scores*. Do not mirror full traces or every score into Sentry (§9). |

**Honest positioning vs Langfuse.** Langfuse (acquired by ClickHouse in Jan 2026, MIT core) already ships much of the eval feature list, including Jev. We are not building "Langfuse but different". Our reasons to exist are:

1. **OpenSearch-native.** No open-source eval system uses OpenSearch or Elasticsearch as its store today. Full-text search and k-NN are built in, and teams can reuse the cluster and the operations skills they already have.
2. **Evaluation as a cascade with calibration built in.** Classifier → judge → human routing by confidence, raw probabilities stored, evaluator versions tracked, and agreement with human labels (Cohen's kappa, per-class precision/recall, calibration error) tracked per evaluator and slice.
3. **Eval-aware retention at scale.** Keep metadata for 100% of traffic. Keep full content when it matters: errors, Sentry-linked traces, low scores, flagged turns, plus a random sample. No tool we surveyed does priority sampling of this kind.
4. **Trace-level and trajectory evals.** These need a "trace complete" signal. Langfuse deprecated trace-level evaluators, and its idle-timer approach is cloud-only.
5. **A bridge to Sentry in both directions.** No eval tool links to an error tracker today.
6. **Governance not behind a license.** RBAC, retention, audit and masking are open. Langfuse gates these behind its EE license.

## 3. Goals and non-goals

**Goals (v1)**
- Accept OTel traces from any agent framework (GenAI semantic conventions, OpenInference, OpenLLMetry, Langfuse SDK attributes).
- Store spans, content, scores and embeddings in OpenSearch, isolated per tenant, with tiered retention.
- Run online evaluators (deterministic, classifier, LLM judge) at observation, trace and session scope, with sampling, filters and cascades.
- Run offline experiments on datasets from a CLI or pytest, with CI gates.
- Provide annotation queues and a calibration report for every evaluator.
- Link to Sentry in both directions.
- Minimal UI: trace viewer, score explorer, experiment comparison, annotation queue.

**Non-goals (v1)**
- Prompt management or a playground. Integrate with existing tools instead.
- Our own dashboards for operational metrics. Use OpenSearch Dashboards.
- A drop-in replacement for the Langfuse API.
- Hosting evaluator models. We call them over HTTP; serving them is the deployer's job.
- Elasticsearch support from day one. We keep a storage interface so it can be added later (§13).

## 4. Architecture

```
 Agents (OTel SDK, gen_ai.* semconv)        Sentry SDK (errors, OTLPIntegration)
        │  traceparent / baggage                     │ same trace_id
        ▼                                            ▼
 ┌──────────────────────────────┐            ┌───────────────┐
 │ OTel Collector (2-tier)      │            │    Sentry     │◄──────────┐
 │ redact · tenant tag ·        │            └──────┬────────┘           │
 │ load-balance by trace_id     │                   │ webhooks           │ external-issue links,
 └──────────────┬───────────────┘                   ▼                    │ eval-failure events
                ▼                            ┌───────────────┐           │
           Kafka (spans topic)               │ Sentry bridge │───────────┘
                │                            └──────┬────────┘
     ┌──────────┴──────────────┐                    │ issue status
     ▼                         ▼                    ▼
 Ingest workers          Eval workers  ◄── eval jobs (Kafka) ── Rule engine
 normalize semconv       tier 0 deterministic                    (filters, sampling,
 write obs/payload docs  tier 1 classifiers (Jev, open models)    cascades, trace-complete
 big payloads → S3       tier 2 LLM judge                         tracker)
 retention decision      write scores (deterministic ids)
     │                         │
     ▼                         ▼
 ┌─────────────────────────────────────────────┐    ┌──────────────────────────────┐
 │ OpenSearch                                  │    │ Postgres                     │
 │ obs-* · payload-* · scores-* · traces-* ·   │    │ tenants, projects, RBAC,     │
 │ embeddings-*  (per-tenant, ISM tiers)       │    │ evaluators+versions, rules,  │
 └─────────────────────────────────────────────┘    │ jobs, datasets, items, runs, │
                ▲                                   │ annotation queues, model     │
                │                                   │ prices, trace-complete timers│
          API (FastAPI) ◄── UI (React) · CLI · pytest plugin       └──────────────────────────────┘
```

**Where each kind of data lives:**
- **OpenSearch:** everything high-volume and append-only.
- **Postgres:** everything that is configuration, transactional or mutable.
- **S3:** large payloads (attachments, long tool results), plus call audio with consent only.

## 5. Ingestion and semantic conventions

- **Wire format:** OTel GenAI `gen_ai.*` semantic conventions, pinned to a specific version. They are still at *Development* stability and will change.
  - At ingest, normalize OpenInference, OpenLLMetry and `langfuse.*` attributes using a priority-ordered mapper, as Langfuse does.
  - Key attributes: `gen_ai.operation.name` (chat, execute_tool, invoke_agent, …), `gen_ai.agent.*`, `gen_ai.conversation.id` (our session key), `gen_ai.tool.*`, `gen_ai.usage.*`, `error.type`.
- **Our extension namespace** (`ee.*`): `ee.tenant_id`, `ee.experiment.run_id`, `ee.experiment.item_id`, `ee.sentry.event_id`, `ee.eval_flag`.
  - Use attributes, not span events. Sentry drops span events on OTLP ingest, and OTel's `gen_ai.evaluation.result` event lacks the evaluator kind, version and probabilities we need.
- **Collector:** two tiers.
  - Tier 1 load-balances by `trace_id`.
  - Tier 2 redacts personal data (Presidio-backed processor), tags the tenant, and routes the data.
  - Agents use an `always_on` sampler. All sampling decisions are made downstream, where we can see errors and scores.
- **Kafka** buffers between the Collector and the workers. This absorbs spikes and lets evaluators read the same stream.
- **Ingest workers:**
  - normalize spans;
  - compute cost from a model-price table (with a path to re-price later);
  - write the truncated `obs` document and the full `payload` document;
  - offload large blobs to S3;
  - update the trace-complete tracker.
- **Idempotency:** use deterministic document IDs (`span_id`, or a hash of the score) with `op_type: create` on time-based indices behind a write alias.
  - Do *not* use OpenSearch data streams for these indices. They reject custom `_id`s and `delete_by_query`, which costs us idempotent ingest and cheap GDPR deletes.
  - The cost: a duplicate is possible across a rollover boundary. These are removed at read time by `span_id`.

## 6. Data model

### 6.1 OpenSearch indices (per tenant, or pooled; see §10)

| Index | One document per | Contents | Notes |
|---|---|---|---|
| `obs-*` | span | Typed columns: ids, parent, trace and session ids, name, type (SPAN, GENERATION, AGENT, TOOL, CHAIN, RETRIEVER, EVALUATOR, EMBEDDING, GUARDRAIL), model, tokens (exclusive buckets), cost, latency, status, `date_nanos` times. Trace attributes copied on (user, session, tags, release, environment). Input/output truncated to ~200 chars. Metadata as `flat_object`. | Strict mapping, `zstd`, routing by `trace_id`. Never updated. Any field that needs charting gets promoted to a typed column, because `flat_object` cannot be aggregated. |
| `payload-*` | span (when content is kept) | Full input/output messages, system prompt, tool arguments and results. `match_only_text` for search; raw JSON with `index: false`. | Written only when the retention decision keeps the content. Shorter retention than `obs`. |
| `scores-*` | score | See §7.2. Copies trace name, tags, user, model, agent, flow and experiment ids so scores can be filtered without joins. | Append-only. A correction is a new score that `supersedes` the old one. |
| `traces-*` | completed trace | Rollup written when a trace completes: total cost, tokens and latency, span count, error flag, tool sequence, latest score per name, Sentry issue ids. | Avoids Langfuse's pain point of expensive trace-level aggregates. This index is the main source for list views. |
| `embeddings-*` | turn or trace | `knn_vector` (on-disk mode, quantized) of the user turn or failure summary. | Default codec, because k-NN does not support `zstd`. Used for "find similar failures" and dataset mining. |

### 6.2 Postgres

The Postgres tables follow Langfuse's model:
- `tenants`, `projects`, `memberships`, `roles`, `api_keys`
- `evaluators` → `evaluator_versions` (immutable) → `eval_rules` (filter, sampling rate, delay, scope, variable mapping, cascade) → `eval_jobs` (status, attempts, cost)
- `datasets` → `dataset_items` (with valid-from/valid-to so items have versions) → `experiment_runs`
- `annotation_queues` → `annotation_items` (assignment, status)
- `score_configs` (name, data type, allowed categories, direction: maximize or minimize)
- `model_prices` (regex match, pricing tiers, start date)
- `trace_tracker` (trace id, last span time, due time) for the trace-complete signal
- `sentry_links` (cache of trace id ↔ Sentry issue)

Experiments are recorded as attributes on spans, as in Langfuse v4. A separate join table for run items is not needed.

## 7. Evaluation engine

### 7.1 Evaluator interface (Python SDK)

```python
class Evaluator(Protocol):
    id: str
    version: str                       # immutable; scores always record it
    kind: Literal["deterministic", "classifier", "llm_judge", "human"]
    scope: Literal["observation", "trace", "session", "experiment_item"]

    def evaluate(self, target: EvalTarget) -> list[Score]: ...
```

`EvalTarget` gives the evaluator the span or trace tree, the mapped variables, and the dataset's expected output when there is one.

Built-in adapters:
- **Jev**, through its question types (yes/no maps to `p_true`, choice maps to a categorical score with its distribution, score maps to ordered levels).
- **Generic HTTP classifiers** returning probabilities: HHEM, Prompt Guard, mDeBERTa zero-shot NLI, ShieldGemma.
- **LLM judges**, forced to call a tool that returns a label and an explanation.
- **Code checks** for schema, tool-call validation, regex and Presidio checks, and comparing amounts and recipients against the real records.

### 7.2 Score shape

```jsonc
{
  "id": "sha256(evaluator_version|target_id|criteria_version)",   // deterministic
  "tenant_id": "...", "trace_id": "...", "observation_id": "...", "session_id": "...",
  "name": "booking_correct", "data_type": "numeric|categorical|boolean|text",
  "value": 0.91, "label": "correct",
  "probs": {"correct": 0.91, "incorrect": 0.09},   // always stored for classifiers
  "confidence": 0.82, "explanation": "...",
  "evaluator": {"id": "jev-booking", "version": "3", "kind": "classifier",
                "model": "jev-1.13.0", "criteria_version": "2"},
  "tier": 1, "sample_reason": "all|band|random_audit|escalated",
  "direction": "maximize", "latency_ms": 240, "cost_usd": 0.000004,
  "supersedes": null, "source": "online|experiment|annotation",
  "experiment": {"run_id": null, "item_id": null},
  "trace_ctx": {"name": "...", "agent": "...", "flow": "...", "model": "...", "tags": []}
}
```

The rule: **store the raw output and the probabilities, never only the decision after the threshold.** Then thresholds can be changed later without running the evaluators again.

### 7.3 The cascade

| Tier | Runs on | Purpose |
|---|---|---|
| 0: deterministic | 100% | Schema and tool-call validity, PII leaks, claimed success with no operation record behind it, critical fields checked against records. **Critical metrics live here, never in a classifier.** |
| 1: classifiers | 100% (or a high fraction) | Routing correctness, frustration, injection, groundedness, safety, topic. Thresholds set per evaluator and per slice for a target precision. |
| 2: LLM judge | Uncertain band of tier 1, plus a random audit slice | A judge from a *different model family* than the agent and than any runtime classifier making the same decision (no correlated judging). Has a cost cap per rule. |
| 3: human | Stratified sample, plus every tier-1/tier-2 disagreement | Annotation queues fill automatically from score conditions. Human labels are the ground truth for calibration. |

Cascade rules are ordinary eval rules with a trigger on another evaluator's score, for example `when jev-routing.confidence < 0.7 or sample(2%) → judge-routing`.

**How sampling works:** deterministic, using a hash of the span ID against the rule's rate, as Langfuse does. Every span is either always in or always out, so results are reproducible.

**Recursion guard:** traces produced by the evaluators themselves get `environment=ee-internal` and are never evaluated.

### 7.4 Trace-complete and trajectory evals

A trace counts as complete when its root span has ended and an idle timeout has passed. The timeout is configurable per agent; voice calls and missions need minutes. The tracker is a Postgres table with a due time per trace; replace it with a stream processor if volume requires.

When a trace completes:
1. write the `traces-*` rollup;
2. run trace-scope evaluators (task completion, tool-sequence F1, plan adherence, step efficiency);
3. make the retention decision.

Sessions work the same way, keyed on `gen_ai.conversation.id`, with a longer cooldown.

### 7.5 Calibration loop

For each evaluator version and slice (tenant, language, flow), compute against human labels:
- Cohen's kappa (weighted for ordinal scores),
- per-class precision and recall,
- expected calibration error,
- tier-1 vs tier-2 agreement.

The API and UI show a **calibration report** for each evaluator. Drift alerts fire when agreement drops.

A new evaluator version is shadowed against the old one on the human-labelled set before it is promoted.

To get valid confidence intervals from mixed human and automatic labels, use prediction-powered inference (stratified PPI, Fisch et al. 2024).

### 7.6 Offline experiments and CI

- **Datasets** have items with versions. Items can be created from traces in bulk, or via a k-NN search for "similar failures".
- **Runner:** the `ee run` CLI and a pytest plugin run the agent over a dataset. Spans are tagged `ee.experiment.*` and the same evaluators run on them.
- **Gates:** thresholds per metric. Gates on critical metrics are absolute (zero false confirmations); the rest compare against a baseline run.
- **Results** go into the same indices, so comparing an experiment with production is just a query.

## 8. Retention and sampling

| Data | Kept | Default retention |
|---|---|---|
| `obs` metadata, scores, `traces` rollups | 100% | hot 14 days → warm (searchable snapshot) 90–180 days → snapshot archive |
| `payload` (full content) | errors, Sentry-linked traces, tier-0 failures, flagged or low-score turns, annotation and dataset items, plus a random N% | shorter, per tenant (e.g. 30 days hot) |
| Audio and large blobs | consent only | S3, per-tenant lifecycle; traces keep a reference |

The retention decision happens *after* tier 0 and tier 1 run. Evaluators see the full content while it is still in the stream, and only the turns that matter keep their content. This is the main lever for keeping OpenSearch storage affordable.

## 9. Sentry integration

**Division of work:**
- Sentry handles exceptions, alerting and issue workflow.
- elastic-evals handles traces, content, scores and the evaluation workflow.
- Both share one W3C trace ID.

**Propagation:** use the Sentry SDK's `OTLPIntegration`. Sentry errors then carry the active OTel trace and span IDs.
- Set `OTEL_PROPAGATORS=tracecontext,baggage` and disable Sentry's own propagator setup. Sentry otherwise writes `sentry-trace` headers, which avoids a conflict over `traceparent` (not yet verified).
- Do not set `traces_sample_rate` in Sentry; tracing belongs to OTel.

**Sentry → our trace:**
- In `before_send`, tag the event with `trace_id`, `gen_ai.conversation.id`, `gen_ai.agent.name` and a deep-link URL to our trace view.
- In the same hook, write `ee.sentry.event_id` onto the active span and set the span status to ERROR. Without this, a *handled* exception leaves the span OK and the trace's content could be dropped.
- The **bridge** service receives Sentry issue and alert webhooks and calls Sentry's External Issue API. That puts a clickable "View agent trace" link on the Sentry issue.

**Our trace → Sentry:**
- The trace view reads `ee.sentry.event_id` and the cached issue ID.
- Fallback: look up Sentry issues by `trace_id` (the endpoint is partly undocumented, so cache the result and degrade gracefully).
- The bridge copies issue status (resolved, regressed) back into `traces-*` and `sentry_links`.

**Eval failures into Sentry:** send only aggregated threshold breaches, using `capture_message` with a stable fingerprint per evaluator, agent and tenant. Examples: "false-confirmation detected" and "routing accuracy below 90% over 1h". Sentry's alerting and on-call routing then work for quality regressions too. Optionally send cron check-ins for scheduled eval jobs.

**Never sent to Sentry:**
- prompt, response or tool content (Sentry does not scrub the gen_ai content attributes by default),
- full-volume traces,
- individual scores,
- span events.

As a backstop, strip personal data at the Collector, keep `send_default_pii=false`, and add Sentry data-scrubbing rules.

**Self-hosted Sentry (banks):** start with errors only.
- OTLP ingest and AI Agents Insights on self-hosted are unproven. Both need a proof of concept before we rely on them.
- Self-hosted Sentry needs at least 16 GB RAM, more with spans enabled.

## 10. Tenancy, security and privacy

- **Two tiers of tenancy:**
  - Regulated tenants (banks) get dedicated indices, roles, retention, a snapshot repository, and optionally their own cluster.
  - Small tenants share pooled indices, with `tenant_id` routing, document-level security from a JWT attribute, a mandatory tenant filter in the application, and Workload Management groups.
- **Watch the shard count.** Index-per-tenant runs into the per-node shard limit, so roll indices over by size (30–50 GB per primary), not daily.
- **Redact at ingest.** OpenSearch field masking is hash-only and breaks search, so redaction must happen at the Collector.
- **GDPR deletion:** per-tenant time-based indices plus `delete_by_query` for a single user (another reason not to use data streams). Payloads have short retention.
- **Audit:** audit logs from the OpenSearch security plugin, plus our own API audit log in Postgres. RBAC, retention, audit and masking are all in the open-source core.
- **Eval datasets built from real conversations** need per-tenant consent and stay separated by tenant (a rule carried over from PayMe).

## 11. UI scope (v1)

This is the part Langfuse gives away for free and we must build. Keep it small:
1. **Trace viewer:** span tree and timeline, content, scores next to each span, Sentry links.
2. **Search:** full-text and filtered search over traces and sessions, plus "similar traces" through k-NN.
3. **Score explorer:** distributions, slices, and the calibration report per evaluator.
4. **Experiment comparison:** runs side by side per item, with diffs and gate status.
5. **Annotation queue:** keyboard-driven labelling, with labels feeding back into calibration.

Operational dashboards (latency, cost, error rate) are saved objects in OpenSearch Dashboards, which ship with the project.

## 12. Technology choices (proposed)

| Area | Pick | Why |
|---|---|---|
| SDK, evaluators, API | Python (FastAPI, Pydantic) | ML and evaluator ecosystem; matches PayMe's engine stack |
| Ingest workers | Python first; Go if profiling demands it | One language to start |
| Collector | Upstream `otelcol-contrib` with config we ship | `loadbalancing`, `tail_sampling`, `redaction`, `transform` processors |
| Queue | Kafka (Redpanda for small installs) | Replay, multiple consumers, backpressure |
| Event store | OpenSearch 3.x | §2 |
| Config store | Postgres | Transactions, foreign keys, small data |
| UI | React + TypeScript | — |
| Deployment | Docker Compose for development, Helm chart for production | — |

## 13. Risks and open questions

| Risk or question | Mitigation or decision needed |
|---|---|
| OpenSearch analytics and storage cost at billions of spans | Keep payloads out of the hot index, write `traces-*` rollups, use star-tree on a metrics index for fixed dashboards, and retain content based on eval results. **Load-test early** (2–3× target peak). Keep a ClickHouse adapter as a possible analytics sink later. |
| **OpenSearch vs Elasticsearch?** (the repo is named *elastic*-evals) | Recommend OpenSearch first (Apache-2.0, open security plugin), behind a storage interface. Elasticsearch has ES\|QL `LOOKUP JOIN` and logsdb, but synthetic source is Enterprise-only and the licensing is AGPL/SSPL/ELv2. **Decide now.** |
| GenAI semantic conventions still changing | Pin the version and normalize at ingest. |
| Jev quality in Spanish | An independent audit found 3–6 points lower accuracy in Spanish. Validate on our own Spanish data before relying on it. Jev is hosted only; check each tenant's data policy. |
| Evaluator model licenses | Patronus Lynx is non-commercial, and Llama and Gemma have custom licenses. The default set should favour Apache and MIT models (Qwen3Guard, HHEM-2.1-Open, Granite Guardian, Flow Judge, mDeBERTa XNLI, Presidio). |
| Self-hosted Sentry OTLP and AI Insights | Proof of concept. Start with errors only. |
| Tail sampling across long agent runs | Make `decision_wait` larger and turn on the decision cache. Make retention decisions in our own workers rather than only in the Collector. |
| Building the UI is the bulk of the work | Keep the v1 UI to the 5 screens in §11 and use OpenSearch Dashboards for the rest. |

## 14. Roadmap

1. **Phase 0: foundations.**
   - Repo skeleton and Docker Compose (OpenSearch, Postgres, Kafka, Collector).
   - Index templates and ISM policies.
   - Postgres schema.
   - Semconv mapper with fixtures from LangGraph, the OpenAI Agents SDK and the Langfuse SDK.
   - A synthetic trace generator for load tests.
2. **Phase 1: ingest and view.**
   - Ingest workers, `obs` and `payload` writes, trace-complete tracker and `traces-*` rollups.
   - Minimal API, trace viewer and search.
   - **Load test at this point** and revisit the OpenSearch decision with real numbers.
3. **Phase 2: evaluation engine.**
   - Evaluator SDK, the rule engine (filters, sampling, cascades) and eval workers.
   - Adapters for Jev, HTTP classifiers and LLM judges.
   - `scores-*`, the score explorer and deterministic score IDs.
4. **Phase 3: offline and humans.**
   - Datasets, the experiment runner (CLI and pytest) and CI gates.
   - Annotation queues and the calibration report.
5. **Phase 4: Sentry bridge.**
   - Propagation guide, the `before_send` helper, the webhook bridge, External Issue links and eval-failure alerts.
6. **Phase 5: scale and governance.**
   - Two-tier tenancy, eval-aware retention, k-NN failure clustering, Helm chart, audit log.

## 15. Research

Detailed reports with sources are in [`docs/research/`](research/):
- [langfuse.md](research/langfuse.md): v4 data model, ingestion, evaluator and rule engine, licensing, pain points
- [oss-evals.md](research/oss-evals.md): Phoenix, Opik, MLflow, DeepEval, Ragas, promptfoo, Inspect, Laminar, Braintrust and others; status of the OTel GenAI conventions
- [opensearch.md](research/opensearch.md): index design, tenancy, retention tiers, comparison with ClickHouse, differences from Elasticsearch
- [sentry.md](research/sentry.md): OTel interoperability, AI Agents Insights, linking APIs, sampling pitfalls, self-hosted limits
- [classifiers.md](research/classifiers.md): Jev, open guard and judge models, calibration methodology, cascade literature

Facts tagged `[unverified]` in those reports need to be confirmed before we build on them.
