# Phase 6: an external-tenant onboarding dry run

The phase-6 gate is a first external tenant. A container cannot supply one, so this was the closest proxy. An independent agent with no prior context played "Acme Travel": an engineer onboarding onto a deployment someone else runs, with only what a tenant gets (the README and DESIGN, an OTLP endpoint, a tenant admin key and the UI URL). It was told to avoid reading the platform's source.

- **Its agent:** a travel-booking assistant it built (OpenAI SDK against OpenRouter, tool calling, an in-memory system of record with confirmed, pending and failed bookings).
- **Its instrumentation:** OpenInference, not our helpers.
- **What it did:** sent 77 conversations, wrote a 3-check suite, labelled 72 conversations through the annotation API and UI, calibrated through the API, promoted, and checked production scoring. About 25 minutes in total.

## What worked

- **Ingest was faithful.** Tool arguments and results, the record (`ee.record.<field>`), trajectory and cost were all correct. OpenInference `session.id` grouped turns into conversations.
- **Calibration took 23–45 s and its report was clear:** routes, floors, share decided per layer, cost.
- **Production scoring was right on 29 of 30 decisions,** about 30–45 s after the last span.
- **Shadow scores and the tier-disagreement queue caught the one miss.**

## What it found, and what changed

| Finding | Kind | Fix (with a test) |
|---|---|---|
| A turn ending on a tool call (no assistant text) made the conversation vanish: never listed, 500 on fetch. OpenAI-style instrumentations send that message with `parts: null` | **Data loss** | The conversation builder tolerates null and non-dict parts. The collector emits `[]`. `POST /v1/conversations/rescore` sends failed or earlier conversations back to the workers. Both lost conversations were recovered on the live deployment |
| A local head trained on 8 positives cleared the **critical** check `false_confirmation` at "100%"; in production it missed a real false confirmation | Calibration | `policy.min_class_examples` (default 20): local heads stay off a critical check until it has that many labels per class; warnings in the report |
| The audit judge was the same model as a routing judge (DESIGN §6.4 forbids it) | Calibration | Calibration audits with another allowed judge and warns. The engine uses the calibration's audit judge |
| Shadow comparison said `enough_data: true` with no data, then refused promotion | API | `enough_data: false` when nothing was audited, plus what is `needed` |
| Annotation locks leaked: a repeated `/next` locked a second item, and there was no API to list or unlock items | Annotation | `/next` resumes the item the annotator holds. `GET …/items` and `POST …/items/{cid}/release` added. The UI refreshes queue counts |
| A disagreement queue had its own label set, so its labels could not feed the same calibration | Annotation | A queue can write to an existing label set (`label_set_id`) |
| Language always `und` for agents that send no `ee.lang` | Ingest | Language detected from the user's messages |
| OpenInference AGENT spans' `input.value` / `output.value` were ignored | Ingest | The normaliser maps plain-text values to the turn's user and assistant messages |
| Unknown UI routes rendered a blank page; the sidebar said "not signed in" after a key was saved | UI | A not-found page; the sidebar refreshes when settings change |
| `artifact_uri` exposed a server file path to the tenant | API | Hidden from tenants |
| No tenant-facing documentation (auth header, OTel attributes, suite fields, annotation-to-calibration flow, timing, label volumes) | Docs | [`docs/TENANT_GUIDE.md`](../../docs/TENANT_GUIDE.md) |

## Found while preparing it

Two security defects in multi-tenancy, both fixed with tests:

- **Tenant admins held `*:*`.**
  - **Impact:** any tenant's admin key could create tenants, mint admin keys for any other tenant, and rewrite the global price book.
  - **Fix:** a separate `platform` role now holds the global scopes; tenant admins administer their own tenant.
- **A tenant-registered suite could make the platform's workers misbehave.**
  - **Impact:**
    - import and call any Python function (`rule: os:system`);
    - send the platform's OpenRouter key, or any environment variable (`api_key_env`), to a URL of the tenant's choosing (`base_url`);
    - load arbitrary Hugging Face weights.
  - **Fix:**
    - suites registered through the API are checked against operator allow-lists (`elastic_evals/suite_policy.py`);
    - only built-in rule functions resolve;
    - a judge with a custom `base_url` gets no key unless one is configured.

## What this does not show

This was one simulated team, on one day, with an agent it built for the purpose. A real external tenant brings its own traffic, its own labellers and its own operational expectations. The gate stays open until one does.
