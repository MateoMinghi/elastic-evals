"""Evaluator adapters. Every evaluator returns, per question, p = probability the problem is present
(binary checks) or {label: probability} (multi-class checks).

  rules        elastic_evals.rules functions; 0.0/1.0 are deterministic, values in between are heuristic
  local_head   sentence encoder + one logistic-regression head per question, trained during calibration
  jev          TypeSafe Jev via OpenRouter's Decisions API: one call answers every question (noul)
  llm          an LLM judge: one call answers every question as JSON booleans
"""

import hashlib
import importlib
import inspect
import json
import pickle
import time
from pathlib import Path

import numpy as np

from . import rules as rules_lib
from .providers import OpenRouter
from .suite import Suite
from .views import text_of, view

RANK = {"rules": 0, "local_head": 1, "jev": 2, "llm": 3}


class Evaluator:
    """Base adapter. Tiers: 0 deterministic, 1 local model, 2 hosted classifier, 3 LLM judge.
    Calibration offers at most one tier-3 evaluator per suite and treats hosted tier-2 evaluators like Jev."""
    hosted = False

    def __init__(self, cfg: dict, suite: Suite, client: OpenRouter | None = None):
        self.id, self.type, self.cfg, self.suite, self.client = cfg["id"], cfg["type"], cfg, suite, client
        self.rank = RANK[self.type]
        self.hosted = cfg.get("hosted", type(self).hosted)  # e.g. an LLM judge served inside the deployment

    @property
    def version(self) -> str:
        """Pinned identity recorded on every score: a model id, or a digest of the code or weights."""
        return self.cfg.get("model", self.type)


class RulesEvaluator(Evaluator):
    @property
    def version(self):
        files = {Path(inspect.getfile(resolve_rule(q.rule))) for q in self.suite.questions if q.rule}
        return "rules@" + hashlib.sha256(b"".join(f.read_bytes() for f in sorted(files))).hexdigest()[:12]

    def evaluate(self, conv, questions):
        t0 = time.perf_counter()
        out = {q.name: resolve_rule(q.rule)(conv) for q in questions if q.rule}
        return out, 0.0, time.perf_counter() - t0


class LocalHeadEvaluator(Evaluator):
    """Small CPU classifier: frozen multilingual encoder + per-question logistic regression."""

    _encoders = {}

    def __init__(self, cfg, suite, client=None):
        super().__init__(cfg, suite)
        self.encoder_name = cfg.get("encoder", "intfloat/multilingual-e5-small")
        self.heads = {}

    @property
    def version(self):
        digest = hashlib.sha256(pickle.dumps(sorted(self.heads.items(), key=lambda kv: kv[0]))).hexdigest()[:12]
        return f"{self.encoder_name}+heads@{digest}"

    def encoder(self):
        if self.encoder_name not in self._encoders:
            from sentence_transformers import SentenceTransformer
            self._encoders[self.encoder_name] = SentenceTransformer(self.encoder_name, device="cpu")
        return self._encoders[self.encoder_name]

    def embed(self, convs, q):
        texts = ["query: " + view(c, q.view) for c in convs]
        return self.encoder().encode(texts, batch_size=32, normalize_embeddings=True)

    @staticmethod
    def _head():
        from sklearn.linear_model import LogisticRegression
        return LogisticRegression(max_iter=2000, class_weight="balanced", C=4.0)

    def fit(self, convs, labels, questions, extra=None):
        """Train one head per question; return out-of-fold p for the labelled conversations.

        extra = (conversations, labels) used for training only, never for measuring: distillation
        trains on LLM-judge labels while thresholds and floors stay measured on human labels."""
        from sklearn.model_selection import KFold, StratifiedKFold
        oof = {}
        ex_convs, ex_labels = extra or ([], {})
        for q in questions:
            have = [c for c in convs if (c["id"], q.name) in labels]
            y = np.array([labels[(c["id"], q.name)] for c in have])
            ex = [c for c in ex_convs if (c["id"], q.name) in ex_labels]
            ey = np.array([ex_labels[(c["id"], q.name)] for c in ex])
            if len(set(y.tolist()) | set(ey.tolist())) < 2 or not len(have):
                continue
            x = self.embed(have, q)
            ex_x = self.embed(ex, q) if ex else np.zeros((0, x.shape[1]))

            def train(xi, yi):
                return self._head().fit(np.vstack([xi, ex_x]), np.concatenate([yi, ey]) if len(ey) else yi)

            self.heads[q.name] = train(x, y)
            smallest = min(np.unique(y, return_counts=True)[1])
            splitter = (StratifiedKFold(min(5, int(smallest)), shuffle=True, random_state=0) if smallest >= 2
                        else KFold(5, shuffle=True, random_state=0))
            for tr, va in splitter.split(x, y):
                if len(set(y[tr].tolist()) | set(ey.tolist())) < 2:
                    continue
                head = train(x[tr], y[tr])
                for i, pr in zip(va, head.predict_proba(x[va])):
                    oof[(have[i]["id"], q.name)] = self._out(q, head.classes_, pr)
        return oof

    @staticmethod
    def _out(q, classes, pr):
        if q.multiclass:
            return {str(c): float(v) for c, v in zip(classes, pr)}
        return float(pr[list(classes).index(1)]) if 1 in classes else 0.0

    def evaluate_batch(self, convs, questions):
        t0 = time.perf_counter()
        out = {c["id"]: {} for c in convs}
        for q in questions:
            if q.name in self.heads:
                head = self.heads[q.name]
                for c, pr in zip(convs, head.predict_proba(self.embed(convs, q))):
                    out[c["id"]][q.name] = self._out(q, head.classes_, pr)
        return out, (time.perf_counter() - t0) / max(1, len(convs))


class JevEvaluator(Evaluator):
    hosted = True

    def __init__(self, cfg, suite, client: OpenRouter):
        super().__init__(cfg, suite, client)
        self.model = cfg.get("model", "typesafe/jev-1.14")

    async def evaluate(self, conv, questions):
        qs = {self.suite.ask_key(q): ({"type": "choice", "instructions": q.ask, "criteria": q.classes} if q.multiclass
                                      else {"type": "noul", "instructions": q.ask}) for q in self.suite.questions}
        res = await self.client.decide(self.model, {"conversation": text_of(conv), "system_of_record": conv.get("record", {})},
                                       qs, tag=self.id)
        out = {}
        for q in questions:
            ans = res["answers"][self.suite.ask_key(q)]
            if q.multiclass:
                probs = ans.get("probabilities") or {ans["choice"]: 1.0}
                out[q.name] = {c: float(probs.get(c, 0.0)) for c in q.classes}
                continue
            p = float(ans["noul"])
            out[q.name] = 1 - p if q.invert else p
        return out, float(res["usage"]["cost"]), float(res.get("_latency_s") or 0)


class LLMJudgeEvaluator(Evaluator):
    hosted = True

    def __init__(self, cfg, suite, client: OpenRouter):
        super().__init__(cfg, suite, client)
        self.model = cfg["model"]

    def system_prompt(self):
        def item(q):
            if q.multiclass:
                return f'"{self.suite.ask_key(q)}" ({q.ask} Answer with one of: {", ".join(q.classes)})'
            return f'"{self.suite.ask_key(q)}" ({q.ask})'
        return self.suite.judge_preamble + ", ".join(item(q) for q in self.suite.questions)

    async def evaluate(self, conv, questions):
        params = {"temperature": 0, "max_tokens": 300, "response_format": {"type": "json_object"}}
        if "deepseek" in self.model:
            params["reasoning"] = {"enabled": False}
        elif self.cfg.get("reasoning_effort"):
            params["reasoning"] = {"effort": self.cfg["reasoning_effort"]}
        user = f"System of record: {json.dumps(conv.get('record', {}), ensure_ascii=False)}\n\nConversation:\n{text_of(conv)}"
        res = await self.client.chat(self.model, [{"role": "system", "content": self.system_prompt()},
                                                  {"role": "user", "content": user}], tag=self.id,
                                     url=self.cfg.get("base_url"), api_key_env=self.cfg.get("api_key_env", "OPENROUTER_API_KEY"),
                                     **params)
        content = res["choices"][0]["message"]["content"] or ""
        try:
            ans = json.loads(content[content.find("{"): content.rfind("}") + 1])
        except json.JSONDecodeError:
            ans = {}
        out = {}
        for q in questions:
            v = ans.get(self.suite.ask_key(q))
            if q.multiclass:
                v = {c.lower(): c for c in q.classes}.get(str(v).strip().lower())
                out[q.name] = {c: float(c == v) for c in q.classes} if v else None
                continue
            if isinstance(v, str):
                v = v.strip().lower() == "true"
            p = None if v is None else float(bool(v))
            out[q.name] = None if p is None else (1 - p if q.invert else p)
        return out, float((res.get("usage") or {}).get("cost") or 0), float(res.get("_latency_s") or 0)


def resolve_rule(name: str):
    """A rule is a function in elastic_evals.rules, or any importable 'package.module:function'."""
    if ":" in name:
        module, fn = name.split(":", 1)
        return getattr(importlib.import_module(module), fn)
    return getattr(rules_lib, name)


TYPES = {"rules": RulesEvaluator, "local_head": LocalHeadEvaluator, "jev": JevEvaluator, "llm": LLMJudgeEvaluator}


def register(type_name: str, cls, tier: int):
    """Plug in an evaluator adapter: `type: <type_name>` in a suite then builds `cls(cfg, suite, client)`.
    Set `hosted = True` on the class if it sends conversation text outside the deployment."""
    TYPES[type_name], RANK[type_name] = cls, tier


def build(suite: Suite, client: OpenRouter | None):
    evs = []
    for cfg in suite.evaluators:
        cls = TYPES.get(cfg["type"])
        if cls is None:
            raise ValueError(f"unknown evaluator type {cfg['type']!r}")
        ev = cls(cfg, suite, client)
        if ev.hosted and not suite.policy.allow_hosted:
            continue
        evs.append(ev)
    return sorted(evs, key=lambda e: e.rank)
