# elastic-evals

Open-source observability and evaluation for production AI agents: OpenTelemetry ingestion, OpenSearch as the event store, a cascade of evaluators (deterministic and lexical checks → small self-hosted classifiers, Jev optional → LLM judges → human review) with calibration and distillation, and links to Sentry in both directions.

Status: design phase. Start with [docs/PROPOSAL.md](docs/PROPOSAL.md); background research is in [docs/research/](docs/research/).
