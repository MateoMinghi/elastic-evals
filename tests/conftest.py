import os
import uuid

import httpx
import pytest

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
    os_.setup(pools=("shared", "acme"))
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
