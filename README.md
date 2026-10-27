<div align="center">

# elastic-evals

**Observability and calibrated evaluation for production AI agents, on OpenSearch.**

Know what your agent did, whether it did it right, and what it cost to find out.

[![CI](https://github.com/MateoMinghi/elastic-evals/actions/workflows/ci.yml/badge.svg)](https://github.com/MateoMinghi/elastic-evals/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
![Status: alpha](https://img.shields.io/badge/status-alpha-orange.svg)

[What it is](#what-is-elastic-evals) ·
[How it works](#how-it-works) ·
[Screenshots](#screenshots) ·
[Results](#does-it-work) ·
[Quick start](#quick-start) ·
[Docs](#documentation)

<img src="docs/images/conversation.png" alt="A conversation in elastic-evals: the transcript, the five checks with the evaluator that decided each one, and the spans" width="900">

</div>

## What is elastic-evals?

If you run an AI agent in production (a support bot, a booking assistant, a coding agent) you eventually need answers to questions like:

- Did the agent tell the user the transfer went through when it actually failed?
- Did it quote the right fee? Did it say it was an AI? Did it loop on a tool? Did the user get frustrated?
- Did last week's prompt change make any of that better or worse?
- Which of the thousands of conversations from yesterday should a human look at?

elastic-evals answers them. It is an open-source system that **collects your agent's traces, scores every conversation against checks you define, and shows you the results**, with the scoring tuned against human labels so you know how far to trust it.

It is built from familiar parts:

- **Your agent sends OpenTelemetry (GenAI) traces.** No proprietary SDK. Traces from OpenInference, OpenLLMetry and Langfuse instrumentation are converted to one format.
- **OpenSearch stores everything:** traces, conversation text, scores, embeddings and metrics. Postgres holds configuration only.
- **Each conversation is scored by a cascade of evaluators**, cheapest first: plain rules, then small local classifiers, then a hosted classifier, then one LLM judge, with humans as the ground truth. Most conversations are settled by the first layers and never reach an LLM.
- **It watches its own accuracy.** A drift monitor re-checks a sample of decisions with an independent judge and alerts when agreement drops.
- **It connects to Sentry** in both directions: an error links to the full conversation, and a conversation links back to its issue.

If you know Langfuse, the concepts will feel familiar (traces, observations, scores, datasets, annotation queues, experiments). The differences are the storage (OpenSearch instead of ClickHouse), the cascade, and the calibration against human labels. See [How it compares](#how-it-compares).

### Why a cascade?

Asking a large LLM to judge every conversation is accurate, but slow and expensive at production volume. Rules and small classifiers are cheap and fast, but only right some of the time. The answer is to use each where it works, and to *measure* where that is rather than guess.

For every check, calibration looks at your labelled conversations and picks the cheapest chain of evaluators that is accurate enough, along with the confidence thresholds at which to stop or to hand over to the next layer. Escalating to a bigger model is not assumed to help: in our tests it sometimes made a check worse, so a layer is only added if it wins on labelled data.

```mermaid
flowchart LR
    C[conversation] --> R["rules<br/>free, instant"]
    R -- confident --> D((decision))
    R -- unsure --> L["local classifier<br/>CPU, ms"]
    L -- confident --> D
    L -- unsure --> J["hosted classifier<br/>cents per 1M"]
    J -- confident --> D
    J -- unsure --> M["LLM judge<br/>most expensive"]
    M --> D
    D -. audit sample .-> A["independent judge<br/>drift monitor"]
    D -. low confidence .-> H["human annotation<br/>ground truth"]
    H -. labels .-> K["calibration<br/>picks routes and thresholds"]
    K -. configures .-> R
```

You choose which layers a suite may use. A suite can be rules only, local only (no conversation text leaves your deployment), or the full cascade.

## How it works

```mermaid
flowchart LR
    subgraph app["Your application"]
        AG["agent + OpenTelemetry SDK"]
    end
    subgraph gw["Gateway (Go Collector)"]
        P["tag tenant · normalise<br/>OpenInference / OpenLLMetry / Langfuse<br/>redact PII (Presidio)"]
    end
    AG -- OTLP --> P
    P --> KF[("Kafka")]
    KF --> IN["Ingest (Go Collector)<br/>price · batch"]
    IN --> OS[("OpenSearch<br/>spans · payloads · scores<br/>traces · embeddings · metrics")]
    KF --> TR["Tracker (Python)<br/>is the conversation finished?"]
    TR --> PG[("Postgres<br/>config · calibrations<br/>queues · keys")]
    PG --> W["Evaluation workers<br/>cascade → scores"]
    OS <--> W
    OS --> API["API + UI"]
    PG --> API
    SE["Sentry"] <--> API
```

1. **Collect.** The agent exports OTLP. A custom OpenTelemetry Collector (the *gateway*) tags each span with its tenant, normalises attributes to the OTel GenAI conventions, and redacts personal data.
2. **Buffer and store.** Kafka decouples collection from storage. A second Collector (*ingest*) writes spans and message payloads to OpenSearch with deterministic ids, so retries never duplicate data. Offsets are committed only after OpenSearch accepts a batch.
3. **Detect the end of a conversation.** The tracker groups spans into traces and traces into sessions, and signals when a conversation has settled.
4. **Score.** Evaluation workers rebuild the conversation, run the calibrated cascade for each check, and write scores, a per-conversation rollup and metrics. They also decide what to keep: failures, flagged and low-confidence conversations are retained in full; the rest expire early.
5. **Review.** The UI and the API show conversations, scores and calibration reports; annotation queues collect human labels; labels feed the next calibration.

The data flow, the OpenSearch index layout, the engine and the roadmap are specified in [docs/DESIGN.md](docs/DESIGN.md).

### A suite, in one file

You describe *what to check* in YAML. Calibration decides *how*.

```yaml
questions:
  false_confirmation:
    ask: Does the assistant tell the user the operation was completed, while the system of record says it was not?
    view: record:status|last_assistant:2     # what the evaluator gets to see
    rule: false_confirmation                 # a free first-pass rule (optional)
    critical: true                           # held to a stricter accuracy floor
  tool_loop:
    ask: Does the assistant call the same tool with identical arguments three or more times in a row?
    view: tools
    rule: tool_loop
evaluators:
  - {id: rules, type: rules}
  - {id: e5, type: local_head, encoder: intfloat/multilingual-e5-small}
  - {id: jev, type: jev, model: typesafe/jev-1.14}
  - {id: llm, type: llm, model: <any OpenRouter model>}
policy:
  target_accuracy: 0.95
  max_usd_per_1M_conv: 100
```

The full example is [`examples/banking_suite.yaml`](examples/banking_suite.yaml). Checks can look at the whole conversation, at the tool-call trajectory, or at single steps (observation-level checks).

## What you get

| | |
|---|---|
| **Collection** | OTLP gRPC/HTTP; OpenInference, OpenLLMetry and Langfuse attributes normalised to OTel GenAI; tenant tagging; Presidio PII redaction; S3 offload for very large content; per-token pricing with a reloadable price book and re-pricing of stored spans |
| **Storage** | OpenSearch only (3.x): per-kind indices with ISM lifecycle policies, k-NN embeddings, star-tree metrics, shared or dedicated pools per tenant; Postgres for configuration |
| **Evaluation** | Calibrated cascade (rules → local heads → Jev → LLM → human); trajectory and per-step checks; multi-class and multilingual checks with per-language accuracy floors; Hugging Face classifier and zero-shot adapters; custom evaluator adapters; local-only mode |
| **Trust** | Calibration with a validation floor and a target; calibrations move through candidate → shadow → active → retired; drift monitor with an independent audit judge; distillation of judge labels into local heads |
| **Humans** | Annotation queues filled by rules (stratified, low-confidence, flagged), keyboard-driven labelling in the UI or terminal; shared label sets; labels feed calibration |
| **Testing** | Datasets from real conversations; experiments comparing two agent versions; model sweeps that rank the LLMs your agent could run on by success, cost per task and cost per 1M tokens; a pytest plugin so CI can fail on flagged conversations |
| **Sentry** | `before_send` hook and span processor to link events to conversations; webhook bridge for issues, errors and regressions; quality-regression issues; External Issue links |
| **Operations** | Docker Compose and a Helm chart; scoped API keys with roles (platform, admin, engineer, annotator, viewer, ingest); allow-lists that make tenant-registered suites safe to run; OpenSearch Dashboards saved objects |

## Screenshots

The screenshots below use a synthetic banking assistant (examples in [`examples/`](examples/)) scored by a calibrated suite. They are generated by [`ui/tests/screens.spec.ts`](ui/tests/screens.spec.ts).

**Conversations.** Browse and search by content, filter by flag, language, agent or error. Each row shows which checks passed and which were flagged.

<img src="docs/images/conversations.png" alt="Conversation list with check badges and facets" width="900">

**Scores and calibration.** For each check: how many decisions each layer made, the flag rate, accuracy by language, cost, and drift alerts from the audit judge.

<img src="docs/images/scores.png" alt="Scores table showing which evaluator layer decided each check, with language breakdown" width="900">

**Calibration report.** Why the engine chose each route: measured accuracy against the floor and the best single evaluator, accuracy by language, cost per million conversations, and every hosted-evaluator set it considered.

<img src="docs/images/calibration.png" alt="Calibration report with the chosen route per check and the hosted sets considered" width="900">

**Experiments.** Run two versions of your agent over the same dataset and compare check by check. Here, a prompt change that stopped the agent from disclosing it is an AI raises that flag from 35% to 80%, and the 18 conversations that changed are listed.

<img src="docs/images/experiments.png" alt="Comparison of two experiment runs showing a disclosure regression" width="900">

**Annotation.** Humans label what the engine is least sure about, with the keyboard. Model scores are shown for reference.

<img src="docs/images/annotation.png" alt="Annotation queue with keyboard shortcuts and the conversation" width="900">

**Sentry.** The conversation behind an error links to its Sentry issue, and the Sentry issue links back.

<img src="docs/images/sentry-link.png" alt="A conversation with its linked Sentry issue" width="900">

## Does it work?

On a synthetic banking assistant (five checks, English and Spanish), the calibrated engine was tested on conversations from a model family it had never seen, written after the last design change and scored exactly once ([ENGINE_SUMMARY.md](experiments/results/ENGINE_SUMMARY.md)):

| | Accuracy | $ per 1M conversations | ms per conversation | Sent to an LLM |
|---|---|---|---|---|
| **elastic-evals cascade** | **93.5%** | **$83** | **564** | **36%** |
| Three single LLM judges, each on every conversation | 92.8–93.1% | $81–135 | 920–2,064 | 100% |
| Hosted classifier alone | 92.3% | $37 | 198 | 0% |
| Cascade, local models only | 87.3% | $0 | 49 | 0% |

Beyond that, each part of the design has been run against real services:

- **Production path = offline results.** The same conversations, sent as OpenTelemetry spans through collector, Kafka, OpenSearch and workers, score identically ([PIPELINE.md](experiments/results/PIPELINE.md)).
- **Load.** 2,900 spans/s (2.5× the design peak) for five minutes on a replicated 3-node OpenSearch cluster: no span lost, ingest p95 22 s ([PHASE2.md](experiments/results/PHASE2.md)).
- **Distillation.** Training local heads on judge labels reached 93.9% with 20% of conversations sent to an LLM ([DISTILL.md](experiments/results/DISTILL.md)).
- **Sentry.** 13/13 checks against a real self-hosted Sentry, click-through both ways ([SENTRY_POC.md](experiments/results/SENTRY_POC.md)).
- **Onboarding.** A simulated outside team went from nothing to scoring its own traffic through the API ([TENANT_DRYRUN.md](experiments/results/TENANT_DRYRUN.md)).

### Honest limits

- **The accuracy numbers come from synthetic, LLM-written conversations** with planted labels. They show the mechanism works, not how it performs on your traffic. The first thing to do with your own agent is to label a couple of hundred conversations and run the [`gate`](#the-engine-without-any-services) command, which tests the engine against the best single judge on your data.
- **No outside team runs it in production yet.** The roadmap marks two gates open for that reason: validation on real human-labelled traffic, and a first real external tenant ([Status](#status)).
- The judges and classifiers used in the experiments are hosted models reached through OpenRouter. Local-only suites work without any hosted model but are less accurate (87.3% in the table above) and are best for rules-friendly checks.

## How it compares

elastic-evals borrows heavily from existing tools ([docs/DESIGN.md §2.1](docs/DESIGN.md#21-the-landscape-existing-evaluation-tools) lists what and from whom). What it adds:

| | Typical LLM observability / evals tools | elastic-evals |
|---|---|---|
| Storage | ClickHouse, Postgres, Redis, object storage | **OpenSearch** (and Postgres for config only); one search stack you may already run |
| Online scoring | One LLM judge per sampled trace | **Calibrated cascade**; each layer used only where it measurably works |
| Trust in the scores | Usually none beyond the judge prompt | **Accuracy measured against human labels**, per check and language; drift monitor |
| Cost control | Uniform sampling | **Per-check routing** under accuracy, cost and latency budgets; eval-aware retention |
| Error tracking | Separate | **Sentry bridge** in both directions |
| Multi-tenancy | Often an enterprise feature | Built in: tenant tagging at the gateway, roles, dedicated indices, suite allow-lists |

Use a different tool if you need a mature hosted product today, a large prompt-management workflow, or a ready catalogue of generic metrics (RAG faithfulness and the like). This project is for teams who want evaluation they can measure and run themselves on OpenSearch.

## Status

**Alpha (v0.3).** The design is implemented end to end ([DESIGN.md §14](docs/DESIGN.md#14-implementation-status-v03)) and tested against real OpenSearch, Postgres, Kafka and Collector builds. Expect rough edges and breaking changes before 1.0. The roadmap gates (§12):

| Phase | Gate | Evidence |
|---|---|---|
| 1. Real-traffic validation | Passes on the synthetic holdout; **open** until run on ~200 real, human-labelled conversations | [GATE_PHASE1.md](experiments/results/GATE_PHASE1.md) |
| 2. Storage path and load | **Passed:** 2,900 spans/s (2.5× the target peak) for 5 minutes on a replicated 3-node cluster, no span lost, ingest p95 22 s | [PHASE2.md](experiments/results/PHASE2.md), [LOAD.md](experiments/results/LOAD.md), [CLUSTER.md](experiments/results/CLUSTER.md) |
| 3. Engine as a service | **Passed:** the holdout through the production path scores exactly as offline (93.5%) | [PIPELINE.md](experiments/results/PIPELINE.md) |
| 4. Humans and offline | **Passed:** distillation, 93.9% with 20% of conversations sent to an LLM | [DISTILL.md](experiments/results/DISTILL.md) |
| 5. Sentry bridge | **Passed** against a self-hosted Sentry, click-through both ways | [SENTRY_POC.md](experiments/results/SENTRY_POC.md) |
| 6. UI and governance | An onboarding dry run passed; **open** until a first real external tenant | [TENANT_DRYRUN.md](experiments/results/TENANT_DRYRUN.md) |

## Quick start

Pick the path that matches what you want to do.

### 1. Try the engine on your laptop (no services)

Calibrate and score with local models only. No API keys, no network after the first model download.

```bash
git clone https://github.com/MateoMinghi/elastic-evals && cd elastic-evals
python -m venv .venv && . .venv/bin/activate
pip install -e ".[local]"

# 395 labelled synthetic conversations ship with the repo
python -m elastic_evals calibrate --suite examples/banking_suite.yaml \
    --data experiments/data/synthetic.jsonl --local-only --out calib/      # a couple of minutes on CPU
python -m elastic_evals report --calib calib/                              # per-check route, accuracy, cost
python -m elastic_evals run --suite examples/banking_suite.yaml --calib calib/ \
    --data experiments/data/synthetic_c.jsonl --out scores.jsonl           # score conversations
```

Add `OPENROUTER_API_KEY` (see [The engine without any services](#the-engine-without-any-services)) and drop `--local-only` to let calibration also consider the hosted classifier and LLM judges.

### 2. Run the whole stack

```bash
docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml logs setup        # prints an admin API key for tenant "demo"
```

This starts OpenSearch, Postgres, Kafka, the gateway and ingest Collectors, the tracker, the workers, and the API with the UI.

- Agents send OTLP to `localhost:4317` (gRPC) or `localhost:4318` (HTTP), with resource attribute `ee.tenant_id`.
- UI and API at http://localhost:8000; paste the API key under Settings.
- Hosted evaluators read `OPENROUTER_API_KEY` from `deploy/.env`. Without it, use local-only suites.
- Behind a TLS-intercepting proxy, build with `--secret id=extra_ca,src=/path/ca.crt`.

Register a suite, calibrate it and start scoring. Run the CLI inside the stack so calibration artifacts land
in the volume the workers read:

```bash
ee() { docker compose -f deploy/docker-compose.yml run --rm --user "$(id -u):$(id -g)" -v "$PWD:/work" -w /work api "$@"; }
ee suite --tenant demo --id banking --file examples/banking_suite.yaml --agent bank-assistant
ee calibrate --suite examples/banking_suite.yaml --data labelled.jsonl --out calib/      # --local-only without a key
ee register --tenant demo --suite banking --calib calib/      # prints the calibration id (status: candidate)
ee promote <calibration id>                                  # or `ee shadow <id>` first
ee replay --data experiments/data/synthetic_c.jsonl --tenant demo --endpoint http://gateway:4318   # demo traffic
```

Kubernetes: `deploy/helm/elastic-evals` (external OpenSearch, Kafka and Postgres; set `tenancy.dedicatedPools` for regulated tenants).

### 3. Instrument your agent

Any OpenTelemetry GenAI instrumentation works. Point the exporter at the gateway and set the tenant:

```python
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

provider = TracerProvider(resource=Resource.create({"ee.tenant_id": "demo", "service.name": "bank-assistant"}))
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint="http://localhost:4318/v1/traces")))
```

Spans should follow the [OTel GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/) (`invoke_agent`, `chat`, `execute_tool`, with `gen_ai.input.messages` / `gen_ai.output.messages`). OpenInference, OpenLLMetry and Langfuse attribute names are converted by the gateway. [`docs/TENANT_GUIDE.md`](docs/TENANT_GUIDE.md) covers sessions, the system-of-record entry, and the rest.

## Onboarding a team (tenants)

Teams whose agent you evaluate: [`docs/TENANT_GUIDE.md`](docs/TENANT_GUIDE.md) (send traces, write a suite, label, calibrate, all through the API or UI). As the operator:

```bash
python -m elastic_evals setup --tenant platform --platform-key      # prints a platform key (tenants, keys, price book)
curl -X POST -H "Authorization: Bearer $PLATFORM_KEY" -H "Content-Type: application/json" $EE/v1/tenants -d '{"id": "acme"}'
curl -X POST -H "Authorization: Bearer $PLATFORM_KEY" -H "Content-Type: application/json" $EE/v1/api-keys -d '{"role": "admin", "tenant": "acme"}'
```

Suites that tenants register are checked against allow-lists (`EE_RULE_MODULES`, `EE_JUDGE_URLS`, `EE_JUDGE_KEY_ENVS`, `EE_HF_MODELS`; see `elastic_evals/suite_policy.py`). Create the Kafka spans topic with 6+ partitions: they are ingest's write concurrency.

## Letting an AI agent operate it (MCP)

`python -m elastic_evals mcp` starts an [MCP](https://modelcontextprotocol.io) server that gives an AI agent (Claude Code, Claude Desktop, your own) tools to inspect and manage a running deployment: search conversations and scores, read and write suites, run calibrations and move them through shadow to active, run drift checks, fill annotation queues, build datasets. It is a thin client of the HTTP API, so it needs no database access and adds no new way in: tenant isolation, role scopes and the audit log are the API's, and the agent acts as the one API key you give it.

```bash
pip install "elastic-evals[mcp]"
export EE_API_URL=http://localhost:8000 EE_API_KEY=ek_xxx.yyy      # the key belongs in the environment, not on a command line
claude mcp add elastic-evals -- python -m elastic_evals mcp --read-only
```

For other clients, the same command in their MCP configuration (`{"command": "python", "args": ["-m", "elastic_evals", "mcp"], "env": {"EE_API_URL": "...", "EE_API_KEY": "..."}}`). `--transport streamable-http --port 8765` serves it over HTTP instead; that endpoint has no authentication of its own and acts with the process's key for everyone who can reach it, so keep it on localhost or behind your own proxy.

Start with `--read-only`, which does not offer the 12 tools that change anything. Give the agent the narrowest role for the job: a tool whose scope the key lacks returns the API's `403 missing scope …`. `whoami` shows what the key can do. Tools that change production behaviour (`promote_calibration`, `rescore`) are marked destructive so clients can ask for confirmation, and promotion keeps the API's own gate: a shadow candidate that does not agree with the audit judge at least as well as the active calibration is refused, and there is no `force`.

What it leaves out on purpose: tenants, API keys and the price book (platform administration), and human labels and score corrections. Labels are the ground truth that calibration and the drift monitor are measured against, so an agent must not write them. This is a choice of tools, not a permission boundary: the same key can still call those API routes directly, so use a role that lacks `annotations:write` and `scores:write` if that matters.

## The engine without any services

```bash
pip install -e ".[local]"
export EVAL_ENV_FILE=/path/outside/repo/.env    # OPENROUTER_API_KEY=... for Jev and LLM judges

python -m elastic_evals calibrate --suite examples/banking_suite.yaml --data labelled.jsonl --out calib/   # --local-only
python -m elastic_evals report    --calib calib/
python -m elastic_evals run       --suite examples/banking_suite.yaml --calib calib/ --data convs.jsonl --out scores.jsonl
python -m elastic_evals synth     --spec examples/banking_synth.yaml --n 50 --out synth.jsonl
python -m elastic_evals calibrate ... --pool unlabelled.jsonl --teacher llm_haiku        # distillation
python -m elastic_evals experiment --suite S --calib calib/ --items items.jsonl --agent my_pkg.agent:run --report run.json
python -m elastic_evals gate      --suite examples/banking_suite.yaml --data human_labelled.jsonl          # phase-1 gate
```

Conversations are JSONL:

```json
{"id": "...", "lang": "es", "turns": [{"role": "user", "text": "..."}, {"role": "tool", "name": "...", "args": {}, "result": "..."}, {"role": "assistant", "text": "..."}], "record": {"status": "pending", "fee": 0}}
```

Calibration data adds `"labels": {"check": 0|1|"class"}`; observation checks are labelled per step as `"check@<span_id>"`.

### Which model should your agent run on?

Not which model makes the best judge, but which one makes the best agent. `sweep` runs the same items with your agent on each candidate model, scores every conversation with the same suite and calibration, and ranks the models:

```bash
python -m elastic_evals sweep --suite S --calib calib/ --items items.jsonl --agent my_pkg.agent:make_agent \
    --models anthropic/claude-haiku-5.5,openai/gpt-6-luna,deepseek/deepseek-v4.1-flash \
    --prices deploy/collector/prices.json --repeats 3 --report sweep.json
```

`make_agent(model)` returns the agent; the conversations it returns carry the agent's `usage` (`input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `reasoning_tokens`, as one dict or one per LLM call with its `model`), or a `cost_usd` the provider reported. For each model:

| Column | Meaning |
|---|---|
| success % | items with no flagged check and no failed assertion, with a 95% Wilson interval; `--repeats k` also reports pass^k (every attempt succeeded) |
| qualified | no critical check flagged on any item; a model that fails one ranks last whatever it costs |
| $/task | what the agent spent per item: token prices *times the tokens the model needed* (reasoning, longer answers), so a cheap rate does not guarantee a cheap task |
| $/success | total spend divided by successful items: failures still cost money |
| $/1M tok | effective: spend divided by tokens actually used, after cache discounts |
| list $/1M | list price blended 7:2:1 cache-hit:input:output, the ratio [Artificial Analysis](https://artificialanalysis.ai/methodology) uses |
| Δ success | against the baseline model (`--baseline`, default the first), item by item, with a paired bootstrap interval |

Models on the Pareto frontier of success against cost per task are starred. The sweep warns when an LLM evaluator in the calibration is from the same family as a candidate, since a judge tends to favour its own family; check that evaluator's accuracy on that model's conversations, or calibrate without it. `experiment` accepts `--prices`, `--model` and `--repeats` and reports the same cost figures for a single run. In production, `GET /v1/models/summary` gives the same figures per model from real traffic, and `/v1/scores/summary?model=` splits flag rates by the agent's model.

### In your test suite

```ini
# pytest.ini
[pytest]
ee_suite = examples/banking_suite.yaml
ee_calibration = calib/
```

```python
def test_transfer(ee):
    conv = run_my_agent("send 100 to Ana")
    ee.check(conv, expect={"fee_mismatch": 0})   # fails on any flagged critical check
```

Use `--ee-baseline run.json` and `--ee-report run.json` for relative gates in CI.

### In your agent (Sentry)

```python
import sentry_sdk
from elastic_evals.sentry import before_send
sentry_sdk.init(dsn=..., before_send=before_send(ui_url="https://evals.example.com"), send_default_pii=False)
```

## Documentation

| Document | For |
|---|---|
| [docs/CONCEPTS.md](docs/CONCEPTS.md) | The vocabulary: tenant, trace, conversation, check, evaluator, route, calibration, shadow, drift, and how they fit together |
| [docs/DESIGN.md](docs/DESIGN.md) | The design of record: architecture, engine, storage, Sentry, tenancy, roadmap, lessons |
| [docs/TENANT_GUIDE.md](docs/TENANT_GUIDE.md) | Teams whose agent is evaluated: send traces, write a suite, label, calibrate, through the API or UI |
| This README | Operators and contributors: run the stack, onboard tenants, use the engine, develop |
| [experiments/results/](experiments/results/README.md) | The evidence: every study and gate, with the script that reproduces it |
| [docs/research/](docs/research/) | The research behind the design: Langfuse, other evaluation tools, OpenSearch, Sentry, classifiers |
| [docs/PROPOSAL.md](docs/PROPOSAL.md) | History: the pre-experiment proposal DESIGN.md superseded |
| [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) | Working on the project, reporting vulnerabilities, community standards |

## Layout

| Path | What |
|---|---|
| `elastic_evals/` | Python: engine, calibration, evaluators, tracker, worker, API (FastAPI), MCP server, Sentry bridge, CLI, pytest plugin |
| `collector/` | Go: the `elasticevals` processor and `elasticevals_opensearch` exporter, built into the `eecol` Collector distribution |
| `ui/` | TypeScript + React: conversations, viewer, scores and calibration, experiments, annotation |
| `deploy/` | Collector configs, docker compose, Helm chart |
| `examples/` | A suite (`banking_suite.yaml`) and a synthetic test-set spec (`banking_synth.yaml`) |
| `experiments/` | The studies behind the design, with their data and results |
| `tests/` | Offline tests and integration tests against real OpenSearch, Postgres, Kafka and collectors |
| `docs/` | The design, the concepts, the tenant guide and the research (see [Documentation](#documentation)) |

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[all]"
cd collector && go test ./... && builder --config builder-config.yaml   # OCB v0.163.0 -> _build/eecol
cd ui && npm ci && npm run build
pytest                     # integration tests skip when OpenSearch / Postgres / Kafka / eecol / Dashboards are absent
EE_RUN_SLOW=1 pytest tests/test_hf_evaluators.py                       # CPU transformer adapters
cd ui && EE_UI_URL=http://localhost:8000 npx playwright test           # against a running, seeded API
cd ui && EE_SHOTS=../docs/images EE_TENANT=bank npx playwright test   # regenerate the README screenshots
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the workflow and what a good pull request looks like.

## Contributing and community

Issues and pull requests are welcome. The most valuable contributions right now:

- **Results on real traffic.** Run the phase-1 gate on your own human-labelled conversations and share the outcome, good or bad.
- **Evaluator adapters** for other classifiers and judges, and **rules** for common agent failure modes.
- **Instrumentation fixtures**: traces from frameworks the gateway's normaliser does not yet handle.
- **Bug reports** with a trace or conversation that reproduces them (please redact personal data).

Report security issues privately, as described in [SECURITY.md](SECURITY.md).

## License

[Apache License 2.0](LICENSE). The synthetic conversations under `experiments/data/` were generated with language models for testing and are provided as-is under the same license. Third-party components (OpenTelemetry Collector, OpenSearch, Presidio, and the Python, Go and JavaScript dependencies) keep their own licenses.
