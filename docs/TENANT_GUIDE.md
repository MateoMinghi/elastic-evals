# Tenant guide

For a team whose agent is evaluated by an elastic-evals deployment that someone else (the platform team) runs. You get from them:

- an **OTLP endpoint** for your traces (HTTP `:4318` or gRPC `:4317`);
- your **tenant id**;
- an **API key** (`ek_….secret`), with a role:
  - `admin`: everything in your tenant, including creating more keys;
  - `engineer`: suites, calibrations, datasets, experiments, labels;
  - `annotator`: read conversations and label them;
  - `viewer`: read only;
- the **UI/API URL**.

Everything below is done with your key, through the API or the UI. The full API schema is at `<URL>/openapi.json`; the UI asks for the key under **Settings**.

```bash
export EE=https://evals.example.com KEY=ek_xxxxxxxx.yyyyyyyy
curl -s -H "Authorization: Bearer $KEY" $EE/v1/me      # your tenant, key and scopes
```

The sections follow an onboarding, in order:

1. [Send traces](#1-send-traces)
2. [See conversations](#2-see-conversations)
3. [Write a suite](#3-write-a-suite)
4. [Label conversations](#4-label-conversations)
5. [Calibrate and turn scoring on](#5-calibrate-and-turn-scoring-on)
6. [In production](#6-in-production)

Two further sections cover [timing](#timing-and-lifecycle) and [how many labels you need](#how-many-labels).

## 1. Send traces

Use any OpenTelemetry instrumentation: the OTel GenAI conventions, OpenInference, OpenLLMetry (Traceloop) and Langfuse attributes are all normalised at the gateway. Set these:

| What | Where | How |
|---|---|---|
| Your tenant | resource attribute | `ee.tenant_id=<tenant id>`; or the OTLP header `x-ee-tenant` |
| One conversation across turns | the turn's root span | `gen_ai.conversation.id`, or OpenInference `session.id`, or Traceloop `traceloop.association.properties.session_id`, or `langfuse.session.id`. Without it, each trace is one conversation |
| What the agent said and heard | LLM spans | the instrumentation's message attributes (`gen_ai.input.messages` / `gen_ai.output.messages`, OpenInference `llm.input_messages.*` / `llm.output_messages.*`) |
| The turn's input and answer | agent span (optional) | OpenInference `input.value` / `output.value` as plain text, or `gen_ai.input.messages` / `gen_ai.output.messages` |
| Tool calls | tool spans | `gen_ai.tool.name`, arguments and result (OpenInference `tool.name`, `input.value`, `output.value`) |
| The system of record | the turn's root span | one attribute per field, `ee.record.<field>` (e.g. `ee.record.status=pending`, `ee.record.price_eur=120`), or a JSON object in `ee.record`. Checks compare what the agent said with it |
| Language | any span (optional) | `ee.lang` (`es`, `en`…). Without it the language is detected from the user's messages |

Conversation turns are rebuilt from the message history of the last LLM call, or, when agent spans carry their input and output, from those plus each LLM call's output. So an agent whose spans carry no messages at all shows tool calls but no text.

Example, OpenInference with the OpenAI SDK:

```python
from openinference.instrumentation.openai import OpenAIInstrumentor
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

provider = TracerProvider(resource=Resource.create({"service.name": "travel-agent", "ee.tenant_id": "acme-travel"}))
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint="http://collector:4318/v1/traces")))
trace.set_tracer_provider(provider)
OpenAIInstrumentor().instrument(tracer_provider=provider)
tracer = trace.get_tracer("travel-agent")

with tracer.start_as_current_span("turn", attributes={
        "openinference.span.kind": "AGENT", "session.id": conversation_id,
        "input.value": user_text}) as span:
    answer, booking = run_agent(user_text)            # LLM and tool spans nest under this one
    span.set_attribute("output.value", answer)
    span.set_attribute("ee.record.status", booking.status)
```

## 2. See conversations

A conversation appears once it is complete, that is, when no span has arrived for a while (see [timing](#timing-and-lifecycle)).

```bash
curl -s -H "Authorization: Bearer $KEY" "$EE/v1/conversations?size=20"           # filters: flag, agent, lang, error, q (full text), from, to
curl -s -H "Authorization: Bearer $KEY" "$EE/v1/conversations/<conversation or trace id>"
```

The detail view has turns, tool calls, the record, the trajectory, spans with cost, scores and linked Sentry issues. Until a calibration is active, conversations are listed but not scored.

## 3. Write a suite

A suite is a YAML file: the checks (questions), the evaluators they may use and a policy. Start from [`examples/banking_suite.yaml`](../examples/banking_suite.yaml) and register it:

```bash
curl -s -X PUT -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d "$(jq -Rs '{yaml: ., agent_name: "travel-agent"}' travel_suite.yaml)" $EE/v1/suites/travel
```

Registering again with a changed file creates a new version.

The platform checks what a suite references, because its workers run it. Built-in rules only; judges at OpenRouter or at URLs the operator allows; Hugging Face models the operator allows. A refused suite gets a 422 with the list of problems.

**Question fields** (`questions: {<name>: {...}}`):

| Field | Meaning |
|---|---|
| `ask` | The check as a yes/no question for judges ("Does the assistant claim the booking is confirmed while the record says otherwise?"). Yes = problem present |
| `view` | The text local models see: parts joined by `\|`. Parts:<br>`user`, `assistant`;<br>`first_assistant:N`, `last_assistant:N`;<br>`tools`, `trajectory`;<br>`record:a/b` (field `a`, else `b`) |
| `rule` | Optional built-in rule (below) |
| `rule_args` | Arguments for the rule, e.g. `{max_tool_calls: 8}` |
| `critical` | Strict floor (one SE, no tolerance). Local heads may decide it only with enough labels per class (`policy.min_class_examples`) |
| `classes` | Multi-class check: `{label: description}` |
| `layers` | Restrict to these evaluator ids |
| `scope` | `conversation` (default), `trajectory` or `observation` (per step, with `select`, e.g. `{type: TOOL, tool: book_flight}`) |
| `criteria_version` | Bump when the meaning changes |

**Built-in rules.** Some read the record or the steps; the text ones are written for the banking example (English and Spanish).

| Rule | What it detects |
|---|---|
| `tool_loop` | The same tool called repeatedly |
| `tool_errors` | `min_errors` tool calls failed |
| `step_budget` | More than `max_tool_calls` tool calls |
| `forbidden_tools` | Any of `tools` was called |
| `expected_sequence` | The `sequence` of tools was not followed |
| `step_failed` | Observation scope: the step reported an error |
| `false_confirmation` | The assistant claims success while `record.status` is not `confirmed` |
| `fee_mismatch` | The stated fee disagrees with `record.fee` |
| `disclosure_missing` | A required disclosure is absent |
| `user_frustrated` | Frustration markers in the user's messages |

A rule that does not fit your agent is still useful as a cheap first layer: calibration keeps it only where it is accurate.

**Evaluator types** (`evaluators: [{id, type, ...}]`):

| Type | What it is | Fields |
|---|---|---|
| `rules` | The rules above | |
| `local_head` | A classifier trained on your labels, on local embeddings (e5) | |
| `jev` | A hosted encoder classifier | |
| `llm` | A judge | `model` (an OpenRouter model id; the platform team can tell you which they allow), `base_url` only if the operator allows it, `reasoning_effort` |
| `hf_classifier`, `hf_zero_shot` | Hugging Face models | Only ids the operator allows |

Pick judges from a different model family than your agent.

**Policy** (`policy:`):
- `target_accuracy`, `tolerance`;
- `audit_rate` and `audit_judge`: if the audit judge also routes, calibration audits with another judge and says so;
- `max_ms_per_conv`, `max_usd_per_1M_conv`;
- `languages`;
- `crossfit_folds`, `floor_se`, `min_class_examples`. See [DESIGN.md §6.2](DESIGN.md).

## 4. Label conversations

Labels go into a *label set*, filled through *annotation queues*.

```bash
# a queue for your suite's checks; fill rules pick conversations
curl -s -X POST -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" $EE/v1/annotation-queues -d '{
  "suite_id": "travel", "name": "first labels", "checks": ["false_confirmation", "user_frustrated"],
  "fill_rules": [{"type": "stratified", "per_lang": 30}]}'
# -> {"id": "aq_…", "label_set_id": "ls_…", …}
```

Ways to fill the queue:
- `POST /v1/annotation-queues/{id}/fill` applies the queue's fill rules:
  - `{"type": "stratified", "per_lang": N}`: a sample per language;
  - `{"type": "low_confidence", "band": [0.3, 0.7]}`: decisions with a probability in the band;
  - `{"type": "tier_disagreement"}`: the routed decision disagrees with a shadow score;
  - `audit_disagreement`: added by the drift monitor.

  Low-confidence and disagreement rules need scores, so they apply once a calibration runs.
- `POST /v1/annotation-queues/{id}/items` with `{"conversation_ids": [...]}` adds specific conversations.

Then label in the UI (**Annotation**: `1`/`0` answer the focused check and move to the next, `j`/`k` move, `Enter` submits, `s` skips), or through the API:

```bash
curl -s -X POST -H "Authorization: Bearer $KEY" $EE/v1/annotation-queues/$Q/next      # locks and returns the next item (the same one again until you answer)
curl -s -X POST -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  $EE/v1/annotation-queues/$Q/items/$CID/labels -d '{"labels": {"false_confirmation": 1, "user_frustrated": 0}}'
```

Other queue calls:
- `GET …/items?status=pending|locked|done|skipped` lists items;
- `POST …/items/{cid}/release` unlocks one;
- `POST …/items/{cid}/skip` skips one;
- locks also expire after 15 minutes.

To feed one calibration from several queues (a weekly sample and a disagreement queue, say), create the later queues with `"label_set_id": "<the first queue's label set>"`.

## 5. Calibrate and turn scoring on

```bash
curl -s -X POST -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  $EE/v1/suites/travel/calibrations -d '{"label_set_id": "ls_…"}'      # synchronous; "local_only": true keeps text in the deployment
```

The response is a candidate calibration with a report, per check:
- the chosen route (e.g. `rules → e5 → llm_ds`);
- its accuracy and floor;
- the share each layer decides;
- cost and latency;
- `warnings`: too few labels for a class, local heads kept off a critical check, a non-independent audit judge.

Read them before turning it on (the UI shows the same under **Scores & calibration**).

| Call | Effect |
|---|---|
| `POST /v1/calibrations/{id}/promote` | Make it active: new conversations are scored with it |
| `POST /v1/calibrations/{id}/shadow` | Score alongside the active one without deciding |
| `GET /v1/suites/{suite}/shadow-comparison` | Whether the shadow agrees with the audit judge at least as well. `enough_data: false` until enough audited traffic arrived; `needed` says how much |
| `POST /v1/calibrations/{id}/promote?force=true` | Promote anyway |
| `POST /v1/conversations/rescore` with `{"from": "2026-10-01"}` or `{"conversation_ids": [...]}` | Score conversations ingested before the calibration was active |

## 6. In production

| What | Call |
|---|---|
| Flags and scores | `GET /v1/conversations?flag=<check>`, `GET /v1/scores/summary` |
| Agreement of your labels with the engine | `GET /v1/label-sets/{id}/agreement` (kappa, precision and recall per class) |
| Drift: live agreement with the audit judge | `POST /v1/suites/{id}/drift-check`, `GET /v1/drift-alerts` |
| Experiments and datasets for CI | `/v1/datasets`, `/v1/experiments`; and the pytest plugin in the [README](../README.md) |
| Sentry | `before_send` from `elastic_evals.sentry` links events to conversations ([DESIGN.md §8](DESIGN.md)) |

## Timing and lifecycle

- **When a conversation is complete.** The operator sets this (tracker `--trace-idle`, `--session-idle`).
  - A single-trace conversation is complete about 30 s after its last span.
  - A session (conversation id across turns) is complete 5 minutes after its last turn by default.
- **Scoring** follows within seconds for local layers, plus the judges' latency.
- **Content is staged for about 3 days.** After that, only conversations kept by retention (flagged, errored or sampled) keep their content, for 30 days.
  - Label within that window.
  - Or add conversations to a queue early: items in an annotation queue are listed by id; their content must still be staged or kept when you open them.
- **Scores and rollups** are kept for about a year, without content.

## How many labels

- **30–60 labelled conversations** are enough for a first calibration whose checks are decided by rules and judges.
- **Local heads (e5) need more.**
  - Calibration keeps them off a *critical* check until it has `min_class_examples` (default 20) labels of each class.
  - On other checks it warns.
- **Labels are what calibration trusts.** Check them: `GET /v1/label-sets/{id}/agreement` after a while shows where the engine and your labellers disagree, and a `tier_disagreement` queue collects those conversations.
