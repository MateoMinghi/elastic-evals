"""Build the engine's conversation (id, lang, turns, record) from stored spans.

Input: `obs` documents (one per span) and `stage` documents (full content) of one conversation:
a trace, or every trace of a session (`gen_ai.conversation.id`).

Two ways to recover the turns, chosen automatically:
  events   an agent span (`invoke_agent`) carries each turn's user input; chat spans carry the
           assistant output; tool spans carry the tool calls. Turns are ordered by span start.
  history  no agent span carries input: the last chat span's input history plus its output,
           followed by any tool spans that ran after it.

The domain record comes from `ee.record` (inline JSON) or `ee.record.*` references; a suite can
resolve references against the system of record with a `record_resolver`.
"""

import calendar
import hashlib
import json
import re

_TS = re.compile(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)?$")


def ns(ts: str) -> int:
    """RFC 3339 timestamp with up to nanosecond precision -> integer nanoseconds (UTC).
    String comparison is wrong here: Go trims trailing zeros from the fraction."""
    m = _TS.match(ts)
    if not m:
        raise ValueError(f"bad timestamp {ts!r}")
    y, mo, d, h, mi, se, frac, tz = m.groups()
    secs = calendar.timegm((int(y), int(mo), int(d), int(h), int(mi), int(se)))
    if tz and tz != "Z":
        sign = 1 if tz[0] == "+" else -1
        secs -= sign * (int(tz[1:3]) * 3600 + int(tz[4:6]) * 60)
    return secs * 1_000_000_000 + int((frac or "0").ljust(9, "0"))


def _content(stage_by_span, span):
    return (stage_by_span.get(span["span_id"]) or {}).get("raw") or {}


def _messages(v):
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except json.JSONDecodeError:
            return [{"role": "user", "parts": [{"type": "text", "content": v}]}]
    return v if isinstance(v, list) else []


def _texts(msg):
    return [p.get("content", "") for p in msg.get("parts", []) if p.get("type") == "text" and p.get("content")]


def _tool_turn(span, content):
    args = content.get("tool_call_arguments", {})
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {"input": args}
    result = content.get("tool_call_result", "")
    if not isinstance(result, str):
        result = json.dumps(result, ensure_ascii=False)
    return {"role": "tool", "name": span.get("tool_name") or span["name"], "args": args, "result": result}


def _from_history(spans, stage_by_span):
    chats = [s for s in spans if s.get("type") == "GENERATION" and _content(stage_by_span, s)]
    if not chats:
        return [_tool_turn(s, _content(stage_by_span, s)) for s in spans if s.get("type") == "TOOL"]
    last = chats[-1]
    c = _content(stage_by_span, last)
    turns = []
    for m in _messages(c.get("input_messages")) + _messages(c.get("output_messages")):
        role = m.get("role")
        if role in ("user", "assistant"):
            turns += [{"role": role, "text": t} for t in _texts(m)]
        elif role == "tool":
            pass  # tool turns come from the tool_call parts below, paired with their responses
        for p in m.get("parts", []):
            if p.get("type") == "tool_call":
                turns.append({"role": "tool", "name": p.get("name", ""), "args": p.get("arguments") or {},
                              "result": "", "_id": p.get("id")})
            elif p.get("type") == "tool_call_response":
                for t in reversed(turns):
                    if t["role"] == "tool" and t.get("_id") == p.get("id"):
                        r = p.get("response", "")
                        t["result"] = r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)
                        break
    for t in turns:
        t.pop("_id", None)
    after = [s for s in spans if s.get("type") == "TOOL" and ns(s["start_time"]) > ns(last["end_time"])]
    return turns + [_tool_turn(s, _content(stage_by_span, s)) for s in after]


def _from_events(spans, stage_by_span):
    turns = []
    for s in spans:
        c = _content(stage_by_span, s)
        if s.get("type") == "AGENT":
            for m in _messages(c.get("input_messages")):
                if m.get("role") == "user":
                    turns += [{"role": "user", "text": t} for t in _texts(m)]
        elif s.get("type") == "GENERATION":
            for m in _messages(c.get("output_messages")):
                if m.get("role") == "assistant":
                    turns += [{"role": "assistant", "text": t} for t in _texts(m)]
        elif s.get("type") == "TOOL":
            turns.append(_tool_turn(s, c))
    return turns


def _trajectory(spans, stage_by_span):
    steps = []
    for s in spans:
        c = _content(stage_by_span, s)
        step = {"span_id": s["span_id"], "type": s.get("type", "SPAN"), "name": s["name"], "status": s.get("status"),
                "error_type": s.get("error_type"), "duration_ms": s.get("duration_ms")}
        if step["type"] == "TOOL":
            t = _tool_turn(s, c)
            step.update(tool=t["name"], args=t["args"], result=t["result"])
        elif step["type"] == "GENERATION":
            out = [t for m in _messages(c.get("output_messages")) for t in _texts(m)]
            calls = [p.get("name") for m in _messages(c.get("output_messages")) for p in m.get("parts", [])
                     if p.get("type") == "tool_call"]
            step.update(model=s.get("response_model") or s.get("request_model"),
                        output=" ".join(out) or (f"calls {', '.join(calls)}" if calls else ""),
                        input_tokens=(s.get("usage") or {}).get("input_tokens"))
        steps.append({k: v for k, v in step.items() if v is not None})
    return steps


def _record(spans):
    record = {}
    for s in spans:
        inline = (s.get("attributes") or {}).get("ee.record")
        if isinstance(inline, str):
            try:
                record.update(json.loads(inline))
            except json.JSONDecodeError:
                pass
        elif isinstance(inline, dict):
            record.update(inline)
        record.update(s.get("record_refs") or {})
    return record


def build(conversation_id: str, spans: list[dict], stage: list[dict], tenant_id: str | None = None,
          record_resolver=None) -> dict:
    spans = sorted(spans, key=lambda s: (ns(s["start_time"]), s.get("parent_span_id") is not None))
    stage_by_span = {d["span_id"]: d for d in stage}
    agents = [s for s in spans if s.get("type") == "AGENT"]
    has_events = any(_messages(_content(stage_by_span, s).get("input_messages")) for s in agents)
    turns = _from_events(spans, stage_by_span) if has_events else _from_history(spans, stage_by_span)
    trajectory = _trajectory(spans, stage_by_span)
    record = _record(spans)
    if record_resolver is not None:
        record = record_resolver(record, spans) or record
    first = spans[0] if spans else {}
    lang = next(((s.get("attributes") or {}).get("ee.lang") for s in spans if (s.get("attributes") or {}).get("ee.lang")), "und")
    models = sorted({s.get("response_model") or s.get("request_model") for s in spans
                     if s.get("type") == "GENERATION" and (s.get("response_model") or s.get("request_model"))})
    conv = {
        "id": conversation_id, "tenant_id": tenant_id or first.get("tenant_id"), "lang": lang,
        "turns": turns, "record": record, "trajectory": trajectory,
        "trace_id": first.get("trace_id"), "trace_ids": sorted({s["trace_id"] for s in spans}),
        "session_id": first.get("session_id"),
        "trace_ctx": {"agent": next((s.get("agent_name") for s in spans if s.get("agent_name")), None),
                      "flow": next((s.get("workflow_name") for s in spans if s.get("workflow_name")), None),
                      "model": models[0] if models else None,
                      "tags": sorted({t for s in spans for t in (s.get("tags") or [])}),
                      "environment": first.get("environment"), "release": first.get("release")},
        "experiment": {"run_id": next((s.get("experiment_run_id") for s in spans if s.get("experiment_run_id")), None),
                       "item_id": next((s.get("experiment_item_id") for s in spans if s.get("experiment_item_id")), None)},
        "internal": any(s.get("internal") for s in spans),
    }
    conv["version"] = hashlib.sha256(json.dumps([turns, record], sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]
    return conv


def rollup(conv: dict, spans: list[dict]) -> dict:
    """The traces-* document fields that come from the spans themselves."""
    starts = sorted(spans, key=lambda s: ns(s["start_time"]))
    ends = sorted(spans, key=lambda s: ns(s.get("end_time") or s["start_time"]))
    start, end = starts[0]["start_time"], ends[-1].get("end_time") or ends[-1]["start_time"]
    gens = [s for s in spans if s.get("type") == "GENERATION"]
    tools = [s for s in spans if s.get("type") == "TOOL"]
    usage = [s.get("usage") or {} for s in spans]
    first_user = next((t["text"] for t in conv["turns"] if t["role"] == "user"), "")
    doc = {
        "conversation_id": conv["id"], "tenant_id": conv["tenant_id"], "trace_ids": conv["trace_ids"],
        "session_id": conv.get("session_id"), "start_time": start, "end_time": end,
        "duration_ms": (ns(end) - ns(start)) / 1e6, "n_spans": len(spans),
        "n_turns": sum(t["role"] != "tool" for t in conv["turns"]),
        "n_llm_calls": len(gens), "n_tool_calls": len(tools),
        "cost_usd": sum(s.get("cost_usd") or 0 for s in spans),
        "price_ids": sorted({s["price_id"] for s in spans if s.get("price_id")}),
        "input_tokens": sum(u.get("input_tokens", 0) for u in usage),
        "output_tokens": sum(u.get("output_tokens", 0) for u in usage),
        "error": any(s.get("status") == "ERROR" for s in spans),
        "error_types": sorted({s["error_type"] for s in spans if s.get("error_type")}),
        "tool_sequence": [s.get("tool_name") or s["name"] for s in tools],
        "agent_name": conv["trace_ctx"]["agent"], "service_name": spans[0].get("service_name") if spans else None,
        "environment": conv["trace_ctx"]["environment"], "release": conv["trace_ctx"]["release"],
        "models": sorted({s.get("response_model") or s.get("request_model") for s in gens
                          if s.get("response_model") or s.get("request_model")}),
        "tags": conv["trace_ctx"]["tags"], "user_id": next((s.get("user_id") for s in spans if s.get("user_id")), None),
        "lang": conv["lang"], "first_user_message": first_user[:500],
        "sentry_event_ids": sorted({s["sentry_event_id"] for s in spans if s.get("sentry_event_id")}),
        "experiment_run_id": conv["experiment"]["run_id"], "experiment_item_id": conv["experiment"]["item_id"],
    }
    return {k: v for k, v in doc.items() if v is not None}
