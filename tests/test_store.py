import pytest

from elastic_evals.store.opensearch import KINDS, tenant_filter

pytestmark = pytest.mark.infra


def test_setup_creates_write_aliases_and_policies(opensearch):
    for kind in KINDS:
        for pool in ("shared", "acme"):
            alias = opensearch.alias(kind, pool)
            got = opensearch.req("GET", f"/_alias/{alias}")
            (index, spec), = got.items()
            assert index == f"{alias}-000001" and spec["aliases"][alias]["is_write_index"]
    # idempotent
    opensearch.setup(pools=("shared", "acme"))
    pol = opensearch.req("GET", f"/_plugins/_ism/policies/{opensearch.prefix}-payload")
    states = [s["name"] for s in pol["policy"]["states"]]
    assert states == ["hot", "warm", "delete"]


def test_obs_mapping_is_strict_and_zstd(opensearch):
    idx = f"{opensearch.alias('obs')}-000001"
    settings = opensearch.req("GET", f"/{idx}/_settings")[idx]["settings"]["index"]
    assert settings["codec"] == "zstd"
    from elastic_evals.store.opensearch import OpenSearchError
    with pytest.raises(OpenSearchError):
        opensearch.bulk("obs", [("x", {"tenant_id": "t", "unknown_field": 1}, "tr")])
    with pytest.raises(OpenSearchError):  # routing is required
        opensearch.bulk("obs", [("y", {"tenant_id": "t"}, None)])


def test_idempotent_create_and_tenant_filter(opensearch):
    docs = [(f"s{i}", {"tenant_id": "t1" if i % 2 else "t2", "trace_id": "tr1", "span_id": f"s{i}",
                       "start_time": "2026-10-20T10:00:00.123456789Z", "attributes": {"a": {"b": "c"}}}, "tr1")
            for i in range(10)]
    assert opensearch.bulk("obs", docs, op="create", refresh=True) == (10, 0)
    assert opensearch.bulk("obs", docs, op="create", refresh=True) == (0, 10)
    hits = opensearch.hits("obs", {"query": {"bool": {"filter": [tenant_filter("t1")]}}, "size": 50}, routing="tr1")
    assert len(hits) == 5 and all(h["tenant_id"] == "t1" for h in hits)
    # flat_object is searchable by path
    n = opensearch.count("obs", {"term": {"attributes.a.b": "c"}})
    assert n == 10


def test_dedupe_across_rollover(opensearch):
    alias = opensearch.alias("traces")
    doc = {"conversation_id": "c1", "tenant_id": "t1", "updated_at": "2026-10-20T10:00:00Z", "flags": ["a"]}
    opensearch.bulk("traces", [("c1", doc, None)], refresh=True)
    opensearch.req("POST", f"/{alias}/_rollover")
    opensearch.bulk("traces", [("c1", dict(doc, flags=["b"]), None)], refresh=True)
    res = opensearch.search("traces", {"query": {"term": {"conversation_id": "c1"}}})
    assert res["hits"]["total"]["value"] == 2
    hits = opensearch.hits("traces", {"query": {"term": {"conversation_id": "c1"}}})
    assert len(hits) == 1 and hits[0]["flags"] == ["b"]


def test_knn_embeddings(opensearch):
    import random
    rng = random.Random(0)
    vecs = {f"c{i}": [rng.uniform(-1, 1) for _ in range(384)] for i in range(20)}
    opensearch.bulk("embeddings", [(k, {"tenant_id": "t1", "conversation_id": k, "unit": "conversation",
                                        "vector": v}, None) for k, v in vecs.items()], refresh=True)
    res = opensearch.search("embeddings", {"size": 3, "query": {"knn": {"vector": {"vector": vecs["c7"], "k": 3}}}})
    assert res["hits"]["hits"][0]["_source"]["conversation_id"] == "c7"


def test_metrics_star_tree(opensearch):
    docs = [(None, {"ts": "2026-10-20T10:00:00Z", "tenant_id": "t1", "agent_name": "bank", "check": "fee",
                    "n": 10, "flagged": i, "cost_usd": 0.001}, None) for i in range(5)]
    opensearch.bulk("metrics", docs, refresh=True)
    res = opensearch.search("metrics", {"size": 0, "query": {"term": {"check": "fee"}},
                                        "aggs": {"f": {"sum": {"field": "flagged"}}}})
    assert res["aggregations"]["f"]["value"] == 10


def test_postgres_schema(postgres):
    names = {r["table_name"] for r in postgres.all(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='public'")}
    assert {"tenants", "suite_versions", "calibrations", "labels", "trace_tracker", "sentry_links",
            "annotation_items", "model_prices", "dataset_items", "experiment_runs", "audit_log"} <= names
    postgres.migrate()  # idempotent
    assert postgres.one("SELECT count(*) AS n FROM roles")["n"] == 6  # admin, engineer, annotator, viewer, ingest, platform
