"""Runtime: score conversations along the calibrated routes, escalating per conversation.

Evaluators run in cost order. A conversation reaches a hosted evaluator only if at least one
of its unresolved questions has that evaluator next on its route, and then a single call
answers every question; answers that are not needed are kept as free "shadow" scores.
A deterministic random slice of conversations is also re-scored by the audit judge.
"""

import asyncio
import hashlib

from .calibrate import confident, decide
from .evaluators import LocalHeadEvaluator


class Engine:
    def __init__(self, suite, calibration, evaluators):
        self.suite, self.cal = suite, calibration
        self.routes, self.th = calibration["routes"], calibration["thresholds"]
        used = {e for r in self.routes.values() for e in r}
        audit = suite.policy.audit_judge if suite.policy.allow_hosted else None
        self.evaluators = [e for e in evaluators if e.id in used]
        self.audit = next((e for e in evaluators if e.id == audit), None)

    def _audited(self, cid):
        h = int(hashlib.sha256(cid.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
        return self.audit is not None and h < self.suite.policy.audit_rate

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
            for q in questions:
                p = out.get(q.name)
                route = self.routes[q.name]
                if q.name in unresolved and ev.id in route:
                    if route[-1] == ev.id or confident(p, self.th[q.name].get(ev.id)):
                        unresolved.discard(q.name)
                        scores.append(self._record(cid, q.name, ev, p, "route"))
                elif ev.hosted and p is not None:
                    scores.append(self._record(cid, q.name, ev, p, "shadow"))
        audit_cost = 0.0
        if self._audited(cid):
            out, audit_cost, _ = await self.audit.evaluate(conv, questions)
            scores += [self._record(cid, q, self.audit, p, "random_audit") for q, p in out.items() if p is not None]
        return {"conversation_id": cid, "scores": scores, "usd": cost, "audit_usd": audit_cost,
                "ms": 1000 * latency, "evaluators_called": called}

    @staticmethod
    def _record(cid, question, ev, p, reason):
        return {"conversation_id": cid, "question": question, "value": decide(p) if p is not None else 0,
                "p": p, "evaluator": {"id": ev.id, "kind": ev.type, "tier": ev.rank},
                "sample_reason": reason, "decision": reason == "route"}

    async def run_async(self, convs):
        local_out, local_lat = {}, 0.0
        for ev in self.evaluators:
            if isinstance(ev, LocalHeadEvaluator):
                qs = [q for q in self.suite.questions if ev.id in self.routes[q.name]]
                local_out, local_lat = ev.evaluate_batch(convs, qs)
        return await asyncio.gather(*(self._score(c, local_out, local_lat) for c in convs))

    def run(self, convs):
        return asyncio.run(self.run_async(convs))


def drift_report(results):
    """Agreement between routed decisions and the audit judge, per question, on audited conversations.
    A drop against the calibration-time agreement means the cheap layers no longer fit the traffic."""
    agree = {}
    for r in results:
        audit = {s["question"]: s["value"] for s in r["scores"] if s["sample_reason"] == "random_audit"}
        for s in r["scores"]:
            if s["decision"] and s["question"] in audit:
                agree.setdefault(s["question"], []).append(s["value"] == audit[s["question"]])
    return {q: {"n": len(v), "agreement": sum(v) / len(v)} for q, v in agree.items()}
