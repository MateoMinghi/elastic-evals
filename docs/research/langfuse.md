# Langfuse deep-dive for an OpenSearch-backed evals + observability framework

Research date: 2026-10-20. Code read from a shallow clone of `langfuse/langfuse` at commit `bde3073` (2026-10-19), package version `4.64.0`, at `/tmp/claude-0/-home-user-elastic-evals/4f62dd2a-8f5e-5c00-9bde-e6748ce07bfd/scratchpad/langfuse-src` (read only, never executed). Docs and blog pages were fetched from langfuse.com and summarised by a small model, so wording of "quotes" comes from those summaries. Items I could not verify are marked **[UNVERIFIED]**. Statements that are my own design inference, not Langfuse fact, are marked **[INFERENCE]**.

Context worth knowing: ClickHouse, Inc. acquired Langfuse (announced 2026-01-16). The repo LICENSE now reads "Copyright (c) 2023-2026 ClickHouse, Inc." ClickHouse stated the MIT core stays and Cloud keeps running standalone. Source: https://clickhouse.com/blog/clickhouse-acquires-langfuse-open-source-llm-observability

---

## 0. Executive summary

1. Langfuse v4 (cloud preview 2026-03-10, GA for self-host mid/late 2026) replaced the `traces` + `observations` + `scores` ClickHouse tables with one wide, append-only table of **observations** (`events_full`), where the trace is just a correlation id (`trace_id`) and trace-level attributes (user, session, tags, release, version, trace name) are copied onto every row. A second table `events_core` is a materialized-view projection with `input`/`output`/`metadata` values truncated to 200 chars, used for lists and dashboards. This is the single most important design lesson. It was driven by ClickHouse-specific costs (ReplacingMergeTree full-row rewrites, `FINAL`, `LIMIT 1 BY`, trace-observation joins).
2. Langfuse already ships almost exactly your evaluator trio: **LLM-as-a-judge**, **code evaluators** (Python/TS, sandboxed, 2 s limit, no network) and **decision-model evaluators** (TypeSafe Jev, a typed classifier, launched 2026-09-22). All three share one rule engine: filter, deterministic hash sampling, delay, variable mapping, per-execution job record in Postgres, and scores written back with deterministic ids. Jev is priced per the changelog at $0.042 per million input tokens.
3. Evaluation is **observation-level** in v4. Trace-level evaluators are deprecated (Cloud stops them 2026-11-16, self-host `events_only` stops them immediately). Observation evaluators only see the matched observation (input/output/metadata/tool_calls), not children. Whole-trace or agent-trajectory evaluation is a gap you can fill. Langfuse has an internal, cloud-gated "trace batch / idle readiness" experiment that points the way.
4. OTel is the only first-class ingestion path going forward. The legacy `POST /api/public/ingestion` stops accepting everything except scores on Cloud on 2026-11-16. The OTLP endpoint (`/api/public/otel`, HTTP protobuf and JSON, no gRPC) maps `langfuse.*`, GenAI semconv, OpenInference, Vercel AI SDK, Traceloop, MLflow, Pydantic AI, Genkit, LiveKit, Google ADK and more through a priority-ordered type mapper.
5. Biggest pain points: trace-level aggregates (cost per trace) became "extremely inefficient to compute" on the wide table (issue #16077), ClickHouse memory/part-count tuning, S3 + Redis + Postgres + ClickHouse + worker operational weight, v3-to-v4 migration risk (data loss if background migrations unfinished, 3x disk, no simple rollback), UI queries without time bounds (#18299), up-to-15-minute delay for older SDKs.

---

## 1. Data model

### 1.1 Where each thing lives

| Thing | Store | Notes |
|---|---|---|
| Observations (spans, generations, events, tool, agent...) and "traces" | ClickHouse `events_full` / `events_core` (v4); legacy `traces`, `observations` still exist for v3 reads | Trace = root observation. Old data gets synthetic root span id `t-<trace_id>`. |
| Scores | ClickHouse `scores` (kept in v4, separate table) | Not denormalized onto observations. |
| Dataset run items | v4: folded into event rows as `experiment_*` columns; legacy `dataset_run_items_rmt` dropped | See 1.8. |
| Audit logs | ClickHouse `audit_logs` (migration 0052) with a Postgres outbox `audit_log_staging` | Legacy Postgres `audit_logs` still in schema. |
| Topics (clustering) | ClickHouse `topics`, `topic_facet_summaries`, `topic_assignments` | Embeddings as `Array(Float32)`. |
| Config/transactional: orgs, projects, API keys, role assignments, score configs, annotation queues, datasets, dataset items, dataset runs, comments, prompts, skills, models/prices, evaluators, rules, job executions, dashboards, widgets, monitors, automations, integrations, batch exports, media | Postgres via Prisma (`packages/shared/prisma/schema.prisma`, 2246 lines) | Most carry `project_id`. |
| Raw ingestion events, media, exports | S3-compatible blob store | Raw events written before queueing. |
| Queues, caches, rate limits | Redis 7+ (BullMQ, Lua) | Sharded queues. |

Sources: https://langfuse.com/self-hosting/upgrade/upgrade-guides/upgrade-v3-to-v4 , https://langfuse.com/self-hosting , code `packages/shared/clickhouse/migrations/canonical/*.sql`, `packages/shared/prisma/schema.prisma`.

### 1.2 Observation row (ClickHouse `events_full`, migration 0039 + later ALTERs)

Engine: `ReplacingMergeTree(event_ts, is_deleted)`, `PARTITION BY toYYYYMM(start_time)`, `PRIMARY KEY (project_id, toStartOfMinute(start_time), xxHash32(trace_id))`, `ORDER BY (project_id, toStartOfMinute(start_time), xxHash32(trace_id), span_id, start_time)`, `SAMPLE BY xxHash32(trace_id)`. Settings: `index_granularity_bytes = 64Mi` (default 10 MiB), `merge_max_block_size_bytes = 64Mi`, block number/offset columns, prewarm caches, full-text index enabled.

Identity and time
- `project_id`, `trace_id`, `span_id`, `parent_span_id` (empty string for root)
- `start_time`, `end_time` (DateTime64(6)), `completion_start_time` (time to first token)

Core properties
- `name`, `type` (LowCardinality), `environment` (LowCardinality, default `'default'`), `version`, `release`, `level` (DEBUG, DEFAULT, WARNING, ERROR), `status_message`, `is_app_root`
- Denormalized trace attributes on every row: `trace_name`, `user_id`, `session_id`, `tags Array(String)`
- Updateable UI flags: `bookmarked`, `public`

Prompt: `prompt_id`, `prompt_name`, `prompt_version`.

Model, usage, cost
- `model_id` (internal model definition id), `provided_model_name`, `model_parameters` (JSON string)
- `provided_usage_details` and `usage_details` `Map(LowCardinality(String), UInt64)`; `provided_cost_details` and `cost_details` `Map(LowCardinality(String), Decimal(18,12))`
- Materialized: `calculated_input_cost`, `calculated_output_cost`, `calculated_total_cost`; alias `total_cost = cost_details['total']`
- `usage_pricing_tier_id`, `usage_pricing_tier_name`

Tools and skills: `tool_definitions Map(String,String)`, `tool_calls Array(String)`, `tool_call_names Array(String)`; `skills_available`, `skills_resource_loaded` (arrays of tuples, migration 0053).

I/O: `input`, `output` (ZSTD(3)), materialized `input_length`, `output_length`.

Metadata: stored as two parallel arrays `metadata_names`, `metadata_values`, with text indexes. Only top-level metadata keys are filterable.

Experiment columns (replace dataset_run_items join): `experiment_id`, `experiment_name`, `experiment_metadata_names/values`, `experiment_description`, `experiment_dataset_id`, `experiment_item_id`, `experiment_item_version`, `experiment_item_expected_output`, `experiment_item_metadata_names/values`, `experiment_item_root_span_id`.

Evaluator-execution columns (migration 0047, defaulted from metadata): `evaluator_id`, `evaluation_rule_id` (falls back to legacy `job_configuration_id`), `evaluator_execution_is_test`.

Instrumentation: `source`, `service_name`, `service_version`, `scope_name`, `scope_version`, `telemetry_sdk_language/name/version`; ingestion attribution `ingestion_api_key`, `ingestion_sdk_name`, `ingestion_sdk_version`.

Housekeeping: `blob_storage_file_path`, `event_bytes`, `created_at`, `updated_at`, `event_ts`, `is_deleted`.

Skip indexes: bloom filters on `span_id`, `trace_id`, `user_id`, `session_id`; minmax on `created_at`/`updated_at`; token text indexes on `lower(input)`, `lower(output)`, `metadata_values`, `metadata_names`; (core only) bloom on `provided_model_name`, `experiment_id`, `metadata_names`, `evaluator_id`, `evaluation_rule_id`, plus n-gram indexes on names/metadata (0043, 0049).

`events_core` (0040/0041, MV `events_core_mv`) is the same column set without the full-text I/O indexes, and the MV inserts `leftUTF8(input, 200)`, `leftUTF8(output, 200)` and `arrayMap(v -> leftUTF8(v,200), metadata_values)`. Per the blog, "Dashboards and table loads use the core table, and the full table is used only for individual record retrieval."

### 1.3 Observation types

Ten values (`packages/shared/src/domain/observations.ts`): `SPAN, EVENT, GENERATION, AGENT, TOOL, CHAIN, RETRIEVER, EVALUATOR, EMBEDDING, GUARDRAIL`. Definitions from docs: event = point-in-time; span = unit of work with duration; generation = model call with prompt and usage/cost; agent = controls flow and calls tools; tool = one action; chain = connects steps passing context; retriever = read-only lookup; evaluator = assesses output quality; embedding = embedding call; guardrail = protects against malicious content. Code treats `GENERATION, AGENT, TOOL, CHAIN, RETRIEVER, EVALUATOR, EMBEDDING, GUARDRAIL` as "generation-like" (`isGenerationLike`). The docs page says only generation and embedding carry usage/cost, so code and docs differ slightly. Source: https://langfuse.com/docs/observability/features/observation-types

Levels: DEBUG, DEFAULT, WARNING, ERROR.

### 1.4 Traces, sessions, users, environments, tags, metadata

- **Trace**: no longer an entity in v4. `trace_id` is "a correlation identifier, like `session_id` or `user_id`" (blog). Trace-level `input`/`output` are deprecated in v4; use the root observation. Dashboards count traces as `uniq(trace_id)` (approximate) and bucket traces by root observation start time (https://langfuse.com/faq/all/dashboard-changes-in-v4).
- **Legacy `traces` row** (migration 0001): `id, timestamp, name, user_id, metadata Map, release, version, project_id, public, bookmarked, tags, input, output, session_id, created_at, updated_at, event_ts, is_deleted`; ReplacingMergeTree ordered by `(project_id, toDate(timestamp), id)`.
- **Sessions**: no analytical entity. Session = group of events sharing `session_id` (US-ASCII, under 200 chars, longer ids dropped). Postgres `trace_sessions(id, project_id, bookmarked, public, environment, created_at, updated_at)` only stores UI state. Sessions can be commented, scored, bookmarked, published. Source: https://langfuse.com/docs/observability/features/sessions
- **Users**: no table. `user_id` string on each row.
- **Environments**: `environment` column, default `default`. The ClickHouse `project_environments` table was dropped (migration 0045), so the list is derived from data. Internal environments are prefixed `langfuse-` (e.g. `langfuse-llm-as-a-judge`, `langfuse-code-eval`, `langfuse-prompt-experiments`) and are hidden by default. Evaluators never run on internal environments (loop guard).
- **Tags**: `Array(String)` on the trace/each row; merged as a sorted union on updates.
- **Metadata**: string map. Merge on update is deep merge.

### 1.5 Scores

Postgres is not used for scores. ClickHouse `scores` (0003 + ALTERs): `id, timestamp, project_id, environment, trace_id (nullable), observation_id, session_id, dataset_run_id, name, value Float64, source, comment (ZSTD), metadata Map, evaluator_id, evaluation_rule_id, author_user_id, config_id, data_type, string_value, long_string_value, queue_id, execution_trace_id, ingestion_api_key, ingestion_sdk_name, ingestion_sdk_version, created_at, updated_at, event_ts, is_deleted`; `ReplacingMergeTree(event_ts,is_deleted)`, `ORDER BY (project_id, toDate(timestamp), name, id)`, bloom indexes on id, (project,trace,observation), evaluator ids.

Domain (`packages/shared/src/domain/scores.ts`):
- `dataType`: `NUMERIC, CATEGORICAL, BOOLEAN, TEXT`, plus internal `CORRECTION` (output corrections, name fixed to `"output"`, never has a configId). Docs list only the first four. TEXT is 1-500 chars (`TEXT_SCORE_MAX_LENGTH`), with `long_string_value` for longer content.
- `source`: `API, EVAL, ANNOTATION`. The public create endpoint only allows API and ANNOTATION; EVAL is reserved for internal evaluators. ANNOTATION requires a `configId` (except CORRECTION).
- Boolean is stored as `value` 0/1 plus `string_value` "True"/"False"; categorical has `string_value` and optional numeric `value`.
- A score references exactly one target: trace, observation, session, or dataset run (`trace_id` is nullable since migration 0014).
- Score `id` doubles as an idempotency key. Evaluator-produced scores use `buildDeterministicEvalScoreIds({scores, jobExecutionId})`, so retries overwrite.
- `execution_trace_id` links an eval score to the trace of the evaluator run itself (judge prompt, response, tokens).
- Score Config (Postgres `score_configs`): `id, project_id, name, data_type (CATEGORICAL|NUMERIC|BOOLEAN|TEXT), is_archived, min_value, max_value, categories JSON (label/value pairs), description`. Immutable, archivable.

Sources: https://langfuse.com/docs/evaluation/scores/data-model , code above.

### 1.6 Annotation queues (Postgres)

- `annotation_queues(id, project_id, name unique per project, description, score_config_ids String[])`
- `annotation_queue_items(id, queue_id, object_id, object_type TRACE|OBSERVATION|SESSION, status PENDING|COMPLETED, locked_at, locked_by_user_id, annotator_user_id, completed_at)`
- `annotation_queue_assignments(project_id, queue_id, user_id)` (unique)
- Scores written from a queue carry `queue_id`. Workflow: create queue with score configs, add items from tables in bulk or single, reviewer sees an Annotate card, "Complete + next". API available. Source: https://langfuse.com/docs/evaluation/evaluation-methods/annotation-queues . The doc page does not describe locking; the schema shows lock fields.

### 1.7 Comments (Postgres)

`comments(project_id, object_type TRACE|OBSERVATION|SESSION|PROMPT, object_id, content, author_user_id, data_field ('input'|'output'|'metadata'), path String[] (JSON Path), range_start Int[], range_end Int[])` supports inline comments anchored to a JSON path and character range. `comment_reactions(comment_id, user_id, emoji)`. `notification_preferences` for `COMMENT_MENTION` email.

### 1.8 Datasets, items, runs, experiments

Postgres:
- `datasets(id, project_id, name unique, description, metadata, input_schema JSON, expected_output_schema JSON, remote_experiment_url/payload/enabled/secret key/headers)`. JSON-schema validation of items and a webhook to trigger remote experiments from the UI.
- `dataset_items(id, project_id, dataset_id, status ACTIVE|ARCHIVED, input, expected_output, metadata, source_trace_id, source_observation_id, valid_from, valid_to, is_deleted)` with PK `(id, project_id, valid_from)`. Items are **versioned by validity interval** (bitemporal-ish); a dataset can be fetched at a past version timestamp (SDK passes a version timestamp). The docs data-model page did not describe versioning, but the schema and the SDK page do.
- `dataset_runs(id, project_id, dataset_id, name unique per dataset, description, metadata)`.
- Legacy run items (ClickHouse `dataset_run_items_rmt`): `id, project_id, dataset_run_id, dataset_item_id, dataset_id, trace_id, observation_id, error, dataset_run_name/description/metadata/created_at, dataset_item_input/expected_output/metadata` (snapshot), `ReplacingMergeTree`, `ORDER BY (project_id, dataset_id, dataset_run_id, id)`. This table is dropped in v4.

v4 experiments: a run is represented by `langfuse.experiment.*` attributes (`id, name, metadata, description, dataset.id, item.id, item.version, item.metadata, item.root_observation_id, item.expected_output`) stamped on spans and stored in the `experiment_*` columns of each event row. Experiments on local data (no hosted dataset) appear in the Experiments UI. "Each dataset item appears at most once per experiment" (repetitions not supported, issue #5855). The public REST endpoint for creating runs is gone; runs come from SDK, UI or OTel, and the Experiments API is read-only. Run-level (aggregate) evaluator scores attach to the dataset run via `dataset_run_id`. Sources: https://langfuse.com/docs/evaluation/experiments/data-model , https://langfuse.com/docs/evaluation/experiments/experiments-via-sdk

SDK experiment runner: `run_experiment(...)` / `dataset.run_experiment(...)` (Python), `langfuse.experiment.run(...)` / `dataset.runExperiment(...)` (JS). A task runs per item, item-level evaluators produce scores on the item's trace, run-level evaluators produce dataset-run scores, `max_concurrency`, per-item failure isolation, autoevals adapter, GitHub Action `langfuse/experiment-action` with `RegressionError` for CI gating (https://langfuse.com/docs/evaluation/overview).

### 1.9 Prompts (Postgres)

`prompts(id, project_id, name, version Int, type 'text'|'chat', prompt JSON, config JSON, labels String[], tags String[], commit_message, created_by, is_active (deprecated))`, unique `(project_id, name, version)`. Versions are immutable and numbered; **labels** (`production`, `latest`, custom) are movable pointers; rollback = move label. `prompt_dependencies(parent_id, child_name, child_label|child_version)` give composability (prompts referencing prompts). `prompt_protected_labels` (EE). SDKs cache prompts client-side. Linking to generations: the SDK/OTel attribute `langfuse.observation.prompt.name` + `.version` is resolved at ingestion into `prompt_id/prompt_name/prompt_version` on the observation. The docs data-model page does not cover linking, so this comes from code (`IngestionService.getPrompt`, columns above). A newer `skills` feature (versioned skill files with sha256 blobs, `skills_available` / `skills_resource_loaded` on observations) mirrors the prompt design. Source: https://langfuse.com/docs/prompt-management/data-model

### 1.10 Models, tokens and cost

Postgres: `models(project_id nullable = global default, model_name, match_pattern (regex), start_date, input_price, output_price, total_price, unit, tokenizer_id, tokenizer_config)`, unique `(project_id, model_name, start_date, unit)`; `pricing_tiers(model_id, name, is_default, priority, conditions JSONB)`; `prices(model_id, pricing_tier_id, usage_type, price)`. Tier conditions test summed usage-detail keys (regex + gt/gte/lt/lte/eq/neq), or a top-level model-parameter/metadata key. Priority 0 is the default tier; first matching tier wins (e.g. Claude Sonnet 4.5 or Gemini 2.5 Pro above 200K input tokens).

Rules (docs): ingested values take priority over inferred; inference matches `model` against `match_pattern` at ingestion time and multiplies per-bucket price by usage; user-defined models win over built-in; applies to new data only (no retroactive recalculation); usage keys are **exclusive buckets** (`input` excludes `input_cached_tokens`, `output` excludes `output_reasoning_tokens`, `total` derived), and price keys must equal usage keys. OTel path normalises many provider shapes (OpenAI cached/reasoning detail, Anthropic `cache_creation_input_tokens` with 5m/1h split, Vercel AI SDK provider metadata, Genkit, Gemini cached content). Docs say no effective-date field is documented, yet the schema has `start_date` (**discrepancy, UNVERIFIED which is current**). Source: https://langfuse.com/docs/observability/features/token-and-cost-tracking

### 1.11 Other config entities worth knowing

- Dashboards: `dashboards(definition JSON, filters JSON, project_id nullable for Langfuse-managed)`, `dashboard_widgets(view TRACES|OBSERVATIONS|SCORES_NUMERIC|SCORES_CATEGORICAL|SCORES_BOOLEAN, dimensions, metrics, filters, chart_type LINE_TIME_SERIES|AREA_TIME_SERIES|BAR_TIME_SERIES|HORIZONTAL_BAR|VERTICAL_BAR|PIE|NUMBER|HISTOGRAM|PIVOT_TABLE, chart_config, min_version)`.
- Monitors: query shape (view, filters, metric) + `window_ms`, `cadence_ms`, threshold operator, alert/warning thresholds, severity state machine (PAUSED, UNKNOWN, NO_DATA, OK, WARNING, ALERT), scheduler batch columns, trigger ids.
- Automations: `triggers(event_source, event_actions, filter)` -> `automations` -> `actions(type WEBHOOK|SLACK|GITHUB_DISPATCH, config)`, with `automation_executions`.
- `table_view_presets`, `default_views` (saved filter views), `batch_exports`, `batch_actions` (bulk add-to-queue, bulk eval), `media` + `trace_media`/`observation_media`, `llm_api_keys` / `default_llm_models` / `llm_schemas` / `llm_tools` (for playground and evals), `pending_deletions`.

---

## 2. Architecture

### 2.1 Components (https://langfuse.com/self-hosting, v3-to-v4 guide)

- **Web** container: Next.js UI, tRPC, public REST API, OTLP endpoint, accepts events.
- **Worker** container: BullMQ consumers: ingestion, OTel ingestion, evals (LLM, code, decision), experiments, batch exports/actions, retention cleaners, blob-storage/PostHog/Mixpanel exports, monitors, topics, trace batching, audit-log relay, etc. (`worker/src/queues`, `worker/src/features`).
- **Postgres** (15 min, 16 recommended), **ClickHouse** (25.12 min / 26.4 recommended for v4: lightweight updates, JSON type, text indexes; the earlier scale post says 24.3+), **Redis/Valkey** (7.0 min), **S3-compatible blob store**, optional LLM gateway. Minimum footprint per blog: ClickHouse 2 CPU / 8 GiB, web and worker 2 CPU / 4 GiB each, Postgres 2 CPU / 4 GiB, Redis 1 CPU / 1.5 GiB. Larger: ClickHouse at least 16 GiB. Source: https://langfuse.com/resources/engineering/clickhouse-at-agent-scale , https://langfuse.com/self-hosting/configuration/scaling

### 2.2 Write path

1. Web validates the batch (zod), groups events by entity and body id, sorts (non-update events first, updates last, by timestamp).
2. **Raw events are uploaded to S3 first** at `<projectId>/<entityType>/<eventBodyId>/<eventId>.json` (legacy) or `otel/<projectId>/<yyyy>/<mm>/<dd>/<hh>/<mm>/<eventId>.json` (OTel). If the upload fails the request fails ("blocking but non-failing" in code comments, though it aborts and marks the project). S3 SlowDown errors move the project to a secondary queue.
3. A job (just a reference: type, body id, file key, bucket prefix, attribution, auth scope) is added to a **Redis BullMQ queue, sharded by `projectId-eventBodyId`**. Delay: OTel 0 s; legacy `min(5000, LANGFUSE_INGESTION_QUEUE_DELAY_MS)`; full configured delay between 23:45 and 00:15 UTC to avoid duplicates when a late update lands in a different ClickHouse day partition.
4. Worker consumes, loads events (from S3, listing the prefix unless `skipS3List`), merges, enriches (model match, cost, prompt id, tool and skill extraction, tokenisation), and writes via the batching `ClickhouseWriter` (in-memory batches + server-side `async_insert`). Spikes become queue depth, and S3 allows replay.
5. Optional per-project ingestion **sampling by trace id** (`isTraceIdInSample`).

### 2.3 Out-of-order and partial updates (legacy/v3 path)

`IngestionService.processObservationEventList` (worker): sort events by timestamp, then read the existing row from ClickHouse (`SELECT * ... ORDER BY event_ts DESC LIMIT 1 BY id, project_id`, filtered by `type` and `start_time >= min start time`), then `overwriteObject` merges: immutable keys (`id, project_id, trace_id, start_time, created_at, environment`) never overwritten; undefined or empty-object sources never overwrite; `metadata` deep-merged; `tags` union-sorted; first non-null input/output taken from the newest event; `created_at` preserved. The merged row is inserted again with a new `event_ts`, and ReplacingMergeTree keeps the highest `event_ts` after merges (or `FINAL`/`LIMIT 1 BY` at read time). Blog: "90% of all updates arrive within 10 seconds", so the pipeline merges state before writing rather than reading it back. A project-level flag lets old projects skip the ClickHouse read for updates (`LANGFUSE_SKIP_INGESTION_CLICKHOUSE_READ_MIN_PROJECT_CREATE_DATE`). Scores and traces follow the same pattern. Dataset run item events are create-only.

### 2.4 The 2025/2026 move to a wide, mostly immutable observations table

What (blog https://langfuse.com/blog/2026-03-10-simplify-langfuse-for-scale , discussion https://github.com/orgs/langfuse/discussions/12518 , docs https://langfuse.com/docs/v4):
- Every LLM call, tool execution and agent step is one row in `events_full`. Trace attributes are copied onto each row. `events_core` (MV with truncated I/O) serves tables and dashboards. Low-volume mutable bits (bookmark, publish) use ClickHouse lightweight updates instead of RMT rewrites.
- Observations arriving via OTel are naturally immutable (about 60% of Cloud observations by March 2026), which let them drop S3 diff-merge reads (claimed about 85% S3 cost saving for some self-hosters and OTel-only projects).
- Trace-level attributes arrive on spans via OTel baggage / SDK `propagate_attributes()` (Python SDK 4.7.0+, JS/TS 5.4.0+ write directly). Older SDKs use a **dual-write**: raw observations go to `observations_batch_staging` (3-minute partitions, TTL 48 h, migration 0038), and a job (`handleEventPropagationJob`) joins them with the `traces` table (7-day look-back, trace metadata merged with observation winning) and inserts into `events_full`, with delay about 15 minutes. It uses `parallel_view_processing=1` because the MV otherwise serialises inserts. Experiment traces are excluded from this join.
- Why: ReplacingMergeTree full-row rewrites and read-time dedup ("hidden tax"), trace-observation joins that stayed slow, S3 diff-merge costs, data growth 19x vs node size 15x in 2025, dashboards over large projects only practical for days. Results: table loads from seconds to tens of milliseconds, dashboards at least 10x faster, a maintainer cited "165x". Initial gain was only 2-3x until they fixed ~1,000 parts per partition (vs 150-200 typical), raised `index_granularity_bytes` from 10 to 64 MiB, and trimmed row size so parts could reach the 150 GiB merge limit.
- Rejected: incremental materialized views for dashboards, AggregatingMergeTree (cross-day inconsistencies), TTL-tiered tables, external metadata stores (Cassandra/DynamoDB).
- Trade-offs they accepted: trace attributes reach older-SDK rows with delay; trace-level metrics (cost per trace, trace latency percentile) are expensive and were removed from list views (issue #16077); a root observation is required for trace-level semantics; dashboards use approximate `uniq(trace_id)`; scores and comments attach to the root observation in the UI.
- Migration modes `legacy | dual | events_only` (`LANGFUSE_MIGRATION_V4_WRITE_MODE`, default `events_only`); backfill is five resumable steps needing about 3x ClickHouse disk. Cloud becomes v4-only on 2026-11-16; v3 gets security patches through January 2027.

### 2.5 Related infra details

- Reads can go to a read-only ClickHouse compute group (`CLICKHOUSE_READ_ONLY_URL`).
- For OTel-only projects `FINAL` can be skipped (`LANGFUSE_SKIP_FINAL_FOR_OTEL_PROJECTS`).
- Redis cluster/queue sharding settings (`LANGFUSE_INGESTION_QUEUE_SHARD_COUNT`, trace-upsert shard count), split web deployment for `/api/public/ingestion*`, `/otel*`, `/media*`.
- Retention and deletion are nightly batched mutation jobs (`LANGFUSE_CLICKHOUSE_DELETION_TIMEOUT_MS` default 10 min). Deletes are mutations that rewrite parts.
- Substring search over multi-MB payloads is slow; v4 uses token-based text indexes and the v2 API rejects `contains`-style operators on `input`/`output`.

---

## 3. OpenTelemetry

Endpoint: `/api/public/otel` (traces at `/api/public/otel/v1/traces`; routes `otlp-proto` and `v1` in `web/src/pages/api/public/otel`). OTLP over HTTP/protobuf and HTTP/JSON. **gRPC not supported.** Auth: HTTP Basic with `pk-lf-...:sk-lf-...`. Header `x-langfuse-ingestion-version=4` is required for the real-time direct path (without it OTel data can take up to 10 minutes). Self-host needs 3.22.0+. Source: https://langfuse.com/integrations/native/opentelemetry

Langfuse attribute namespace (`packages/shared/src/server/otel/attributes.ts`):
- Trace: `langfuse.trace.name`, `user.id` (also `langfuse.user.id`), `session.id` (also `langfuse.session.id`), `langfuse.trace.tags`, `langfuse.trace.public`, `langfuse.trace.metadata.*`, `langfuse.trace.input/output` (deprecated).
- Observation: `langfuse.observation.type`, `.metadata.*`, `.level`, `.status_message`, `.input`, `.output`, `.completion_start_time`, `.model.name`, `.model.parameters`, `.usage_details`, `.cost_details`, `.prompt.name`, `.prompt.version`, `.skills_available`.
- General: `langfuse.environment` (also `deployment.environment[.name]`), `langfuse.release`, `langfuse.version`; internal `langfuse.internal.as_root`, `is_app_root`.
- Experiments: `langfuse.experiment.{id,name,metadata,description,dataset.id,item.id,item.version,item.metadata,item.root_observation_id,item.expected_output}`.
- Evaluator executions: `langfuse.evaluator.id`, `langfuse.evaluation.rule.id`, `langfuse.evaluator.execution.is_test`.
- Langfuse-namespaced attributes beat generic ones. Unmapped span attributes go under `metadata.attributes`; resource attributes under `metadata.resourceAttributes`. Keys with `__proto__`, `constructor`, `prototype` path segments are dropped. Missing start/end time: copy the other, else ingest time.

Observation type inference (`ObservationTypeMapper.ts`, priority order, first hit wins, default SPAN):
0. Python SDK <= 3.3.0 override (span with generation-like attrs -> GENERATION)
1. `langfuse.observation.type` (span, generation, event, embedding, agent, tool, chain, retriever, guardrail, evaluator)
2. OpenInference `openinference.span.kind` (CHAIN, RETRIEVER, LLM->GENERATION, EMBEDDING, AGENT, TOOL, GUARDRAIL, EVALUATOR)
3. GenAI semconv `gen_ai.operation.name` (chat, completion, text_completion, generate_content, generate -> GENERATION; embeddings -> EMBEDDING; invoke_agent, create_agent -> AGENT; execute_tool -> TOOL)
4. Genkit `genkit:metadata:subtype`
5/6. Vercel AI SDK `ai.*` operations (doGenerate/doStream -> GENERATION; embed -> EMBEDDING; toolCall -> TOOL)
7. GenAI tool detection (`gen_ai.tool.name` / `gen_ai.tool.call.id` -> TOOL; e.g. Pydantic AI)
8. Flue, 9. LiveKit span names, 10. Claude Managed Agent session creation
11. Fallback: any model attribute (`langfuse.observation.model.name`, `gen_ai.request.model`, `gen_ai.response.model`, `llm.model_name`, `model`) -> GENERATION.

Input/output extraction (`OtelIngestionProcessor.extractInputAndOutput`, ~4,200-line file) falls through many conventions: `langfuse.observation.input/output`, Vercel `ai.prompt.messages`/`ai.response.text`/`ai.toolCall.*`, GenAI events (`gen_ai.system.message`, `gen_ai.user.message`, `gen_ai.assistant.message`, `gen_ai.tool.message`, `gen_ai.choice`, `gen_ai.client.inference.operation.details`, legacy `gen_ai.content.prompt/completion`), new `gen_ai.input.messages`/`gen_ai.output.messages`/`gen_ai.system_instructions`, `gen_ai.tool.call.arguments/result`, OpenInference `input.value`/`output.value` and `llm.input_messages.*`, Traceloop `traceloop.entity.input/output`, MLflow `mlflow.spanInputs/spanOutputs`, Google ADK `gcp.vertex.agent.*`, Pydantic AI `pydantic_ai.all_messages`, Logfire. Model name from `gen_ai.response.model`, `ai.model.id`, `gen_ai.request.model`, `llm.response.model`, `llm.model_name`. Model parameters from `gen_ai.request.*`, `llm.invocation_parameters`. Usage from `gen_ai.usage.*`, `llm.token_count.*`, plus provider-specific cache/reasoning fields. Cost from `langfuse.observation.cost_details`, `gen_ai.usage.cost`. Tags from `langfuse.trace.tags`, Vercel `ai.telemetry.metadata.*`. Session from `gen_ai.conversation.id` too.

SDKs: Python SDK v3+ and JS/TS v4+ are OTel-based; current are Python v5 / JS-TS v6 (require server 4.59.0+ and use only the v4 API). "A Langfuse trace shares its ID with the OTel trace"; observations are spans typed by `langfuse.observation.type`. APIs: `start_as_current_observation(as_type=...)`, `@observe`, `propagate_attributes()` / `propagateAttributes()`, `mask_otel_spans`/`mask`, `should_export_span`/`shouldExportSpan` (default exports Langfuse + GenAI spans only). Propagation of trace attributes onto every span (via OTel Baggage or the SDK helper) is **required** for filtering/aggregating by user/session/metadata in v4. Sources: https://langfuse.com/docs/observability/sdk/overview , https://langfuse.com/docs/observability/features/masking

Deprecation: legacy `POST /api/public/ingestion` accepts only scores on Cloud from 2026-11-16 (SDK overview page). Self-host `events_only` already returns 400 for all event types except `score-create`.

---

## 4. Evaluation

### 4.1 Core concepts (https://langfuse.com/docs/evaluation/core-concepts)

Evaluator (how to score) + Rule (which incoming observations to score; filters + sampling) -> Score (name, value, data type). Evaluators can be used for online evaluation through a rule, for **batch evaluation** of historical observations, or for prompt experiments. Offline = experiments on datasets; online = live traces. Edge cases found online feed back into datasets.

### 4.2 Postgres model for evaluators (new in v4)

- `evaluators(id, project_id, name, type LLM_AS_JUDGE|CODE|DECISION_MODEL|FACET, is_built_in, blocked_at, block_reason, block_message)` + immutable `evaluator_versions(evaluator_id, version, prompt, prompt_messages, model, provider, model_params, vars[], variable_mapping, output_definition, source_code (<=256 KB), source_code_language PYTHON|TYPESCRIPT, questions JSON)`. Updating an evaluator creates a new version; active rules use the latest.
- `evaluation_rules(id, project_id, name, status ACTIVE|INACTIVE, target_object, filter JSON, sampling Decimal 0..1, delay ms, time_scope[] (NEW, EXISTING))` + `evaluation_rule_evaluator_assignments(rule_id, evaluator_id, variable_mapping override)`. One rule can attach several evaluators; evaluators are reusable across rules.
- `job_executions(project_id, job_configuration_id, status PENDING|COMPLETED|ERROR|CANCELLED|DELAYED, start/end, error, job_input_trace_id, job_input_observation_id, job_input_dataset_item_id + valid_from, job_output_score_id, execution_trace_id)`.
- Legacy `job_configurations` and `eval_templates` still exist (trace-level evals).
- Evaluators auto-block on bad LLM connections (`EvaluatorBlockReason`: auth invalid, billing exhausted, endpoint unreachable, connection missing, default model missing, model config invalid/unavailable).

### 4.3 LLM-as-a-judge (https://langfuse.com/docs/evaluation/evaluation-methods/llm-as-a-judge)

- Prompt with `{{variables}}` and system/user/assistant message roles. Structured output with a score type: numeric, categorical (allowed labels, optionally multiple), boolean; reasoning field. Validated on save with a real test call (so the model must support structured output).
- Target: **observations** (recommended for live data; one score per matched observation per evaluator), **experiments** (dataset runs), traces (deprecated). Observation evaluators see only the matched observation. To judge a whole request, target a logical root and write the needed context onto it.
- Filters: observation type, name, metadata, plus trace-level filters (userId, sessionId, tags, version); "Is Root Observation" for logical roots.
- Sampling and delay on the rule. Variable mapping: input, output, metadata, tool calls from the observation; Expected Output and Experiment Item Metadata only in experiments. Default mapping on evaluator, overridable per rule.
- Models: project LLM connections (OpenAI, Anthropic, Bedrock, Vertex, Azure, etc.) or project default model. Multimodal supported.
- Execution: worker queue; each execution produces a trace in environment `langfuse-llm-as-a-judge`; statuses Pending/Completed/Error/Delayed (rate-limited, exponential backoff); concurrency `LANGFUSE_EVAL_EXECUTION_WORKER_CONCURRENCY` default 5.
- Calibration: compare against human labels in Score Analytics; guide at /guides/llm-as-a-judge-calibration-skill.

### 4.4 Code evaluators (https://langfuse.com/docs/evaluation/evaluation-methods/code-evaluators)

Python or TypeScript `evaluate(ctx)` returning `{scores: [...]}`. Context: `input, output, metadata, tool_calls` (+ `item_expected_output`, `item_metadata` in experiments). Score: `name, value, data_type (NUMERIC|CATEGORICAL|BOOLEAN|TEXT), comment, config_id, metadata`. Limits: stdlib only, no network, 2 s, source < 256 KB, dispatch payload < 5.5 MB, result < 256 KB. Self-host needs a dispatcher (AWS Lambda for Python, `insecure-local` for TS only). Execution traces in `langfuse-code-eval`. Requires OTel-based SDK data.

### 4.5 Decision-model evaluators (TypeSafe Jev) (https://langfuse.com/changelog/2026-09-22-jev-as-a-judge)

Directly relevant to your design. Jev "does not generate text": you give a **state** (mapped observation fields) and **typed questions**, it returns typed answers with probabilities. Question types in code (`packages/shared/src/features/evals/decisionModel.ts`): `choice` (2-255 options -> CATEGORICAL score), `score` (2-10 ordered levels -> NUMERIC expected level), `noul` (yes/no -> NUMERIC P(true) in 0-1). Max 50 questions per evaluator, one score per question, unique score names. Probability distribution and confidence are stored in score metadata. All questions answered in parallel against the same state; priced by input tokens only ($0.042 per million per the changelog; not independently verified); "well under a second". Limitations stated: no rationale; answer space must be defined up front. Docs suggest running Jev on every observation and an LLM judge on a sample or on flagged ones, but I did not find a built-in rule that conditionally escalates from a classifier score to an LLM judge **[UNVERIFIED that none exists]**. Setup: TypeSafe connection in LLM Connections; templates (Assign Input Topic, Flag Out-of-Scope Request, Rate Customer Frustration, User Conversation Signal). The worker (`runDecisionModelEvaluation.ts`) restricts allowed decision models/adapters, auto-blocks the evaluator on invalid config, writes an execution trace, and shares the score-writing path. Separately, Morph "Reflex" small classifiers plug in as categorical LLM-as-judge (https://langfuse.com/integrations/model-providers/morph). The dedicated decision-model docs URL I tried returned 404, so details come from the changelog and code.

### 4.6 How online evaluation is triggered (v4 code path)

In `processOtelEvents` (worker, OTel ingestion): after each span is enriched into an event record, `scheduleObservationEvals` runs in-process: (1) load the project's active rules (cached), (2) in-memory filter evaluation on the record (`InMemoryFilterService`), (3) deterministic sampling: `sha256("langfuse:evaluation-sampling:v1\0" + span_id)` -> 53 bits -> [0,1) compared to the rule's rate (so the same observation always gets the same decision), (4) dedupe, (5) snapshot the observation to S3 once, (6) create a `job_execution` row per matching rule x evaluator, (7) enqueue a delayed job (rule `delay`). The processor downloads the snapshot (no ClickHouse read-back), runs LLM/code/decision evaluator, writes scores through the normal score ingestion path with deterministic ids, `source = EVAL`, `execution_trace_id`, `evaluator_id`, `evaluation_rule_id`. A recursion guard skips internal `langfuse-*` environments (except experiment run-item roots). Observation-level evals thus avoid a per-eval ClickHouse query (stated benefit in discussion #12518).

### 4.7 Trace-level readiness (internal, cloud-gated) (`worker/src/features/traceBatching/README.md`)

For Topics (LLM-summarised, embedded, clustered conversations) and trace-level work, Langfuse built an **idle-timer readiness** system: every admitted event refreshes the trace's due time in a Redis sorted set (`due = last event + idle`, default 10 min, per-project override 60 s to 60 min via `tracing_readiness_settings`), min/max start times and size estimates kept in Lua-merged hashes, sharded by SHA-256 of (project, trace), a single dispatcher lease, late arrivals reopen the window and re-read the whole trace with 2 minutes of padding, stable BullMQ job ids, no reconciler (Redis loss leaves gaps). It requires `NEXT_PUBLIC_LANGFUSE_CLOUD_REGION` and explicit flags, i.e. not generally available for OSS. Facet evaluators (`FACET` type) summarise a transcript; `topic_facet_summaries` stores summary + embedding; clustering names topics.

### 4.8 Experiments, annotation queues, external pipelines, score analytics

- Experiments: see 1.8. UI comparison of runs exists (docs page for comparison returned 404, **details UNVERIFIED**; the prompt-experiment feature runs an LLM over a dataset from the UI).
- Annotation queues: see 1.6. Human scores carry `source = ANNOTATION` and `queue_id`; CORRECTION scores capture corrected outputs.
- External evaluation pipelines: fetch data via Observations API v2 / Metrics API v2 / blob-storage export, run any pipeline (RAGAS, DeepEval, autoevals), post scores with `POST /api/public/scores` (source API) or `score-create` ingestion events (still accepted after the legacy cut-over). Public v2 APIs for evaluators and rules (`/api/public/v2/evaluators`, `/v2/evaluation-rules`) allow IaC-style setup. Also integrations to PostHog, Mixpanel, blob export (CSV/JSONL/Parquet **[format list UNVERIFIED]**).
- Score analytics (beta) (https://langfuse.com/docs/evaluation/scores/score-analytics): single-score stats, distribution, trend; two-score comparison with matched counts, Pearson, Spearman, MAE, RMSE, Cohen's kappa, F1, overall agreement, heatmap/confusion matrix; max two scores; > 100k scores are sampled. Use cases: human-vs-LLM agreement, judge-vs-judge reliability.
- Automations/Monitors: threshold alerts on score or observation metrics to Slack/webhook/GitHub Actions.

---

## 5. UI features that matter (partly UNVERIFIED; docs for trace viewer and experiment comparison returned 404, so the list below comes from docs summaries, the v4 overview and the code tree names)

- **Observations table as the primary surface** (v4): flat table with filter search bar (typed filters, metadata, scores in one shareable query), full-text search over input/output/metadata, "Pulse" chart above the table (count/cost/latency, click a spike to filter), saved views (table view presets), "Halo" natural-language assistant. Waterfall/trace detail one click from any row (https://langfuse.com/docs/v4).
- **Trace detail**: tree + timeline (waterfall), agent graph view (`features/trace-graph-view`), JSON/markdown/chat rendering of I/O, inline comments anchored to JSON paths, public share links. [UNVERIFIED details]
- **Session view**: conversation replay grouping all traces/observations with the same `session_id`, scores, bookmarks, public sharing.
- **Experiments**: dataset runs listed and compared side by side (scores, cost, latency); prompt experiments run from the UI; remote experiment webhook. [UNVERIFIED comparison UI details]
- **Dashboards**: custom dashboards from saved widgets, views traces/observations/scores, chart types listed above, query via Metrics API v2 with `{view, dimensions, metrics, filters, timeDimension.granularity, orderBy, rowLimit}` (`packages/shared/src/features/query/types.ts`). High-cardinality dimensions (user/trace/session) need row limit and sort; no time series on them. Home dashboard selectable per project (https://langfuse.com/docs/metrics/features/custom-dashboards).
- **Playground**: prompt/model playground using project LLM connections, structured output schemas and tools (`llm_schemas`, `llm_tools`), save to prompt. [UNVERIFIED details]
- **Annotation queue UI**: keyboard-driven scoring card.
- **Score analytics, topics view, monitors/alerts**.

---

## 6. Multi-tenancy, RBAC, retention, masking, audit, licensing

- **Hierarchy**: Organization -> Project. Almost every Postgres table has `project_id`; every ClickHouse table has `project_id` as first sort-key column; queries always filter project and time.
- **Roles**: org roles OWNER, ADMIN, MEMBER, VIEWER, NONE; project-level roles override org roles (EE). New `role_assignments` table (`owner_id, project_id, org_id, principal_id (user or API key), role_id, system_role`) with system roles OWNER, ADMIN, MEMBER, VIEWER, NONE, plus API-key roles `INGEST`, `SCORES_INGEST`, `AI_GATEWAY`, and legacy key roles. Scope strings are `resource:action` (e.g. `traces:read`, `scores:save`, `evaluator:CUD`, `datasets:CUD`, `projectAuditLogs:read`, `playground:execute`; about 50 resources in `projectAccessRights.ts`). Source: https://langfuse.com/docs/administration/rbac
- **API keys**: project keys (`pk-lf-`/`sk-lf-`), org keys (for the org management API); permission types Full, Viewer, Ingestion, Scores ingestion; secret shown once, hash stored (bcrypt for old keys, "fast hash" for newer); `expires_at`, `last_used_at`.
- **Retention**: per project `retention_days` (min 3), nightly deletion of traces, observations, scores, media; not audit logs, datasets, dataset items; EE / Pro+ only. Source: https://langfuse.com/docs/administration/data-retention
- **Data masking**: client-side in SDKs (`mask_otel_spans`, `mask`) and OTel Collector processors; **server-side masking is an EE feature**, but the docs page I fetched did not describe it **[UNVERIFIED]**. Source: https://langfuse.com/docs/observability/features/masking
- **Audit logs** (EE): about 35 resource types, actor (USER or API_KEY), action, before/after JSON, roles at the time, org/project; v4 ClickHouse `audit_logs` also records access events (reads, lists, exports, downloads) with request id, IP, user agent, `resource_refs` like `trace:<id>`; Postgres outbox with retry. Source: https://langfuse.com/docs/administration/audit-logs
- **Licensing**: `LICENSE` = MIT except `ee/`, `web/src/ee/`, `worker/src/ee/` under the Langfuse Enterprise License. EE directories contain: web: admin-api, audit-log-viewer, billing, multi-tenant-sso, sfdc-sync, sso-settings, ui-customization, verified-domains; worker: cloudSpendAlerts, cloudUsageMetering, dataRetention, fipsMode, meteringDataPostgresExport, usageThresholds; `ee/src/ee-license-check`. Per https://langfuse.com/self-hosting/license-key , a license key is needed for: project-level RBAC roles, protected prompt labels, data retention, audit logs, server-side data masking, UI customization, organization creators, org management API + SCIM, instance management API. Everything else (tracing, evals incl. LLM-judge/code/decision evaluators, prompts, experiments, annotation, playground, dashboards) is MIT "without any limits" per https://langfuse.com/docs/open-source . Note the code evaluator on self-host needs a dispatcher you supply.
- Cloud plans (code): Hobby, Core, Pro, Team, Enterprise; data-access window Hobby 30 d, Core 90 d, Pro/Enterprise 3 y.

---

## 7. Known pain points and scaling issues

(From issues/discussions fetched or seen in search snippets. Items marked (snippet) were not opened.)

1. **Trace-level aggregates on the wide table**: #16077 (open, 19 comments): v4 tracing table shows $0.00 on root SPAN rows; maintainer said trace-level metrics were removed because they are "extremely inefficient to compute"; no pre-aggregation on `trace_id`; "average cost per trace" impossible; plan to pre-aggregate on the write path at trace/session/user level, no timeline. https://github.com/langfuse/langfuse/issues/16077
2. **UI queries with fixed/unbounded windows plus retry amplification**: #18299 (open): sessions table reads 2 days, evaluator cost reads 7 days, session score filters read all history; web client retries failing queries 3 times while the first keeps running (`cancel_http_readonly_queries_on_client_close` not set); 32 GB group-by spill default exceeds small nodes; memory pressure made inserts fail with `MEMORY_LIMIT_EXCEEDED` and `ClickhouseWriter` dropped records after max attempts. https://github.com/langfuse/langfuse/issues/18299
3. **Ingestion lag / not found**: discussion #9243 (traces 12-24 h behind, unresolved), #10533 ("Trace not found" + ClickHouse ingestion timeouts on large deployment, closed as not planned) (snippet), #10204 "Database Resource Limit Exceed" (title only). Maintainer advice: scale workers on queue depth, split web for ingestion, shard Redis queues, skip ClickHouse reads on ingestion.
4. **ClickHouse operations**: memory from deletes not using the primary key (#7764, "2.66 EiB of memory" on deletion) (snippet); slow `trace_id` filters because not in the primary key (#8675) (snippet); known ClickHouse 24.3.2.23 bug fixed in 24.8 (#10877) (snippet); `system.trace_log` growth and unbounded container logs filling disk (#16339) (snippet); migration deadlock (#6198); too many parts, Keeper dead parts delaying replication up to 25 min, merges stalling near 100-150 GiB (blog).
5. **v3 -> v4 migration risk**: unfinished background migrations cause permanent row loss when v4 drops tables; 3x disk for backfill; extra ClickHouse grants; rollback requires `migrate goto 37`; `events_only` default means one rollout does the cutover; 22 legacy endpoints return 404; trace-level evaluators and legacy exports stop; self-hosted path was repeatedly delayed through 2026 (discussion #12518). https://particula.tech/blog/langfuse-v3-to-v4-upgrade-self-hosted (third party)
6. **Cost/usage correctness**: inclusive vs exclusive token buckets cause double counting (#10592 input tokens counted twice with cached tokens; #7518 Claude 3.7 caching price; #8020 Gemini mismatch; #7549 duplicate token counts when a generation is parent of multiple generations in Strands). Cost only inferred at ingest time, never retroactively.
7. **Semantic drift in dashboards after v4** (synthetic root spans inflate observation counts, approximate trace counts, trace-less roots dropped from time series) (https://langfuse.com/faq/all/dashboard-changes-in-v4).
8. **Operational weight**: five stateful systems (Postgres, ClickHouse, Redis, S3, plus worker/web), self-host startup/Zod errors (#7995, #4675), media upload to GCS (#6504), dashboard "tracing setup" screen despite ingestion (#8206).
9. **Evals**: observation evaluators see no children/siblings; code evaluators require OTel SDK data and a dispatcher on self-host; custom evaluator type mismatch string vs number (#10499); dataset item input not exposed to experiment evaluator mapping (#12785); repetitions per dataset item unsupported (#5855).
10. **OTel integration friction**: conflicts with Google ADK context management (#8316); gRPC unsupported; collector filtering that drops the root span breaks traces; must propagate attributes onto every span.

---

## 8. Mapping to an OpenSearch design (my analysis, [INFERENCE] throughout)

### Copy
- **One wide observation document per span**, trace attributes (user, session, tags, release, version, trace_name, environment) copied onto each doc, root flagged (`is_root`/`is_app_root`) so "trace" views = root docs. Deterministic `_id = project|trace|span` makes OpenSearch index ops idempotent, so you get "last write wins" without ReplacingMergeTree machinery.
- **Two representations**: a compact "core/list" doc with truncated I/O (200 chars) and metadata values, and a full doc. In OpenSearch there are no materialized views and `_source` is returned whole, so produce both in the ingest worker (two indices, or one index with large payloads in a second index / S3 pointer and `_source` excludes).
- **Raw-event durability + queue**: write raw OTLP batches to object storage first, enqueue a reference; workers enrich and bulk-index. Enables replay and absorbs spikes. (Consider a Postgres-backed queue to avoid adding Redis; Langfuse uses Redis for BullMQ, caches, Lua timers.)
- **Evaluator model**: Evaluator + immutable EvaluatorVersion + Rule (filter, sampling, delay, target) + Assignment (variable-mapping override) + JobExecution (Postgres), types LLM_AS_JUDGE / CODE / DECISION_MODEL. In-stream scheduling: filter in memory right after enrichment, hash-based deterministic sampling, S3 snapshot so the evaluator never re-queries the store, deterministic score ids, execution traces written back to an internal environment with a recursion guard, auto-block evaluators on bad model connections.
- **Typed classifier output contract**: choice -> categorical, score levels -> numeric expected value, yes/no -> P(true); store probability distribution and confidence in score metadata. Limits (50 questions, 255 options, 2-10 levels) are sensible defaults.
- **Score model**: dataType NUMERIC/CATEGORICAL/BOOLEAN/TEXT, source API/EVAL/ANNOTATION, target trace/observation/session/dataset run, config id, comment, metadata, `execution_trace_id`, `queue_id`, `evaluator_id`, `evaluation_rule_id`. Plus ScoreConfig (immutable, archivable).
- **Exclusive-bucket usage/cost model** (`input` excludes `input_cached_*`) with model definitions (regex match, tiers by priority and conditions, per-usage-type price) and "ingested beats inferred".
- **Experiments as attributes on spans** (`experiment.*`) instead of a run-item join table; dataset items with validity intervals for versioning; dataset JSON schemas.
- **Semantic query DSL** (view, dimensions, metrics, filters, time granularity, row limits) compiled to OpenSearch aggregations, used for dashboards, monitors and the public metrics API.
- OTel mapping tables and the priority-ordered type mapper (port the logic; it encodes years of framework quirks).
- Score analytics (kappa, Pearson/Spearman, confusion matrices) for judge calibration; these are directly useful for validating classifiers against human labels.

### Change for OpenSearch
- **Scores**: keep a separate scores index (mutable, small, evaluators/humans add over time) but denormalize `trace_id, session_id, user_id, environment, observation name/type, tags` onto the score doc so score-centric analytics need no join. Filtering observations by score value (Langfuse's filter bar does this with a ClickHouse join) needs a two-phase query or a rolled-up per-trace/per-observation score summary doc; OpenSearch joins (parent-child) are costly, so avoid them.
- **Trace-level aggregates from day 1**: Langfuse's biggest regret (#16077). Maintain a small `trace_rollup` index (cost, tokens, latency, error count, root name, first/last time, score summaries) written by the idle-timer/trace-complete job or an OpenSearch transform; do not rely on aggregations over all spans for list views.
- **Routing and layout**: use custom routing by `trace_id` (or project+trace) so a whole trace is on one shard, time-based indices/data streams (monthly or daily) with project_id as a filter field and index sorting; ISM for rollover/tiering. Per-project retention differs from per-index ISM: either use `delete_by_query` nightly (Langfuse does batched deletes, but they are expensive) or bucket indices by retention class.
- **Metadata**: Langfuse uses string maps/parallel arrays with token indexes and only top-level keys filterable. Use `flat_object` (or a constrained dynamic template) to avoid mapping explosion; cap key count.
- **Search**: OpenSearch full-text on input/output is a strength (Langfuse had to add ClickHouse text indexes in v4). Embeddings/topics can use native k-NN rather than `Array(Float32)` brute force.
- **Updates**: ClickHouse needed immutability because updates are rewrites. OpenSearch update = reindex of the whole `_source`, also costly with large I/O. Keep observation docs immutable after the span ends; keep mutable bits (bookmark, public, tags edited in UI, comments) in Postgres or a tiny side index.
- **Dual-write/propagation job**: avoid it by requiring trace-attribute propagation (OTel baggage / SDK helper) and treating missing attrs as "unknown"; optionally a lazy repair job using `update_by_query` per trace on idle.
- **Trace-level / session-level / agent-trajectory evals**: Langfuse dropped them in OSS v4 and only prototyped idle-timer readiness for cloud. Since agents are your focus, implement an explicit "trace complete" signal (root span ended OR idle timer) in the worker, with the whole trace fetched by routing key. Avoid Langfuse's Redis+Lua complexity if possible (e.g. a Postgres `due_at` table).
- **Cascade evaluators**: Langfuse suggests classifier on all, LLM on a sample or flagged, but exposes only per-rule sampling. Add rule-level conditions on prior scores (classifier says X with probability > p -> run judge), and deterministic checks first.

### Avoid
- Supporting the legacy event API (trace-create/span-update/generation-update) and a dual-write mode; go OTel-only plus a small score/dataset API. This removed most of Langfuse's complexity at the cost of a long migration.
- A separate trace entity that is both a table and a derived concept (they spent 2025 unwinding it).
- Unbounded UI queries and automatic retries on resource-limit errors; require time ranges everywhere (Langfuse's v2 API does) and cancel queries on client close.
- S3 diff-merge for updates (they removed it for OTel).
- Feature sprawl behind an EE license key; decide up front which governance pieces (project RBAC, retention, audit, masking) are core. Langfuse gates exactly those, which causes friction for self-hosters.
- Coupling cost to ingestion-time-only pricing without a re-price job; store raw usage so cost can be recomputed.

---

## 9. Source list

- https://github.com/langfuse/langfuse (HEAD bde3073, v4.64.0) - key files: `packages/shared/clickhouse/migrations/canonical/*.sql`; `packages/shared/prisma/schema.prisma`; `packages/shared/src/domain/{observations,scores}.ts`; `packages/shared/src/server/ingestion/processEventBatch.ts`; `packages/shared/src/server/otel/{OtelIngestionProcessor,ObservationTypeMapper,attributes}.ts`; `worker/src/services/IngestionService/index.ts`; `worker/src/features/eventPropagation/handleEventPropagationJob.ts`; `worker/src/features/otel-ingestion/processOtelEvents.ts`; `worker/src/features/evaluation/**`; `packages/shared/src/features/evals/decisionModel.ts`; `worker/src/features/traceBatching/README.md`; `packages/shared/src/features/query/{types,dataModel}.ts`; `LICENSE`, `ee/LICENSE`
- https://langfuse.com/blog/2026-03-10-simplify-langfuse-for-scale
- https://github.com/orgs/langfuse/discussions/12518
- https://langfuse.com/self-hosting/upgrade/upgrade-guides/upgrade-v3-to-v4
- https://langfuse.com/resources/engineering/clickhouse-at-agent-scale
- https://langfuse.com/self-hosting , https://langfuse.com/self-hosting/configuration/scaling , https://langfuse.com/self-hosting/license-key
- https://langfuse.com/docs/v4 , https://langfuse.com/faq/all/dashboard-changes-in-v4
- https://langfuse.com/docs/observability/data-model , /features/observation-types , /features/sessions , /features/token-and-cost-tracking , /features/masking , /sdk/overview
- https://langfuse.com/integrations/native/opentelemetry
- https://langfuse.com/docs/evaluation/overview , /core-concepts , /evaluation-methods/llm-as-a-judge , /code-evaluators , /annotation-queues , /scores/data-model , /scores/score-analytics , /experiments/data-model , /experiments/experiments-via-sdk
- https://langfuse.com/changelog/2026-09-22-jev-as-a-judge ; https://langfuse.com/integrations/model-providers/morph
- https://langfuse.com/docs/prompt-management/data-model
- https://langfuse.com/docs/metrics/features/custom-dashboards
- https://langfuse.com/docs/administration/rbac , /data-retention , /audit-logs ; https://langfuse.com/docs/open-source
- https://clickhouse.com/blog/clickhouse-acquires-langfuse-open-source-llm-observability
- Issues/discussions: https://github.com/langfuse/langfuse/issues/16077 , /issues/18299 , /issues/7764 , /issues/10533 , /issues/16339 , https://github.com/orgs/langfuse/discussions/9243 , /discussions/10877 , /discussions/8675 , /discussions/15710
- Third party: https://particula.tech/blog/langfuse-v3-to-v4-upgrade-self-hosted

Pages that returned 404 or were not retrievable: docs `/evaluation/evaluation-methods/decisions-models`, `/evaluation/experiments/experiments-compare`, `/observability/features/log-view`. GitHub search API was not available in this session, so issue coverage relies on the most-commented list page and web-search snippets.
