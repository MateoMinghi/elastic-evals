"""Drift monitor and shadow comparison (DESIGN.md §6.3 step 6, §6.7).

  check()          live agreement between routed decisions and the audit judge, per check and
                   language, against the baseline frozen at promotion; a drop beyond `threshold`
                   (with at least `min_n` audited conversations) opens a drift alert, and the
                   disagreements go to the suite's annotation queues
  compare_shadow() the candidate (shadow) calibration against the active one on the same audited
                   conversations: promote when it agrees with the audit judge at least as well
"""

import datetime as dt
from collections import defaultdict

from .engine import drift_report
from .registry import Registry
from .store.opensearch import OpenSearch
from .store.postgres import Postgres


def _scores(os_: OpenSearch, pool, calibration_ids, reasons, since, conv_ids=None, tenant=None):
    filt = [{"terms": {"calibration_id": calibration_ids}}, {"terms": {"sample_reason": reasons}},
            {"range": {"timestamp": {"gte": since.isoformat()}}}]
    if tenant:
        filt.append({"term": {"tenant_id": tenant}})
    out = []
    if conv_ids is not None:
        ids = sorted(conv_ids)
        for i in range(0, len(ids), 1000):
            body = {"query": {"bool": {"filter": filt + [{"terms": {"conversation_id": ids[i:i + 1000]}}]}}, "size": 10000}
            out += os_.hits("scores", body, pool)
        return out
    return os_.hits("scores", {"query": {"bool": {"filter": filt}}, "size": 10000}, pool)


def _latest_version(scores):
    """Keep each conversation's latest version only (sessions are re-scored as they grow)."""
    latest = {}
    for s in scores:
        cur = latest.get(s["conversation_id"])
        if cur is None or s["timestamp"] > cur:
            latest[s["conversation_id"]] = s["timestamp"]
    return [s for s in scores if s["timestamp"] == latest[s["conversation_id"]]]


def agreement(os_, pool, tenant, calibration_id, since, decision_reasons=("route",), decision_cal=None):
    """Audit agreement of `decision_cal`'s decisions (default: the same calibration) on the
    conversations audited under `calibration_id`."""
    audits = _scores(os_, pool, [calibration_id], ["random_audit"], since, tenant=tenant)
    if not audits:
        return {}, {}, []
    convs = {s["conversation_id"] for s in audits}
    decisions = _scores(os_, pool, [decision_cal or calibration_id], list(decision_reasons), since, convs, tenant)
    merged = _latest_version(audits + [dict(s, decision=True) for s in decisions])
    by_check = drift_report(merged)
    by_lang = drift_report(merged, by_lang=True)
    # disagreements, for annotation
    per_conv = defaultdict(dict)
    for s in merged:
        per_conv[s["conversation_id"]].setdefault(s["check"], {})[s["sample_reason"] == "random_audit"] = (s["value"], s["label"])
    disagree = sorted({(cid, chk) for cid, checks in per_conv.items() for chk, v in checks.items()
                       if True in v and False in v and v[True] != v[False]})
    return by_check, by_lang, disagree


def check(pg: Postgres, os_: OpenSearch, registry: Registry, tenant: str, suite_id: str, window_days: float = 7,
          min_n: int = 50, threshold: float = 0.05, notify=None) -> dict:
    cal = registry.by_status(tenant, suite_id, "active")
    if cal is None:
        return {"status": "no active calibration"}
    pool = registry.pool(tenant)
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=window_days)
    by_check, by_lang, disagree = agreement(os_, pool, tenant, cal["id"], since)
    baseline = cal["baseline_agreement"] or {}
    alerts = []
    rows = [((chk, None), v, (baseline.get(chk) or {}).get("agreement")) for chk, v in by_check.items()]
    rows += [((chk, lang), v, ((baseline.get(chk) or {}).get("by_lang") or {}).get(lang, {}).get("agreement"))
             for (chk, lang), v in by_lang.items()]
    for (chk, lang), v, base in rows:
        if base is None or v["n"] < min_n or v["agreement"] >= base - threshold:
            continue
        exists = pg.one("""SELECT id FROM drift_alerts WHERE calibration_id=%s AND check_name=%s
                           AND lang IS NOT DISTINCT FROM %s AND status='open'""", cal["id"], chk, lang)
        if exists:
            continue
        alert = pg.one("""INSERT INTO drift_alerts (tenant_id, suite_id, calibration_id, check_name, lang, baseline, observed, n)
                          VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
                       tenant, suite_id, cal["id"], chk, lang, base, v["agreement"], v["n"])
        alerts.append(alert)
        if notify:
            notify(alert)
    queued = 0
    if alerts or disagree:
        for q in pg.all("SELECT id, checks FROM annotation_queues WHERE tenant_id=%s AND suite_id=%s "
                        "AND fill_rules @> '[{\"type\": \"audit_disagreement\"}]'", tenant, suite_id):
            for cid, chk in disagree:
                if chk in q["checks"]:
                    queued += pg.exec("""INSERT INTO annotation_items (queue_id, conversation_id, reason, priority)
                                         VALUES (%s, %s, 'audit_disagreement', 1) ON CONFLICT DO NOTHING""", q["id"], cid)
    return {"calibration_id": cal["id"], "agreement": by_check,
            "by_lang": {f"{c}/{lg}": v for (c, lg), v in by_lang.items()},
            "baseline": baseline, "alerts": alerts, "disagreements": len(disagree), "queued_for_annotation": queued}


def compare_shadow(os_: OpenSearch, registry: Registry, tenant: str, suite_id: str, window_days: float = 7,
                   min_n: int = 30) -> dict:
    """Agreement with the audit judge of the shadow candidate and of the active calibration, on
    the same audited conversations. `promote` is True when the candidate is at least as good on
    every check with enough data."""
    active, shadow = (registry.by_status(tenant, suite_id, s) for s in ("active", "shadow"))
    if not active or not shadow:
        return {"status": "need an active and a shadow calibration"}
    pool = registry.pool(tenant)
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=window_days)
    act, _, _ = agreement(os_, pool, tenant, active["id"], since)
    cand, _, _ = agreement(os_, pool, tenant, active["id"], since, ("candidate",), shadow["id"])
    checks = sorted(set(act) | set(cand))
    rows = {c: {"active": act.get(c), "candidate": cand.get(c)} for c in checks}
    enough = all((r["candidate"] or {}).get("n", 0) >= min_n for r in rows.values())
    better = all((r["candidate"] or {}).get("agreement", 0) >= (r["active"] or {}).get("agreement", 0) - 1e-9
                 for r in rows.values())
    return {"active": active["id"], "candidate": shadow["id"], "checks": rows, "enough_data": enough,
            "promote": bool(rows) and enough and better,
            "candidate_agreement": {c: r["candidate"]["agreement"] for c, r in rows.items() if r["candidate"]}}
