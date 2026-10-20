"""Labels and annotation queues (DESIGN.md §6.9).

Label sources stay apart: `human` (annotation queues), `synthetic` (planted by construction) and
`judge` (LLM labels, for distillation only). Calibration uses human and synthetic labels.

Queues fill automatically from conditions (Opik-style fill rules):
  {"type": "audit_disagreement"}                     routed decision != audit judge (drift.check)
  {"type": "tier_disagreement"}                      a shadow score disagrees with the decision
  {"type": "low_confidence", "band": [0.3, 0.7]}     decided with p inside the band
  {"type": "stratified", "per_lang": 20}             random conversations per language slice
"""

import datetime as dt
import hashlib
import uuid

from .engine import unit_hash
from .store.opensearch import OpenSearch
from .store.postgres import Jsonb, Postgres
from .worker import fetch_conversation


def new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# -- labels --------------------------------------------------------------------------------------
def create_label_set(pg: Postgres, tenant, suite_id, name) -> str:
    lid = new_id("ls")
    pg.exec("INSERT INTO label_sets (id, tenant_id, suite_id, name) VALUES (%s, %s, %s, %s)", lid, tenant, suite_id, name)
    return lid


def add_labels(pg: Postgres, label_set_id, conversation_id, labels: dict, source="human", annotator=None, lang=None):
    with pg.tx() as c:
        for check, value in labels.items():
            c.execute("""INSERT INTO labels (label_set_id, conversation_id, check_name, value, source, annotator, lang)
                         VALUES (%s, %s, %s, %s, %s, %s, %s)
                         ON CONFLICT (label_set_id, conversation_id, check_name, source)
                         DO UPDATE SET value=EXCLUDED.value, annotator=EXCLUDED.annotator, created_at=now()""",
                      (label_set_id, conversation_id, check, str(value), source, annotator, lang))


def labels_of(pg: Postgres, label_set_id, sources=("human", "synthetic")) -> dict:
    """{(conversation_id, check): value}; human labels win over synthetic ones."""
    rows = pg.all("SELECT conversation_id, check_name, value, source FROM labels WHERE label_set_id=%s AND source = ANY(%s)",
                  label_set_id, list(sources))
    out, rank = {}, {"judge": 0, "synthetic": 1, "human": 2}
    best = {}
    for r in rows:
        key = (r["conversation_id"], r["check_name"])
        if rank[r["source"]] >= best.get(key, -1):
            best[key] = rank[r["source"]]
            v = r["value"]
            out[key] = int(v) if v in ("0", "1") else v
    return out


def labelled_conversations(pg, os_, registry, tenant, label_set_id, sources=("human", "synthetic")):
    """Rebuild the labelled conversations from OpenSearch, for calibration."""
    from .conversation import build
    labels = labels_of(pg, label_set_id, sources)
    pool = registry.pool(tenant)
    convs = []
    for cid in sorted({c for c, _ in labels}):
        row = pg.one("SELECT kind FROM trace_tracker WHERE tenant_id=%s AND conversation_id=%s", tenant, cid)
        spans, content = fetch_conversation(os_, tenant, cid, row["kind"] if row else "session", pool)
        if not spans and not row:
            spans, content = fetch_conversation(os_, tenant, cid, "trace", pool)
        if spans:
            conv = build(cid, spans, content, tenant)
            conv["labels"] = {q: v for (c, q), v in labels.items() if c == cid}
            convs.append(conv)
    return convs, labels


# -- queues --------------------------------------------------------------------------------------
def create_queue(pg: Postgres, tenant, suite_id, name, checks, fill_rules=None) -> dict:
    qid = new_id("aq")
    lsid = create_label_set(pg, tenant, suite_id, f"queue:{name}")
    pg.exec("""INSERT INTO annotation_queues (id, tenant_id, suite_id, name, checks, label_set_id, fill_rules)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""", qid, tenant, suite_id, name, list(checks), lsid,
            Jsonb(fill_rules or []))
    return queue(pg, qid)


def queue(pg, qid):
    q = pg.one("SELECT * FROM annotation_queues WHERE id=%s", qid)
    if q is None:
        raise KeyError(qid)
    counts = pg.all("SELECT status, count(*) AS n FROM annotation_items WHERE queue_id=%s GROUP BY status", qid)
    q["counts"] = {r["status"]: r["n"] for r in counts}
    return q


def add_items(pg: Postgres, qid, conversation_ids, reason="manual", priority=0.0) -> int:
    n = 0
    for cid in conversation_ids:
        n += pg.exec("""INSERT INTO annotation_items (queue_id, conversation_id, reason, priority) VALUES (%s, %s, %s, %s)
                        ON CONFLICT DO NOTHING""", qid, cid, reason, priority)
    return n


def fill(pg: Postgres, os_: OpenSearch, registry, qid, window_days=1) -> dict:
    """Apply the queue's fill rules (other than audit_disagreement, which drift.check applies)."""
    q = queue(pg, qid)
    pool = registry.pool(q["tenant_id"])
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=window_days)).isoformat()
    base = [{"term": {"tenant_id": q["tenant_id"]}}, {"terms": {"check": q["checks"]}},
            {"range": {"timestamp": {"gte": since}}}]
    added = {}
    for rule in q["fill_rules"]:
        t = rule.get("type")
        if t == "low_confidence":
            lo, hi = rule.get("band", [0.3, 0.7])
            hits = os_.hits("scores", {"size": 10000, "_source": ["conversation_id"], "query": {"bool": {"filter": base + [
                {"term": {"decision": True}}, {"range": {"p": {"gt": lo, "lt": hi}}}]}}}, pool)
            added[t] = add_items(pg, qid, {h["conversation_id"] for h in hits}, t, 0.5)
        elif t == "tier_disagreement":
            hits = os_.hits("scores", {"size": 10000, "_source": ["conversation_id", "check", "value", "decision", "sample_reason"],
                                       "query": {"bool": {"filter": base + [{"terms": {"sample_reason": ["route", "shadow"]}}]}}}, pool)
            by = {}
            for h in hits:
                by.setdefault((h["conversation_id"], h["check"]), set()).add(h["value"])
            added[t] = add_items(pg, qid, {c for (c, _), vals in by.items() if len(vals) > 1}, t, 0.8)
        elif t == "stratified":
            per = int(rule.get("per_lang", 20))
            res = os_.search("traces", {"size": 0, "query": {"bool": {"filter": [
                {"term": {"tenant_id": q["tenant_id"]}}, {"range": {"evaluated_at": {"gte": since}}}]}},
                "aggs": {"lang": {"terms": {"field": "lang", "size": 50}, "aggs": {"s": {"top_hits": {
                    "size": per, "_source": ["conversation_id"],
                    "sort": [{"_script": {"type": "number", "order": "asc", "script": {
                        "source": "(doc['conversation_id'].value + params.seed).hashCode()", "params": {"seed": qid}}}}]}}}}}},
                pool)
            ids = {h["_source"]["conversation_id"] for b in res["aggregations"]["lang"]["buckets"] for h in b["s"]["hits"]["hits"]}
            added[t] = add_items(pg, qid, ids, t, 0.2)
    return added


def next_item(pg: Postgres, qid, annotator, lock_minutes=15):
    """Lock the next pending item (or one whose lock expired) for this annotator."""
    with pg.tx() as c:
        row = c.execute("""SELECT conversation_id FROM annotation_items
                           WHERE queue_id=%s AND (status='pending' OR (status='locked' AND locked_at < now() - make_interval(mins => %s)))
                           ORDER BY priority DESC, created_at LIMIT 1 FOR UPDATE SKIP LOCKED""", (qid, lock_minutes)).fetchone()
        if row is None:
            return None
        c.execute("""UPDATE annotation_items SET status='locked', locked_by=%s, locked_at=now()
                     WHERE queue_id=%s AND conversation_id=%s""", (annotator, qid, row["conversation_id"]))
    return pg.one("SELECT * FROM annotation_items WHERE queue_id=%s AND conversation_id=%s", qid, row["conversation_id"])


def submit(pg: Postgres, os_: OpenSearch, registry, qid, conversation_id, labels: dict, annotator, lang=None):
    """Store human labels and write them as annotation scores (so they show next to model scores)."""
    q = queue(pg, qid)
    unknown = set(labels) - set(q["checks"])
    if unknown:
        raise ValueError(f"checks not in this queue: {sorted(unknown)}")
    add_labels(pg, q["label_set_id"], conversation_id, labels, "human", annotator, lang)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    docs = []
    for check, value in labels.items():
        sid = hashlib.sha256(f"annotation|{q['label_set_id']}|{conversation_id}|{check}|{annotator}".encode()).hexdigest()
        binary = str(value) in ("0", "1")
        docs.append((sid, {"id": sid, "timestamp": now, "tenant_id": q["tenant_id"], "conversation_id": conversation_id,
                           "check": check, "question": check, "value": float(value) if binary else None,
                           "label": None if binary else str(value), "p": 1.0, "decision": False,
                           "sample_reason": "annotation", "annotator": annotator, "lang": lang,
                           "evaluator": {"id": "human", "kind": "human", "tier": 4, "version": annotator,
                                         "criteria_version": None}}, None))
    os_.bulk("scores", docs, registry.pool(q["tenant_id"]), op="index")
    pg.exec("UPDATE annotation_items SET status='done' WHERE queue_id=%s AND conversation_id=%s", qid, conversation_id)
    pg.audit(annotator, "annotation.submit", conversation_id, q["tenant_id"], queue=qid, labels=labels)


def skip(pg: Postgres, qid, conversation_id):
    pg.exec("UPDATE annotation_items SET status='skipped' WHERE queue_id=%s AND conversation_id=%s", qid, conversation_id)


def agreement_stats(pairs):
    """Cohen's kappa, accuracy and per-class precision/recall for (truth, predicted) pairs."""
    pairs = [(str(a), str(b)) for a, b in pairs]
    n = len(pairs)
    if not n:
        return {"n": 0}
    classes = sorted({x for p in pairs for x in p})
    po = sum(a == b for a, b in pairs) / n
    pe = sum((sum(a == c for a, _ in pairs) / n) * (sum(b == c for _, b in pairs) / n) for c in classes)
    per = {}
    for c in classes:
        tp = sum(a == c and b == c for a, b in pairs)
        pp, ap = sum(b == c for _, b in pairs), sum(a == c for a, _ in pairs)
        per[c] = {"precision": tp / pp if pp else None, "recall": tp / ap if ap else None, "support": ap}
    return {"n": n, "accuracy": po, "kappa": (po - pe) / (1 - pe) if pe < 1 else 1.0, "per_class": per}


__all__ = ["create_label_set", "add_labels", "labels_of", "labelled_conversations", "create_queue", "queue", "add_items",
           "fill", "next_item", "submit", "skip", "agreement_stats", "unit_hash"]
