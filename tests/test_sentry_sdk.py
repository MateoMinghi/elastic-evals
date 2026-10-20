"""The real Sentry SDK against a local ingest endpoint: what our hook puts on events, what never
leaves (conversation content), and the drift alert as a fingerprinted message."""

import gzip
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

sentry_sdk = pytest.importorskip("sentry_sdk")

SECRET = "my card is 4111 1111 1111 1111"


class Ingest(BaseHTTPRequestHandler):
    envelopes: list = []

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if self.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        Ingest.envelopes.append(body.decode())
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


@pytest.fixture
def ingest():
    Ingest.envelopes = []
    srv = HTTPServer(("127.0.0.1", 0), Ingest)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


def items(envelope: str):
    lines = envelope.split("\n")
    out = []
    for i in range(1, len(lines) - 1, 2):
        header = json.loads(lines[i])
        if header.get("type") == "event":
            out.append(json.loads(lines[i + 1]))
    return out


def test_sdk_events_link_to_traces_and_carry_no_content(ingest):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.trace import StatusCode

    from elastic_evals.sentry import Bridge, before_send

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("agent")
    sentry_sdk.init(dsn=f"http://public@127.0.0.1:{ingest}/1", before_send=before_send(ui_url="https://ui.test", tenant_id="acme"),
                    send_default_pii=False, default_integrations=False)
    try:
        with tracer.start_as_current_span("invoke_agent bank", attributes={
                "gen_ai.conversation.id": "conv-9", "gen_ai.agent.name": "bank",
                "gen_ai.input.messages": json.dumps([{"role": "user", "parts": [{"type": "text", "content": SECRET}]}])}) as span:
            trace_id = format(span.get_span_context().trace_id, "032x")
            try:
                raise ValueError("transfer service unavailable")
            except ValueError:
                event_id = sentry_sdk.capture_exception()
        Bridge("s").quality_regression({"tenant_id": "acme", "suite_id": "banking", "check_name": "user_frustrated",
                                        "lang": "es", "baseline": 0.88, "observed": 0.71, "n": 120, "calibration_id": "cal_1"})
        sentry_sdk.flush(5)
    finally:
        sentry_sdk.get_client().close()
    events = [e for env in Ingest.envelopes for e in items(env)]
    exc = next(e for e in events if e.get("exception"))
    assert exc["tags"]["trace_id"] == trace_id and exc["tags"]["gen_ai.conversation.id"] == "conv-9"
    assert exc["tags"]["ee.tenant_id"] == "acme"
    assert exc["contexts"]["elastic_evals"]["url"] == "https://ui.test/conversations/conv-9"
    assert SECRET not in "".join(Ingest.envelopes)  # content never reaches Sentry
    done = exporter.get_finished_spans()[0]
    assert done.attributes["ee.sentry.event_id"] == event_id and done.status.status_code == StatusCode.ERROR
    msg = next(e for e in events if e.get("message") or e.get("logentry"))
    assert msg["fingerprint"] == ["ee-quality", "acme", "banking", "user_frustrated", "es"]
    assert msg["level"] == "warning" and msg["contexts"]["drift"]["observed"] == 0.71
