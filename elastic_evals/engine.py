"""Runtime: score conversations along the calibrated routes, escalating per conversation.

Evaluators run in cost order. A conversation reaches a hosted evaluator only if at least one
of its unresolved questions has that evaluator next on its route, and then a single call
answers every question; answers that are not needed are kept as free "shadow" scores.
A deterministic random slice of conversations is also re-scored by the audit judge.

Every score is a self-contained record (DESIGN.md §6.6) with a deterministic id, so writing
the same score twice is idempotent.
"""

import asyncio
import hashlib

from .calibrate import confident, fallback, final
from .evaluators import LocalHeadEvaluator


def score_id(calibration_id, evaluator_version, conversation_id, check, criteria_version, sample_reason):
    key = "|".join(map(str, (calibration_id, evaluator_version, conversation_id, check, criteria_version, sample_reason)))
    return hashlib.sha256(key.encode()).hexdigest()


def unit_hash(key: str) -> float:
    """Deterministic value in [0, 1] for reproducible sampling (Langfuse-style sha256 sampling)."""
    return int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


class Engine:
    def __init__(self, suite, calibration, evaluators):
        self.suite, self.cal = suite, calibration
        self.routes = calibration["routes"]
        self.th = {q: {e: tuple(t) if t else None for e, t in th.items()} for q, th in calibration["thresholds"].items()}
        self.strictness = calibration.get("suite_choice", {}).get("strictness", {})
        self.cal_id = calibration.get("id", "uncalibrated")
        used = {e for r in self.routes.values() for e in r}
        audit = suite.policy.audit_judge if suite.policy.allow_hosted else None
        self.evaluators = [e for e in evaluators if e.id in used]
        self.audit = next((e for e in evaluators if e.id == audit), None)

    def _audited(self, cid):
        return self.audit is not None and unit_hash(cid) < self.suite.policy.audit_rate

    async def _score(self, conv, local_out, local_lat):
        cid, questions = conv["id"], self.suite.questions
        unresolved = {q.name for q in questions}
        scores, cost, latency, called = [], 0.0, 0.0, []
        for ev in self.evaluators:
            need = [q for q in questions if q.name in unresolved and ev.id in self.routes[q.name]]
            if not need:
                continue
            if isinstance(ev, LocalHeadEvaluator):
                out, cst, lt = local_out.get(cid, {}), 0.0, local_lat
            elif ev.hosted:
                out, cst, lt = await ev.evaluate(conv, questions)
            else:
                out, cst, lt = ev.evaluate(conv, questions)
            cost, latency = cost + cst, latency + lt
            called.append(ev.id)
            answered = [q for q in questions if out.get(q.name) is not None] or need
            share = cst / len(answered)
            for q in questions:
                p = out.get(q.name)
                route = self.routes[q.name]
                if q.name in unresolved and ev.id in route:
                    if route[-1] == ev.id or confident(p, self.th[q.name].get(ev.id)):
                        unresolved.discard(q.name)
                        scores.append(self._record(conv, q, ev, p, "route", share, lt))
                elif ev.hosted and p is not None:
                    scores.append(self._record(conv, q, ev, p, "shadow", share, lt))
        audit_cost = 0.0
        if self._audited(cid):
            out, audit_cost, lt = await self.audit.evaluate(conv, questions)
            answered = [q for q in questions if out.get(q.name) is not None]
            for q in answered:
                scores.append(self._record(conv, q, self.audit, out[q.name], "random_audit",
                                           audit_cost / len(answered), lt))
        return {"conversation_id": cid, "scores": scores, "usd": cost, "audit_usd": audit_cost,
                "ms": 1000 * latency, "evaluators_called": called,
                "decisions": {s["check"]: s["label"] if s["value"] is None else s["value"]
                              for s in scores if s["decision"]}}

    def _record(self, conv, q, ev, p, reason, cost, latency):
        if q.multiclass:
            label = final(p, q)
            value, probs = None, (p or {})
            top = probs.get(label) if label is not None else None
        else:
            label, value = None, final(p, q) if p is not None else fallback(q)
            probs = {"yes": p, "no": 1 - p} if p is not None else {}
            top = p
        ctx = conv.get("trace_ctx") or {}
        exp = conv.get("experiment") or {}
        return {
            "id": score_id(self.cal_id, ev.version, conv["id"], q.name, q.criteria_version, reason),
            "tenant_id": conv.get("tenant_id"), "conversation_id": conv["id"], "trace_id": conv.get("trace_id"),
            "observation_id": None, "session_id": conv.get("session_id"),
            "check": q.name, "question": q.name, "value": value, "p": top, "probs": probs, "label": label,
            "explanation": None, "decision": reason == "route", "sample_reason": reason,
            "evaluator": {"id": ev.id, "kind": ev.type, "tier": ev.rank, "version": ev.version,
                          "criteria_version": q.criteria_version},
            "calibration_id": self.cal_id, "route": self.routes.get(q.name, []),
            "strictness": self.strictness.get(q.name), "critical": q.critical,
            "cost_usd": cost, "latency_ms": 1000 * latency, "lang": conv.get("lang"),
            "experiment": {"run_id": exp.get("run_id"), "item_id": exp.get("item_id")},
            "trace_ctx": {k: ctx.get(k) for k in ("agent", "flow", "model", "tags", "environment", "release")},
            "supersedes": None,
        }

    async def run_async(self, convs):
        local_out, local_lat = {}, 0.0
        for ev in self.evaluators:
            if isinstance(ev, LocalHeadEvaluator):
                qs = [q for q in self.suite.questions if ev.id in self.routes[q.name]]
                local_out, local_lat = ev.evaluate_batch(convs, qs)
        return await asyncio.gather(*(self._score(c, local_out, local_lat) for c in convs))

    def run(self, convs):
        return asyncio.run(self.run_async(convs))


def drift_report(results, by_lang=False):
    """Agreement between routed decisions and the audit judge, per question (and language), on
    audited conversations. A drop against the calibration-time agreement means the cheap layers
    no longer fit the traffic."""
    groups = {}
    for r in results:  # engine results, or a flat list of score records
        for s in (r["scores"] if "scores" in r else [r]):
            groups.setdefault(s["conversation_id"], []).append(s)
    agree = {}
    for scores in groups.values():
        audit = {s["check"]: (s["value"], s["label"]) for s in scores if s["sample_reason"] == "random_audit"}
        for s in scores:
            if s["decision"] and s["check"] in audit:
                key = (s["check"], s.get("lang")) if by_lang else s["check"]
                agree.setdefault(key, []).append((s["value"], s["label"]) == audit[s["check"]])
    return {k: {"n": len(v), "agreement": sum(v) / len(v)} for k, v in agree.items()}
