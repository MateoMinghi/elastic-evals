# Survey: open-source LLM / agent evaluation and observability frameworks

Date of research: 2026-10-20. Scope: everything except Langfuse (covered separately). Method: WebFetch of GitHub READMEs and vendor docs plus WebSearch. WebFetch summarises pages with a small model, so several pages returned partial information; anything I could not confirm is marked **[UNVERIFIED]** or **[from memory]**. Star counts are as displayed on GitHub at fetch time.

---

## 0. TL;DR table

| Project | License | Trace/score store | OTel ingest | Evaluator abstraction (score repr.) | Online evals | Classifier/small-model evaluators | Human annotation | Agent/multi-turn evals | CI |
|---|---|---|---|---|---|---|---|---|---|
| Arize Phoenix | **ELv2** (not OSS by OSI; no managed-service resale) | SQLite (default) or **Postgres** (+ read replica) | Yes; OpenInference native, `gen_ai.*` auto-converted to OpenInference at ingest (15.10+) | `Evaluator` -> `Score(name, score, label, explanation, kind, direction)`; `create_classifier` (label -> numeric map via forced tool-call) | Not in OSS on live traces per release-notes index; server-side evaluators run on dataset experiments only. Online evals are in Arize AX (commercial) | Classifier = LLM w/ forced label; code evaluators run in sandboxes. No small-model/typed-classifier backend | Annotations (human/LLM/code kinds), annotation configs, `user_feedback`; annotation queues not confirmed in OSS | Tool Selection / Tool Invocation evaluators, User Friction, conversation-grounded hallucination, Sessions + Session Turns APIs | Python/TS clients; no dedicated CI runner **[UNVERIFIED]** |
| Comet Opik | **Apache-2.0** | **ClickHouse** (traces/spans/feedback/experiments) + MySQL + Redis (streams) + MinIO | Yes, OTLP/HTTP only (no gRPC); `thread_id`, `opik.*` attrs | `BaseMetric.score(...) -> ScoreResult(name, value, reason, metadata)` or list | **Yes**: rules w/ sampling, LLM judge, Python metrics (thread rules), agent-as-judge, alerts | LLM judges; supports "Jev" classifier through OpenRouter for pass/fail | **Annotation queues** (traces or threads), automation rules to fill queues | Agent Task Completion / Tool Correctness / Trajectory Accuracy judges; thread rules (coherence, user frustration) | PyTest integration |
| Traceloop OpenLLMetry | **Apache-2.0** | None (instrumentation only; any OTel backend) | Is the OTel instrumentation | None (no eval abstraction in repo) | n/a | n/a | n/a | n/a | n/a |
| MLflow GenAI | **Apache-2.0** | SQL (Postgres/MySQL recommended) + artifact store | Yes: OTLP `/v1/traces`; ingests and exports GenAI semconv | `@scorer` fn / `Scorer` / `make_judge` -> bool/num/categorical or `Feedback(value, rationale)` attached as "assessment" on trace | **Yes (LLM judges only)**: sample rate + filter, runs async in server, 5-min settle, only traces <1h old, no retries | Judges via AI Gateway; no classifier path | Human feedback assessments on traces; judge alignment (DSPy) | Session-level judges (multi-turn) online; trace-based judges see whole trace | `mlflow.genai.evaluate` in CI docs |
| DeepEval | **Apache-2.0** | Local/none; optional Confident AI cloud | `@observe` tracing (own), integrations; not OTel-store | `BaseMetric.measure(test_case)` sets `score`, `success`, `reason`; `threshold` | Production monitoring is Confident AI (commercial) | **JevEval / `system_one` mode** (typed classifier "Jev"); local models via `DeepEvalBaseLLM` | Via Confident AI | Large set: Task Completion, Tool/Argument Correctness, Plan Adherence/Quality, Step Efficiency; conversational metrics | **Pytest-native** `deepeval test run` |
| Ragas | **Apache-2.0** | none | Integrations only | Metric classes; `SingleTurnSample` / `MultiTurnSample`; `DiscreteMetric` | No | Some non-LLM metrics (BLEU, ROUGE, string) | No | Tool Call Accuracy / F1, Agent Goal Accuracy, Topic Adherence | Library; none native **[UNVERIFIED]** |
| promptfoo | **MIT** (now part of OpenAI) | Local (SQLite **[from memory]**) | Can ingest traces for trajectory assertions (tracing support, details **[UNVERIFIED]**) | Assertion -> `{pass, score, reason, componentResults}`; weights, thresholds, named metrics | No (offline) | `classifier` assertion type (HF-style model) | Web UI review only | `trajectory:*`, `tool-call-f1`, `is-valid-*-call`; red-team | **Best in class**: YAML config, GH Action, PR scanning |
| Inspect AI (UK AISI) | **MIT** | Log files: `.eval` (zstd, binary, incremental read) or JSON on local/S3/Azure/HF buckets | No (own transcript events) | `async def score(state, target) -> Score(value, answer, explanation, reason, metadata)`; `Value` = str/num/bool/list/dict; metrics declared on scorer (accuracy, stderr) | No; but **Inspect Scout** scans transcripts post hoc at scale, reads Phoenix/LangSmith/MLflow/Weave/Logfire | `model_graded_*`; any callable | Score editing with provenance (`edit_score`) | Best-in-class agent harness, sandboxes, multi-epoch reducers, 200+ evals | `inspect eval` CLI; `eval-retry` |
| OpenAI Evals | **MIT** repo | JSONL/registry; OpenAI hosted | No | Eval classes + YAML registry; hosted graders (`string_check`...) | Hosted stored-completions evals | Model-graded YAML | No | Completion Function protocol | n/a. **Hosted Evals platform deprecated: read-only 2026-10-31, shutdown 2026-11-30** |
| TruLens | **MIT** (Snowflake-maintained) | SQLAlchemy DBs (SQLite, Postgres, Snowflake) per site | **OTel-native** (spans) | `Metric` + `Selector`s over span attributes -> score 0-1 + explanation | **Yes**: live as traces recorded, or batch | LLM judges; benchmark claims self-reported | Not confirmed | Agent GPA: Plan Adherence, Tool Selection, etc. | Library |
| Agenta | **MIT** (`ee/` folder separate **[license UNVERIFIED]**) | **Postgres** (3 DBs: core, tracing, supertokens), Redis, S3-compatible store | Yes (OTel, OpenInference, OpenLLMetry compat) | Evaluator = workflow with slug; annotation payload attaches to span | Docs summary lists "automated evaluation of live traffic"; mechanism **[UNVERIFIED]** | LLM-as-judge, custom code evaluators | **Annotation queues** over traces or test cases; export as test set | Limited **[UNVERIFIED]** | Via API |
| Laminar | **Apache-2.0** (Signals gated behind enterprise license) | **ClickHouse** + Postgres + **Quickwit** (full-text) + optional RabbitMQ/Redis; Rust app server | Yes, OTel gRPC/HTTP | `evaluate({data, executor, evaluators})`, evaluators return number or map of numbers; each datapoint = one trace w/ `EVALUATION`, `EXECUTOR`, `EVALUATOR` spans | Span labels with one evaluator per label (code or LLM-judge); Signals (NL-described events, enterprise) | LLM / code | **Labeling queues** (FIFO) | Browser/agent session focus; trajectory via SQL over traces | CLI `lmnr eval` |
| Helicone | **Apache-2.0**, **maintenance mode since Mintlify acquisition (Mar 2026)** | ClickHouse + Supabase(Postgres) + MinIO | Proxy/gateway first, not OTel-first | Scores API **[UNVERIFIED]** | Limited | No | No | Sessions only | No |
| Evidently | **Apache-2.0** | OSS tracing service: SQLite/Postgres/FS/S3 (fsspec); Evidently Cloud | Via Tracely (OTel-based) | Descriptors (row-level columns) + Reports; Test Suites w/ conditions | Batch reports; monitoring dashboards | Statistical/ML drift + text descriptors (sentiment, length) are cheap non-LLM checks | No | Light | Test Suites for CI |
| Giskard (v3) | **Apache-2.0** | none (results in-process) | No | `Scenario` (interactions + checks), `Check` (assertion or LLM judge), `Suite`; default judge gpt-4o-mini | No | Deterministic checks (string/regex/semantic sim) + judges | No | Multi-turn scenario API; vulnerability scan / red-team | pip library |
| autoevals (Braintrust) | **MIT** | n/a (library) | n/a | `Score(name, score in [0,1] or null, metadata)`; `LLMClassifier` prompt + `choice_scores` map | n/a (used by Braintrust online scoring) | **`LLMClassifier` w/ `choice_scores`** pattern, fine-tuned binary classifiers listed | n/a | n/a | n/a |
| LangSmith (closed) | Proprietary; self-host = Enterprise add-on | **ClickHouse** (traces+feedback) + Postgres + Redis + blob; optional "SmithDB" columnar store (0.17+) | OTel ingest not confirmed in fetched page | Feedback `{key, score/value, comment}` **[from memory]** | Online evaluators w/ filters + sampling; run-level and thread-level | LLM/code | Annotation queues **[from memory]** | Threads (multi-turn); agentevals trajectory **[from memory]** | pytest/vitest plugins **[from memory]** |
| Braintrust (closed) | Proprietary; `autoevals` MIT | **Brainstore**: object storage (S3/GCS/Azure) as source of truth + WAL, Tantivy index, Rust, stateless nodes; Postgres metadata; Redis | OTel supported **[UNVERIFIED in fetched pages]** | Scorers -> 0-1 + metadata; **Classifiers** -> `{name, id, label}` | Online scoring rules, scope = Span / Trace / Group | Code, LLM, autoevals | Human review | Trace-level scorers get full `trace` | GitHub Action |

---

## 1. OpenTelemetry GenAI semantic conventions (state as of Oct 2026)

Sources:
- Repo (moved out of the main semconv repo): https://github.com/open-telemetry/semantic-conventions-genai (Apache-2.0; 668 commits, 143 open issues; `model/` YAML + `docs/gen-ai/`). The old pages on opentelemetry.io/docs/specs/semconv/gen-ai/ now just say "moved".
- Events doc: https://raw.githubusercontent.com/open-telemetry/semantic-conventions-genai/main/docs/gen-ai/gen-ai-events.md
- Agent spans doc: https://raw.githubusercontent.com/open-telemetry/semantic-conventions-genai/main/docs/gen-ai/gen-ai-agent-spans.md
- Secondary analysis (May 2026): https://www.greptime.com/blogs/2026-05-09-opentelemetry-genai-semantic-conventions

**Status**: GenAI and MCP conventions are still **Development** (not Stable) as of May 2026 per the secondary source and per the per-attribute "Development" status in the spec docs; no published stabilisation date. Spec docs site was at semconv v1.41.0 (v1.41.1 latest GitHub release, no GenAI changes). Timeline per secondary source (not verified against official changelog): v1.36 baseline; v1.37 replaced per-message events with `gen_ai.input.messages` / `gen_ai.output.messages` / `gen_ai.system_instructions` attributes and renamed `gen_ai.system` -> `gen_ai.provider.name`; v1.38 added the evaluation event; v1.39 MCP; v1.40 retrieval spans + cache tokens; v1.41 reasoning tokens, `invoke_workflow`, `execute_tool {tool}` naming, `invoke_agent` split into CLIENT (remote agents) vs INTERNAL (in-process).

**Opt-in switches**: `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental`; `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true` (content capture off by default).

**Spans** (verified from the agent-spans doc):
- Inference: `gen_ai.operation.name` in {`chat`, `text_completion`, `generate_content`, `embeddings`, `retrieval`...}, name `chat {model}`, kind CLIENT.
- `create_agent {gen_ai.agent.name}` (CLIENT): `gen_ai.agent.id/name/description/version`, `gen_ai.request.model`, opt-in `gen_ai.system_instructions`.
- `invoke_agent {gen_ai.agent.name}`: CLIENT (remote) or INTERNAL (in-process). Cond-required: `gen_ai.conversation.id`, `gen_ai.data_source.id`, `gen_ai.agent.*`, `gen_ai.output.type`, `error.type`. Recommended: request settings, finish reasons, `gen_ai.usage.*`. Opt-in: `gen_ai.input.messages`, `gen_ai.output.messages`, `gen_ai.system_instructions`, `gen_ai.tool.definitions`. Internal variant has `gen_ai.main_agent` entity association.
- `invoke_workflow {gen_ai.workflow.name}` (INTERNAL): graph/orchestrator across agents.
- `execute_tool {gen_ai.tool.name}` (INTERNAL): `gen_ai.tool.name`, `gen_ai.tool.call.arguments`, `gen_ai.tool.call.result` (opt-in/privacy-gated). (I could not read the execute_tool section; the page was truncated - attribute list from secondary source.)
- MCP: `tools/call {tool}`, `mcp.method.name`, `mcp.session.id`, etc.

**Usage / models**: `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.response.id`, `gen_ai.response.finish_reasons`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.usage.cache_read.input_tokens`, `gen_ai.usage.cache_creation.input_tokens`, `gen_ai.usage.reasoning.output_tokens`.

**Metrics**: `gen_ai.client.operation.duration`, `gen_ai.client.token.usage`, MCP duration/session metrics.

**Evaluation event `gen_ai.evaluation.result`** (Development, Recommended; verified): attributes `gen_ai.evaluation.name` (Required), `gen_ai.evaluation.score.value` (double, cond.), `gen_ai.evaluation.score.label` (string, low cardinality, cond.), `gen_ai.evaluation.explanation` (Recommended), `gen_ai.response.id`, `error.type`. Should be parented to the GenAI operation span being evaluated (or reference via `gen_ai.response.id`). No evaluator-kind (human/llm/code), evaluator version, or target-span-type field - these are gaps to fill with a vendor namespace.

**Practical implication**: Phoenix auto-converts `gen_ai.*` -> OpenInference at ingest (v15.10+); OpenSearch's collector config enables the upstream "GenAI normalizer" that maps OpenInference and OpenLLMetry -> GenAI semconv (see section 3). So the ecosystem is converging on `gen_ai.*` as the wire format, with normalisers at the edge.

---

## 2. Per-project notes

### Arize Phoenix (+ OpenInference)
- Repo https://github.com/Arize-ai/phoenix, ~11.9k stars. License **Elastic License 2.0** (https://github.com/Arize-ai/phoenix/blob/main/LICENSE): no hosted/managed-service offering, no circumventing license-key features. Not OSI open source - matters if we embed or fork.
- Storage: SQLite default, PostgreSQL optional with `PHOENIX_SQL_DATABASE_READ_REPLICA_URL` (14.0.0+); retention via `PHOENIX_DEFAULT_RETENTION_POLICY_DAYS` (default infinite). No ClickHouse/OpenSearch. Postgres-only = ceiling on volume. https://arize.com/docs/phoenix/self-hosting/configuration
- OpenInference spec (Apache-2.0, https://github.com/Arize-ai/openinference): `openinference.span.kind` in {LLM, EMBEDDING, CHAIN, RETRIEVER, RERANKER, TOOL, AGENT, GUARDRAIL, EVALUATOR, PROMPT, DECISION}; `llm.input_messages`, `llm.output_messages`, `llm.token_count.*` (+ cache details), `llm.cost.*`, `tool.*`, `tool_call.*`, `retrieval.documents`, `session.id`, `user.id`, `metadata`, `input.value`/`output.value`, `graph.node.*`, `agent.name`. Notably has **cost attributes** and **EVALUATOR/GUARDRAIL span kinds**, which GenAI semconv lacks. Large instrumentation matrix (Py/JS/Java/Go). Span processors that normalise OpenLIT/OpenLLMetry. https://github.com/Arize-ai/openinference/blob/main/spec/semantic_conventions.md
- Evals: `arize-phoenix-evals`; LLM evaluators use **forced tool calling** to return label + explanation, label mapped to numeric score; all evals return explanations; executors with rate-limit retries and adaptive concurrency; **evaluator runs are themselves traced** into a dedicated project. https://arize.com/docs/phoenix/evaluation/evals
- 2026 release notes (https://arize.com/docs/phoenix/release-notes): dataset-attached server-side evaluators (13.x), **code evaluators in sandboxes** (16.0+; E2B/Daytona/Vercel/Modal/Docker), Tool Selection / Tool Invocation evaluators, User Friction `friction/no_friction` classifier, conversation-grounded hallucination, Sessions API / Session Turns API, session notes + per-turn feedback. I found **no release describing continuous evals on live traces in OSS** (online evals appear to be Arize AX).
- Annotations: configs (categorical/continuous/freeform, optimisation direction maximize/minimize/none), built-in `user_feedback`, annotator kind HUMAN/LLM/CODE, shown by author to expose disagreements; filter traces by annotation -> export to dataset. Annotation queues not confirmed in OSS (AX has them). https://arize.com/docs/phoenix/tracing/how-to-tracing/feedback-and-annotations/annotating-in-the-ui
- Strengths: cleanest annotation/score model with annotator kind; OpenInference richness; evaluator tracing; dataset/experiment loop. Weaknesses: ELv2; Postgres ceiling; no online evals in OSS; no sampling/triage for huge volume.

### Comet Opik
- https://github.com/comet-ml/opik, ~22.8k stars, **Apache-2.0**.
- Architecture (https://www.comet.com/docs/opik/self-host/architecture): Java backend (Dropwizard) + separate **Python evaluator service** (Flask, RQ, sandboxed subprocesses with timeouts/memory/network isolation) + React UI. **ClickHouse** (traces, spans, experiments, feedback scores, dataset items), **MySQL** (projects, datasets, prompts), **Redis** (cache, locks, rate limiting, **Redis Streams with consumer groups for online evaluation**, stalled-message auto-claim), MinIO. ClickHouse via Altinity operator + ZooKeeper.
- OTel: OTLP/HTTP only at `/api/v1/private/otel` (gRPC "causes errors"); `opik.tags`, `opik.metadata.*`, `thread_id` attrs; mapping of `gen_ai.*` not documented on the page. https://www.comet.com/docs/opik/tracing/opentelemetry/overview
- Evaluator abstraction: `BaseMetric` with `score(...)` (+ optional `ascore`), params can be dataset fields / task output / `task_span` (full trace for trajectory); returns `ScoreResult(value, name, reason, metadata)` or a list. https://www.comet.com/docs/opik/evaluation/metrics/custom_metric
- Online rules (https://www.comet.com/docs/opik/production/rules): name, **sampling rate**, judge model, prompt with `{{variable}}` mapping, one or more score definitions. Trace / thread / span scopes. **Agent-as-Judge** reads the trace via tools (`read`, `jq`, `search`) with **max cost per evaluation (USD)**. **Thread rules** wait for a cooldown (default 15 min) after last activity. Python user metrics for thread rules receive a `Conversation`. New rules apply only to new data; backfill via manual run. Scores stored as ordinary feedback scores; threshold alerts to Slack/PagerDuty/webhook. A classifier ("Jev", via OpenRouter) is an option for pass/fail; prob >= 0.5 stored as 1.
- Metric library: heuristic (BLEU/ROUGE/Levenshtein/Regex/IsJson/Sentiment/Readability...), LLM (Hallucination, Moderation, G-Eval, ...), agent (Agent Task Completion Judge, Agent Tool Correctness Judge, Trajectory Accuracy), conversation (Coherence, User Frustration, Session Completeness). https://www.comet.com/docs/opik/evaluation/metrics/overview
- Annotation queues (https://www.comet.com/docs/opik/evaluation/advanced/annotation_queues): scope = traces or threads, reviewer instructions, feedback definitions, shareable link, **automation: queue auto-fills from feedback-score conditions (up to 5 ANDed), cap on items**, skips playground/experiment/evaluator traces. No explicit reviewer assignment.
- Strengths: closest to our target architecture (ClickHouse events + relational config + Redis streams for online eval); queue automation driven by scores; agent-as-judge with cost cap; thread-level scoring with settle time. Weaknesses: heavy stack (Java + Python + CH + MySQL + ZK + Redis + MinIO); OTLP/HTTP only; self-host lacks user management (per docs); no classifier backend of its own.

### Traceloop OpenLLMetry
- https://github.com/traceloop/openllmetry, ~7.5k stars, **Apache-2.0**. Pure instrumentation (OpenAI, Anthropic, Bedrock, vector DBs, LangChain/LlamaIndex/CrewAI, MCP) exporting to any OTel backend (~two dozen listed). README says its semconv "are now part of OpenTelemetry". Telemetry in SDK removed as of v0.49.2.
- Traceloop **joined ServiceNow in March 2026** (est. $60-80M; secondary sources); committed to keep OpenLLMetry open source. https://traceloop.com/blog/traceloop-is-joining-servicenow
- No eval layer in the repo. Uses `traceloop.*`/`gen_ai.*`/`llm.*` legacy attrs - normalisers exist (OpenInference span processor; OpenSearch collector GenAI normalizer).
- Take: instrumentation source, not a competitor; ensure ingest normalises its attributes.

### MLflow GenAI (evaluation + tracing)
- https://github.com/mlflow/mlflow, ~28.5k stars, **Apache-2.0**.
- Tracing: OTLP `/v1/traces` endpoint; "ingests and exports traces in the OpenTelemetry GenAI Semantic Convention format"; SQL backend (Postgres/MySQL recommended for scale); `MLFLOW_TRACE_SAMPLING_RATIO` (per-trace head sampling), async logging with bounded queue (**queue full -> traces dropped**); slim `mlflow-tracing` SDK. https://mlflow.org/docs/latest/genai/tracing/prod-tracing/ , https://mlflow.org/docs/latest/genai/tracing/opentelemetry/
- Evaluation: `mlflow.genai.evaluate(data, predict_fn, scorers)`. Three parts: dataset, scorer, predict function. Judges (built-in `Correctness`, `RetrievalGroundedness`, guidelines judges, `make_judge` custom) and code scorers are both "scorers"; return pass/fail, numeric, or categorical `Feedback` attached to trace as **assessment** with source + rationale. Judge model by `<provider>:/<model>`. **Judge alignment with human feedback (DSPy-based) and judge versioning.** https://mlflow.org/docs/latest/genai/eval-monitor/scorers/
- Automatic/online eval (https://mlflow.org/docs/latest/genai/eval-monitor/automatic-evaluations/): scope Traces or **Sessions**; sample rate 0-100%; filter in trace-search syntax; trace eligible after 5 min (configurable); session complete after 5 min idle, re-evaluated and replaced on new traces; runs async inside MLflow server via AI Gateway; **only LLM judges (no code scorers)**; only traces <1 h old when judge created; **no automatic retries**; config changes don't re-evaluate.
- Strengths: assessments-on-trace model with source typing; judge alignment loop; session-level online scoring; GenAI semconv both directions; wide adoption (also wraps DeepEval/RAGAS/Phoenix/TruLens/Guardrails scorers per scorer docs **[UNVERIFIED: page didn't confirm]**). Weaknesses: SQL store, not built for extreme event volume; online evals LLM-only, run inside server, no retry.

### DeepEval (Confident AI)
- https://github.com/confident-ai/deepeval, ~19k stars, **Apache-2.0**.
- Abstraction: `BaseMetric` (single-turn, `LLMTestCase`) / `BaseConversationalMetric` (`ConversationalTestCase`); `measure()`/`a_measure()` set `self.score`, `self.success`, `self.reason`, `self.error`; `threshold` default 0.5; `is_successful()`. https://deepeval.com/docs/metrics-custom
- Metric families (https://deepeval.com/docs/metrics-introduction): custom (G-Eval, DAG deterministic graph, Arena G-Eval, JevEval); agents - trajectory (Task Completion, Step Efficiency, Plan Adherence, Plan Quality) and component (Tool Correctness, Argument Correctness); multi-turn (Knowledge Retention, Role Adherence, Conversation Completeness, Turn Relevancy/Faithfulness); RAG; safety; MCP; voice agent; multimodal.
- Tracing via `@observe` enables component-level evals and trajectory metrics (`evals_iterator()`).
- **JevEval / `system_one` eval mode** (https://deepeval.com/docs/metrics-jev-eval , https://deepeval.com/docs/evaluation-eval-modes): scores via a non-generative "System One" model (Jev, by TypeSafe AI) answering typed questions with calibrated probabilities - `Noul` (yes/no probability), `Score` (ordered levels -> expected position normalised 0-1), `Choice` (options with credits 0-1). Weighted mean across questions; reason built **in code** from answers+confidence (deterministic). Modes: `llm` (default), `hybrid`, `system_one`.
- CI: `deepeval test run` pytest-native, caching, parallelism. Production monitoring/clustering of failing traces/annotation routing is in commercial Confident AI.
- Strengths: richest agent/multi-turn metric catalogue; pytest ergonomics; typed classifier mode. Weaknesses: online monitoring paywalled; no store of its own; metric-class API mutates state on the metric instance (not thread-safe by design).

### Ragas
- https://github.com/explodinggradients/ragas, ~16.1k stars, **Apache-2.0**.
- `SingleTurnSample(user_input, retrieved_contexts, response, reference, rubric)`, `MultiTurnSample(user_input=[HumanMessage|AIMessage(tool_calls)|ToolMessage], reference)`. https://docs.ragas.io/en/stable/concepts/components/eval_sample/
- Metrics: RAG family; Agents/tool use: **Topic Adherence, Tool Call Accuracy, Tool Call F1, Agent Goal Accuracy**; NLP (BLEU/ROUGE/chrF/exact/string); SQL (execution-based Datacompy, query equivalence); general (AspectCritic, Simple Criteria, Rubrics). https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/
- Strengths: well-defined multi-turn message schema with tool calls; tool-call F1 against reference; synthetic test generation. Weaknesses: library only; no storage/online/annotation; metric API details (SingleTurnMetric, etc.) not verified by me.

### promptfoo
- https://github.com/promptfoo/promptfoo, ~26.2k stars, **MIT**; README states "Promptfoo is now part of OpenAI" and remains MIT/open source.
- Assertion types (https://www.promptfoo.dev/docs/configuration/expected-outputs/): deterministic (contains, regex, is-json...), `javascript`/`python` returning bool|float|GradingResult, `llm-rubric`, `g-eval`, model-graded family (factuality, closedqa, answer-relevance, context-*), **`classifier`** (model-assisted, HF-style), `similar` (embedding cosine), `tool-call-f1`, `is-valid-openai-tools-call`, **`trajectory:tool-used | tool-args-match | tool-sequence | step-count | goal-success(LLM)`**. Any type negatable with `not-`.
- Result structure: pass + score + reason; `weight` (default 1, 0 = informational), `threshold`, `metric` name for aggregation in UI; test passes if weighted score >= threshold or all non-zero-weight assertions pass.
- Strengths: declarative YAML, cheap deterministic-first assertions, weights/named metrics, CI/PR scanning, red teaming, runs locally. Weaknesses: offline only; no production store; trajectory depends on traces provided to it.

### Inspect AI (UK AISI)
- https://github.com/UKGovernmentBEIS/inspect_ai, ~3k stars, **MIT**; 200+ prebuilt evals (inspect_evals).
- Concepts: Task = dataset + solver + scorer. Scorer: `@scorer(metrics=[accuracy(), stderr()])` factory returning `async def score(state: TaskState, target: Target) -> Score`; `Score(value, answer, explanation, reason, metadata)`; `Value` = str|int|float|bool or sequences/mappings thereof (multi-dimensional scores); constants `C/I/P/N`. Multiple scorers, reducers across epochs, deferred scoring and `inspect score` re-scoring of logs. https://inspect.aisi.org.uk/custom-scorers.html , https://inspect.aisi.org.uk/scorers.html
- Logs (https://inspect.aisi.org.uk/eval-logs.html): `.eval` binary default (incremental sample access; message dedup + zstd ~10:1 on agentic benchmarks), JSON alt; S3/Azure/HF bucket/GCS-like filesystems; sample summaries readable without loading samples; per-sample event transcript (`ModelEvent`, `ToolEvent`, `ScoreEditEvent`); `edit_score()` with provenance history; attachments dedup large content.
- **Inspect Scout** (https://meridianlabs-ai.github.io/inspect_scout/): post-hoc transcript scanners (LLM scanner, grep scanner, custom), validation against human-labelled sets, resume, parallel batching; reads Inspect logs, Phoenix, LangSmith, Logfire, MLflow, W&B Weave, Claude Code; results as Parquet. License not stated on the page.
- Strengths: best score `Value` typing and score-edit provenance; log format engineering (dedup, summaries, streaming reads); scanner-over-transcripts pattern; sandboxed agent evals. Weaknesses: batch/offline only; no OTel; no live monitoring.

### OpenAI Evals
- https://github.com/openai/evals, ~19.6k stars, MIT (per contribution note; LICENSE text not displayed). Registry of YAML-defined evals + JSONL data in Git-LFS; basic and model-graded templates; Completion Function protocol for chains/agents; "not accepting evals with custom code".
- Hosted Evals API (https://developers.openai.com/api/docs/guides/evals): `data_source_config` + `testing_criteria` (graders such as `string_check`), runs with `per_testing_criteria_results`, webhooks. **Page states the hosted Evals platform is being deprecated: read-only 2026-10-31, shut down 2026-11-30**, pointing to Datasets. Treat as a legacy design reference only.

### TruLens
- https://github.com/truera/trulens, ~3.6k stars, **MIT**; created by TruEra, now maintained by Snowflake. **OTel-native** (spans exportable to OTLP backends). https://www.trulens.org/
- Abstraction: `Metric` with `Selector`s picking record fields from span attributes; rubrics, few-shot examples, custom score ranges; each judged step has an explanation. Modes: live (online) as traces are recorded, or batch over datasets.
- Metrics: agentic (tool selection, plan adherence, "Agent GPA"), RAG triad, safety. Vendor benchmarks (267/281 human-annotated agent errors on TRAIL/GAIA; groundedness F1 0.81 on LLM-AggreFact) are **self-reported**.
- Weaknesses: small community; storage SQLAlchemy-based (SQLite/Postgres/Snowflake) - not scale oriented.

### Agenta
- https://github.com/Agenta-AI/agenta, ~4.8k stars, **MIT** (an `ee/` dir exists; its license **[UNVERIFIED]**).
- Stack (third-party Railway template, not official docs): FastAPI core, Next.js UI, workers, **Postgres with 3 DBs (`agenta_oss_core`, `agenta_oss_tracing`, `agenta_oss_supertokens`)**, Redis (cache/queues/streams), S3-compatible store, SuperTokens. So tracing is Postgres-based **[inference]**.
- Docs list OTel + OpenInference + OpenLLMetry compatibility, annotation, automated eval of live traffic, human evaluation. **Annotation queues** hold traces or test cases and can be exported as labelled test sets (changelog https://agenta.ai/docs/changelog/annotation-queues). Annotations API attaches evaluator output to span by trace/span IDs (preview).
- Strength: prompt-management + eval + annotate-in-one for product teams; "save any production span as a test case". Weakness: Postgres tracing store; limited agent-specific evals **[UNVERIFIED]**.

### Laminar
- https://github.com/lmnr-ai/lmnr, ~3.4k stars, **Apache-2.0**, written in Rust. Stack from official docs via search: Postgres (projects/API keys), **ClickHouse** (spans/analytics), **Quickwit** (full-text search over span content), RabbitMQ/Redis in the full/Helm profile; ingests OTel over gRPC/HTTP. https://laminar.sh/docs/self-hosting
- Evals (https://laminar.sh/docs/evaluations/introduction): datapoint `{data, target?, metadata?}`; executor; evaluators return number or map of numbers; **each datapoint is itself a trace** with `EVALUATION` root, `EXECUTOR` child, `EVALUATOR` children; `group name` to chart runs over time; SQL over traces/spans/metrics/events (also MCP/CLI); custom SQL dashboards.
- Online evals: "span path -> span label -> evaluator" (one evaluator per label; labels boolean or categorical; code or LLM-judge). **Labeling queues** FIFO (2024 launch post). **Signals** (plain-English behaviours to track with Slack alerts) are **gated behind enterprise licence** in OSS.
- Strengths: ClickHouse+Quickwit split (analytics vs text search) is notable; SQL-first access for agents; evals-as-traces. Weaknesses: smaller; signals paywalled; online eval granularity is span-label oriented.

### Helicone
- https://github.com/Helicone/helicone, ~6.2k stars, **Apache-2.0**. Stack: Next.js, Cloudflare Workers proxy, Jawn (Express/Tsoa) log collector, Supabase (app DB), **ClickHouse** (analytics), MinIO. Gateway/proxy-first (100+ models), sessions.
- **Mintlify acquired Helicone (announced 2026-03-03); product in maintenance mode** (security/bugs/new models), no new features. https://helicone.ai/blog/joining-mintlify , https://mintlify.com/blog/mintlify-acquires-helicone
- Eval/scoring: not described on README; treat as not an eval framework. Useful only as a proxy-logging reference.

### Evidently
- https://github.com/evidentlyai/evidently, ~8k stars, **Apache-2.0**. Reports, Test Suites (pass/fail conditions for CI), monitoring dashboard, LLM-judge and text **descriptors** (sentiment, text length, word presence). OSS tracing service stores in filesystem/SQLite/Postgres/S3 (fsspec); OTel-based `Tracely` for instrumentation. https://www.evidentlyai.com/blog/open-source-llm-tracing
- Strength: the "descriptor" idea (cheap row-level derived columns) and drift/stat tests on text; Test Suites for CI. Weakness: not agent-aware; lightweight storage.

### Giskard (v3)
- https://github.com/Giskard-AI/giskard-oss, ~5.9k stars, **Apache-2.0**; v3 is a rewrite for **multi-turn agent testing**, Python 3.12+; v2 unmaintained (except legacy tabular scan). Packages `giskard-checks` (Scenario, Check, Suite; built-in string/regex/semantic-similarity checks and judges `Groundedness`, `Conformity`, `LLMJudge`; default judge `openai/gpt-4o-mini`), `giskard-scan` (`vulnerability_scan`, `quality_scan` replaces RAGET).
- Strengths: scenario = scripted interactions + checks over the whole trace; adversarial scenario generation from an agent description. Weaknesses: no persistence, no online, no OTel. Telemetry (aggregated analytics) on by default, opt-out via env.

### autoevals (Braintrust)
- https://github.com/braintrustdata/autoevals, ~1.1k stars, **MIT**, Python + TS.
- `Score(name, score in [0,1] | None, metadata)`; scorers take `output`, `expected`, `input`. LLM scorers: Factuality, ClosedQA, Battle, Moderation, Security, Summarization, SQL, Translation, Humor; RAG metrics (context precision/recall, faithfulness...); heuristics (Levenshtein, ExactMatch, NumericDiff, JSONDiff, ValidJSON); embedding similarity.
- **`LLMClassifier` / `LLMClassifierFromTemplate`: prompt template with `{{input}}/{{output}}/{{expected}}` + `choice_scores` map (label -> score), optional chain-of-thought.** This is the cleanest published pattern for "classifier-as-scorer" - small models can slot in by replacing the LLM call.

### LangSmith (closed, concepts only)
- Evaluator types: human review, code rules, LLM-as-judge, pairwise; **online evaluators on runs and on threads (multi-turn)** with filters and sampling to control cost. https://docs.langchain.com/langsmith/evaluation
- Self-hosted (Enterprise add-on): **ClickHouse** (traces + feedback), Postgres (everything transactional), Redis/Valkey queue, optional blob storage, optional **SmithDB** columnar trace store (0.17+); async queue ingest with retries. https://docs.langchain.com/langsmith/self-hosted
- LangChain blog tested Jev as a custom judge via `langchain-typesafe` on 5 runs: 0.44 s and $0.00035 per call vs $28.17 total for a Claude judge, lowest variance (vendor-adjacent, tiny sample): https://langchain.com/blog/jev-agent-evals-langsmith
- Feedback shape `{key, score|value, comment}`, annotation queues, `agentevals` trajectory matching, pytest/vitest plugins: **[from memory, not verified this session]**.

### Braintrust (closed, concepts only)
- Eval = data + task + scorers; five-stage loop (playground -> immutable experiment -> CI per PR -> online scoring -> production traces back into datasets). https://www.braintrust.dev/docs/guides/evals
- **Scorers return 0-1 (+ name, metadata); Classifiers return categorical `{name, id, label, metadata}`**; inputs `input, output, expected, metadata, trace`; online rules scope = **Span / Trace / Group**; trace-scorers run once on completed trace. https://www.braintrust.dev/docs/guides/functions/scorers
- **Brainstore** (https://www.braintrust.dev/docs/platform/architecture , https://www.braintrust.dev/blog/brainstore): hybrid control/data plane; object storage (S3/GCS/Azure) is source of truth; write-ahead log on object storage -> time-ordered segments -> compacted **Tantivy** indexes; stateless writer / reader / fast-reader node types; Postgres holds metadata + pointers + aggregates; Redis for coordination and txn ids. Vendor claims: 80x faster than an unnamed warehouse; full-text "out of memory" 240 ms vs 78,963 ms on 3.9M x 25 KB docs; warehouse wins on write flush. Their stated reasons against warehouses: poor targeted text search, JSON shredding breaks with many fields, ops complexity. All vendor numbers - unverified.

---

## 3. Projects using OpenSearch / Elasticsearch as backend

| Project | What it does with it | Evals? | Sources |
|---|---|---|---|
| **OpenSearch Agent Traces + Observability Stack** (Apache-2.0, repo `opensearch-project/observability-stack`, 43 stars, **alpha**) | OTel Collector -> Data Prepper (`otel_trace_raw`) -> OpenSearch indices `otel-v1-apm-span-*`; Collector enables the upstream GenAI normalizer (maps OpenInference + OpenLLMetry to GenAI semconv); Dashboards "Agent Traces" plugin (tree, DAG, timeline, token cost, PPL queries, Spans tab) introduced in **OpenSearch 3.6**; alerting + anomaly detection on trace data; Python/TS GenAI SDK (`opensearch_genai_sdk_py` - package naming inconsistent across docs); Amazon OpenSearch Service categorises spans as agent/LLM/tool/embeddings/retrieval | No score/eval store described. A docker-compose file named `agent-eval-llm` exists but is not documented on the page I read | https://docs.opensearch.org/latest/observing-your-data/agent-traces/index/ ; https://observability.opensearch.org/ ; https://github.com/opensearch-project/observability-stack ; https://dev.to/jon_handler_9bb3e6b4a2fd0/read-your-ai-agents-mind-opensearch-observability-for-llm-traces-2ll ; https://aws.amazon.com/blogs/big-data/unified-observability-in-amazon-opensearch-service-metrics-traces-and-ai-agent-debugging-in-a-single-interface/ |
| **Strands Evals SDK** `OpenSearchProvider` | Reads traces (`otel-v1-apm-span-*`, basic or SigV4 auth) via `get_evaluation_data(session_id)` -> `TaskOutput(output, trajectory: Session)`; evaluators: Trajectory, Helpfulness, Output (rubric), Coherence; `Experiment.run_evaluations_async` | Yes (offline, session-based pull model) | https://strandsagents.com/docs/user-guide/evals-sdk/how-to/trace_providers/index.md |
| **Elastic Observability + EDOT** | LLM observability via Elastic Distributions of OpenTelemetry (Java/Node/Python; OpenAI in all, Bedrock/Vertex in Python); docs say tech preview (a 9.0 blog says GA - inconsistent) | No eval framework | https://www.elastic.co/docs/solutions/observability/get-started/opentelemetry/use-cases/llms |
| LiteLLM | Can log request/response/cost to Elasticsearch via OTel | No | https://docs.litellm.ai/docs/tutorials/observability_evaluation |
| Future AGI | Commercial Elasticsearch integration scoring trace I/O with groundedness etc.; results land back on trace as attributes (not clearly OSS) | Yes (commercial) | https://futureagi.com/integrations/elasticsearch/evaluate |

**Finding: I found no open-source eval framework that uses OpenSearch/Elasticsearch as its primary event+score store.** OpenSearch has the trace/observability half (and a pull-style Strands provider); nobody ships the eval/score/annotation/queue half on it.

---

## 4. Sentry and error-tracker angle
- Sentry has first-party **AI Agent Monitoring** (Explore -> Agents: Conversations, Traces, LLM Calls; dashboards for model cost, tokens, tool calls, errors). It relies on spans following OTel GenAI conventions (`gen_ai.invoke_agent` op, `gen_ai.agent.name`), supports `setConversationId()` for multi-turn linking; content capture is opt-in; since JS SDK 10.61.0 gen_ai spans go as **standalone envelope items** by default (self-hosted Sentry should disable). Sources: https://docs.sentry.io/ai/monitoring/agents/ , https://docs.sentry.io/platforms/javascript/guides/connect/ai-agent-monitoring/ (via search snippet), https://blog.sentry.io/ai-agent-observability-developers-guide-to-agent-monitoring
- Mastra and Vercel AI SDK have Sentry exporters/integrations (https://mastra.ai/docs/observability/tracing/exporters/sentry , https://ai-sdk.dev/providers/observability/sentry).
- I found **no** documentation for sending evaluation scores to Sentry, nor for any eval framework creating Sentry issues from failed/low-scoring evals, and no OTLP-ingest verification for Sentry in these sources. **[UNVERIFIED: Sentry OTLP endpoint status]** - needs a dedicated check.
- None of the surveyed eval projects has a Sentry integration (Opik alerts go to Slack/PagerDuty/webhook; Laminar Signals to Slack/email).

---

## 5. Cross-cutting observations

### What to borrow
1. **Opik** - online-eval architecture: Redis Streams consumer groups for scoring, Python sandboxed evaluator service, rules = sampling rate + variable mapping + multiple score definitions, agent-as-judge with per-eval USD cap, thread rules with idle cooldown, **annotation queue automation fed by score conditions**, alert thresholds on averaged scores.
2. **Jev / DeepEval `Noul/Score/Choice` typed questions + Braintrust/autoevals `choice_scores` + Braintrust "classifiers"** - typed, closed-set evaluator outputs with probabilities; reason generated by code; "always add an `other/unclear` option" (Langfuse Jev docs: https://langfuse.com/docs/evaluation/evaluation-methods/jev-as-a-judge; 32k-token input cap; hosted via TypeSafe/Vercel AI Gateway/OpenRouter; open-weights status **unverified**; vendor claims 40-400x cheaper, 20-200x faster **unverified independently**).
3. **Phoenix** - score = `(name, score, label, explanation, annotator kind HUMAN|LLM|CODE, optimisation direction)`; annotation configs; evaluator runs traced; forced-tool-call structured judge output; conversation-grounded and tool-selection evaluators.
4. **OTel `gen_ai.evaluation.result`** as wire format for scores (+ vendor extension attributes).
5. **MLflow** - assessments on traces with source typing; session-level online scoring with settle time and re-evaluation; judge alignment/versioning.
6. **Inspect AI** - `Value` typing (scalar/list/dict), declared metrics per scorer, `edit_score` provenance, log summaries without sample bodies, message dedup; **Inspect Scout** scanners with validation set and resume; reducers over epochs.
7. **promptfoo** - deterministic-first assertion vocabulary (trajectory:tool-sequence, tool-call-f1), weights/thresholds/named metrics, `not-` negation, YAML.
8. **Braintrust Brainstore** - object-storage-as-truth, WAL + compaction, scopes Span/Trace/Group for scorers (design idea even if we use OpenSearch).
9. **Laminar** - evals-as-traces (each eval datapoint is a trace with EVALUATOR spans), SQL over traces for agents, ClickHouse + Quickwit split.
10. **Ragas / DeepEval** - message schema with tool calls; Tool Call F1; Plan Adherence; Step Efficiency; Topic Adherence.
11. **OpenSearch Observability Stack** - collector-edge GenAI normalizer (OpenInference/OpenLLMetry -> gen_ai.*), `otel-v1-apm-span-*` index convention, PPL.

### Gaps nobody fills well
1. **Very-high-volume production scale.** Open-source options top out at Postgres (Phoenix, Agenta, MLflow SQL, TruLens) or ClickHouse (Opik, Laminar, Helicone, LangSmith). No project documents sampling/triage strategy for billions of spans: MLflow drops traces when its async queue is full; online eval is always "sample N%" uniformly. Missing: tail-based/priority sampling that over-samples errors, low scores, anomalies; per-evaluator budgets; backpressure; hot/warm/cold tiering. OpenSearch's own stack gives no scale numbers.
2. **Classifier-based online scoring.** Only Jev (hosted, closed weights as far as documented) is wired in as an alternative to LLM judges, and only in Langfuse, DeepEval, Opik (via OpenRouter), LangSmith (custom). No framework offers: self-hosted small-model serving (ONNX/ModernBERT-style classifier), calibration/threshold management, labelled-set validation of classifier vs human, drift monitoring of classifier outputs, or cascading (classifier -> LLM judge on uncertain cases). promptfoo's `classifier` assertion is offline only.
3. **Evaluator cascade/routing**: deterministic checks -> classifier -> sampled LLM judge -> human queue with escalation on disagreement is not a first-class concept anywhere (Opik's queue automation is the closest).
4. **Error-tracker integration.** No eval framework links failed/low-score evaluations to Sentry issues (fingerprinting by failure class, release tagging, linking trace_id <-> Sentry event). Sentry consumes gen_ai spans but has no documented score ingestion.
5. **Online eval correctness**: settle/idle windows exist (MLflow 5 min, Opik 15 min) but late-arriving spans, re-evaluation versioning, idempotency and retries are weak (MLflow: no retries, 1-hour window).
6. **Evaluator version/provenance on scores**: only Inspect (edit provenance) and MLflow (judge versions) track it; OTel evaluation event lacks evaluator kind/version.
7. **Open-source with OpenSearch as the store**: none for evals/annotation.
8. **Licensing risk**: Phoenix is ELv2; Laminar Signals gated; LangSmith/Braintrust closed; Helicone in maintenance; OpenAI hosted Evals sunset - good OSS governance is a differentiator.
9. **Agent evals over huge multi-step traces**: most "trajectory" evaluators require loading the whole trace into an LLM; Opik's agent-as-judge tools (`read`/`jq`/`search`) and Inspect Scout chunking are the only scalable-ish patterns.

### Recommended semantic conventions to adopt
- **Wire format**: OTel GenAI (`gen_ai.*`) per `semantic-conventions-genai` as canonical, with a normaliser at ingest for OpenInference and OpenLLMetry (Phoenix does inverse; OpenSearch collector does this direction). Pin a semconv version and honour `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental`; expect churn (still Development).
- **Operations**: `gen_ai.operation.name` in {chat, text_completion, generate_content, embeddings, retrieval, create_agent, invoke_agent, invoke_workflow, execute_tool}; `gen_ai.agent.{id,name,description,version}`, `gen_ai.workflow.name`, `gen_ai.conversation.id` (our session/thread key; also map OpenInference `session.id` and Opik `thread_id`), `gen_ai.tool.name`, `gen_ai.tool.call.arguments/result`, `gen_ai.tool.definitions`, `gen_ai.provider.name`, `gen_ai.request.model`/`gen_ai.response.model`, `gen_ai.usage.*` (incl. cache and reasoning), `gen_ai.response.finish_reasons`, `error.type`.
- **Content**: `gen_ai.input.messages` / `gen_ai.output.messages` / `gen_ai.system_instructions` as span attributes or the `gen_ai.client.inference.operation.details` event; opt-in capture + redaction hook; keep large payloads out of the hot index (Inspect attachments dedup / Brainstore-like pointer to object store).
- **Scores**: persist as events following `gen_ai.evaluation.result` (`gen_ai.evaluation.name`, `.score.value`, `.score.label`, `.explanation`, `gen_ai.response.id`/parent span ID, `error.type`) **plus** an extension namespace (e.g. `evals.*` - to be defined by us): `evaluator.kind` (deterministic|classifier|llm_judge|human), `evaluator.id/version`, `evaluator.model`, `sample.rate/reason`, `score.direction` (maximize|minimize|none, from Phoenix), `score.confidence/probabilities` (from Jev), `target.scope` (span|trace|session), `annotator.id`.
- **Borrow from OpenInference** (no GenAI equivalent): `GUARDRAIL`/`EVALUATOR` span kinds, `llm.cost.*`, `graph.node.*`, prompt template version attributes (`llm.prompt_template.*`) - carry as vendor attrs.
- **Sentry**: use Sentry's `gen_ai.*` span op conventions, `setConversationId`, and propagate `trace_id`/`sentry-trace` baggage so evaluation-produced issues link back; verify OTLP ingestion path **[UNVERIFIED]**.
- **OpenSearch indexing**: align with `otel-v1-apm-span-*` / Data Prepper conventions so OpenSearch Agent Traces UI and Strands OpenSearchProvider can read our spans.

---

## 6. Open questions / unverified items
- Whether Phoenix OSS has annotation queues or any live-trace online evals (release-notes index shows none).
- Opik: exact `gen_ai.*` mapping on OTLP ingest; docs page did not state it.
- Sentry: OTLP ingest and any score/evaluation API.
- Jev: open weights vs hosted only; independent benchmarks (all numbers vendor-reported).
- Agenta online-eval mechanism and `ee/` licence; Helicone scores API.
- promptfoo storage backend and OTel trace ingestion for trajectory assertions (from memory: local SQLite).
- LangSmith/Braintrust OTel ingest details; LangSmith feedback schema, annotation queues, agentevals (from memory).
- Official OTel changelog confirmation of version-by-version GenAI history (relied on Greptime blog for timeline).
- `execute_tool` span attribute table (doc fetch truncated).
- MLflow third-party scorer integrations (DeepEval/RAGAS/Phoenix/TruLens/Guardrails) - the scorer page did not confirm.
