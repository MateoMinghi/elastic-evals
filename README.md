# elastic-evals

Open-source observability and evaluation for production AI agents:
- **OpenTelemetry in.** Agents emit GenAI spans; a custom Collector tags tenants, normalises OpenInference / OpenLLMetry / Langfuse attributes and redacts.
- **OpenSearch as the event store**, Postgres for configuration, Kafka in between.
- **A calibrated cascade of evaluators** scores every conversation: rules → small local classifiers → Jev → one LLM judge, with routes chosen from labels and humans as ground truth.
- **A drift monitor** for the evaluators themselves, **eval-aware retention**, and a **Sentry bridge**.

Design of record: [docs/DESIGN.md](docs/DESIGN.md). Evidence: [experiments/results/](experiments/results/).

## Status

v0.2 implements the design end to end; see [DESIGN.md §14](docs/DESIGN.md#14-implementation-status-v02). Highlights:

| Claim | Evidence |
|---|---|
| The engine matches the best single LLM judge at lower cost and latency | [ENGINE_SUMMARY.md](experiments/results/ENGINE_SUMMARY.md): 93.5% on the holdout, $83 / 1M conversations, 36% sent to an LLM |
| The production path is lossless | [PIPELINE.md](experiments/results/PIPELINE.md): the same holdout replayed as OTel spans through collectors, Kafka, OpenSearch and the worker gives the same 93.5%, check by check |
| Distillation replaces judge calls | [DISTILL.md](experiments/results/DISTILL.md): 93.9%, $53 / 1M, 20% sent to an LLM |
| Single-node storage footprint and query latency | [LOAD.md](experiments/results/LOAD.md) |
| No span lost when a cluster node dies mid-ingest; rollover by size | [CLUSTER.md](experiments/results/CLUSTER.md) |

Everything in the design is built. Roadmap gates 2–5 have passed, including a load test at 2.5× the target peak ([PHASE2.md](experiments/results/PHASE2.md)) and the Sentry bridge against a self-hosted Sentry ([SENTRY_POC.md](experiments/results/SENTRY_POC.md)). Still open: phase 1 on real human-labelled traffic (it passes on the synthetic holdout, [GATE_PHASE1.md](experiments/results/GATE_PHASE1.md)) and phase 6, a first external tenant (an onboarding dry run passed: [TENANT_DRYRUN.md](experiments/results/TENANT_DRYRUN.md)).

Beyond the cascade: trajectory and per-step (observation) checks, Presidio redaction at the gateway, S3 offload of very large content, re-pricing of stored spans, OpenSearch Dashboards saved objects, Hugging Face classifier and zero-shot evaluator adapters.

## Layout

| Path | What |
|---|---|
| `elastic_evals/` | Python: engine, calibration, evaluators, tracker, worker, API (FastAPI), Sentry bridge, CLI, pytest plugin |
| `collector/` | Go: the `elasticevals` processor and `elasticevals_opensearch` exporter, built into the `eecol` Collector distribution |
| `ui/` | TypeScript + React: conversations, viewer, scores and calibration, experiments, annotation |
| `deploy/` | Collector configs, docker compose, Helm chart |
| `examples/` | A suite (`banking_suite.yaml`) and a synthetic test-set spec (`banking_synth.yaml`) |
| `experiments/` | The studies behind the design, with their data and results |
| `tests/` | Offline tests and integration tests against real OpenSearch, Postgres, Kafka and collectors |

## Onboarding a team (tenants)

Teams whose agent you evaluate: [`docs/TENANT_GUIDE.md`](docs/TENANT_GUIDE.md) (send traces, write a suite, label, calibrate, all through the API or UI). As the operator:

```bash
python -m elastic_evals setup --tenant platform --platform-key      # prints a platform key (tenants, keys, price book)
curl -X POST -H "Authorization: Bearer $PLATFORM_KEY" -H "Content-Type: application/json" $EE/v1/tenants -d '{"id": "acme"}'
curl -X POST -H "Authorization: Bearer $PLATFORM_KEY" -H "Content-Type: application/json" $EE/v1/api-keys -d '{"role": "admin", "tenant": "acme"}'
```

Suites that tenants register are checked against allow-lists (`EE_RULE_MODULES`, `EE_JUDGE_URLS`, `EE_JUDGE_KEY_ENVS`, `EE_HF_MODELS`; see `elastic_evals/suite_policy.py`). Create the Kafka spans topic with 6+ partitions: they are ingest's write concurrency.

## Run the whole stack

```bash
docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml logs setup        # prints an admin API key for tenant "demo"
```

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

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[all]"
cd collector && go test ./... && builder --config builder-config.yaml   # OCB v0.163.0 -> _build/eecol
cd ui && npm ci && npm run build
pytest                     # integration tests skip when OpenSearch / Postgres / Kafka / eecol / Dashboards are absent
EE_RUN_SLOW=1 pytest tests/test_hf_evaluators.py                       # CPU transformer adapters
cd ui && EE_UI_URL=http://localhost:8000 npx playwright test           # against a running, seeded API
```
