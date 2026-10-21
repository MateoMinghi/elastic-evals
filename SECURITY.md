# Security policy

## Reporting a vulnerability

**Do not open a public issue for a security problem.** Report it privately through GitHub: on the repository page choose **Security → Report a vulnerability** ([private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)). Include what you found, how to reproduce it, the version or commit, and the impact you see.

You can expect an acknowledgement within a few days. We will keep you informed while we investigate, fix it, and agree on disclosure. We credit reporters who want credit.

The project is alpha software maintained by a small team; we cannot offer bug bounties or a guaranteed response time.

## Supported versions

Only the latest release and `main` receive fixes while the project is pre-1.0.

## What is in scope

The areas where a flaw matters most:

- **Tenant isolation:** one tenant reading or changing another tenant's traces, conversations, scores, labels, calibrations or keys, through the API, the UI, the Collector or the Dashboards objects.
- **Authentication and authorisation:** API keys (`ek_<id>.<secret>`), roles (`platform`, `admin`, `engineer`, `annotator`, `viewer`, `ingest`), and the scopes behind them. A tenant `admin` must not be able to act outside its tenant.
- **Tenant-registered suites:** suites that tenants submit through the API are untrusted input. They must not make a worker import code, send a credential to a URL the tenant chooses, or load model weights the operator has not allowed (`EE_RULE_MODULES`, `EE_JUDGE_URLS`, `EE_JUDGE_KEY_ENVS`, `EE_HF_MODELS`; see [`elastic_evals/suite_policy.py`](elastic_evals/suite_policy.py)).
- **Data handling:** PII redaction at the gateway, content retention, and S3 offload.
- **The Sentry webhook bridge:** HMAC verification and the External Issue endpoints.
- **The Collector:** the `elasticevals` processor and `elasticevals_opensearch` exporter.

Out of scope: vulnerabilities in OpenSearch, Kafka, Postgres, Sentry or other components themselves (report those upstream), and findings that need an already-compromised operator account.

## Deploying safely

- Run the API with authentication on. `EE_AUTH=off` exists for local development only: it takes the tenant from a request header.
- Keep provider keys such as `OPENROUTER_API_KEY` in the environment or an env file outside the repository (`EVAL_ENV_FILE`), never in a suite or in git.
- Hand tenants an `admin` key for their tenant only; keep the `platform` key to operators.
- Put the API and the OTLP endpoints behind TLS. The default compose file is for evaluation, not for the open internet.
- Treat the stored conversations as sensitive. Enable Presidio redaction at the gateway, and use dedicated pools (`tenancy.dedicatedPools` in the Helm chart) for regulated tenants.
- Review the operator allow-lists above before enabling tenant-registered suites.
