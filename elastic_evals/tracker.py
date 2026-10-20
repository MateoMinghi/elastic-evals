"""Trace-complete tracker (DESIGN.md §5.2, §6.5): decides when a conversation is ready to score.

Consumes the same Kafka topic as ingest (its own consumer group), reads only ids, parents and
timestamps, and upserts one `trace_tracker` row per conversation per poll:

  trace     (no gen_ai.conversation.id)  due `trace_idle_s` after its root span ended; a trace
            whose root never arrives is due after `max_wait_s`
  session   (gen_ai.conversation.id set) due `session_idle_s` after its latest span, and re-opened
            when new turns arrive after it was evaluated

Spans marked ee.internal (evaluator traffic) are never tracked, so evaluations are never evaluated.
Offsets are committed after the upsert: at-least-once, and the upsert is idempotent enough
(re-counting spans only delays a conversation).
"""

import asyncio
import datetime as dt
import logging
import time
from dataclasses import dataclass, field

from opentelemetry.proto.trace.v1 import trace_pb2

from .store.postgres import Postgres

log = logging.getLogger("elastic_evals.tracker")


@dataclass
class Timing:
    trace_idle_s: float = 30
    session_idle_s: float = 300
    max_wait_s: float = 3600
    per_agent: dict = field(default_factory=dict)  # agent -> {"trace_idle_s": .., "session_idle_s": ..}

    def get(self, agent, key):
        return (self.per_agent.get(agent) or {}).get(key, getattr(self, key))


def _str(attrs, key):
    for kv in attrs:
        if kv.key == key:
            v = kv.value
            return v.string_value or (str(v.int_value) if v.HasField("int_value") else "")
    return ""


def _bool(attrs, key):
    return any(kv.key == key and kv.value.bool_value for kv in attrs)


class Summarizer:
    """Turns OTLP messages into per-conversation summaries.

    Usually only the root span carries gen_ai.conversation.id, and children end (and arrive)
    before their root. So spans are first grouped by trace; a trace is attributed to a session as
    soon as any of its spans names one, or to itself once its root arrives without one. Traces
    still undecided after `max_wait_s` are flushed as plain traces.
    """

    def __init__(self, max_wait_s: float = 3600, memory: int = 200_000):
        self.max_wait_s, self.memory = max_wait_s, memory
        self.trace_conv = {}   # (tenant, trace_id) -> conversation id (session id or trace id)
        self.pending = {}      # (tenant, trace_id) -> summary of spans waiting for their conversation

    def feed(self, payloads, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        out = {}

        def add(tenant, cid, kind, s):
            o = out.setdefault((tenant, cid), {"kind": kind, "agent": None, "root_ended": False, "n": 0})
            o["n"] += s["n"]
            o["agent"] = o["agent"] or s["agent"]
            o["root_ended"] = o["root_ended"] or s["root_ended"]
            if kind == "session":
                o["kind"] = "session"

        for raw in payloads:
            data = trace_pb2.TracesData()
            data.ParseFromString(raw)
            for rs in data.resource_spans:
                tenant = _str(rs.resource.attributes, "ee.tenant_id")
                if not tenant:
                    continue
                for ss in rs.scope_spans:
                    for sp in ss.spans:
                        if _bool(sp.attributes, "ee.internal"):
                            continue
                        tkey = (tenant, sp.trace_id.hex())
                        p = self.pending.setdefault(tkey, {"n": 0, "agent": None, "root_ended": False, "session": None,
                                                           "since": now})
                        p["n"] += 1
                        p["agent"] = p["agent"] or _str(sp.attributes, "gen_ai.agent.name") or None
                        p["session"] = p["session"] or _str(sp.attributes, "gen_ai.conversation.id") or None
                        if not sp.parent_span_id:
                            p["root_ended"] = True
        for tkey, p in list(self.pending.items()):
            cid = self.trace_conv.get(tkey) or p["session"] or (tkey[1] if p["root_ended"] else None)
            if cid is None and now - p["since"] >= self.max_wait_s:
                cid = tkey[1]
            if cid is None:
                continue
            self.trace_conv[tkey] = cid
            add(tkey[0], cid, "trace" if cid == tkey[1] else "session", p)
            del self.pending[tkey]
        if len(self.trace_conv) > self.memory:  # forget the oldest half
            for k in list(self.trace_conv)[: self.memory // 2]:
                del self.trace_conv[k]
        return out


def summarize(payloads) -> dict:
    """One-shot summary (every trace in `payloads` is complete)."""
    return Summarizer(max_wait_s=0).feed(payloads)


UPSERT = """
INSERT INTO trace_tracker (tenant_id, conversation_id, kind, agent_name, last_span_at, root_ended, due_at, n_spans)
VALUES (%(tenant)s, %(cid)s, %(kind)s, %(agent)s, %(now)s, %(root_ended)s,
        CASE WHEN %(kind)s = 'session' THEN %(due_session)s WHEN %(root_ended)s THEN %(due_root)s ELSE %(due_open)s END,
        %(n)s)
ON CONFLICT (tenant_id, conversation_id) DO UPDATE SET
    kind = CASE WHEN EXCLUDED.kind = 'session' THEN 'session' ELSE trace_tracker.kind END,
    agent_name = coalesce(trace_tracker.agent_name, EXCLUDED.agent_name),
    last_span_at = greatest(trace_tracker.last_span_at, EXCLUDED.last_span_at),
    root_ended = trace_tracker.root_ended OR EXCLUDED.root_ended,
    n_spans = trace_tracker.n_spans + EXCLUDED.n_spans,
    due_at = CASE WHEN trace_tracker.kind = 'session' OR EXCLUDED.kind = 'session' THEN %(due_session)s
                  WHEN trace_tracker.root_ended OR EXCLUDED.root_ended THEN %(due_root)s
                  ELSE greatest(trace_tracker.due_at, %(due_open)s) END,
    status = CASE WHEN trace_tracker.status IN ('evaluated', 'error') THEN 'open' ELSE trace_tracker.status END,
    attempts = CASE WHEN trace_tracker.status IN ('evaluated', 'error') THEN 0 ELSE trace_tracker.attempts END
"""


def apply(pg: Postgres, summary: dict, timing: Timing, now: dt.datetime | None = None):
    now = now or dt.datetime.now(dt.timezone.utc)
    rows = []
    for (tenant, cid), s in summary.items():
        agent = s["agent"]
        rows.append({"tenant": tenant, "cid": cid, "kind": s["kind"], "agent": agent, "now": now,
                     "root_ended": s["root_ended"], "n": s["n"],
                     "due_session": now + dt.timedelta(seconds=timing.get(agent, "session_idle_s")),
                     "due_root": now + dt.timedelta(seconds=timing.get(agent, "trace_idle_s")),
                     "due_open": now + dt.timedelta(seconds=timing.max_wait_s)})
    if rows:
        with pg.tx() as c, c.cursor() as cur:
            cur.executemany(UPSERT, rows)
    return len(rows)


async def run(pg: Postgres, brokers: str, timing: Timing, topic: str = "ee-spans", group: str = "ee-tracker",
              stop: asyncio.Event | None = None):
    from aiokafka import AIOKafkaConsumer
    consumer = AIOKafkaConsumer(topic, bootstrap_servers=brokers, group_id=group, enable_auto_commit=False,
                                auto_offset_reset="earliest", max_poll_records=2000)
    await consumer.start()
    summarizer = Summarizer(timing.max_wait_s)
    try:
        while not (stop and stop.is_set()):
            batches = await consumer.getmany(timeout_ms=500)
            payloads = [m.value for msgs in batches.values() for m in msgs]
            summary = summarizer.feed(payloads)
            if summary:
                n = await asyncio.to_thread(apply, pg, summary, timing)
                log.info("tracked %d conversations from %d messages", n, len(payloads))
            if payloads:
                # spans still pending (trace not yet attributed) are not durable across a restart;
                # their root span, when it arrives, still tracks the conversation
                await consumer.commit()
    finally:
        await consumer.stop()
