# OpenSearch (and Elasticsearch for comparison) as the event store for an LLM/agent observability + evals system

Research date: 2026-10-20. Current OpenSearch release per the docs repo `_config.yml` is **3.9.0** (Lucene 10.5.1). Docs repo HEAD I read: documentation-website `main`, commit dated 2026-10-19.

Legend: **[doc]** = read in the official docs/source; **[blog]** = vendor or third-party blog; **[claim]** = vendor claim, not independently verified; **[inferred]** = my reasoning; **[unverified]** = could not confirm.

Primary source for most OpenSearch facts: the OpenSearch docs repo, https://github.com/opensearch-project/documentation-website (I cloned it and read the markdown directly; rendered equivalents live under https://docs.opensearch.org/latest/). Release notes: https://github.com/opensearch-project/opensearch-build/tree/main/release-notes (opensearch-release-notes-3.0.0.md ... 3.9.0.md).

---

## 0. Executive summary

1. OpenSearch is a workable event store for this workload, and it has the most momentum it has ever had on GenAI tracing: Agent Traces UI (introduced 3.6), an OTel `gen_ai.*`-aware schema, a Python SDK with a `score()` call, Data Prepper, and an OTel Collector exporter. But almost all of the GenAI pieces are **new (3.6+, Python SDK "Alpha"), UI-centric, and built around one flat `otel-v1-apm-span-*` index family with no tenancy model**. You would use OpenSearch as a storage engine and keep your own Langfuse-style data model on top of it, not adopt their schema wholesale.
2. It is **materially weaker than ClickHouse** at wide analytical aggregation, storage density, joins, and updates. No public benchmark compares OpenSearch with ClickHouse directly. The nearest evidence (ClickHouse vs Elasticsearch, vendor-run) shows ClickHouse about 5x smaller on disk and 1.7-6x faster. OpenSearch is faster than Elasticsearch on terms/date-histogram aggregations in AWS-commissioned tests, so the gap to ClickHouse is probably somewhat smaller than the ES numbers suggest **[inferred]**.
3. It is clearly **stronger than ClickHouse at full-text search over conversations, k-NN/hybrid search over embeddings, and per-index/per-document security controls** (and it is Apache-2.0 with open governance, with the security plugin included).
4. Biggest design risks: (a) tenant count vs shard count (index-per-tenant does not scale to thousands of tenants); (b) no joins at scale, so scores must be denormalized or joined in application code; (c) Lucene index overhead on large message payloads; (d) updates are full-document rewrites, so append-only plus separate score docs is the right pattern; (e) field masking is hash-only and breaks search on that field; (f) the interesting "new engine" (Parquet + DataFusion, 4x price-performance claims) is, as far as I can verify, **AWS-managed-only** or sandbox code, not something to build on today.
5. Elasticsearch is technically a bit ahead on columnar-ish features (logsdb, synthetic `_source`, ES|QL LOOKUP JOIN) but the best of them are paid (synthetic `_source` is Enterprise-only per Elastic's subscriptions matrix), and AGPL gives you the code but not those proprietary-tier features. Recommendation: **abstract the storage behind a narrow interface**, target the OpenSearch Query DSL/PPL subset that is common to both, and keep ClickHouse as a possible future "analytics sink" backend.

---

## 1. OpenSearch Observability for traces and GenAI

### 1.1 Components and their maturity

| Piece | Status | Notes / source |
|---|---|---|
| Trace Analytics (older) | GA, legacy | Uses Data Prepper `otel_trace` processor + `otel-v1-apm-span-*` and `otel-v1-apm-service-map` indices. Docs now say APM is "distinct from the older Application analytics and Trace analytics". https://github.com/opensearch-project/documentation-website/blob/main/_observing-your-data/apm/index.md **[doc]** |
| APM (services, application map, RED metrics) | Introduced 3.6 | RED metrics are stored in **Prometheus**, not OpenSearch; Data Prepper `otel_apm_service_map` processor writes `otel-v2-apm-service-map*`. Requires Dashboards workspaces + feature flags (`workspace.enabled`, `explore.enabled`, ...). https://github.com/opensearch-project/documentation-website/blob/main/_observing-your-data/apm/configuring-apm.md **[doc]** |
| Agent Traces (Dashboards) | Introduced 3.6; headline feature in 3.9 notes | Traces/Spans tabs, tree, DAG ("agent graph"), timeline; categories from `gen_ai.operation.name` (`invoke_agent`, `create_agent`, `chat`, `text_completion`, `generate_content`, `execute_tool`, `embeddings`, `retrieval`). All reads use PPL. https://github.com/opensearch-project/documentation-website/blob/main/_observing-your-data/agent-traces/agent-tracing.md **[doc]** |
| Python GenAI SDK (`opensearch-genai-observability-sdk-py`) | **Alpha** (0.2.x, Mar 2026 on package index) | `register()`, `@observe(op=...)`, `enrich()`, `score()`; auto-instruments OpenAI, Anthropic, Bedrock, Google, LangChain, LlamaIndex. TypeScript SDK "coming soon". https://observability.opensearch.org/docs/ai-observability **[doc]**, https://simple-repository.app.cern.ch/project/opensearch-genai-observability-sdk-py **[blog/package index]** |
| Agent Health / evals | Early; docs give no version or stability | LLM-as-judge ("Golden Path" trajectory comparison), pass/fail scoring, experiments; judge calls proxied to AWS Bedrock; storage backend not stated. https://observability.opensearch.org/docs/agent-health/ **[doc]** |
| OpenSearch Observability Stack | Separate distribution, Apache-2.0, versioned independently of core | OTel + Prometheus based; 3.9 notes call it "separately deployed". https://observability.opensearch.org/ **[doc]** |
| SS4O (Simple Schema for Observability) | Introduced 2.6; **effectively not the center of gravity** | Docs say Data Prepper conforms for metrics and will "gradually support traces and logs". GenAI tracing uses `otel-v1-apm-span-*` (the Data Prepper raw span schema), not SS4O. https://github.com/opensearch-project/documentation-website/blob/main/_observing-your-data/ss4o.md **[doc]** |

### 1.2 Ingestion paths

**OTel Collector `opensearchexporter` (contrib)** [README: https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/exporter/opensearchexporter/README.md **[doc]**]
- Stability: **traces and logs alpha, metrics development**; distribution: contrib only.
- Mapping modes: `ss4o` (default; logs/traces/metrics), `otel-v1` (logs/traces/metrics), `ecs`, `flatten_attributes`, `bodymap` (logs only; `ecs`/`flatten_attributes` marked unstable).
- Writes to immutable data streams named `ss4o_{type}-{dataset}-{namespace}` in ss4o mode; supports `bulk_action: create|index`, `pipeline`, retry/queue/TLS.
- Caveats from the README: high-cardinality values in index names can destabilize clusters; the SS4O metrics template's `value@int` is 32-bit; without a template `otel-v1` timestamps fall back to millisecond `date` instead of `date_nanos`; `manage_index_template` only valid with `otel-v1`.
- Code owners: two active (ps48, kylehounslow), "seeking more owners"; four emeritus. A March 2025 forum post described it as "unmaintained" in a component listing **[blog; likely outdated]**: https://forum.opensearch.org/t/opentelelmetry-data-w-o-dataprep/23934. Net: usable, but treat as thinly maintained and alpha. The Elastic equivalent is **beta** with 5 listed owners (see section 6).

**Data Prepper** [https://github.com/opensearch-project/documentation-website/tree/main/_data-prepper **[doc]**]
- Source `otel_trace_source`/OTLP, processors `otel_traces` (formerly `otel_trace_raw`), `otel_trace_group`, `service_map`, `otel_apm_service_map`, `otel_metrics`; sink `opensearch` with `index_type` in `custom, trace-analytics-raw, trace-analytics-plain-raw, trace-analytics-service-map, log-analytics, metric-analytics, ...`.
- The `otel_traces` processor is **stateful**: it caches root-span info per `traceId` and fills `traceGroup`, trace `endTime`, `durationInNanos`, `statusCode`; `trace_flush_interval` default 180 s for orphan spans. This is a scaling and correctness concern for long-running agent traces (an agent trace can last minutes to hours) and for multi-instance deployments **[inferred; verify peer-forwarder behavior]**. For a Langfuse-like model I would compute trace-level rollups elsewhere.
- The sink supports `routing`, `document_id`, `document_version(_type)`, `action: create|index|update|upsert|delete`, per-event `actions` with `when` expressions, custom `template_file`, `ism_policy_file` (only for `custom` index type), and dynamic index names via `${/field}`. So Data Prepper can do per-tenant index routing if the tenant is on the event.
- The span index template that Data Prepper ships (https://github.com/opensearch-project/data-prepper/blob/main/data-prepper-plugins/opensearch/src/main/resources/otel-v1-apm-span-index-template.json **[doc]**): `date_detection:false`; dynamic templates mapping **all** `resource.attributes.*` and `span.attributes.*` to `keyword`; `events` and `links` as **nested**; `startTime/endTime` as `date_nanos`; `_source` enabled. This default is poor for LLM payloads: unbounded keyword attributes (Lucene's 32,766-byte term limit unless `ignore_above` is set **[inferred from Lucene; the template sets none on attributes]**), nested events (each event becomes a hidden Lucene doc), and no `text` fields for conversation search. Plan to ship your own template.

**Gen AI attribute handling** (Agent Traces page **[doc]**): required `gen_ai.operation.name`, `gen_ai.provider.name`; optional `gen_ai.agent.name`, `gen_ai.request.model`, `gen_ai.usage.input_tokens/output_tokens`, `gen_ai.input.messages`, `gen_ai.output.messages`, `gen_ai.tool.name`, `gen_ai.tool.call.id`. Notably there is **no documented cost, session/conversation id in the table (a `gen_ai.conversation.id` appeared in a fetched summary of the observability.opensearch.org mirror page, not in the docs-repo table)** **[partially unverified]**, and nothing about prompt/version, user id, tags, or evaluation scores in the schema.

### 1.3 Scores/evals in OpenSearch's own tooling

From https://observability.opensearch.org/docs/agent-evals/evaluation/ **[doc]**:
- `score(name, value, trace_id, span_id, label, explanation, response_id, attributes)` submits **each score as its own OTel span linked to the trace** and stored "in the same OpenSearch index as your traces".
- `evaluate()`/`Benchmark` create a root benchmark span with `test_case` child spans and "evaluation result events".
- `OpenSearchTraceRetriever.find_evaluated_trace_ids()` finds traces that already have an evaluation span. `list_root_spans()` defaults to the last 15 minutes.
- No documented schema, no separate score index, **no explicit late-arriving-score handling** (it works naturally because a score is just another append-only doc referencing an existing trace id). Explanations truncated to 500 chars.
- This confirms the append-only "score as separate document" pattern is the idiomatic OpenSearch approach; the weakness is that the UI/queries have to join them.

### 1.4 Dashboards plugins and licensing
- Agent Traces is in the default distributions; requires `workspace.enabled`, `data_source.enabled`, `explore.enabled`, `explore.agentTraces.enabled` in `opensearch_dashboards.yml` and PPL (SQL plugin) **[doc]**.
- Everything is Apache-2.0; governance moved to the Linux Foundation's OpenSearch Software Foundation (Sept 2024) **[blog]**: https://www.linuxfoundation.org/blog/how-the-opensearch-software-foundation-will-ensure-long-term-sustainability-of-the-opensearch-project.

**Implication for the new framework:** reuse the *OTLP + gen_ai semconv* conventions (so users can point existing OTel instrumentation at you) and optionally let users browse raw data in OpenSearch Dashboards, but own your schema, API, and UI.

---

## 2. Index design for traces / observations / scores

### 2.1 Entities and shape

Langfuse-style entities map to three append-only event streams plus a few small mutable indices:

| Logical entity | OpenSearch object | Mutability | Notes |
|---|---|---|---|
| Observation (span/generation/event) | data stream `obs-*` (or per-tenant) | append-only | Flat doc with typed core fields, `gen_ai.*` columns, `trace_id`, `parent_id`, `start/end` as `date_nanos`, token/cost numerics, `tenant_id`, `session_id`, `user_id`, `tags`, `metadata` as `flat_object` |
| Trace | derived **or** thin doc in `traces-*` | mostly append-only | Option A: no trace doc; aggregate by `trace_id` at query time. Option B: write a trace "header" doc (name, tags, user, session) at ingest and treat late changes as new doc versions |
| Payloads (prompt/completion messages, tool IO) | separate data stream `payload-*` keyed by `observation_id` | append-only | Keeps the hot, aggregated index small; payload index carries the full-text mapping and the heavier codec |
| Score | data stream `scores-*` | append-only | One doc per score event: `tenant_id, trace_id, observation_id?, name, value (numeric), string_value, data_type, source (API/EVAL/ANNOTATION), eval_run_id, created_at`; latest-wins resolved at read time |
| Dataset / dataset item / eval run | **Postgres** (config) or small `datasets-*` index | mutable | Low volume; do not put in the event store |
| Embeddings | `embeddings-*` (k-NN), separate from payload | append-only | See section 4 |

### 2.2 Data streams, rollover, lifecycle

- **Data streams** [https://github.com/opensearch-project/documentation-website/blob/main/_im-plugin/data-streams.md **[doc]**]: hidden backing indices `.ds-<name>-<gen>`, **append-only via the stream name** (updates/deletes only by `_update_by_query`/`_delete_by_query` or addressing the backing index), `@timestamp` required (configurable via `timestamp_field`), ISM policy applied to each backing index when created, ACLs by data-stream name.
- **ISM** operations available **[doc]** (`_im-plugin/ism/policies-operations.md`): `rollover` (`min_size`, `min_primary_shard_size`, `min_doc_count`, `min_index_age`, `any_of`), `force_merge`, `read_only`, `replica_count`, `shrink`, `allocation`, `snapshot`, **`convert_index_to_remote`** (turns an index into a searchable snapshot from a repo, optionally deleting the original), `search_only`, `rollup`, `delete`, `notification`, `index_priority`. ISM evaluates every `job_interval` (5 min default).
- **Tiers in open source** [https://github.com/opensearch-project/documentation-website/blob/main/_tuning-your-cluster/availability-and-recovery/snapshots/searchable_snapshot.md **[doc]**]:
  - Hot: local SSD data nodes.
  - Warm/cold: **searchable snapshots** on nodes with the `warm` role (renamed from `search` in 3.0). Read-only; data fetched on demand and cached (`node.search.cache.size`; recommended `cluster.filecache.remote_data_ratio` around 5). Docs warn of higher latency, per-request object-store costs, and interference with other queries on shared nodes. Recommendation: force-merge to few segments (ideally 1) before snapshotting. `cluster.max_remote_capable_shards_per_node` (default 1000) caps warm shards.
  - Archive: plain snapshots in S3/GCS/Azure.
- **Remote-backed storage** (2.10+) [https://github.com/opensearch-project/documentation-website/blob/main/_tuning-your-cluster/availability-and-recovery/remote-store/index.md **[doc]**]: segments + translog uploaded to object storage; cluster-level setting that **can only be enabled at cluster bootstrap**. Enables **segment replication** and **search replicas / reader-writer separation** (`search` node role, ISM `search_only`), documented at `_tuning-your-cluster/separate-index-and-search-workloads.md`. This is the most relevant primitive for isolating heavy dashboard queries from ingest.
- **UltraWarm / writable warm / multi-tier**: These are **Amazon OpenSearch Service** features. I found no OSS docs for "writable warm"; the only OSS hint is the 3.9 note "Skip warmup on warm indices since data is fetched on demand from remote store" (k-NN PR #3565) **[unverified whether this refers to an OSS or AWS-only tiering feature]**. Do not design around UltraWarm if you want to be portable.
- **Time-range pruning:** OpenSearch 3.9 adds coordinator-side index pruning (skip indices outside the query time range before any shard work; "more than 80% tail latency reduction" in local benchmarks **[claim]**). Release notes: `opensearch-release-notes-3.9.0.md`. Relevant for many small time-based indices.

**Suggested retention tiers (see recommendation in section 7):** hot 7-14 days, warm (searchable snapshot) to 90-180 days, snapshot-only archive beyond that, with per-tenant retention overrides applied by index name or `delete_by_query`.

### 2.3 Per-tenant strategies (banks)

| Strategy | Isolation | Cost | Verdict |
|---|---|---|---|
| **Index/data-stream per tenant (+ time)** | Strongest in OSS: separate indices, security-plugin index permissions, independent ISM/retention, trivial hard-delete by dropping indices, per-tenant mappings/codec/encryption-at-rest via separate snapshot repos | Shard count: default `cluster.max_shards_per_node` = 1000 non-frozen; every daily backing index of every tenant of every data stream multiplies. Data Prepper/exporter docs explicitly warn "OpenSearch clusters can become unstable or even break down when index counts reach a very high level." | Right for **tens to low hundreds** of regulated tenants, using rollover by size (not daily) and shard counts of 1 for small tenants |
| **Shared indices + `tenant_id` + custom routing** (`index.routing_partition_size`) | Logical only; relies on DLS/application filters | Best resource efficiency; routing keeps one tenant's data on few shards and cuts fan-out | Good for the long tail of small, non-regulated tenants. Partitioned routing exists **[doc]** `_mappings/metadata-fields/routing.md` |
| **Document-level security (DLS) on shared indices** | Read-side filter only: "does not restrict write operations" **[doc]**; supports `${attr.<type>.<name>}` substitution (e.g. `{"term":{"tenant_id":"${attr.jwt.tenant}"}}`); max DLS config 1024 KB; docs recommend keeping queries simple for performance | Cheap on shards, adds per-query filter cost | Acceptable as defense-in-depth on top of app-level filtering, **not** as the sole isolation mechanism for banks |
| **Cluster per tenant / cell** | Strongest | Expensive | For the few tenants that demand it (data residency, dedicated KMS) |
| Dashboards "multi-tenancy" | **Not data isolation.** Tenants (global/private/custom) isolate saved objects (index patterns, dashboards) only **[doc]** `_security/multi-tenancy/` | n/a | Do not conflate |

Recommended hybrid: **tiered tenancy**: "dedicated" tenants get their own data streams (`obs-<tenant>`, `scores-<tenant>`, ...) + security roles scoped to `*-<tenant>*`; "pooled" tenants share `obs-pooled` with `tenant_id`, routing by `tenant_id` (partition size 2-4), DLS via JWT attribute, and an app-layer mandatory filter. Add per-tenant **workload management** groups (Workload Management plugin, introduced 2.18, tenant-level search admission control and cancellation; group settings added 3.7) to stop one tenant's heavy query from starving others **[doc]** `_tuning-your-cluster/availability-and-recovery/workload-management/`.

### 2.4 Mappings: flat_object vs dynamic, explosion limits, big payloads

- **`flat_object`** (2.7+) [**doc** `_mappings/supported-field-types/flat-object.md`]: stores the whole JSON as one field, no per-subfield inverted index, so it avoids mapping explosion and saves space. But it **does not support aggregations on subfields, filtering by subfields (per the docs' "do not support" list), numeric comparison/sorting, text analysis, or highlighting**; it supports term/terms/prefix/range/match/query_string/wildcard/exists on the whole object. Fine for `metadata` you only display or exact-match; wrong for anything you chart (tokens, cost, latency, custom numeric attributes). Promote analytics fields to typed top-level columns; keep arbitrary user metadata in `flat_object`; maintain a small set of **user-configurable promoted fields** per tenant via dynamic templates or reindex.
- **Mapping limits** [**doc** `_mappings/mapping-explosion.md`]: `index.mapping.total_fields.limit` 1000, `depth.limit` 20, `nested_fields.limit` 50, `nested_objects.limit` 10000, `field_name_length.limit` 50000, `index.mapper.dynamic` toggle. Set `dynamic: strict` for core objects, `dynamic: false` or `flat_object` for user metadata.
- **Avoid `nested`** for events/links/messages at billion-span scale; each nested object is a hidden Lucene document. Store messages in the payload stream, one doc per observation (messages array as `flat_object` for display + a concatenated `text` field for search).
- **Large text payloads:**
  - `_source` can be disabled, filtered (`includes/excludes`), or replaced by **derived source** (`index.derived_source.enabled`, static, set at creation). Derived source reconstructs `_source` from doc_values/stored fields, supports boolean, numerics, date/date_nanos, geo_point, ip, keyword, unsigned_long, scaled_float, text, wildcard; **does not support `copy_to` fields, keyword/wildcard with `ignore_above` or `normalizer`, nested fields**, and returns normalized (not byte-identical) JSON, and is slower to fetch many docs **[doc]** `_mappings/metadata-fields/source.md`. Third-party summary claims 41-58% storage reduction and ~18% indexing throughput gains for derived source **[blog/unverified]** (https://bigdataboutique.com/blog/opensearch-tweaks-you-probably-didnt-know-about). Since `flat_object` is not in the supported list, derived source is hard to combine with `flat_object` metadata **[inferred; test]**, and keyword fields with `ignore_above` (the usual safeguard) are excluded. Practical conclusion: use derived source on the numeric/keyword-heavy observation index only if mappings stay within the supported list; keep normal `_source` + `zstd` for payload indices.
  - **Index codec** [**doc** `_im-plugin/index-codecs.md`]: `default` (LZ4), `best_compression` (zlib), `zstd` and `zstd_no_dict` (2.9+; `index.codec.compression_level` 1-6), QAT hardware variants. **zstd codecs cannot be used for k-NN or Security Analytics indexes.** Reported ~30-35% smaller than default **[blog]** https://aws.amazon.com/blogs/big-data/optimize-storage-costs-in-amazon-opensearch-service-using-zstandard-compression. Segments written before a codec change keep the old codec until merged.
  - `index: false` on fields you only retrieve (stored in `_source`, no inverted index/doc values), `doc_values: false` on never-aggregated keywords, `norms: false` and `index_options: docs` on text, and `match_only_text` (no positions/scoring; cheaper but slower phrase queries) are the knobs for conversation text **[doc: field-type pages; trade-off numbers unverified]**.
  - Bulk and HTTP: Data Prepper bulk default 5 MiB; `http.max_content_length` exists for guarding request size **[doc]**.
- **Append-only index** (`index.append_only.enabled`; doc says it skips soft deletes and version tracking, reducing storage and merge work; **Adaptive shard selection for bulk (introduced 3.5)** routes all sub-bulks of a bulk to one shard to cut long-tail bulk latency) **[doc]** `_im-plugin/append-only-index.md`. **Catch:** append-only indices **reject bulk `index` actions with a custom `_id`**, so you lose idempotent ingestion by deterministic id (OTel retries can produce duplicates). Data streams allow `create` with explicit `_id` (dedup only within the same backing index). **Decision needed**: accept duplicates and dedupe on read (`span_id` unique key), or use data streams (not append_only) with `create` and explicit ids. Star-tree *requires* append-only.
- **Update costs:** an update rewrites the whole doc (new segment doc + tombstone); with a large `_source`, updating a trace to attach scores would re-index the payload. Hence: never update observation docs; write scores as separate docs; use a small mutable "trace header" index only if you must.

### 2.5 Joining scores to traces (no real joins)

Options, in the order I'd use them:
1. **Denormalize at ingest** what is known at ingest time (trace name, tags, user, session, release) onto every observation doc.
2. **Query-time two-step in the application:** (a) filter scores (`name=correctness AND value<0.5 AND tenant`) -> collect trace ids (up to thousands), (b) `terms` query / terms-lookup on observations. Cheap, cacheable, and the pattern most OpenSearch-based products use. Needs pagination for big id sets.
3. **Rollup into an entity-centric index with Transforms** (`_plugins/_transform`): continuous job groups scores by `trace_id` and computes `avg/min/max/value_count/percentiles/scripted_metric` into a `trace-scores` index; supports a source-query filter, runs at intervals; groups/aggs cannot be changed after creation **[doc]** `_im-plugin/index-transforms/index.md`. Good for "latest score per trace" or "traces with any failing eval", at minute-level lag. Late scores are naturally handled because continuous transforms re-process changed buckets.
4. **Parent/child `join` field** [**doc** `_mappings/supported-field-types/join.md`]: parent and child must be in the same index and on the same shard (routing); costly `has_child` queries and global ordinals. Not recommended at billions of docs.
5. **PPL `join` / `lookup` / subsearch** (Calcite engine, "V3"): in 3.9 `plugins.calcite.enabled` default is **true** and the docs list `join` (inner/left/semi/anti; `right/full/cross` need `plugins.calcite.all_join_types.allowed`), `lookup` (left-join-like enrich from a "dimension table"), `subsearch`, `graphlookup`. Limits **[doc]** `_sql-and-ppl/settings.md`: `plugins.query.size_limit` 10,000 rows, `plugins.ppl.join.subsearch_maxout` 50,000 rows, `plugins.query.memory_limit` 85% heap breaker; the 3.0 docs labeled these as experimental, and I could not find a statement of when they went GA (fallback to the V2 engine is off by default now: `plugins.calcite.fallback.allowed=false`). Verdict: handy for ad-hoc analysis and for the Agent Traces UI, **not** a billion-row analytic join engine. In the 3.8/3.9 notes there is substantial work on an "analytics-engine route" (PPL on a DataFusion/Parquet backend, "Bring `CalcitePPLJoinIT` to parity on the analytics-engine route"), i.e., a future where PPL joins run on a columnar engine, but it is not documented as available in OSS **[unverified]**.
6. **Rollups** (`_plugins/_rollup`) for pre-aggregated time series (cost/tokens/latency by model/day).

---

## 3. Analytics performance vs ClickHouse (honest assessment)

### 3.1 What exists publicly
- **No public OpenSearch vs ClickHouse benchmark found.** ClickBench's own README says the Elasticsearch entry is a "naive" deployment (9.1, c6a.4xlarge, load ~3 h), and lists search engines "Elasticsearch and Manticore" only; I found no OpenSearch directory (HTTP 404 for `opensearch/README.md`) **[doc]** https://github.com/ClickHouse/ClickBench (README + `elasticsearch/README.md`). So "ClickBench for OpenSearch" is **[unverified / likely does not exist]**.
- **ClickHouse vs Elasticsearch on OTel logs (ClickHouse-authored, April 2026)** https://clickhouse.com/blog/elasticsearch-log-analytics-clickhouse **[blog, vendor]**: 1B/10B/50B rows, m6i.8xlarge, ES OSS 9.3.2 (force-merged) vs ClickHouse 26.3. Storage: CH 49.5 GiB vs ES 245 GiB at 1B (4.95x), and 2.43 TiB vs 12.01 TiB at 50B. Nine full-text+analytics queries: cold 4.1-6.2x faster, hot 1.7-2.6x faster for ClickHouse (gap narrows with scale). ES ingest of 50B took ~5 days vs under 4 hours. ES tuning only shaved 2-20% of storage. The authors explicitly state they did not run the suite against OpenSearch.
- **OpenSearch vs Elasticsearch (Trail of Bits for AWS, March 2025)** https://blog.trailofbits.com/2025/03/06/benchmarking-opensearch-and-elasticsearch/ **[blog, AWS-commissioned]**: OpenSearch 2.17.1 vs ES 8.15.4 on Big5, p90 service time: terms aggs 3.38x faster, date histograms 16.55x faster, sort/range ~parity, **text queries 2.42x slower**, overall 1.56x faster. Noted a pathological outlier for OpenSearch (`composite-date_histogram-daily` max/median 1412x).
- **OpenSearch's own Big5 trend (mean ms, single node c5.2xlarge, 116M docs)** https://opensearch.org/blog/opensearch-3-3-performance-innovations-for-ai-search-solutions/ **[blog, project]**: terms agg 112 ms (2.19) -> 78.5 ms (3.3); date histogram 160 -> 91 ms; geo-mean 21.2 -> 14.0 ms. The 116M-doc/100 GB dataset is far below billions of spans.
- **Amazon OpenSearch Service "optimized log analytics" engine (July 2026)** https://aws.amazon.com/blogs/big-data/run-log-analytics-for-a-fraction-of-the-cost-with-the-new-engine-for-amazon-opensearch-service/ **[blog, vendor claim]**: Parquet primary storage + Lucene inverted index only for searchable fields, Calcite planner routing to DataFusion or Lucene. On 24.4B docs / 9.5 TB raw JSON: OTel traces 841 GB vs 4,131 GB raw (80% savings), OTel logs 78%, web logs 89%; against **default Lucene (LZ4) savings of 59-73%, against best_compression 47-59%** (implying a Lucene footprint of roughly 2-3x the Parquet one **[inferred]**). "Up to 4x price-performance", 2x ingestion. Limitations: domain-level, **PPL/SQL only, DSL not supported**, cannot enable on existing domains, **managed-AWS-only as far as the page says**. This is the strongest *signal* that the OpenSearch community itself recognizes Lucene's analytic/storage gap. In upstream, sandbox plugins `analytics-engine`, `analytics-backend-datafusion`, `analytics-backend-lucene`, `analytics-framework` exist on Maven (last released 2026-04-07) **[blog/index]** https://mvnrepository.com/artifact/org.opensearch.sandbox. Treat as roadmap, not a dependency.

### 3.2 Star-tree index
- Introduced 2.18 (experimental), **GA in 3.1** per 3.1 release notes ("Star-tree indexes in production ... aggregations by up to 100x"), multi-term aggregations in 3.3 (~3x on high-cardinality fields, "up to ~40x" complex queries, vendor figures) **[doc/blog]**.
- Docs (`_search-plugins/star-tree-index.md`, `_mappings/supported-field-types/star-tree.md`) **[doc]**: pre-aggregates metrics per dimension combination at refresh/merge. **Requirements/limits:** `index.composite_index: true` and `index.append_only.enabled: true` (no updates/deletes), 2-10 dimensions, only **one star-tree per index**, config changes need reindex, no array values, `must_not`/`minimum_should_match` unsupported, queries must be only on dimension fields and aggs only `sum/min/max/value_count/avg` (plus date histogram with metric sub-aggs and terms/multi-terms/range buckets). **No percentiles, no cardinality, no top_hits.** Avoid high-cardinality dimensions like ids.
- Fit for this product: great for pre-defined dashboards (`tenant, model, operation, status, day` -> `sum(tokens), sum(cost), count, avg(latency)`), useless for p95 latency or distinct users/traces, and for ad-hoc drill-down. In effect it is a materialized view you must design in advance, whereas ClickHouse gets this from `AggregatingMergeTree`/materialized views with percentile states.

### 3.3 Where OpenSearch is weaker (be honest)
1. **Percentiles / cardinality** are approximate (t-digest / HLL++), computed per-shard then merged; accurate but not exact, and cost scales with matched docs; star-tree cannot accelerate them **[doc]**.
2. **High-cardinality group-bys** (trace_id, user_id, session_id) hit per-shard top-N limits (`shard_size`), `search.max_buckets`, heap circuit breakers, and the PPL `plugins.query.buckets`/`size_limit` caps (10,000 by default) **[doc]**. Paging through "all sessions with stats" needs composite aggs or transforms.
3. **Storage density:** Lucene stores each field up to three times (inverted, doc_values, `_source`/stored). The ClickHouse study shows ~5x for ES; OpenSearch with `zstd` closes only part of it **[blog]**.
4. **Wide scans and ad-hoc SQL:** no vectorized execution, no sparse primary index with granule skipping (index sorting + time-range pruning help but are not equivalent), JVM heap pressure, shard-per-index overhead.
5. **Joins/subqueries/window functions:** limited and size-capped (section 2.5).
6. **Updates/upserts/deletes:** costly (segment rewrite, tombstones merged lazily), unlike ClickHouse where ReplacingMergeTree is also lazy but cheaper per row.
7. **Ingest cost:** inverted indexing of large text is CPU-heavy; ES needed days for 50B rows in ClickHouse's test (single node) **[blog]**.
8. **Tail latency variance** in published runs (Trail of Bits outlier) **[blog]**.

### 3.4 Where OpenSearch is better
Free-text relevance/phrase/fuzzy search, highlighting, hybrid lexical + vector retrieval, per-index/field/doc security, easy horizontal scale-out for point lookups (`trace_id` fetches routed to one shard if you route by `trace_id`), snapshot/restore and searchable snapshots, mature ecosystem and Dashboards.

---

## 4. Vector / k-NN for failure clustering and dataset building

Facts **[doc]** (`_vector-search/`, 3.9 release notes):
- `knn_vector` field with engines Faiss, Lucene (and JVector mentioned for efficient filtering), HNSW/IVF; **disk-based vector search** (`mode: on_disk`, default 32x binary compression with rescoring; introduced 2.17); scalar quantization 1/2/4-bit and FP16 (`half_float`, bf16) as of 3.9; **derived source for k-NN vectors production-ready in 3.9** (storage reduced up to 3x, per notes); dynamic mapping for `knn_vector` (3.9); `remote-index-build` (GPU offload) docs exist; efficient filtering with Lucene/Faiss engines for pre-filtering by `tenant_id`.
- **zstd/zstd_no_dict codecs do not work on k-NN indexes** (index-codecs page). So keep embeddings in their own index (no zstd), separate from payload/text indices.
- Hybrid search, neural search, `semantic` field type, and ML Commons connectors let OpenSearch call an external embedding model at ingest or query time.
- PPL `ml` command (and deprecated `kmeans`, which requires Calcite disabled) can run k-means/RCF via ML Commons; for real clustering (HDBSCAN, UMAP) plan on exporting vectors to a batch job instead.

Pattern for this product:
1. At ingest or asynchronously, embed `input`/`output` (or an LLM-produced "failure summary") and write `{tenant_id, observation_id, trace_id, vector, eval_failed, score_summary, ts}` to `embeddings-<tenant|pooled>`.
2. "Similar conversations" -> k-NN with a filter (`tenant_id`, time window, `eval_failed:true`). Dataset-building -> k-NN seeded from a chosen failing trace (+ MMR re-ranking, which exists as `vector-search-mmr`).
3. Failure clusters -> periodic job: sample failed-eval vectors, cluster offline (e.g. HDBSCAN), write `cluster_id` back as a field on the embeddings doc (an update; these docs are small) and expose cluster stats via normal aggregations.
4. Cost: vectors at 768-1536 dims dominate memory; use disk-based mode + quantization. Security: vectors leak information about content; apply the same index-level permissions.

Compared with ClickHouse (vector similarity index is experimental/limited) this is a real advantage of OpenSearch. In Trail of Bits' test OpenSearch's NMSLIB/Faiss engines were 11-14% faster than ES's Lucene HNSW at 10M x 768 **[blog]**.

---

## 5. Security, compliance, deletion

**Security plugin (OSS, Apache-2.0, included)** [**doc** `_security/`]:
- TLS, authn backends (basic, LDAP, SAML, OIDC, JWT, proxy), RBAC with index/data-stream permissions, API keys.
- **DLS** (query-filter per role; substitution `${user.name}`, `${attr.<type>.<name>}`; read-only; 1024 KB config limit; "keep queries simple"), **FLS** (include/exclude fields), **field masking** (string fields only, replaced with a **cryptographic hash**, salt via `plugins.security.compliance.salt`). **Gotcha:** the docs state masked fields **cannot be searched**, even for non-masked terms, because masking is applied after indexing, and offer workarounds: separate fields, a pre-masked second index, or FLS to hide the field. That means "show redacted prompt text to analysts but let them full-text search it" requires **ingest-time PII redaction** into a separate field or index, not field masking.
- **Audit logs**: disabled by default; modes standard/standalone; categories configurable; storage types `internal_opensearch`, `internal_opensearch_data_stream`, `external_opensearch`, `webhook`, `log4j` (Kafka via appenders). The standalone-mode page lists SOC 2, HIPAA, PCI DSS, GDPR as target frameworks. Good for banks (ship to an external cluster/SIEM).
- **Resource Sharing** framework went GA in 3.9 (for plugin-owned resources such as dashboards/detectors) - not for event data.
- **Dashboards multi-tenancy** = saved-object spaces only (global/private/custom tenants). **Workspaces** (2.18+) add ACLed workspaces; the Agent Traces and APM UIs require workspaces to be enabled.
- Encryption at rest is a disk/KMS concern (no per-tenant keys in OSS **[unverified]**); per-tenant keys imply per-tenant clusters/volumes or application-level encryption of payload fields.

**GDPR / right-to-erasure in time-based indices**
- Index-per-tenant: tenant offboarding or retention = delete indices (and their snapshots). Cheap and provable.
- Per-subject erasure inside shared/time-based indices: `_delete_by_query` (marks docs deleted; space reclaimed only on segment merge; heavy on large indices; blocked by append-only indices, which reject delete/update-by-query) **[doc]** (`append-only-index.md`, data streams page). Strategies: (a) keep a **subject-keyed payload stream** so erasure touches only the small payload index (not the metrics/aggregates), (b) store PII in encrypted payload fields with per-subject keys and delete the key (crypto-shredding; application-level), (c) pseudonymize `user_id` before indexing and keep the mapping in Postgres, (d) for snapshots/searchable snapshots, deletion requires restore -> delete -> re-snapshot or waiting for retention expiry; document this in the DPA.
- Do not make the observation/metrics data streams `append_only` if you want `delete_by_query`; or make only the metrics/non-PII stream append-only (star-tree) and keep payloads in a non-append-only stream.

---

## 6. Elasticsearch differences relevant here

| Topic | Elasticsearch | OpenSearch | Source |
|---|---|---|---|
| License | Since Aug/Sept 2024 the free source is **triple-licensed: AGPLv3, SSPL 1.0, ELv2** (OSI-approved open source via AGPL); client libraries Apache-2.0; binaries/distribution unchanged. Paid features remain under Elastic's commercial license | Apache-2.0, Linux Foundation OpenSearch Software Foundation | https://www.businesswire.com/news/home/20240829537786/en **[blog/press]**, https://itbrief.com.au/story/elastic-adds-agpl-option-to-elasticsearch-kibana-licences |
| Embedding/redistributing in an open-source framework | AGPL network copyleft is a concern if you bundle/modify ES; using ES as an external service is fine | No concern | **[inferred; legal review needed]** |
| Security features | Field- and document-level security, ABAC, SAML/OIDC, audit logging listed on the self-managed subscriptions matrix; the extracted text did not preserve tier checkmarks, so which tier covers which feature is **[unverified from my extraction]** | All in the OSS security plugin | https://www.elastic.co/subscriptions, https://www.elastic.co/pdf/subscriptions-2026-08-04.pdf |
| Storage-saving mode | **logsdb index mode** GA in 8.17 (index sorting + synthetic `_source` + better compression; Elastic claims up to 65% less log storage). **Synthetic `_source` is an Enterprise feature from 8.17 on** (footnote 15 of the subscriptions PDF). A "Columnar indices (Tech Preview)" row and "Synthetic columnar `_source` (Tech Preview)" appear in the matrix | zstd codec + derived source (open) | https://www.businesswire.com/news/home/20241213828255/en/Elastic-Announces-Elasticsearch-Logsdb-Index-Mode-to-Reduce-Log-Data-Storage-Footprint-by-Up-to-65, https://www.elastic.co/search-labs/blog/elasticsearch-logsdb-index-mode |
| Query language / joins | **ES\|QL** with `LOOKUP JOIN`: preview in 9.0, **GA in 9.1**; multi-field joins GA 9.2; expression joins preview 9.2, full-text join conditions preview 9.3; requires the lookup index to use `lookup` index mode, no wildcards in the lookup name; ES\|QL "Lookup Joins" appears in the subscriptions matrix | PPL `join`/`lookup` (size-capped, section 2.5) | https://www.elastic.co/docs/reference/query-languages/esql/commands/lookup-join |
| Tiering | Data tiers, ILM, data stream lifecycle, **searchable snapshots/frozen** (listed in the matrix; tier unverified) | ISM + searchable snapshots (OSS) | subscriptions matrix |
| LLM observability | "LLM Observability" and "LLM tracing" are rows in the commercial matrix; docs describe EDOT (Elastic Distributions of OTel) Java/Node/Python in **tech preview**, integrations for OpenAI, Azure OpenAI, Bedrock, Vertex AI (+ Anthropic in newer docs), ingest of Langtrace/OpenLIT/OpenLLMetry traces, prebuilt dashboards | Agent Traces + Python SDK (Alpha), 3.6+ | https://www.elastic.co/docs/solutions/observability/get-started/opentelemetry/use-cases/llms |
| OTel exporter | `elasticsearchexporter`: **beta** for logs/traces/metrics, default `otel` mapping mode (8.12+, best 8.16+), data stream routing `${type}-${dataset}-${namespace}`, 4 code owners | `opensearchexporter`: **alpha** traces/logs | READMEs in opentelemetry-collector-contrib |
| Benchmarks | ClickBench has an ES entry (naive config); ClickHouse's OTel-log study (above) is ES-only | AWS-commissioned: OS faster than ES on terms/date-histogram aggs, slower on text queries (Trail of Bits) | links above |

Summary: Elasticsearch is a few features ahead on storage efficiency and joins, but the best (synthetic `_source`, and probably DLS/FLS and frozen tier) are commercial. For an Apache-2.0 open-source framework whose enterprise users (banks) want in-cluster DLS/FLS/audit, OpenSearch's fully-open security plugin is a real advantage. Elastic's open AGPL tier gives you the engine but not these.

### Should the framework abstract the backend?
**Yes, but narrowly.**
- Abstract at the **repository/port level** ("append observations", "append scores", "query traces by filter", "aggregate metrics", "search messages", "similar-to", "delete subject/tenant"), not at a generic query-DSL level.
- Keep one **common Query DSL subset** (bool/term/range/match, terms aggs, date_histogram, percentiles) so OpenSearch 2.x/3.x and Elasticsearch 8.x/9.x both work; keep PPL/ES|QL out of the core path (use them only in optional adapters or for ad-hoc/UI features).
- Avoid vendor-specific index features in the core contract (star-tree, flat_object, derived source, ISM, DLS syntax). Isolate them in a per-backend "index provisioner" module (templates, lifecycle, security roles).
- Keep a **ClickHouse adapter as a future option** for analytic aggregates (read-heavy dashboards, percentiles at scale). The Langfuse-style write path (OTLP -> queue -> batch writer) already makes dual-write plausible.
- Use the OTel Collector for ingestion with a custom pipeline in front of the store (Kafka/queue + your writer) rather than the contrib exporters, since both OpenSearch (alpha) and Elasticsearch (beta) exporters are not stable and neither knows your tenancy/payload split.

---

## 7. Recommended concrete layout (summary)

**Cluster**
- OpenSearch 3.9+ (Lucene 10), dedicated cluster-manager nodes, hot data nodes (NVMe), `warm`-role nodes for searchable snapshots; consider remote-backed storage + segment replication + search replicas for ingest/query isolation (must be chosen at cluster bootstrap).
- Writer service (not Data Prepper's stateful trace processor) bulk-indexes with 5-15 MB bulks; Collector -> Kafka -> writer.

**Indices / data streams** (all with `dynamic: strict` on core fields, `date_nanos` timestamps, `routing = trace_id`)
- `obs-<tenantTier>` : one doc per observation; typed columns: `tenant_id, project_id, trace_id, span_id, parent_span_id, name, type, model, provider, operation, status, start/end, duration_ns, input_tokens, output_tokens, total_cost, session_id, user_id, release, tags (keyword[]), metadata (flat_object)`; **no message bodies**. Codec `zstd`; consider derived source if mappings qualify. Rollover at ~30-50 GB per primary shard (not daily), 1 primary shard for small tenants.
- `payload-<tenantTier>` : `observation_id, trace_id, tenant_id, input_text, output_text (text, match_only_text or index_options docs), messages (flat_object, index:false for display), tool_io`. Codec `zstd` level 3-6. Optionally separate retention.
- `scores-<tenantTier>` : append-only score events as above; denormalize `trace_name, tags, user_id, model` at write time when known; Transform job to a `trace-scores` summary for "latest per trace/name".
- `embeddings-<tenantTier>` : `knn_vector` (on_disk + quantization), default codec (no zstd), `tenant_id` filterable.
- `traces-<tenantTier>` (optional thin header docs) and a continuous Transform -> `trace-rollup` for list views (trace-level latency/cost/token sums).
- Star-tree on a separate `obs-metrics-<tenantTier>` (append-only, derived from observations or written in parallel) for fixed dashboards; percentiles via normal aggs on the raw index.

**Tenancy**
- Tiered: dedicated data streams for regulated/large tenants (banks) with role `tenant_<id>` scoped to `*-<id>-*`, own retention, own snapshot repository/KMS key, optional dedicated cluster for the strictest; pooled indices for the long tail with `tenant_id` routing (partition size 2-4), mandatory application filter + DLS via JWT attribute, and Workload Management groups. Cap tenants-per-cluster and total shards; plan "cell" clusters.

**Retention tiers (defaults to be overridden per tenant)**
- Hot 0-14 d (full indices, replicas 1) -> force-merge + `snapshot` + `convert_index_to_remote` (searchable snapshot, warm nodes, 0 replicas) to ~90-180 d -> snapshot-only archive/delete per policy. Payload streams may use shorter hot windows than score/metric streams. Scores are small and can be retained longer than payloads.

**Main risks**
1. Shard/index explosion with index-per-tenant; ISM/cluster state pressure.
2. Aggregation and storage gap vs ClickHouse at billions of spans (approx percentiles, high-cardinality group-bys, 3-5x storage), mitigated only partially by star-tree (append-only, fixed dims, no percentiles) and zstd.
3. No scalable join; scores must be denormalized, transformed, or joined in the app; PPL join capped at 50k subsearch rows.
4. Exporter/Data Prepper immaturity: `opensearchexporter` alpha with two active owners; Data Prepper trace processor is stateful (180 s flush).
5. GenAI UX features are new (3.6+; SDK Alpha); schema mismatch vs your Langfuse-like model.
6. Append-only conflicts with idempotent ids and with `delete_by_query` (GDPR); field masking is hash-only and breaks search.
7. Searchable-snapshot warm tier has real latency and request-cost trade-offs; UltraWarm/writable warm are AWS-only.
8. The faster columnar engine (Parquet/DataFusion) appears AWS-managed or sandbox only **[unverified upstream status]**.
9. Large payload cost in Lucene (text indexed + stored); needs aggressive field-level index control.

---

## 8. Open questions to verify with a spike
- Real storage per million LLM spans (with 2-20 KB payloads) under: default vs `zstd` vs derived source; `text` vs `match_only_text`.
- Whether derived source works with `flat_object` fields (not in the supported list) and with `date_nanos`/`ip` mixes.
- Star-tree behavior with DLS/index aliases, and whether percentiles on a star-tree index silently fall back to the normal path.
- PPL `join`/`lookup` behavior when the right side exceeds 50,000 rows, and whether PPL on 3.9 is stable enough to expose to users.
- Deduplication strategy under append-only vs data stream `create` with deterministic ids.
- Behavior of coordinator-side time-range pruning (3.9) with data streams and many small tenant indices.
- Whether an OpenSearch "analytics-engine"/Parquet path is shipping in the open-source distribution (3.10+).

---

## Source list

OpenSearch (docs repo, read locally): https://github.com/opensearch-project/documentation-website
- Agent traces: `_observing-your-data/agent-traces/{index,agent-tracing,instrument}.md`
- APM: `_observing-your-data/apm/*.md`; SS4O: `_observing-your-data/ss4o.md`
- Data Prepper: `_data-prepper/pipelines/configuration/{sinks/opensearch.md,processors/otel-traces.md}`
- Data streams `_im-plugin/data-streams.md`; append-only `_im-plugin/append-only-index.md`; codecs `_im-plugin/index-codecs.md`; ISM `_im-plugin/ism/policies*.md`; transforms `_im-plugin/index-transforms/index.md`
- Searchable snapshots `_tuning-your-cluster/availability-and-recovery/snapshots/searchable_snapshot.md`; remote store `.../remote-store/index.md`; workload mgmt `.../workload-management/`
- Mapping: `_mappings/supported-field-types/{flat-object,star-tree,join,derived}.md`, `_mappings/metadata-fields/{source,routing}.md`, `_mappings/mapping-explosion.md`
- Star-tree: `_search-plugins/star-tree-index.md`
- PPL/SQL: `_sql-and-ppl/{settings,limitation}.md`, `_sql-and-ppl/ppl/commands/{join,lookup,kmeans}.md`
- Vector: `_vector-search/optimizing-storage/disk-based-vector-search.md`, `_vector-search/filter-search-knn/efficient-knn-filtering.md`
- Security: `_security/access-control/{document-level-security,field-level-security,field-masking}.md`, `_security/audit-logs/*.md`, `_security/multi-tenancy/*.md`
- Release notes: https://github.com/opensearch-project/opensearch-build/tree/main/release-notes
- Web mirrors: https://docs.opensearch.org/latest/ ; https://observability.opensearch.org/docs/ai-observability ; https://observability.opensearch.org/docs/ai-observability/agent-tracing/ ; https://observability.opensearch.org/docs/agent-evals/evaluation/ ; https://observability.opensearch.org/docs/agent-health/ ; https://observability.opensearch.org/

OTel Collector exporters: https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/exporter/opensearchexporter/README.md ; https://raw.githubusercontent.com/open-telemetry/opentelemetry-collector-contrib/main/exporter/elasticsearchexporter/README.md ; https://forum.opensearch.org/t/opentelelmetry-data-w-o-dataprep/23934

Benchmarks and comparisons: https://clickhouse.com/blog/elasticsearch-log-analytics-clickhouse ; https://blog.trailofbits.com/2025/03/06/benchmarking-opensearch-and-elasticsearch/ ; https://opensearch.org/blog/opensearch-3-3-performance-innovations-for-ai-search-solutions/ ; https://opensearch.org/blog/opensearch-performance-improvements/ (2.11-era) ; https://aws.amazon.com/blogs/big-data/run-log-analytics-for-a-fraction-of-the-cost-with-the-new-engine-for-amazon-opensearch-service/ ; https://docs.aws.amazon.com/opensearch-service/latest/developerguide/optimized-log-analytics.html ; https://mvnrepository.com/artifact/org.opensearch.sandbox ; https://github.com/ClickHouse/ClickBench (README, `elasticsearch/README.md`) ; https://aws.amazon.com/blogs/big-data/optimize-storage-costs-in-amazon-opensearch-service-using-zstandard-compression ; https://bigdataboutique.com/blog/opensearch-tweaks-you-probably-didnt-know-about ; https://aws.amazon.com/about-aws/whats-new/2025/09/amazon-opensearch-star-tree-index

Governance: https://www.linuxfoundation.org/blog/how-the-opensearch-software-foundation-will-ensure-long-term-sustainability-of-the-opensearch-project

Elasticsearch: https://www.businesswire.com/news/home/20240829537786/en ; https://itbrief.com.au/story/elastic-adds-agpl-option-to-elasticsearch-kibana-licences ; https://www.elastic.co/subscriptions ; https://www.elastic.co/pdf/subscriptions-2026-08-04.pdf ; https://www.elastic.co/docs/reference/query-languages/esql/commands/lookup-join ; https://www.elastic.co/search-labs/blog/elasticsearch-logsdb-index-mode ; https://www.businesswire.com/news/home/20241213828255/en/Elastic-Announces-Elasticsearch-Logsdb-Index-Mode-to-Reduce-Log-Data-Storage-Footprint-by-Up-to-65 ; https://www.elastic.co/docs/solutions/observability/get-started/opentelemetry/use-cases/llms
