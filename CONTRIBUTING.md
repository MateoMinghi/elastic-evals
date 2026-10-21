# Contributing to elastic-evals

Thank you for helping. This project is young, so the most useful contributions are the ones that test its claims against real use: results on your traffic, instrumentation it does not handle yet, evaluators and rules for failure modes you see.

Read [docs/CONCEPTS.md](docs/CONCEPTS.md) for the vocabulary and [docs/DESIGN.md](docs/DESIGN.md) for how the system is built and why. [DESIGN.md §2.2](docs/DESIGN.md#22-experiments) and its lessons explain several decisions that look odd until you see the experiment behind them.

## Ways to contribute

- **Report a result.** Run `python -m elastic_evals gate` on your own human-labelled conversations and open an issue with the outcome, whatever it is. Redact personal data.
- **Report a bug.** Include the version, how you run the stack (compose, Helm, CLI only), what you expected, what happened, and logs. For a wrong score, include the conversation (redacted), the suite, and the calibration id.
- **Propose a change.** For anything larger than a bug fix, open an issue first so we can agree on the approach. Design changes are recorded in `docs/DESIGN.md`.
- **Send a pull request.** See below.

Security problems: do not open a public issue; follow [SECURITY.md](SECURITY.md).

## Development setup

```bash
git clone https://github.com/MateoMinghi/elastic-evals && cd elastic-evals
python -m venv .venv && .venv/bin/pip install -e ".[all]"
cd collector && go test ./... && cd ..           # needs Go; `builder --config builder-config.yaml` builds eecol (OCB v0.163.0)
cd ui && npm ci && npm run build && cd ..
```

The test suite runs offline by default. Integration tests use real OpenSearch 3.x, Postgres 16, Kafka and the built `eecol`, and **skip themselves** when a service is not reachable, so `pytest` is always safe to run. To run them, start the services (`docker compose -f deploy/docker-compose.yml up -d opensearch postgres kafka` is enough) and build the collector. Slow tests that download Hugging Face models are opt-in: `EE_RUN_SLOW=1 pytest tests/test_hf_evaluators.py`.

### Checks CI runs

| Area | Command |
|---|---|
| Python | `ruff check elastic_evals tests --select E,F,W,B --ignore E501,B008,E731,B905,B023` and `pytest -q` |
| Go | `cd collector && go vet ./... && go test ./... && test -z "$(gofmt -l processor exporter)"` |
| UI | `cd ui && npm ci && npm run build` |

Please run them before you push. UI changes should also pass the Playwright tests against a running, seeded API (`cd ui && EE_UI_URL=http://localhost:8000 npx playwright test`); `EE_SHOTS=../docs/images EE_TENANT=bank npx playwright test` regenerates the README screenshots.

## Pull requests

- **Keep them focused.** One change, with tests. A bug fix includes a test that failed before it.
- **Match the surrounding code**: naming, comment density, idiom. Comments say *why*, not *what*.
- **Do not add a dependency** without saying why a few lines of code would not do.
- **Claims need evidence.** If a change is meant to improve accuracy, cost or latency, include the measurement (the `experiments/` scripts show how) and update the relevant report under `experiments/results/`.
- **Update the docs** that describe what you changed: the README if you change how to run or use it, `docs/DESIGN.md` if you change the design (including §14, the implementation status), and `docs/TENANT_GUIDE.md` if tenants see the change.
- **Secrets stay out.** API keys belong in an env file outside the repository (`EVAL_ENV_FILE`). Before you push, run `git grep -n "sk-or-v1-"` and check that nothing you add contains a credential or a real conversation.
- **Sign off your work** if you can: add `Signed-off-by` (`git commit -s`) to certify the [Developer Certificate of Origin](https://developercertificate.org/). By contributing you agree your contribution is licensed under the [Apache License 2.0](LICENSE).

## Where things live

| Path | What |
|---|---|
| `elastic_evals/` | Python: engine (`suite`, `calibrate`, `evaluators`, `engine`), services (`tracker`, `worker`, `api`), `sentry`, CLI (`__main__`), pytest plugin |
| `elastic_evals/store/` | OpenSearch client and templates, Postgres access and migrations (`store/schema/*.sql`) |
| `collector/` | Go: `processor/eeprocessor` (tenant tagging, normaliser, redaction), `exporter` (OpenSearch writer) |
| `ui/` | React + TypeScript (Vite); tests in `ui/tests/` |
| `deploy/` | Collector configs, Compose, Helm, the Sentry proof-of-concept |
| `experiments/` | Study scripts, data, and results |

## Adding an evaluator, a rule or a normaliser rule

- **Rule** (a free, deterministic first pass): add a function to `elastic_evals/rules.py` (conversation structure and content) or `elastic_evals/trajectory.py` (tool-call sequences). It takes the conversation (`item` or `conv`, a dict with `turns` and `record`) and returns the probability of the flagged outcome, usually `0.0` or `1.0`; a name listed in `builtin_rules()` becomes usable from a suite's `rule:`. Add a test in `tests/test_engine.py`.
- **Evaluator type**: subclass the base in `elastic_evals/evaluators.py` and add it with `register(type_name, cls, tier)` (the `HFClassifierEvaluator` is a small example). Anything that a tenant can reference from a suite must also be covered by `elastic_evals/suite_policy.py`, because tenant suites are untrusted input.
- **Attribute mapping** for another instrumentation library: `collector/processor/eeprocessor/normalize.go`, with a test in `processor_test.go` built from a real span sample.

## Conduct

Be kind and assume good faith. See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
