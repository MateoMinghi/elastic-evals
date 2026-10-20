import contextlib
import json
import os
import socket
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
EECOL = ROOT / "collector" / "_build" / "eecol"
DATA = ROOT / "experiments" / "data"

OS_URL = os.environ.get("EE_OPENSEARCH_URL", "http://localhost:9200")
PG_BASE = os.environ.get("EE_POSTGRES_BASE", "postgresql://postgres@localhost:5432")
KAFKA = os.environ.get("EE_KAFKA", "localhost:9092")


def _up(url):
    try:
        return httpx.get(url, timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


@pytest.fixture(scope="session")
def opensearch():
    if not _up(OS_URL):
        pytest.skip("OpenSearch not running")
    from elastic_evals.store.opensearch import OpenSearch
    os_ = OpenSearch(OS_URL, prefix=f"t{uuid.uuid4().hex[:6]}")
    os_.setup(pools=("shared", "acme", "pipe"))
    yield os_
    os_.teardown()


@pytest.fixture(scope="session")
def postgres():
    try:
        import psycopg
        psycopg.connect(PG_BASE + "/postgres", connect_timeout=2).close()
    except Exception:
        pytest.skip("Postgres not running")
    from elastic_evals.store.postgres import Postgres, create_database
    name = f"ee_test_{uuid.uuid4().hex[:6]}"
    dsn = f"{PG_BASE}/{name}"
    create_database(dsn)
    pg = Postgres(dsn)
    pg.migrate()
    yield pg
    pg.close()
    import psycopg
    with psycopg.connect(PG_BASE + "/postgres", autocommit=True) as c:
        c.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_http(url, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if httpx.get(url, timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    raise TimeoutError(url)


def load(name, n):
    return [json.loads(line) for line in (DATA / name).open()][:n]


@contextlib.contextmanager
def run_collectors(opensearch, tmp, extra_env=None):
    """Gateway + ingest collectors on free ports and a fresh Kafka topic."""
    if not EECOL.exists():
        pytest.skip("collector not built (cd collector && builder --config builder-config.yaml)")
    try:
        socket.create_connection(tuple(KAFKA.split(":")), timeout=2).close()
    except OSError:
        pytest.skip("Kafka not running")
    ingest = yaml.safe_load((ROOT / "deploy/collector/ingest.yaml").read_text())
    ingest["exporters"]["elasticevals_opensearch"]["pools"] = {"acme": "acme", "pipe": "pipe"}
    (tmp / "ingest.yaml").write_text(yaml.safe_dump(ingest))
    otlp, gh, ih = free_port(), free_port(), free_port()
    env = dict(os.environ, EE_KAFKA_TOPIC=f"ee-spans-{uuid.uuid4().hex[:8]}", EE_INGEST_GROUP=f"g-{uuid.uuid4().hex[:8]}",
               EE_INDEX_PREFIX=opensearch.prefix, EE_OTLP_HTTP=f"127.0.0.1:{otlp}", EE_OTLP_GRPC=f"127.0.0.1:{free_port()}",
               EE_GATEWAY_HEALTH=f"127.0.0.1:{gh}", EE_INGEST_HEALTH=f"127.0.0.1:{ih}",
               EE_PRICES_FILE=str(ROOT / "deploy/collector/prices.json"), **(extra_env or {}))
    logs = [open(tmp / f"{n}.log", "w") for n in ("gateway", "ingest")]
    procs = [subprocess.Popen([str(EECOL), "--config", str(ROOT / "deploy/collector/gateway.yaml")], env=env,
                              stdout=logs[0], stderr=subprocess.STDOUT),
             subprocess.Popen([str(EECOL), "--config", str(tmp / "ingest.yaml")], env=env,
                              stdout=logs[1], stderr=subprocess.STDOUT)]
    try:
        wait_http(f"http://127.0.0.1:{gh}/")
        wait_http(f"http://127.0.0.1:{ih}/")
        yield {"otlp": f"http://127.0.0.1:{otlp}", "topic": env["EE_KAFKA_TOPIC"], "logs": tmp}
    finally:
        for p in procs:
            p.terminate()
            p.wait(10)


@pytest.fixture(scope="module")
def collectors(opensearch, tmp_path_factory):
    with run_collectors(opensearch, tmp_path_factory.mktemp("collector")) as c:
        yield c
