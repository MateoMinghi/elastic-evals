"""Suite configuration: the questions to evaluate, the evaluators available, and the policy."""

import hashlib
from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml


@dataclass
class Question:
    name: str
    ask: str                 # wording for hosted classifiers / LLM judges
    view: str                # text view for local heads, see views.py
    rule: str | None = None  # name of a rule in elastic_evals.rules, if any
    invert: bool = False     # ask() is phrased positively; the problem is its negation
    critical: bool = False   # floor is the best accuracy minus one standard error, no tolerance
    layers: list[str] | None = None  # restrict to these evaluator ids (user choice)
    classes: dict[str, str] | None = None  # multi-class check: {label: description}; None = binary
    criteria_version: str = "1"  # bump when the wording or the meaning of the check changes

    @property
    def multiclass(self) -> bool:
        return bool(self.classes)


@dataclass
class Policy:
    allow_hosted: bool = True          # False: rules + local models only (data never leaves)
    target_accuracy: float = 0.95      # confident decisions of non-final layers must reach this on dev
    tolerance: float = 0.01            # route may be this much below the best dev accuracy if cheaper
    audit_rate: float = 0.02           # share of conversations re-scored by the audit judge
    audit_judge: str | None = None
    max_ms_per_conv: float | None = None      # latency budget for the chosen hosted set
    max_usd_per_1M_conv: float | None = None  # cost budget for the chosen hosted set
    languages: list[str] | None = None        # language slices that must each clear the floor


@dataclass
class Suite:
    name: str
    judge_preamble: str
    questions: list[Question]
    evaluators: list[dict]
    policy: Policy = field(default_factory=Policy)
    source: str = ""  # the YAML text; its hash is the suite version

    @classmethod
    def load(cls, path: str | Path) -> "Suite":
        return cls.from_yaml(Path(path).read_text())

    @classmethod
    def from_yaml(cls, text: str) -> "Suite":
        raw = yaml.safe_load(text)
        known = {f.name for f in fields(Policy)}
        policy = raw.get("policy", {})
        unknown = set(policy) - known
        if unknown:
            raise ValueError(f"unknown policy fields: {sorted(unknown)}")
        return cls(
            name=raw["name"],
            judge_preamble=raw["judge_preamble"],
            questions=[Question(name=k, **v) for k, v in raw["questions"].items()],
            evaluators=raw["evaluators"],
            policy=Policy(**policy),
            source=text,
        )

    @property
    def version(self) -> str:
        return hashlib.sha256(self.source.encode()).hexdigest()[:12]

    def question(self, name: str) -> Question:
        return next(q for q in self.questions if q.name == name)

    def ask_key(self, q: Question) -> str:
        """Key used in hosted prompts (inverted questions are asked under their positive name)."""
        return q.name.replace("_missing", "_given") if q.invert else q.name
