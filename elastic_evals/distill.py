"""Distillation (DESIGN.md §6.8, lesson L9): LLM labels train the small local heads.

  1. the teacher (an LLM judge of the suite) labels an unlabelled pool of conversations
  2. local heads train on human labels + teacher labels
  3. calibration measures them out-of-fold on the human labels only, and decides whether a
     head enters a route; the judge keeps the uncertain band and the audit

Teacher labels are stored with source "judge" and never used as calibration truth.
"""

import asyncio

from .calibrate import calibrate_async, final
from .evaluators import build


async def teacher_labels(suite, pool, teacher_id, client):
    teacher = next(e for e in build(suite, client) if e.id == teacher_id)
    results = await asyncio.gather(*(teacher.evaluate(c, suite.questions) for c in pool))
    labels, cost = {}, 0.0
    for c, (out, usd, _) in zip(pool, results):
        cost += usd
        for q in suite.questions:
            if out.get(q.name) is not None:
                labels[(c["id"], q.name)] = final(out[q.name], q)
    return labels, cost


async def distill_async(suite, labelled, labels, pool, teacher_id, client):
    judge, cost = await teacher_labels(suite, pool, teacher_id, client)
    cal, evaluators = await calibrate_async(suite, labelled, labels, client, extra=(pool, judge))
    cal["distillation"] = {"teacher": teacher_id, "pool": len(pool), "teacher_usd": cost}
    return cal, evaluators, judge


def distill(suite, labelled, labels, pool, teacher_id, client):
    """Returns (calibration, evaluators, teacher labels)."""
    return asyncio.run(distill_async(suite, labelled, labels, pool, teacher_id, client))
