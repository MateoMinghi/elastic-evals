"""elastic-evals: layered evaluation of agent conversations.

Rules -> small local classifiers -> hosted classifiers (Jev) -> LLM judges, with each
question's route chosen by calibration on labelled data, and escalation decided per
conversation so one hosted call answers every question that still needs it.

Entry points: elastic_evals.suite.Suite, elastic_evals.calibrate.calibrate, elastic_evals.engine.Engine,
and the CLI (python -m elastic_evals).
"""
