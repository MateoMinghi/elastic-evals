"""Suite configuration: the questions to evaluate, the evaluators available, and the policy."""

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Question:
    name: str
    ask: str                 # wording for hosted classifiers / LLM judges
    view: str                # text view for local heads, see views.py
    rule: str | None = None  # name of a rule in elastic_evals.rules, if any
    invert: bool = False     # ask() is phrased positively; the problem is its negation
    critical: bool = False   # route must reach the best dev accuracy, not just near it
    layers: list[str] | None = None  # restrict to these evaluator ids (user choice)


@dataclass
class Policy:
    allow_hosted: bool = True          # False: rules + local models only (data never leaves)
    target_accuracy: float = 0.95      # confident decisions of non-final layers must reach this on dev
    tolerance: float = 0.01            # route may be this much below the best dev accuracy if cheaper
    audit_rate: float = 0.02           # share of conversations re-scored by the audit judge
    audit_judge: str | None = None


@dataclass
class Suite:
    name: str
    judge_preamble: str
    questions: list[Question]
    evaluators: list[dict]
    policy: Policy = field(default_factory=Policy)

    @classmethod
    def load(cls, path: str | Path) -> "Suite":
        raw = yaml.safe_load(Path(path).read_text())
        return cls(
            name=raw["name"],
            judge_preamble=raw["judge_preamble"],
            questions=[Question(name=k, **v) for k, v in raw["questions"].items()],
            evaluators=raw["evaluators"],
            policy=Policy(**raw.get("policy", {})),
        )

    def question(self, name: str) -> Question:
        return next(q for q in self.questions if q.name == name)

    def ask_key(self, q: Question) -> str:
        """Key used in hosted prompts (inverted questions are asked under their positive name)."""
        return q.name.replace("_missing", "_given") if q.invert else q.name
