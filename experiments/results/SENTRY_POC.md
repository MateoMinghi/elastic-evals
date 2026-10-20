# Sentry bridge against a self-hosted Sentry (phase-5 gate)

Self-hosted Sentry 25.7.0 (web, worker, ingest and post-process consumers, Snuba, ClickHouse, Relay) running next to the elastic-evals pipeline; set up by `deploy/sentry-poc/`. One demo conversation through our collectors; its transfer tool raises and the Sentry SDK captures it with `before_send`. Script: `experiments/sentry_poc.py`.

| check | result |
|---|---|
| conversation evaluated | ✓ |
| event tagged with the trace id | ✓ |
| event tagged with the conversation id | ✓ |
| event carries our deep link | ✓ |
| no conversation content in Sentry | ✓ |
| webhook linked issue to trace | ✓ |
| External Issue on the Sentry issue opens our conversations for that issue | ✓ |
| our API returns the issue with a Sentry permalink | ✓ |
| traces-* rollup carries the issue id | ✓ |
| click: our conversation -> Sentry issue | ✓ |
| click: Sentry issue -> our conversations for that issue -> the conversation | ✓ |
| resolving in Sentry updates our rollup | ✓ |
| drift alert is a Sentry issue, tagged by check | ✓ |

Gate: **passed**. The issue was linked to our trace 11 s after the exception was captured.

**What the real Sentry exposed** (each fixed, with a test, before this run):

- `issue.created` fires once per issue; later conversations hitting the same issue arrive only as `error.created`, which the bridge ignored. A regression (a resolved issue seen again) arrives as `issue.unresolved`, not always with `error.created`. The bridge now links on all three.
- Sentry keeps one External Issue per issue and integration, so it pointed at the first trace only. It now opens our conversation list filtered to the issue (`/conversations?sentry_issue=…`).
- Sentry links by trace id; our API looked conversations up by conversation id only. It now resolves a trace id to its conversation.
- The issue usually exists before the conversation is evaluated, and the worker rewrote the rollup without the link. The worker now carries Sentry links into every rollup it writes.
- Errors are raised in child spans (tool calls), which do not carry `gen_ai.conversation.id`; `before_send` now gets it from a span processor that indexes each trace's conversation and agent.
- Stored links had no Sentry URL for the UI to open; the API now builds the issue permalink.

Self-hosted specifics, not ours: Sentry post-processes an event (and sends its webhooks) once the next event arrives; Relay needs keep-alive off on uWSGI without nginx; the classic issue page swallows clicks on integration External Issue links (the new issue UI opens them).

Screenshots: [our conversation with its Sentry issue](sentry_poc/1-ours-conversation.png), [the Sentry issue opened from it](sentry_poc/2-sentry-issue.png), [our conversations for the issue, opened from its External Issue link](sentry_poc/3-ours-from-sentry.png).
