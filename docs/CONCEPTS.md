# Concepts

The vocabulary of elastic-evals, in the order you meet it. Read this before [DESIGN.md](DESIGN.md) (which specifies everything) or [TENANT_GUIDE.md](TENANT_GUIDE.md) (which walks you through using it).

## Telemetry: what comes in

**Span.** One unit of work reported by your agent through OpenTelemetry: an agent invocation (`invoke_agent`), a model call (`chat`), a tool call (`execute_tool`). Spans follow the [OTel GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/). The *gateway* converts OpenInference, OpenLLMetry and Langfuse attribute names to those conventions, so you do not have to change your instrumentation.

**Trace.** All spans for one request, joined by a trace id.

**Session.** Several traces that belong together, typically one chat with several user turns. Spans carry a session or conversation id; the tracker uses it.

**Conversation.** The unit that is scored. It is rebuilt from a trace or session: the user, assistant and tool turns in order, plus the *record*. A conversation is scored once it has settled (no new spans for a configurable idle time) and re-scored if it grows.

**Record.** The system-of-record entry the conversation should agree with: the actual transfer status and fee, the actual booking. Checks such as "did the agent confirm something that did not happen" compare the transcript with it. A record is optional; checks that only need the transcript do not use one.

**Tenant.** A team or product whose traffic is kept apart from others'. The gateway tags every span with its tenant from the resource attribute `ee.tenant_id` (or an OTLP header). Tenants share indices by default; regulated tenants get dedicated pools.

## Evaluation: what the engine does

**Check.** A yes/no (or multi-class) judgment about a conversation, such as `false_confirmation`, `fee_mismatch`, `user_frustrated`, `tool_loop`. A check has a question (`ask`) for hosted evaluators, a `view` that says which parts of the conversation an evaluator sees, optionally a built-in `rule`, and flags such as `critical`. Checks can apply to the whole conversation, to the tool-call trajectory, or to a single step (an *observation* check).

**Suite.** A versioned YAML file holding the checks, the evaluators you allow, and a policy. It says *what to check*; calibration works out *how*.

**Evaluator.** Something that can answer a check. In order of cost:

| Type | Runs | Good for |
|---|---|---|
| `rules` | in process, free | record comparisons, loops, retries, step budgets, required phrases |
| `local_head` | on CPU, milliseconds | style and topic: frustration, intent, sentiment |
| `jev` and other hosted classifiers | hosted, cents per million conversations | typed judgments with probabilities |
| `llm` | hosted, seconds | reasoning against records, nuanced judgments |
| `hf_classifier`, `hf_zero_shot` | on CPU | Hugging Face models you allow |
| your own | whatever you register | anything, through the adapter registry |

**Cascade and route.** The *route* of a check is the ordered list of evaluators it may use. Each evaluator returns an answer and a probability. If the probability clears the layer's confidence threshold the answer is final; otherwise the conversation goes to the next layer. The last layer always decides.

**Policy.** The knobs on a suite: `target_accuracy`, `tolerance`, a cost ceiling per million conversations, a latency ceiling per conversation, the audit rate and judge, the languages to report, whether hosted evaluators are allowed at all (`allow_hosted: false` is *local-only*: nothing leaves your deployment).

**Calibration.** A one-off (and repeatable) job that takes your labelled conversations and chooses, for each check, the route and the thresholds. It also picks which hosted evaluators the suite uses as a set, because several checks can share one hosted call. The output is a versioned artifact: routes, thresholds, trained local heads, and a report of measured accuracy, cost and latency.

**Floor, target, strictness.** The *floor* is the least accuracy acceptable for a check: the best accuracy any single evaluator achieved, minus a tolerance. The *target* is the accuracy you want. *Strictness* is how accurate a cheap layer's confident decisions must be before it is allowed to stop the cascade. Critical checks use stricter values. If calibration cannot meet the floor for a check, it says so in the report instead of quietly shipping a weak route.

**Shadow score.** A hosted call made for one check often answers others for free. Those extra answers are stored as *shadow* scores. They do not decide anything; they feed the drift monitor and show you what another evaluator would have said.

**Score.** One check's result for one conversation: value, probability, which evaluator decided, the route taken, the calibration id, the evaluator version, cost and latency. Scores have deterministic ids, so re-scoring never creates duplicates.

## Trust: how you know the scores are right

**Label.** A human's answer to a check for a conversation. Labels come from *annotation queues*, which are filled by rules (stratified by language, low confidence, flagged conversations) and worked through in the UI or terminal. Labels are the ground truth for calibration, the drift monitor and the experiments.

**Calibration lifecycle.** A new calibration starts as a *candidate*. Run it in **shadow** next to the active one to see how it would have scored live traffic, then **promote** it to *active*; the previous one is retired. A bad candidate can be **rejected**.

**Audit and drift.** A small random share of production decisions (the audit rate) is re-scored by an *independent* judge: one that is not part of the route being audited. The drift monitor compares the routed decisions with the audit judge. If agreement falls below the calibrated baseline it raises an alert, which is the cue to look at the failing check and recalibrate.

**Distillation.** Judge answers on unlabelled conversations can be used to train the local classifiers, so more decisions are made in the cheap layers. Distilled heads go through the same calibration and gate as any other.

**Gate.** `python -m elastic_evals gate` cross-validates the engine on human-labelled data and compares it with the best single evaluator for each check. It passes if the engine is within one standard error of the best single evaluator for every check, or a check has a documented reason to be excepted (see [`examples/gate_reasons.yaml`](../examples/gate_reasons.yaml)). Run it on your own data before you rely on the scores.

## Operations

**Retention, eval-aware.** Spans, message payloads and scores live in separate OpenSearch indices with different lifetimes. After scoring, the worker decides what to keep: errors, flagged and low-confidence conversations and ones linked to Sentry are retained in full; healthy ones can expire early. See [DESIGN.md §7](DESIGN.md#7-retention-and-sampling-eval-aware).

**Dataset and experiment.** A *dataset* is a set of conversations (often picked from production) to replay. An *experiment* runs an agent version over a dataset and scores the outputs with a calibrated suite; two experiments can be compared check by check. The pytest plugin turns the same machinery into CI assertions.

**Sentry bridge.** Events captured in your agent carry the conversation id; the bridge links the Sentry issue and the conversation in both directions, and can report a quality regression to Sentry as an event with a stable fingerprint, so repeated regressions group into one issue.

**Roles.** API keys are `ek_<id>.<secret>` with a role: `platform` (operator: tenants, keys, price book), `admin` (one tenant), `engineer`, `annotator`, `viewer`, `ingest`. Suites that tenants register are checked against operator allow-lists before they can run.

## Where next

- Try it: [README quick start](../README.md#quick-start).
- Use it as a team: [TENANT_GUIDE.md](TENANT_GUIDE.md).
- How and why it is built this way: [DESIGN.md](DESIGN.md).
- The evidence: [experiments/results/](../experiments/results/README.md).
