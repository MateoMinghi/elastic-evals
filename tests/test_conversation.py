"""Conversation rebuilding from spans, for shapes other instrumentations send."""

import json

from elastic_evals.conversation import build, detect_lang


def _span(sid, kind, t, **kw):
    return {"span_id": sid, "trace_id": "t1", "type": kind, "name": kind.lower(), "start_time": t, "end_time": t,
            "tenant_id": "acme", **kw}


def test_turn_ending_on_a_tool_call_without_text():
    """An agent that hits its step limit after a tool call: the last assistant message has no text and
    OpenAI-style instrumentations send its parts as null. The conversation must still be rebuilt."""
    history = [{"role": "user", "parts": [{"type": "text", "content": "Book me the cheapest flight to Lisbon on Friday please"}]}]
    out = [{"role": "assistant", "parts": None},
           {"role": "assistant", "parts": [{"type": "tool_call", "id": "c1", "name": "book_flight", "arguments": {"id": "F1"}}]}]
    spans = [_span("a", "AGENT", "2026-10-20T10:00:00Z", parent_span_id=None),
             _span("g", "GENERATION", "2026-10-20T10:00:01Z", parent_span_id="a"),
             _span("x", "TOOL", "2026-10-20T10:00:02Z", parent_span_id="a", tool_name="book_flight")]
    stage = [{"span_id": "g", "raw": {"input_messages": json.dumps(history), "output_messages": json.dumps(out)}},
             {"span_id": "x", "raw": {"tool_call_arguments": '{"id": "F1"}', "tool_call_result": "pending"}}]
    conv = build("c1", spans, stage, "acme")
    roles = [t["role"] for t in conv["turns"]]
    assert roles[0] == "user" and "tool" in roles and "assistant" not in roles
    assert conv["lang"] == "en"  # no ee.lang attribute: detected from the user's text


def test_detect_lang():
    assert detect_lang("Hola, quiero mandar plata a mi amiga para el cumple") == "es"
    assert detect_lang("I want to change my booking for the trip to Paris please") == "en"
    assert detect_lang("ok") == "und"


def test_dropped_content_is_reported_not_silent():
    """When redaction fails the gateway drops the content and marks the span. The conversation and its
    rollup must say so, with the reason, instead of looking like a short conversation."""
    from elastic_evals.conversation import content_gaps, rollup
    spans = [_span("a", "AGENT", "2026-10-20T10:00:00Z", parent_span_id=None),
             _span("g", "GENERATION", "2026-10-20T10:00:01Z", parent_span_id="a",
                   redaction_failed=True, redaction_reason="timeout,http_503", redaction_dropped=2),
             _span("h", "GENERATION", "2026-10-20T10:00:02Z", parent_span_id="a",
                   redaction_failed=True, redaction_reason="timeout", redaction_dropped=1)]
    gaps = content_gaps(spans)
    assert gaps == {"spans": 2, "dropped_attributes": 3, "reasons": {"http_503": 1, "timeout": 2}}
    conv = build("c1", spans, [], "acme")
    assert conv["content_gaps"] == gaps
    r = rollup(conv, spans)
    assert r["content_incomplete"] is True and r["content_gap_dropped"] == 3
    assert r["content_gap_reasons"] == ["http_503", "timeout"]

    clean = build("c2", spans[:1], [], "acme")
    assert clean["content_gaps"] is None
    assert "content_incomplete" not in rollup(clean, spans[:1])
