"""Presidio redaction at the gateway and S3 offload of large content at ingest, with real services:
the Presidio analyzer service, a moto S3 server, both collectors, Kafka and OpenSearch."""

import json
import subprocess
import sys
import time

import pytest

from elastic_evals.conversation import build
from elastic_evals.replay import replay
from elastic_evals.worker import fetch_conversation

from .conftest import free_port, run_collectors, wait_http

pytestmark = pytest.mark.infra

LONG = "Here is the full statement of your account. " * 40  # about 1.8 kB: above the test threshold
CONV = {"id": "red-0001", "lang": "en", "record": {"status": "confirmed"}, "turns": [
    {"role": "user", "text": "Hi, I'm John Smith, my email is john.smith@example.com and my card is 4111 1111 1111 1111."},
    {"role": "tool", "name": "get_statement", "args": {"account": "1"}, "result": "ok"},
    {"role": "assistant", "text": LONG + "Anything else, John?"},
]}


@pytest.fixture(scope="module")
def services(tmp_path_factory):
    pytest.importorskip("presidio_analyzer")
    pytest.importorskip("moto")
    tmp = tmp_path_factory.mktemp("svc")
    pp, sp = free_port(), free_port()
    procs = [subprocess.Popen([sys.executable, "-m", "uvicorn", "elastic_evals.presidio_service:app", "--port", str(pp)],
                              stdout=open(tmp / "presidio.log", "w"), stderr=subprocess.STDOUT),
             subprocess.Popen([sys.executable, "-m", "moto.server", "-p", str(sp)],
                              stdout=open(tmp / "moto.log", "w"), stderr=subprocess.STDOUT)]
    try:
        wait_http(f"http://127.0.0.1:{pp}/health", 120)
        wait_http(f"http://127.0.0.1:{sp}/moto-api/", 60)
        yield {"presidio": f"http://127.0.0.1:{pp}", "s3": f"http://127.0.0.1:{sp}"}
    finally:
        for p in procs:
            p.terminate()


def test_redaction_and_offload(opensearch, services, tmp_path, monkeypatch):
    import boto3
    for k, v in {"EE_S3_ENDPOINT": services["s3"], "AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test",
                 "EE_S3_REGION": "us-east-1"}.items():
        monkeypatch.setenv(k, v)
    from elastic_evals import blobs
    blobs._client.cache_clear()
    blobs._get.cache_clear()
    boto3.client("s3", endpoint_url=services["s3"], region_name="us-east-1").create_bucket(Bucket="ee-content")
    env = {"EE_PRESIDIO_URL": services["presidio"], "EE_S3_ENDPOINT": services["s3"], "EE_S3_BUCKET": "ee-content",
           "AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test", "EE_BLOB_THRESHOLD": "1000"}
    with run_collectors(opensearch, tmp_path, env) as col:
        n = replay([CONV], col["otlp"], tenant="red")
        end = time.time() + 60
        while time.time() < end:
            opensearch.refresh()
            if opensearch.count("obs", {"term": {"tenant_id": "red"}}) >= n:
                break
            time.sleep(0.5)
    stage = opensearch.hits("stage", {"size": 100, "query": {"term": {"tenant_id": "red"}}})
    text = json.dumps(stage, ensure_ascii=False)
    assert "john.smith@example.com" not in text and "4111 1111 1111 1111" not in text
    assert "<EMAIL_ADDRESS>" in text and "<CREDIT_CARD>" in text and "<PERSON>" in text
    assert any(d.get("redacted") for d in stage)
    previews = json.dumps(opensearch.hits("obs", {"size": 100, "query": {"term": {"tenant_id": "red"}}}))
    assert "john.smith@example.com" not in previews
    offloaded = [d for d in stage if d.get("blob_uri")]
    assert offloaded and all("raw" not in d for d in offloaded)
    assert all(d["blob_uri"].startswith("s3://ee-content/red/") for d in offloaded)
    # the worker reads offloaded content back: the long reply arrives whole
    spans, content = fetch_conversation(opensearch, "red", "red-0001", "session", "shared")
    conv = build("red-0001", spans, content, "red")
    assert conv["turns"][2]["text"].startswith(LONG) and conv["turns"][2]["text"].endswith("?")
    assert "<EMAIL_ADDRESS>" in conv["turns"][0]["text"]
