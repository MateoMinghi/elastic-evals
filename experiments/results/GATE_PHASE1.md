# Phase-1 gate on the synthetic holdout

The phase-1 gate (DESIGN.md §12): per check, the engine's held-out accuracy is within one standard error of the best single evaluator's, or the check has a documented reason. `python -m elastic_evals gate` measures it by K-fold cross-validation: for each fold it calibrates on the other folds, then scores the held-out fold with the engine and with every single evaluator alone.

Run here on the 300-conversation synthetic holdout C, 5 folds, hosted answers from the cache. The phase-1 gate itself needs ~200 real, human-labelled conversations; this run checks the tooling and the engine's behaviour against the gate's own definition.

| policy | false_confirmation | disclosure_missing | fee_mismatch | user_frustrated | tool_loop | gate | $ / 1M conv | sent to an LLM |
|---|---|---|---|---|---|---|---|---|
| defaults (in-sample thresholds, floor 1 SE) | 96.7 vs 97.0 | 99.3 vs 99.7 | 91.3 vs 94.0 ✗ | 84.0 vs 83.7 | 100.0 vs 100.0 | not passed | 97 | 76% |
| `crossfit_folds: 5` | 97.0 vs 97.0 | 99.3 vs 99.7 | 92.0 vs 94.0 ✗ | 83.3 vs 83.7 | 100.0 vs 100.0 | not passed | 92 | 70% |
| `crossfit_folds: 5`, `floor_se: 0.5` | 97.0 vs 97.0 | 99.3 vs 99.7 | 92.7 vs 94.0 | 85.0 vs 83.7 | 100.0 vs 100.0 | **passed** | 117 | 93% |

Cells are engine vs the best single evaluator, held-out accuracy in %. The SE on fee_mismatch is 1.4 points, so the line is 92.6.

## Why fee_mismatch missed

- The best single evaluators on fee_mismatch are Haiku and DeepSeek alone (94.0%). Calibration hosted Haiku but routed fee_mismatch through Jev first (Jev decides when confident, Haiku otherwise), because that route cleared the floor on the training folds and cost less.
- **Optimistic estimates.** With in-sample thresholds, a cheap route's accuracy is measured on the same conversations its thresholds were tuned on. Cross-fitting the thresholds (`crossfit_folds`) removes part of that optimism: 91.3 → 92.0.
- **The floor sits on the gate's line.** Calibration accepts the cheapest route within one SE of the best (on training folds). The gate rejects anything more than one SE below the best (on held-out folds). A route chosen at the floor therefore misses the gate about as often as it clears it. A floor at half an SE (`floor_se: 0.5`) leaves headroom: 92.7, passing, at 27% more cost.

## What this means for deployments

- **Default unchanged** (`floor_se: 1`, `crossfit_folds: 0`): the cheapest route statistically indistinguishable from the best. Expect individual checks to land just under the gate.
- **Tenants who must pass the gate:** set `crossfit_folds: 5` and `floor_se: 0.5`, or list the check in the gate's `--reasons` file with the cost trade-off as its reason.

## Reproduce

Data: [`crossfit.json`](crossfit.json), [`gate_phase1_c.json`](gate_phase1_c.json) (the `gate` command's output with defaults). Script: `experiments/crossfit_experiment.py` ([CROSSFIT.md](CROSSFIT.md)). Command:

```
python -m elastic_evals gate --suite examples/banking_suite.yaml --data experiments/data/synthetic_c.jsonl --folds 5
```
