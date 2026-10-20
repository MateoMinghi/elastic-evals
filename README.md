# elastic-evals

Open-source observability and evaluation for production AI agents. The plan:
- OpenTelemetry ingestion, with OpenSearch as the event store;
- a calibrated cascade of evaluators: rules → small local classifiers → Jev → LLM judges → human review;
- links to Sentry in both directions.

**Status:** design and working prototype.

- **Design:** [docs/PROPOSAL.md](docs/PROPOSAL.md). Background research: [docs/research/](docs/research/).
- **Evidence:** [experiments/results/ENGINE_SUMMARY.md](experiments/results/ENGINE_SUMMARY.md) for the engine, [experiments/results/RESULTS.md](experiments/results/RESULTS.md) for the single layers.
- **Prototype:** the `elastic_evals` package, an evaluation engine with no storage or UI yet.

## The prototype in one minute

Describe your checks in a suite file (`examples/banking_suite.yaml`): the question wording, the rule and the local-model view per question, which evaluators are allowed, and the policy (target accuracy, local-only or hosted, audit rate).

Calibrate on labelled conversations. The engine:
- learns confidence thresholds per layer;
- trains the small local heads;
- picks the cheapest set of hosted evaluators, and the route per question, that stays near the best achievable accuracy.

```bash
pip install sentence-transformers scikit-learn httpx snowballstemmer unidecode pyyaml tabulate
export EVAL_ENV_FILE=/path/outside/repo/.env    # OPENROUTER_API_KEY=... (Jev and LLM judges via OpenRouter)

python -m elastic_evals calibrate --suite examples/banking_suite.yaml --data labelled.jsonl --out calib/
python -m elastic_evals report    --calib calib/
python -m elastic_evals run       --suite examples/banking_suite.yaml --calib calib/ --data conversations.jsonl --out scores.jsonl
```

Add `--local-only` to `calibrate` to forbid hosted models. The report then tells you which checks cannot reach the target without them.

Conversations are JSONL:

```json
{"id": "...", "lang": "es", "turns": [{"role": "user", "text": "..."}, {"role": "tool", "name": "...", "args": {}, "result": "..."}, {"role": "assistant", "text": "..."}], "record": {"status": "pending", "fee": 0}}
```

Calibration data also carries `"labels": {"question": 0|1}`. Scores come out as one JSON record per question, giving the value, the probability, the evaluator and its tier, and the sample reason (`route`, `shadow` or `random_audit`).
