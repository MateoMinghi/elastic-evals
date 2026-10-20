"""Evaluator adapters. Every evaluator returns, per question, p = probability the problem is present.

  rules        elastic_evals.rules functions; 0.0/1.0 are deterministic, values in between are heuristic
  local_head   sentence encoder + one logistic-regression head per question, trained during calibration
  jev          TypeSafe Jev via OpenRouter's Decisions API: one call answers every question (noul)
  llm          an LLM judge: one call answers every question as JSON booleans
"""

import json
import time

import numpy as np

from . import rules as rules_lib
from .providers import OpenRouter
from .suite import Suite
from .views import text_of, view

RANK = {"rules": 0, "local_head": 1, "jev": 2, "llm": 3}


class Evaluator:
    hosted = False

    def __init__(self, cfg: dict, suite: Suite):
        self.id, self.type, self.cfg, self.suite = cfg["id"], cfg["type"], cfg, suite
        self.rank = RANK[self.type]


class RulesEvaluator(Evaluator):
    def evaluate(self, conv, questions):
        t0 = time.perf_counter()
        out = {q.name: getattr(rules_lib, q.rule)(conv) for q in questions if q.rule}
        return out, 0.0, time.perf_counter() - t0


class LocalHeadEvaluator(Evaluator):
    """Small CPU classifier: frozen multilingual encoder + per-question logistic regression."""

    _encoders = {}

    def __init__(self, cfg, suite):
        super().__init__(cfg, suite)
        self.encoder_name = cfg.get("encoder", "intfloat/multilingual-e5-small")
        self.heads = {}

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

    def fit(self, convs, labels, questions):
        """Train one head per question; return out-of-fold p for the training conversations."""
        from sklearn.model_selection import StratifiedKFold
        oof = {}
        for q in questions:
            x, y = self.embed(convs, q), np.array([labels[(c["id"], q.name)] for c in convs])
            if len(set(y)) < 2:
                continue
            self.heads[q.name] = self._head().fit(x, y)
            p = np.zeros(len(convs))
            folds = min(5, int(min(np.bincount(y))))
            for tr, va in StratifiedKFold(folds, shuffle=True, random_state=0).split(x, y):
                p[va] = self._head().fit(x[tr], y[tr]).predict_proba(x[va])[:, 1]
            for c, pi in zip(convs, p):
                oof[(c["id"], q.name)] = float(pi)
        return oof

    def evaluate_batch(self, convs, questions):
        t0 = time.perf_counter()
        out = {c["id"]: {} for c in convs}
        for q in questions:
            if q.name in self.heads:
                p = self.heads[q.name].predict_proba(self.embed(convs, q))[:, 1]
                for c, pi in zip(convs, p):
                    out[c["id"]][q.name] = float(pi)
        return out, (time.perf_counter() - t0) / max(1, len(convs))


class JevEvaluator(Evaluator):
    hosted = True

    def __init__(self, cfg, suite, client: OpenRouter):
        super().__init__(cfg, suite)
        self.client, self.model = client, cfg.get("model", "typesafe/jev-1.14")

    async def evaluate(self, conv, questions):
        qs = {self.suite.ask_key(q): {"type": "noul", "instructions": q.ask} for q in self.suite.questions}
        res = await self.client.decide(self.model, {"conversation": text_of(conv), "system_of_record": conv["record"]},
                                       qs, tag=self.id)
        out = {}
        for q in questions:
            p = float(res["answers"][self.suite.ask_key(q)]["noul"])
            out[q.name] = 1 - p if q.invert else p
        return out, float(res["usage"]["cost"]), float(res.get("_latency_s") or 0)


class LLMJudgeEvaluator(Evaluator):
    hosted = True

    def __init__(self, cfg, suite, client: OpenRouter):
        super().__init__(cfg, suite)
        self.client, self.model = client, cfg["model"]

    def system_prompt(self):
        return self.suite.judge_preamble + ", ".join(f'"{self.suite.ask_key(q)}" ({q.ask})' for q in self.suite.questions)

    async def evaluate(self, conv, questions):
        params = {"temperature": 0, "max_tokens": 300, "response_format": {"type": "json_object"}}
        if "deepseek" in self.model:
            params["reasoning"] = {"enabled": False}
        elif self.cfg.get("reasoning_effort"):
            params["reasoning"] = {"effort": self.cfg["reasoning_effort"]}
        user = f"System of record: {json.dumps(conv['record'], ensure_ascii=False)}\n\nConversation:\n{text_of(conv)}"
        res = await self.client.chat(self.model, [{"role": "system", "content": self.system_prompt()},
                                                  {"role": "user", "content": user}], tag=self.id, **params)
        content = res["choices"][0]["message"]["content"] or ""
        try:
            ans = json.loads(content[content.find("{"): content.rfind("}") + 1])
        except json.JSONDecodeError:
            ans = {}
        out = {}
        for q in questions:
            v = ans.get(self.suite.ask_key(q))
            if isinstance(v, str):
                v = v.strip().lower() == "true"
            p = None if v is None else float(bool(v))
            out[q.name] = None if p is None else (1 - p if q.invert else p)
        return out, float((res.get("usage") or {}).get("cost") or 0), float(res.get("_latency_s") or 0)


def build(suite: Suite, client: OpenRouter | None):
    evs = []
    for cfg in suite.evaluators:
        t = cfg["type"]
        if t in ("jev", "llm") and not suite.policy.allow_hosted:
            continue
        if t == "rules":
            evs.append(RulesEvaluator(cfg, suite))
        elif t == "local_head":
            evs.append(LocalHeadEvaluator(cfg, suite))
        elif t == "jev":
            evs.append(JevEvaluator(cfg, suite, client))
        elif t == "llm":
            evs.append(LLMJudgeEvaluator(cfg, suite, client))
        else:
            raise ValueError(f"unknown evaluator type {t!r}")
    return sorted(evs, key=lambda e: e.rank)
