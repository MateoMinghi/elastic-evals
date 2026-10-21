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
    def __init__(self, suite, calibration, evaluators, audit: bool = True):
        main, obs = suite.split()
        self.obs_engine = None
        if obs is not None and calibration.get("observation"):
            from .evaluators import LocalHeadEvaluator as _Local, build
            client = next((e.client for e in evaluators if e.client is not None), None)
            obs_evs = build(obs, client)  # evaluators whose prompts list the observation checks only
            for e in obs_evs:
                twin = next((x for x in evaluators if x.id == e.id), None)
                if isinstance(e, _Local) and twin is not None:
                    e.heads = twin.heads
            self.obs_engine = Engine(obs, calibration["observation"], obs_evs, audit)
        suite = main
        self.suite, self.cal = suite, calibration
        self.routes = calibration["routes"]
        self.th = {q: {e: tuple(t) if t else None for e, t in th.items()} for q, th in calibration["thresholds"].items()}
        self.strictness = calibration.get("suite_choice", {}).get("strictness", {})
        self.cal_id = calibration.get("id", "uncalibrated")
        used = {e for r in self.routes.values() for e in r}
        audit = calibration.get("audit_judge", suite.policy.audit_judge) if suite.policy.allow_hosted and audit else None
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
        conv_key = conv["id"] + ("@" + conv["version"] if conv.get("version") else "")
        return {
            "id": score_id(self.cal_id, ev.version, conv_key, q.name, q.criteria_version, reason),
            "tenant_id": conv.get("tenant_id"), "conversation_id": conv["id"], "trace_id": conv.get("trace_id"),
            "observation_id": None, "session_id": conv.get("session_id"),
            "check": q.name, "question": q.name, "value": value, "p": top, "probs": probs, "label": label,
            "explanation": None, "decision": reason == "route", "sample_reason": reason,
            "conversation_version": conv.get("version"),
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
                if qs:
                    local_out, local_lat = ev.evaluate_batch(convs, qs)
        if self.suite.questions:
            results = await asyncio.gather(*(self._score(c, local_out, local_lat) for c in convs))
        else:
            results = [{"conversation_id": c["id"], "scores": [], "usd": 0.0, "audit_usd": 0.0, "ms": 0.0,
                        "evaluators_called": [], "decisions": {}} for c in convs]
        if self.obs_engine is not None:
            await self._observations(convs, results)
        return results

    async def _observations(self, convs, results):
        """Score the selected steps of each conversation and attach them as observation scores."""
        from .trajectory import items
        by_conv = {c["id"]: items(c, self.obs_engine.suite.questions) for c in convs}
        flat = [it for its in by_conv.values() for it in its]
        if not flat:
            for r in results:
                r["observations"] = {}
            return
        obs_results = await self.obs_engine.run_async(flat)
        by_item = {r["conversation_id"]: r for r in obs_results}
        for c, r in zip(convs, results):
            r["observations"] = {}
            for it in by_conv[c["id"]]:
                o = by_item[it["id"]]
                for s in o["scores"]:
                    if s["check"] not in it["applies"]:
                        continue
                    s["conversation_id"], s["observation_id"] = c["id"], it["observation_id"]
                    r["scores"].append(s)
                r["observations"][it["observation_id"]] = {k: v for k, v in o["decisions"].items() if k in it["applies"]}
                r["usd"] += o["usd"]
                r["audit_usd"] += o["audit_usd"]
                r["evaluators_called"] = sorted(set(r["evaluators_called"]) | set(o["evaluators_called"]))
            for check in self.obs_engine.suite.questions:
                vals = [d[check.name] for d in r["observations"].values() if check.name in d]
                if vals:
                    r["decisions"][check.name] = max(vals) if not check.multiclass else vals[-1]

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
