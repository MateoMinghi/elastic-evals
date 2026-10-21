# Results

The evidence behind [DESIGN.md](../../docs/DESIGN.md), in the order it was produced. Each report names the script that regenerates it; the `.json` files next to them hold the raw numbers.

## The studies that shaped the design (§2.2)

| Report | What it shows | Script |
|---|---|---|
| [RESULTS.md](RESULTS.md), [tables.md](tables.md) | Every evaluator layer on its own, on public and synthetic datasets | `experiments/prep_public.py`, `gen_synthetic.py`, `run_layers.py`, `analyze.py` |
| [ENGINE_SUMMARY.md](ENGINE_SUMMARY.md), [ENGINE.md](ENGINE.md) | The calibrated engine against single judges, on the held-out set and an out-of-distribution set | `experiments/eval_engine.py` |

## The implementation, end to end (§14)

| Report | What it shows | Script |
|---|---|---|
| [PIPELINE.md](PIPELINE.md) | The holdout replayed as OTel spans through the production path scores exactly as offline | `experiments/e2e_pipeline.py` |
| [DISTILL.md](DISTILL.md) | Judge labels train local heads: fewer LLM calls, same accuracy | `experiments/distill_experiment.py` |
| [CROSSFIT.md](CROSSFIT.md) | Cross-fitted thresholds and floors with headroom | `experiments/crossfit_experiment.py` |

## Roadmap gates (§12)

| Phase | Report | Result |
|---|---|---|
| 1. Real-traffic validation | [GATE_PHASE1.md](GATE_PHASE1.md) | Passes on the synthetic holdout; still to run on real labelled traffic |
| 2. Storage path and load | [LOAD.md](LOAD.md), [CLUSTER.md](CLUSTER.md), [PHASE2.md](PHASE2.md) | Passed at 2.5× the target peak on a replicated 3-node cluster |
| 3. Engine as a service | [PIPELINE.md](PIPELINE.md) | Passed |
| 4. Humans and offline | [DISTILL.md](DISTILL.md) | Passed |
| 5. Sentry bridge | [SENTRY_POC.md](SENTRY_POC.md) | Passed against a self-hosted Sentry |
| 6. UI and governance | [TENANT_DRYRUN.md](TENANT_DRYRUN.md) | Onboarding dry run passed; a first real external tenant is still to come |
