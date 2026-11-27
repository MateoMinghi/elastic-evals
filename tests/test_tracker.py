"""The tracker counts every span once, however many times it is delivered (no services needed)."""

from opentelemetry.proto.common.v1 import common_pb2
from opentelemetry.proto.trace.v1 import trace_pb2

from elastic_evals.tracker import Summarizer


def _payload(spans, tenant="acme"):
    """spans: [(trace_id, span_id, parent_span_id, conversation_id)] -> one serialized OTLP message."""
    data = trace_pb2.TracesData()
    rs = data.resource_spans.add()
    kv = rs.resource.attributes.add()
    kv.key, kv.value.string_value = "ee.tenant_id", tenant
    ss = rs.scope_spans.add()
    for trace, span, parent, conv in spans:
        sp = ss.spans.add()
        sp.trace_id, sp.span_id, sp.parent_span_id = trace, span, parent
        if conv:
            a = sp.attributes.add()
            a.key, a.value.string_value = "gen_ai.conversation.id", conv
    return data.SerializeToString()


T1, T2 = b"\x01" * 16, b"\x02" * 16
ROOT, CHILD1, CHILD2 = b"\xa0" * 8, b"\xa1" * 8, b"\xa2" * 8


def test_resent_span_in_the_same_batch_counts_once():
    s = Summarizer(max_wait_s=0)
    out = s.feed([_payload([(T1, CHILD1, ROOT, None), (T1, ROOT, b"", None)]),
                  _payload([(T1, CHILD1, ROOT, None)])])  # a client retry of the child
    assert out[("acme", T1.hex())]["n"] == 2
    assert s.duplicates == 1


def test_redelivery_in_a_later_batch_counts_once():
    s = Summarizer(max_wait_s=0)
    first = s.feed([_payload([(T1, CHILD1, ROOT, "conv"), (T1, ROOT, b"", None)])])
    assert first[("acme", "conv")]["n"] == 2
    # Kafka redelivers the same message after a restart of the consumer group, plus one new span
    again = s.feed([_payload([(T1, CHILD1, ROOT, "conv"), (T1, CHILD2, ROOT, None)])])
    assert again[("acme", "conv")]["n"] == 1  # only the new span
    assert s.duplicates == 1


def test_a_batch_of_only_duplicates_produces_nothing():
    s = Summarizer(max_wait_s=0)
    msg = _payload([(T1, ROOT, b"", None)])
    assert s.feed([msg])
    assert s.feed([msg]) == {}


def test_same_span_id_in_another_tenant_or_trace_is_not_a_duplicate():
    s = Summarizer(max_wait_s=0)
    out = s.feed([_payload([(T1, ROOT, b"", None)]), _payload([(T1, ROOT, b"", None)], tenant="globex"),
                  _payload([(T2, ROOT, b"", None)])])
    assert sorted(k for k in out) == sorted([("acme", T1.hex()), ("globex", T1.hex()), ("acme", T2.hex())])
    assert s.duplicates == 0


def test_seen_set_is_bounded():
    s = Summarizer(max_wait_s=0, span_memory=10)
    for i in range(30):
        s.feed([_payload([(T1, i.to_bytes(8, "big"), b"", None)])])
    assert len(s.seen) <= 10
