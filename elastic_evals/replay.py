"""Replay conversations as OpenTelemetry GenAI spans, the way an instrumented agent emits them.

Used for demos, load tests and end-to-end tests: each user turn becomes one trace (an
`invoke_agent` root span carrying the user's message), with a `chat` span per model call and an
`execute_tool` span per tool call underneath; every trace of a conversation shares
`gen_ai.conversation.id`. The domain record travels as `ee.record` (inline JSON) on the root spans.

    python -m elastic_evals replay --data convs.jsonl --endpoint http://localhost:4318 --tenant acme
"""

import json
import time

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

STEP_NS = 40_000_000  # 40 ms between synthetic events


def _msg(role, *parts):
    return {"role": role, "parts": list(parts)}


def _text(t):
    return {"type": "text", "content": t}


def tracer_provider(endpoint: str, tenant: str, service: str = "demo-agent", environment: str = "dev",
                    headers: dict | None = None) -> TracerProvider:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    resource = Resource.create({"service.name": service, "deployment.environment.name": environment,
                                "ee.tenant_id": tenant})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(
        OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces", headers=headers or {}),
        max_export_batch_size=512, schedule_delay_millis=200))
    return provider


def _turn_groups(turns):
    """Split turns into agent turns: a user message (or several in a row) starts one; agent output
    before the first user message forms its own."""
    groups, cur = [], None
    for t in turns:
        if t["role"] == "user":
            if cur is None or cur["steps"]:
                cur = {"user": [], "steps": []}
                groups.append(cur)
            cur["user"].append(t["text"])
        else:
            if cur is None:
                cur = {"user": [], "steps": []}
                groups.append(cur)
            cur["steps"].append(t)
    return groups


def emit(tracer, conv: dict, agent: str = "bank-assistant", model: str = "demo-model", start_ns: int | None = None,
         extra: dict | None = None) -> int:
    """Emit one conversation; returns the number of spans."""
    clock = [start_ns or time.time_ns()]

    def tick():
        clock[0] += STEP_NS
        return clock[0]

    history, n = [], 0
    record = json.dumps(conv.get("record", {}), ensure_ascii=False)
    for g in _turn_groups(conv["turns"]):
        root_attrs = {"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": agent,
                      "gen_ai.conversation.id": conv["id"], "ee.record": record, "ee.lang": conv.get("lang", "und"),
                      **(extra or {})}
        user_msgs = [_msg("user", _text(u)) for u in g["user"]]
        if user_msgs:
            root_attrs["gen_ai.input.messages"] = json.dumps(user_msgs, ensure_ascii=False)
        history += user_msgs
        root = tracer.start_span(f"invoke_agent {agent}", attributes=root_attrs, start_time=tick())
        ctx = trace.set_span_in_context(root)
        last_text = None
        for i, step in enumerate(g["steps"]):
            if step["role"] == "assistant":
                out = [_msg("assistant", _text(step["text"]))]
                last_text = step["text"]
            else:
                call_id = f"call_{n}_{i}"
                out = [_msg("assistant", {"type": "tool_call", "id": call_id, "name": step["name"],
                                          "arguments": step.get("args", {})})]
            s = tracer.start_span(f"chat {model}", context=ctx, start_time=tick(), attributes={
                "gen_ai.operation.name": "chat", "gen_ai.request.model": model, "gen_ai.provider.name": "demo",
                "gen_ai.usage.input_tokens": 50 * (len(history) + 1), "gen_ai.usage.output_tokens": 40,
                "gen_ai.input.messages": json.dumps(history, ensure_ascii=False),
                "gen_ai.output.messages": json.dumps(out, ensure_ascii=False)})
            s.end(end_time=tick())
            n += 1
            history += out
            if step["role"] == "tool":
                t = tracer.start_span(f"execute_tool {step['name']}", context=ctx, start_time=tick(), attributes={
                    "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": step["name"],
                    "gen_ai.tool.call.id": call_id,
                    "gen_ai.tool.call.arguments": json.dumps(step.get("args", {}), ensure_ascii=False),
                    "gen_ai.tool.call.result": str(step.get("result", ""))})
                if str(step.get("result", "")).startswith("error"):
                    t.set_status(trace.Status(trace.StatusCode.ERROR))
                t.end(end_time=tick())
                n += 1
                history.append(_msg("tool", {"type": "tool_call_response", "id": call_id,
                                             "response": str(step.get("result", ""))}))
        if last_text is not None:
            root.set_attribute("gen_ai.output.messages", json.dumps([_msg("assistant", _text(last_text))], ensure_ascii=False))
        root.end(end_time=tick())
        n += 1
    return n


def replay(convs, endpoint: str, tenant: str, agent: str = "bank-assistant", rate: float | None = None,
           environment: str = "dev") -> int:
    provider = tracer_provider(endpoint, tenant, environment=environment)
    tracer = provider.get_tracer("elastic_evals.replay")
    spans, t0 = 0, time.time()
    for i, conv in enumerate(convs):
        spans += emit(tracer, conv, agent=agent)
        if rate:
            ahead = (i + 1) / rate - (time.time() - t0)
            if ahead > 0:
                time.sleep(ahead)
    provider.force_flush()
    provider.shutdown()
    return spans
