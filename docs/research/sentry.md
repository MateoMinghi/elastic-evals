# Sentry <-> OpenTelemetry LLM/agent observability + evals: integration research

Date of research: 2026-10-20. Status legend: **[V]** verified in a fetched primary source (URL given); **[I]** inference from verified facts; **[U]** unverified / could not confirm - test before relying on it.

Context: OSS framework, agents instrumented with OTel (GenAI semconv), OTel Collector routes data, OpenSearch event store, Sentry already used for errors, self-hosted Sentry possibly mandatory (bank, data residency).

---

## 0. Executive summary

- The two systems can share **one trace_id with no translation**: OTel trace_id (128-bit, 32 hex) is exactly what Sentry uses as `trace_id`. With the Sentry SDK's **OTLPIntegration** (Python, Node, Java, .NET, Go, Ruby, PHP), Sentry error events/logs are stamped with the *active OTel* trace_id/span_id. So "Sentry issue -> trace id -> our trace UI" works by construction, and "our trace -> Sentry issues" works by searching `trace:<id>` (or an explicit `trace_id` tag) via the Sentry API.
- Sentry's OTLP ingest (`/api/<project>/integration/otlp/v1/traces`) exists and is routed by Relay. Docs still say "open beta" for SaaS, but a Relay PR dated 2026-05-07 says "The endpoints are GA" (Relay 26.5.0 "Enable OTLP endpoints by default"). Self-hosted nginx forwards `/api/<n>/...` to Relay, so the path very likely works on self-hosted >= the release bundling Relay 26.5, but **no official doc says "self-hosted supported" [U]**.
- Big limitation for OTel GenAI: **Sentry drops OTel span events on OTLP ingest**; GenAI semconv puts content/evals in events (`gen_ai.evaluation.result`, inference operation details). Sentry's AI Agents UI reads *attributes* (`gen_ai.input.messages`, etc.). So eval scores will not reach Sentry as span events; send them as attributes/tags/separate Sentry events if wanted at all.
- Recommended pattern: **OpenSearch/our system is the system of record for traces, content and evals. Sentry gets errors (+ trace_id/tags/links), not prompt content and not all spans.** Optionally send *error-only, content-stripped* traces to Sentry via a Collector pipeline. Link both ways with IDs (trace_id, sentry event_id/issue id) and Sentry's External Issue API.

---

## 1. Sentry + OpenTelemetry today

### 1.1 Which SDKs are built on OTel (important correction)
- Historic model (Python SDK 1.x/2.x, JS SDK v8+): `SentrySpanProcessor` + `SentryPropagator` + `instrumenter="otel"` mapped OTel spans to Sentry transactions. The Python page now says this is "Legacy OpenTelemetry Integration Support" and the `OpenTelemetryIntegration` is deprecated in favour of `OTLPIntegration`. [V] https://docs.sentry.io/platforms/python/tracing/instrumentation/opentelemetry/
- **Python SDK 3.x removed** `SentrySpanProcessor`, `SentryPropagator`, the `instrumenter` option, and the `opentelemetry` extra; recommends `OTLPIntegration`. 3.x uses span streaming (each span sent as it finishes), transaction-based tracing removed. So Python 3.x is *not* "built on OTel"; it interoperates via OTLP. [V] https://docs.sentry.io/platforms/python/migration/2.x-to-3.x/
- JS Node SDK: by default "creates and sends spans without an OpenTelemetry pipeline"; two modes - capture spans from OTel APIs, or "use your own OpenTelemetry pipeline" (connect to Sentry). (SDK-internals-on-OTel claims for v8+ not confirmed on that page.) [V partial] https://docs.sentry.io/platforms/javascript/guides/node/opentelemetry/
- Sentry's SDK dev spec (marked "under active development"): "Span/Trace ID's must match between Sentry and OpenTelemetry"; the OTel traceId/spanId are reused. Exception span events become Sentry errors linked to the trace; events carry an `otel` context (attributes+resource). [V] https://develop.sentry.dev/sdk/telemetry/traces/opentelemetry/

### 1.2 OTLPIntegration (the current recommended path)
Python docs: https://docs.sentry.io/platforms/python/integrations/otlp/ [V]
- `pip install "sentry-sdk[opentelemetry-otlp]"`, add `OTLPIntegration()` to `sentry_sdk.init(dsn=...)`.
- Sets up a span exporter whose OTLP endpoint is derived from the DSN; registers a Sentry propagator for compatibility with Sentry-native services; **links Sentry events (errors, logs, metrics) to the active OTel trace/span**; cross-service propagation uses W3C `traceparent` "so no extra setup is needed".
- **Do not also set `traces_sample_rate` / `traces_sampler`** (duplicate spans; sampling is OTel's job).
- Options: `setup_otlp_traces_exporter` (default True; set False to keep your own TracerProvider), `collector_url` (send to *your* Collector instead of the DSN-derived endpoint), `setup_propagator` (default True; auto-setup "will be removed in next major"), `capture_exceptions` (default False; sends exceptions passed to `Span.record_exception` to Sentry, dedupe may drop duplicates).
- Source (sentry-python `integrations/otlp.py`) [V]: reads `get_current_span().get_span_context()`; if trace/span ids are valid, returns hex-formatted ids and registers via `register_external_propagation_context`, so every Sentry event gets the OTel trace_id/span_id. The Sentry OTLP propagator `inject()` writes a **`sentry-trace`** header `{trace_id}-{span_id}-{1|0}` (sampled bit from OTel `trace_flags`), forwards existing baggage only; does not create baggage. https://raw.githubusercontent.com/getsentry/sentry-python/master/sentry_sdk/integrations/otlp.py
- Blog: integration stamps `trace_id`/`span_id` on every Sentry event; error appears inside the OTel waterfall; "logs connect to errors only through shared trace context". https://blog.sentry.io/otel-spans-errors-sentry-trace
- **Pitfall [U]**: the docs say W3C `traceparent` works with no extra setup, but the source shows the Sentry propagator injects `sentry-trace`. OTel has one global propagator; if the Sentry propagator replaces the default composite you may lose `traceparent` on outgoing calls. Safest: `setup_propagator=False` and set `OTEL_PROPAGATORS=tracecontext,baggage` yourself (optionally add sentry-trace via a composite only if you have Sentry-native services).

### 1.3 Trace ID / span ID / header formats
- `sentry-trace`: `traceid-spanid-sampled`; trace id 128-bit (32 hex), span id 64-bit (16 hex); sampled `1`/`0`/absent (defer). [V] https://develop.sentry.dev/sdk/telemetry/traces/distributed-tracing/
- Baggage keys: `sentry-org_id`, `sentry-sample_rand` (reused across services for consistent sampling), `sentry-sample_rate`, `sentry-sampled`, etc. [V same page]
- `propagateTraceparent` (SDK option, default false) makes Sentry SDKs also send W3C `traceparent` `00-<traceId>-<spanId>-<01|00>`, only for targets matching `tracePropagationTargets`. [V same page]. Available in JS (browser, frameworks), mobile SDKs, .NET. [V] https://docs.sentry.io/concepts/otlp/sentry-with-otel/
- W3C `traceparent` and Sentry trace_id are the same 32-hex value, so **a Sentry trace_id == an OTel trace_id** whenever the OTel context is the source (OTLPIntegration) or `traceparent` is propagated. If a Sentry-native (non-OTel) SDK starts the trace and only sends `sentry-trace`, an OTel service with only the tracecontext propagator will start a *new* trace (ids don't join) - hence propagate `traceparent` or add Sentry propagator.
- Strict trace continuation: `sentry-org_id` in baggage vs the SDK's own org id; recommend setting `orgId` on self-hosted. [V same page]

### 1.4 OTLP ingestion endpoint (traces/logs)
Docs: https://docs.sentry.io/concepts/otlp/ (traces and logs only; **no OTLP metrics**; "open beta" wording; feedback address feedback-tracing@sentry.io) [V]
- Endpoints (per project): `https://o<orgId>.ingest.sentry.io/api/<projectId>/integration/otlp/v1/traces` (and `/v1/logs`, and combined base `/integration/otlp`). Auth header `x-sentry-auth: sentry sentry_key=<public key>`. Collector config uses `otlphttp` exporter with `encoding: proto`, `compression: gzip`. [V] https://docs.sentry.io/concepts/otlp/forwarding/pipelines/collector.md and https://docs.sentry.io/concepts/otlp/direct/traces.md
- Limitations (traces): **span events dropped on ingest**; span links ingested and shown in Trace View but not searchable; array attributes shown but not searchable. [V direct/traces.md]. Attributes prefixed `sentry.` are reserved (user got "Invalid key" in search). [V] https://github.com/getsentry/sentry/discussions/85902
- Endpoints are project-scoped: multi-project routing needs the routing connector or the **Sentry exporter** (`sentry` in otelcol-contrib, **alpha**, routes by `service.name`, takes `url` (base URL of your Sentry instance), `org_slug`, `auth_token` from an Internal Integration, optional `auto_create_projects`; honours Sentry rate-limit headers; first batch for auto-created projects may be dropped). [V] https://docs.sentry.io/concepts/otlp/forwarding/pipelines/sentry-exporter.md
- Status: the Sentry docs/changelog (2025-10-22) say open beta; access for traces originally gated by emailing org slug (2025-10 comment). [V] https://sentry.io/changelog/otlp-ingestion-endpoints-for-traces-and-logs-are-in-open-beta. Relay changelog: 25.10.0 added integration endpoints; **26.5.0 "Enable OTLP endpoints by default"**; Relay PR #5951 "Graduate otel endpoints/features ... The endpoints are GA" merged 2026-05-07. [V] https://raw.githubusercontent.com/getsentry/relay/master/CHANGELOG.md , https://github.com/getsentry/relay/pull/5951 . Docs and code disagree on beta vs GA - treat as "GA in code, beta in docs".
- Relay-side OTLP handling worth knowing [V, changelog]: OTLP spans with ERROR status map to `internal_error`; `deployment.environment` resource attribute sets Sentry environment (26.3); resource + scope attributes written as span attributes (25.3); legacy `ai.*` normalised to `gen_ai.*` (25.8); `gen_ai.request.messages` merged into `gen_ai.input.messages` (26.4); `gen_ai.function_id` fallback for `gen_ai.agent.name` (26.4); **client sample rate extracted from TraceState** for extrapolation (26.9, key not named [U]).

---

## 2. Sentry AI / agent monitoring

### 2.1 What it shows
Explore -> Agents with tabs **Conversations** (grouped by `gen_ai.conversation.id`, replay multi-turn), **Traces** (agent runs + surrounding app work), **LLM Calls** (model, tokens, cost, latency); dashboards for executions, model cost, token usage, tool calls, recent errors. [V] https://docs.sentry.io/product/insights/ai/agents/ and https://docs.sentry.io/ai/monitoring/agents/getting-started/
- MCP monitoring: connections, resource access, tool execution, errors; `mcp.*` attribute details not found on the fetched page [U]. https://docs.sentry.io/product/insights/ai/mcp/
- Supported SDK setups listed: JS (Node, Next.js, Cloudflare, Mastra ...), Python, PHP/Laravel, Ruby, .NET; others via manual instrumentation. [V getting-started]

### 2.2 Span contract (so OTel GenAI spans show up)
From the SDK dev spec [V] https://develop.sentry.dev/sdk/telemetry/traces/modules/ai-agents/ :
- Ops: `gen_ai.invoke_agent` (name `invoke_agent {gen_ai.agent.name}`), `gen_ai.{operation}` e.g. `gen_ai.chat` / embeddings / generate_content / text_completion (name `{op} {gen_ai.request.model}`), `gen_ai.execute_tool` (name `execute_tool {gen_ai.tool.name}`).
- MUST attributes: `gen_ai.operation.name`; for client spans also `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.response.model`. Missing required data => span doesn't appear in the Agents dashboard.
- Optional: `gen_ai.input.messages`, `gen_ai.output.messages`, `gen_ai.system_instructions`, `gen_ai.tool.definitions`, `gen_ai.usage.input_tokens/output_tokens/total_tokens/cache_read.input_tokens/reasoning.output_tokens`, `gen_ai.tool.call.arguments/result`, `gen_ai.response.finish_reasons`, etc. Messages as `{role, parts}`; object/array values must be stringified JSON. Legacy `ai.*` and some old gen_ai names deprecated but normalised by Relay.
- Conversations need `gen_ai.conversation.id` (SDK `setConversationId`). [V] https://docs.sentry.io/platforms/javascript/guides/node/tracing/instrumentation/ai-agents-module/
- Note the span *op* must be `gen_ai.*`; for OTLP data Relay infers op from `gen_ai.operation.name` ("Use `gen_ai.operation.name` in `is_ai_span` and `infer_ai_operation_type`", 25.12) [V changelog]. Whether pure-OTel spans (no `sentry.op`) light up Agents Insights on all versions: **[U]**, test.
- Costs: Relay computes `gen_ai` cost from token counts (changelog 25.9/26.1/26.2) [V]; model price table coverage [U].

### 2.3 Limits / gotchas
- Sampling is head-based at the root span: "either capture the full span tree or lose it entirely"; Sentry suggests 100% for gen_ai root spans and AI routes, 20% otherwise via `traces_sampler`. Cost rationale: LLM calls cost more than the trace events. [V] https://docs.sentry.io/ai/monitoring/agents/sampling/
- JS SDK >= 10.61.0 sends gen_ai spans as **standalone envelope items** (avoids transaction payload-size drops; required for Conversations). Docs found via search advise **self-hosted users set `streamGenAiSpans: false`** because standalone gen_ai spans "may not be ingested by their Sentry instance". [Search snippet of https://docs.sentry.io/platforms/javascript/guides/eve/agent-tracing/ - re-verify; V-ish]
- Span events dropped (OTLP) => OTel-style content events not visible; use attributes. [V]
- Four attributes are **not scrubbed by default**: `gen_ai.input.messages`, `gen_ai.tool.call.arguments`, `gen_ai.tool.call.result`, `gen_ai.output.messages` - add Advanced Data Scrubbing rules `$span.data.'<attr>'`. With `sendDefaultPii=false` SDK integrations only collect metadata. [V] https://docs.sentry.io/product/agents/privacy.md
- No truncation limits stated on the spec page [U for real limits].
- No eval/quality-score concept found in Sentry's AI Agents docs (search for `gen_ai.evaluation` returned nothing Sentry-related). Treat as **not supported** [U].

### 2.4 Self-hosted availability of AI features
- Official self-hosted page: "all other features are available" except billing, Spike Protection, Spend Allocation, Seer/AI-ML (closed source), data storage location. Spans/Insights/AI not specifically addressed. [V] https://develop.sentry.dev/self-hosted/
- `sentry.conf.example.py` enables Performance/Tracing/Spans flags incl. `organizations:visibility-explore-view`, `insights-*`, `insights-modules-use-eap`, `span-stats`, `indexed-spans-extraction`, `dynamic-sampling`, `ourlogs-*`; no OTLP/gen_ai/agents/conversations flags. [V] https://raw.githubusercontent.com/getsentry/self-hosted/master/sentry/sentry.conf.example.py
- `organizations:gen-ai-conversations` ("Conversation-focused views in AI Insights") is a Flagpole flag with no default in `sentry/features/temporary.py` -> on self-hosted you likely must enable it manually in `SENTRY_FEATURES` (whether Flagpole flags honour that on self-hosted: **[U]**). [V flag exists] https://raw.githubusercontent.com/getsentry/sentry/master/src/sentry/features/temporary.py
- Conclusion: AI Agents Insights *might* work on self-hosted >= 26.x but is **not documented as supported; budget a PoC [U]**.

---

## 3. Linking mechanisms

### 3.1 Sentry -> our trace (deep link from a Sentry issue/event)
1. **Automatic id**: every event carries `contexts.trace.trace_id` (== OTel trace_id with OTLPIntegration). The Sentry UI shows it and links to Sentry's own trace view. [V blog above]
2. **Explicit tags** (indexed + searchable; key/value <= 200 chars, no newline in value; custom contexts are *not* searchable): `trace_id`, `agent.run_id`, `agent.name`, `conversation_id`, `eval.name`, ... [V] https://docs.sentry.io/platforms/python/enriching-events/tags/ , https://docs.sentry.io/platforms/python/enriching-events/context/
3. **Deep-link URL in context**: `set_context("evals_link", {"trace_url": "https://evals.bank.internal/traces/<id>"})` - visible on issue page, not clickable-guaranteed [U whether URLs render as links; tag docs silent]. Prefer next item for a real link.
4. **External Issue link (clickable "Linked" entry in issue sidebar)**: `POST /api/0/sentry-app-installations/{uuid}/external-issues/` body `{issueId, webUrl, project, identifier}`, token with `event:write`. Result displayed like `project#identifier` linking to `webUrl`. So **our system (or a small bridge) can programmatically attach "trace <id> in <our UI>" to a Sentry issue** (needs an Internal Integration; on self-hosted API is the same `/api/0/` [U, secondhand]). [V] https://docs.sentry.io/api/integration/create-or-update-an-external-issue/ ; endpoint list https://docs.sentry.io/api/integration/
5. **Issue Link UI component** (`issue-link` schema element) gives a manual "Link <Service> Issue" modal: Sentry POSTs to your `uri` and expects `webUrl`, `project`, `identifier`. [V] https://docs.sentry.io/organization/integrations/integration-platform/ui-components/issue-link/ . Whether UI components work for *internal* integrations: not stated [U].
6. **Alert action**: integrations can expose an `alert-rule-action` component and receive `event_alert` webhooks (needs "Alert Action" toggle). [V] https://docs.sentry.io/organization/integrations/integration-platform/ . Good for "on new issue, call our bridge, which stamps the external-issue link".

### 3.2 Our system -> Sentry (from a bad trace / low eval)
- **Store Sentry ids on spans/traces in OpenSearch**: In the SDK `before_send` (or after `capture_exception` returns the event id) set OTel span attribute `sentry.event_id` (+ later `sentry.issue_id` via webhook). Beware: Sentry reserves the `sentry.` prefix for its own attributes in search (user report) - use `sentry_link.event_id` or similar to be safe [V report, [U] for non-Sentry-indexed data]. Caveat that the event dict has `event_id` in `before_send`: believed true in Python [U].
- **Query Sentry by trace id**:
  - `GET /api/0/organizations/{org}/issues/?query=trace:<32hex>&statsPeriod=...&project=-1` - issues list takes a free `query`; `trace` is a documented searchable event property (UUID) and "searching event properties on Issues returns any issue with one or more matching events". Default query `is:unresolved`, so pass `query=trace:<id>` explicitly (it replaces default). limit <=100. Scope `event:read`. [V] https://docs.sentry.io/api/events/list-an-organizations-issues/ , https://docs.sentry.io/concepts/search/searchable-properties/events/ . Exact `trace:` support on the *issues* endpoint not demonstrated in docs [U]; fallback: the explicit `trace_id` tag, or `GET /organizations/{org}/events/?dataset=errors&query=trace:<id>` (endpoint widely used but page 404'd in docs [U]).
  - `GET /api/0/organizations/{org}/trace/{trace_id}/` returns spans + errors (nested `errors`) for a trace, optional `errorId`, scope `org:read`. Depends on spans being in Sentry. [V] https://docs.sentry.io/api/discover/retrieve-a-trace/
  - `GET /api/0/organizations/{org}/issues/{issue_id}/events/?query=...` and `/projects/{org}/{proj}/events/{event_id}/` for details. [V] https://docs.sentry.io/api/events/list-an-issues-events/ , https://docs.sentry.io/api/events/retrieve-an-event-for-a-project/ . No org-wide event-id lookup documented [U].
- **Sentry web UI links from our UI**: build `https://<sentry-host>/organizations/<org>/issues/<id>/` (from API/webhook `web_url`) or trace view URL (exact path for trace view not verified [U]; docs pages 404'd).
- **Webhooks to push Sentry changes into our store** (Integration Platform; HMAC-SHA256 `Sentry-Hook-Signature` with Client Secret; respond within 1 s): resources `issue` (actions created/resolved/assigned/archived/unresolved, with `web_url`, `issueCategory`), `error` (action `created`; payload has tags, contexts, `event_id`, `issue_id`, `issue_url`, `web_url` but **no explicit trace id in the documented example**; **Business/Enterprise plan only on SaaS - presumably N/A on self-hosted [U]**), `event_alert`, `metric_alert`, `comment`. Issue-alert webhook sample includes the full event under `data.event` with tags/contexts. [V] https://docs.sentry.io/organization/integrations/integration-platform/webhooks/ , .../webhooks/errors/ , .../webhooks/issues/ , .../webhooks/issue-alerts/
  - Pragmatic: have the bridge call `GET .../events/{event_id}/` to read `contexts.trace.trace_id` / tags, then write `{trace_id -> sentry issue id, url, status}` into OpenSearch. Or rely on the tag `trace_id` being in the webhook payload's `tags` array (tags are shown) [V for tags presence].

### 3.3 Eval scores / quality issues into Sentry
- **No first-class "eval score" or custom issue type** found. Sentry's Issue Platform is internal; no public ingestion API for third-party issue types found [U - one search, no hits].
- Options that do work with ordinary SDK features [I, standard SDK behaviour]:
  - `capture_message("eval failed: groundedness", level="warning")` with `fingerprint=["eval", eval_name, agent_name]`, tags `eval.name`, `eval.label`, `trace_id`, and context `{score, threshold, evaluator_version}`. Creates an issue grouped by fingerprint (avoid one issue per sample; Sentry quotas still count events). Use only for threshold breaches / regressions, and rate-limit (e.g. aggregated per eval run).
  - To make that event's *own* `contexts.trace` point at the evaluated trace, run the capture with the evaluated span context attached (OTel `NonRecordingSpan(SpanContext(trace_id, span_id, is_remote=True,...))` set as current) - OTLPIntegration will then stamp those ids [I; U untested].
  - **User feedback**: Feedback is an issue category (`issueCategory: feedback`); the SDK User Feedback API can associate feedback with an event; details (event association, self-hosted) were not on the fetched page [U]. https://docs.sentry.io/product/user-feedback/ - good for human thumbs-down tied to trace.
  - **Cron monitors** for scheduled eval jobs (alert if the eval job fails/misses) [I; general Sentry feature, not fetched].
  - Span attributes: you can add `gen_ai.evaluation.*` as *attributes* on the evaluated span (searchable in Sentry's span explorer if you send spans), but note OTel's native vehicle is an **event** which Sentry drops.
- OTel GenAI semconv: `gen_ai.evaluation.result` event (Development stability) - attrs `gen_ai.evaluation.name` (req), `gen_ai.evaluation.score.value`, `gen_ai.evaluation.score.label`, `gen_ai.evaluation.explanation`, `gen_ai.response.id`; SHOULD be parented to the evaluated GenAI span, else correlate by `gen_ai.response.id`. [V] https://raw.githubusercontent.com/open-telemetry/semantic-conventions-genai/main/docs/gen-ai/gen-ai-events.md . GenAI semconv moved to repo `semantic-conventions-genai`, no official release as of Aug 2026 (search snippet) - expect churn.

---

## 4. Sampling consistency

### 4.1 Facts
- Sentry error events are **not** subject to trace sampling; Dynamic Sampling "only applies to spans and transactions, not errors". [V] https://docs.sentry.io/organization/dynamic-sampling/ (availability: "limited to existing customers who already have it enabled"; self-hosted not mentioned; but `organizations:dynamic-sampling` flag is on in the self-hosted example config [V]).
- Sentry distributed tracing = head-based; "either all of a trace's transactions are kept or none"; rate limiting/quota may leave gaps. [V] https://docs.sentry.io/concepts/key-terms/tracing/distributed-tracing/
- OTel `parentbased_*` samplers (default `parentbased_always_on`) honour an upstream sampled flag and bypass the Collector's tail sampler; to keep tail sampling working set `OTEL_TRACES_SAMPLER=always_on` so every span reaches the Collector. [V] https://docs.sentry.io/concepts/otlp/sentry-with-otel/ (said in the context of Sentry frontend + `propagateTraceparent`).
- Collector `tail_sampling` (beta, contrib): groups by trace_id; policies `status_code`, `string_attribute`, `ottl_condition`, `latency`, `probabilistic`, `and`, `composite`, `drop`...; `decision_wait` default 30 s; `num_traces` default 50k (older traces dropped before decision); late spans inherit decision if still buffered, otherwise treated as a new trace unless `decision_cache` is configured; **all spans of a trace must reach the same Collector instance** (two-tier with load-balancing exporter). Optional feature gate `processor.tailsamplingprocessor.usetracestate` rewrites `th`. [V] https://raw.githubusercontent.com/open-telemetry/opentelemetry-collector-contrib/main/processor/tailsamplingprocessor/README.md
- Relay now reads the client sample rate from OTLP TraceState (26.9) to extrapolate counts. [V changelog] Without it Sentry treats OTLP spans as 100% sampled and counts/charts under-report after sampling.

### 4.2 Pitfalls with differing rates
1. **Error exists in Sentry but trace not in OpenSearch** (most important). Sentry captures handled/unhandled exceptions regardless of trace sampling; if the OTel span status stays OK (e.g. `capture_exception` on a handled exception) the tail sampler drops the trace. Fix: (a) on capture set span status ERROR / attribute (e.g. `sentry_link.event_id`) in `before_send`; (b) tail policy `ottl_condition`/`string_attribute` "keep if attribute `sentry_link.event_id` exists" plus `status_code: ERROR`; (c) for evals, keep traces whose eval failed (ensure the eval result gets to the Collector before `decision_wait` expires, or use decision cache / mark at the source).
2. **Trace in OpenSearch but not Sentry**: if only error-traces go to Sentry that's by design; if both get the same stream, Sentry's own quotas/rate limits/dynamic sampling can drop spans independently. Sentry rate limits (429) are handled by the Sentry exporter via queue retries [V]; `otlphttp` exporter would just retry/drop per its config.
3. **Late spans / long agent runs**: agent runs lasting minutes exceed the default 30 s `decision_wait`; the root span arrives last. Use `decision_wait_after_root_received`, larger `decision_wait`, sized `num_traces`, `decision_cache`. Memory cost scales with LLM payload size in attributes - strip/offload content before tail sampling or keep content out of spans (store in OpenSearch via separate path).
4. **Double-sampling math**: if the SDK head-samples at 10% and Collector keeps 100% of errors of the remaining 10%, the "errors" are under-represented; errors only seen in dropped traces are lost from traces (still in Sentry). Prefer `always_on` at SDK + all sampling in Collector (as Sentry docs advise for parent-based) for agent workloads where LLM cost >> telemetry cost (Sentry's own argument for 100% for gen_ai).
5. **Mixed sentry-trace/traceparent**: the sampled flag is carried in both; an upstream Sentry-native service that decided "0" will propagate unsampled, and OTel parent-based will honour it.
6. **Extrapolation mismatch**: Sentry multiplies counts by 1/client_sample_rate; if Collector tail sampling keeps all errors but 5% of OK traces, Sentry's span counts will be wrong unless threshold/rate is written to `tracestate` (alpha gate) [I, U]. Practical advice: send Sentry only error traces and don't use Sentry's span aggregates for volume.
7. **Sentry quota/Dynamic Sampling** on SaaS may drop kept spans; on self-hosted there is no billing/quota, but Relay/EAP ingest capacity matters.

---

## 5. Self-hosted Sentry constraints

- Requirements: min 4 cores, 16 GB RAM + 16 GB swap, 20 GB disk; 32 GB recommended; disk I/O critical; Docker >=19.03.6, Compose >=2.32.2; "no guarantees or dedicated support"; FSL license (Apache 2.0 after 2 years). [V] https://develop.sentry.dev/self-hosted/
- Self-hosted CHANGELOG milestones [V] https://raw.githubusercontent.com/getsentry/self-hosted/master/CHANGELOG.md : spans ingest 24.3.0; EAP containers 25.7.0; spans queries moved to `eap` dataset 25.9.0; old snuba-spans consumer removed 25.10.0; standalone span ingestion flag graduated 26.4.0; process-segments as a task 26.9.0. **No OTLP/gen_ai/AI-agent entries** in the self-hosted changelog (these ride on Relay/sentry versions). Self-hosted release numbering: latest seen 26.7.x (self-hosted), sentry 26.10.0.
- EAP spans on self-hosted: issue #3592 documents the enabling steps for 25.x (flags + extra containers `eap-spans-subscription-results`, `snuba-eap-items-span-consumer`, `snuba-subscription-consumer-eap-spans`); warns spans are **dual-written to two ClickHouse tables** (disk!) and sized 8 core/16 GB+16 GB swap for the author's setup; closed Aug 2025. Self-hosted 26.7.0 notes: Snuba SQL-injection fix needed if EAP array-attribute feature enabled; nginx `proxy_read_timeout` 30s->90s for slow EAP views (spans/metrics/logs). [V issue; V via search snippet for release notes] https://github.com/getsentry/self-hosted/issues/3592
- nginx routes `~ ^/api/[1-9]\d*/` to Relay (client_max_body_size 100m, proxy_read_timeout 90s), so `/api/<project>/integration/otlp/v1/traces` reaches Relay, which has the OTLP handlers since 25.10 and default-on since 26.5. [V] https://raw.githubusercontent.com/getsentry/self-hosted/master/nginx.conf . Needs: self-hosted release whose bundled Relay >= 26.5 [I]; project config feature gating on the Sentry side [U].
- Not available self-hosted: Seer / AI features, Spike Protection, Spend Allocation, data storage location. [V] self-hosted page.
- "errors-only" install profile exists (25.4.0 changelog "Minimum requirements for 'errors-only' profile") - lowest footprint if Sentry is used for errors only. [V changelog]
- Self-hosted `sentry-exporter` in Collector: `url` = base URL of instance (suggests self-hosted OK, not confirmed) [V partial].
- Recommendation for a bank: start with **errors-only Sentry** (cheap, supported) + linking via IDs; treat spans/AI Insights on self-hosted as an optional, resource-heavy PoC.

---

## 6. PII: Sentry scrubbing vs Collector redaction

- Sentry server-side: default scrubbers (credit cards, SSNs, passwords) on most event data; **not** applied to the four gen_ai content attributes by default. Advanced Data Scrubbing rules (Remove/Mask/Hash/Replace; types regex + built-ins incl. email, IP, UUID, PEM, auth-in-URL, US SSN...; sources like `$error.value`, `extra.**`, `tags.x`, `$span` alias, `$span.data.'attr'`) apply just before storing. Known limits: non-string values removed/nulled, false positives/negatives, `**` only covers default PII fields, no performance or self-hosted statements. [V] https://docs.sentry.io/security-legal-pii/scrubbing/advanced-datascrubbing/ , https://docs.sentry.io/product/agents/privacy.md
- Sentry SDK side: `send_default_pii=false` (default) limits AI integrations to metadata; `before_send`/`before_send_log` hooks for event scrubbing; stack-frame local variables and breadcrumbs can contain prompts/PII [I; not fetched] - disable `include_local_variables` or scrub in `before_send`.
- Collector side: `redaction` processor (traces beta): `allowed_keys` allow-list (fails closed - deletes everything else), `blocked_key_patterns`, `blocked_values` regex masking, optional HMAC hashing; applies to span/log/metric-datapoint attributes; **span events and resource attributes not confirmed covered [U]**; one line of defence. [V] https://raw.githubusercontent.com/open-telemetry/opentelemetry-collector-contrib/main/processor/redactionprocessor/README.md . Use `transform`/`attributes` processors for deleting `gen_ai.input.messages`/`output.messages`/`tool.call.*` per exporter (not fetched [U on syntax]).
- **Design implication**: separate pipelines. Pipeline A -> OpenSearch (full fidelity inside the bank perimeter, apply PII policy per org; RBAC). Pipeline B -> Sentry: strip all GenAI content attributes, keep `gen_ai.*` metadata (model, tokens, tool names, operation), IDs, status; run tail sampling (errors only). Even with self-hosted Sentry, data-minimisation reduces scope of Sentry's DB/ClickHouse/Kafka retention and access model (Sentry has broad org-level read for devs).
- Defence in depth: Collector redaction before export + Sentry Advanced Scrubbing as backstop (including the four gen_ai attrs) + SDK `send_default_pii=false`.

---

## 7. Recommended concrete design

**Identity & propagation**
- One `trace_id` (W3C, 32-hex) everywhere. Propagate `traceparent`+`baggage` (OTel default). `OTLPIntegration(setup_otlp_traces_exporter=False, setup_propagator=False)` so Sentry SDK only reads OTel context (no duplicate span export, no propagator surprises). No `traces_sample_rate`.
- Resource attrs: `service.name`, `deployment.environment`, `service.version` -> Sentry `release`/`environment` (set `release` in `sentry_sdk.init`), plus framework IDs on spans: `gen_ai.conversation.id`, `agent.run_id` (our own), `gen_ai.agent.name`.
- Sentry event tags (set in a scope/`before_send` from the active span): `trace_id`, `agent.run_id`, `gen_ai.conversation.id`, `gen_ai.agent.name`, `eval.suite`/`eval.name` where relevant. Context `evals_link` with the deep link URL. Keep tag cardinality sane (run_id/trace_id are high-cardinality but are exactly what you want to search; fine for Sentry search).
- Reverse IDs on the OTel side: `before_send` writes span attribute `sentry_link.event_id` (+ `sentry_link.issue_short_id` when known) so it lands in OpenSearch; flags the span `ERROR`.

**Sentry -> our trace** (human clicks in Sentry)
1. Deep link set at capture time: `https://<evals-ui>/traces/<trace_id>` as a context + tag.
2. Optional: bridge service subscribed to Sentry `issue.created`/alert webhooks (internal integration) calls `POST /sentry-app-installations/{uuid}/external-issues/` to add a clickable "Evals trace" link to the issue sidebar.

**Our trace -> Sentry** (human clicks in our UI)
1. At trace-view time: look up `sentry_link.event_id` / issue id in the OpenSearch doc (populated by `before_send` + webhook bridge). Link to `<sentry>/organizations/<org>/issues/<id>/?...`.
2. Fallback / backfill: server-side call `GET /organizations/{org}/issues/?query=trace_id:<id>` (tag; or `trace:<id>`) with a read-only token; cache results with TTL in OpenSearch.
3. Bridge also mirrors issue status (resolved/assigned) into the trace/issue-link doc so bad-trace lists show "Sentry: unresolved, ISSUE-123".

**What to send to Sentry**
- Exceptions (unhandled + deliberately captured), with the tags above; logs at WARN+ if desired (OTLP logs endpoint or SDK logs).
- Eval *failures that matter* (threshold breaches, regressions), as aggregated `capture_message` with stable fingerprint, tags and link - not every score. Scores themselves stay in OpenSearch.
- Human feedback via Sentry User Feedback API if you want it in Sentry (optional).
- Optional PoC: error-only, content-stripped traces via Collector `otlphttp/sentry` exporter (or `sentry` exporter), so Sentry's trace view shows spans around the error. Skip Sentry AI Agents Insights initially.
- Cron monitor check-ins for batch eval jobs (optional).

**What NOT to send**
- Prompt/response/tool-argument content, `gen_ai.*.messages`, `gen_ai.tool.call.*`, system instructions; full-volume traces; every eval score; span events (dropped anyway); anything that is the evidence record (OpenSearch is system of record).

**Sampling**
- SDK: `always_on` (or parentbased with always_on root); sample in Collector only. Two-tier Collector: LB exporter by trace_id -> tail_sampling tier. Policies: keep `status_code ERROR`; keep if `sentry_link.event_id` present; keep if eval-failed marker; keep latency/cost outliers; probabilistic baseline for OK traces. `decision_wait` >= typical agent run length; decision cache on.
- Sentry gets pipeline B after tail sampling (errors-only) so every Sentry issue with a trace id has the trace in OpenSearch (capture sets ERROR/attribute -> kept) and vice-versa for error traces.

---

## 8. Risks / open questions to test
1. Self-hosted OTLP endpoint availability and required feature flags: not documented [U]. PoC: curl `/api/<id>/integration/otlp/v1/traces` with an OTLP proto sample on the target self-hosted version; check span shows in Explore.
2. Self-hosted AI Agents Insights/Conversations (flag `gen-ai-conversations`, standalone gen_ai spans) [U]; JS `streamGenAiSpans:false` workaround.
3. Docs say OTLP beta; Relay says GA - API shape may change (span model tweaks promised in discussion #85902).
4. Span events dropped: no eval events or exception events in Sentry traces; rely on attributes.
5. Propagator conflict (`sentry-trace` vs `traceparent`) when OTLPIntegration's propagator is on [U].
6. Race: Sentry error captured before the trace's other spans arrive / tail-sampling decision; late `sentry_link.event_id` may arrive after decision. Mitigate by marking the *same* span ERROR at capture time and `decision_cache`.
7. Sentry API access patterns (`trace:` on issues endpoint, org events endpoint) are partly undocumented/internal; versioning risk; API rate limits not checked [U]. Build the bridge so lookups are cached and degrade gracefully.
8. `error` webhook is Business/Enterprise on SaaS and lacks an explicit trace id in docs; use issue/alert webhooks + event fetch [V/U].
9. GenAI semconv is Development stability (attribute names/events still moving, e.g., `gen_ai.input.messages` vs older `gen_ai.prompt`); Sentry normalises some legacy names, pin versions and test.
10. PII: Sentry does not scrub gen_ai content attributes by default; local variables in stack traces may carry prompts; sentry exporter + `unredacted` content in Sentry would extend the data-residency/audit scope.
11. Operational cost of self-hosted Sentry with EAP (dual-write ClickHouse, 16-32 GB RAM, support "no guarantees").
12. Tag/URL rendering: whether Sentry renders URL-valued tags/context as clickable links not verified [U]; External Issue API is the verified clickable path.

---

## Source list
- https://docs.sentry.io/concepts/otlp/
- https://docs.sentry.io/concepts/otlp/direct/traces.md
- https://docs.sentry.io/concepts/otlp/forwarding/pipelines/collector.md
- https://docs.sentry.io/concepts/otlp/forwarding/pipelines/sentry-exporter.md
- https://docs.sentry.io/concepts/otlp/sentry-with-otel/
- https://docs.sentry.io/platforms/python/integrations/otlp/
- https://docs.sentry.io/platforms/python/tracing/instrumentation/opentelemetry/
- https://docs.sentry.io/platforms/python/migration/2.x-to-3.x/
- https://raw.githubusercontent.com/getsentry/sentry-python/master/sentry_sdk/integrations/otlp.py
- https://blog.sentry.io/otel-spans-errors-sentry-trace
- https://develop.sentry.dev/sdk/telemetry/traces/opentelemetry/
- https://develop.sentry.dev/sdk/telemetry/traces/distributed-tracing/
- https://develop.sentry.dev/sdk/telemetry/traces/modules/ai-agents/
- https://docs.sentry.io/product/insights/ai/agents/ , https://docs.sentry.io/ai/monitoring/agents/getting-started/ , https://docs.sentry.io/ai/monitoring/agents/sampling/ , https://docs.sentry.io/product/agents/privacy.md , https://docs.sentry.io/product/insights/ai/mcp/
- https://docs.sentry.io/platforms/javascript/guides/node/tracing/instrumentation/ai-agents-module/
- https://sentry.io/changelog/otlp-ingestion-endpoints-for-traces-and-logs-are-in-open-beta
- https://github.com/getsentry/sentry/discussions/85902
- https://raw.githubusercontent.com/getsentry/relay/master/CHANGELOG.md ; https://github.com/getsentry/relay/pull/5951 ; /pull/5176 ; /pull/6312
- https://develop.sentry.dev/self-hosted/ ; https://raw.githubusercontent.com/getsentry/self-hosted/master/CHANGELOG.md ; https://github.com/getsentry/self-hosted/issues/3592 ; https://raw.githubusercontent.com/getsentry/self-hosted/master/sentry/sentry.conf.example.py ; https://raw.githubusercontent.com/getsentry/self-hosted/master/nginx.conf
- https://raw.githubusercontent.com/getsentry/sentry/master/src/sentry/features/temporary.py
- https://docs.sentry.io/organization/dynamic-sampling/ ; https://docs.sentry.io/concepts/key-terms/tracing/distributed-tracing/
- https://docs.sentry.io/api/events/list-an-organizations-issues/ ; .../list-an-issues-events/ ; .../retrieve-an-event-for-a-project/ ; https://docs.sentry.io/api/discover/retrieve-a-trace/ ; https://docs.sentry.io/api/integration/create-or-update-an-external-issue/ ; https://docs.sentry.io/concepts/search/searchable-properties/events/
- https://docs.sentry.io/organization/integrations/integration-platform/ (+ /webhooks/, /webhooks/errors/, /webhooks/issues/, /webhooks/issue-alerts/, /ui-components/, /ui-components/issue-link/, /internal-integration/)
- https://docs.sentry.io/platforms/python/enriching-events/tags/ ; .../context/ ; https://docs.sentry.io/product/user-feedback/
- https://docs.sentry.io/security-legal-pii/scrubbing/advanced-datascrubbing/
- https://raw.githubusercontent.com/open-telemetry/opentelemetry-collector-contrib/main/processor/tailsamplingprocessor/README.md ; .../redactionprocessor/README.md ; https://opentelemetry.io/docs/concepts/sampling/
- https://raw.githubusercontent.com/open-telemetry/semantic-conventions-genai/main/docs/gen-ai/gen-ai-events.md
