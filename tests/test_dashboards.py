"""OpenSearch Dashboards saved objects: every field they aggregate exists and is aggregatable in our
index templates, and they import into a real Dashboards (skipped when none runs)."""

import json
import os

import httpx
import pytest

from elastic_evals.dashboards import import_objects, ndjson, objects

pytestmark = pytest.mark.infra
OSD = os.environ.get("EE_DASHBOARDS_URL", "http://localhost:5601")


def fields_used():
    pats = {o["id"]: o["attributes"]["title"] for o in objects("p") if o["type"] == "index-pattern"}
    used = []
    for o in objects("p"):
        if o["type"] != "visualization":
            continue
        pattern = pats[o["references"][0]["id"]]
        for agg in json.loads(o["attributes"]["visState"])["aggs"]:
            if agg["params"].get("field"):
                used.append((pattern, agg["params"]["field"]))
        query = json.loads(o["attributes"]["kibanaSavedObjectMeta"]["searchSourceJSON"])["query"]["query"]
        for clause in filter(None, query.split(" and ")):
            used.append((pattern, clause.split(":")[0].strip()))
    return used


def test_saved_objects_reference_aggregatable_fields(opensearch):
    for pattern, field in fields_used():
        kind = pattern.split("-")[1]
        res = opensearch.req("GET", f"/{opensearch.pattern(kind)}/_field_caps", fields=field)
        caps = res["fields"].get(field)
        assert caps, f"{field} not mapped in {kind}"
        assert any(c["aggregatable"] or c["searchable"] for c in caps.values()), f"{kind}.{field}"


def test_ndjson_shape():
    lines = ndjson("ee").splitlines()
    objs = [json.loads(line) for line in lines]
    dash = next(o for o in objs if o["type"] == "dashboard")
    vis_ids = {o["id"] for o in objs if o["type"] == "visualization"}
    assert {r["id"] for r in dash["references"]} == vis_ids and len(vis_ids) == 7


def test_import_into_dashboards(opensearch):
    try:
        httpx.get(OSD + "/api/status", timeout=3).raise_for_status()
    except httpx.HTTPError:
        pytest.skip("OpenSearch Dashboards not running")
    res = import_objects(OSD, prefix=opensearch.prefix)
    assert res["successCount"] == len(objects("ee"))
    pattern = httpx.get(OSD + f"/api/saved_objects/index-pattern/{opensearch.prefix}-scores-pattern", timeout=10).json()
    names = {f["name"] for f in json.loads(pattern["attributes"]["fields"])}
    assert {"value", "check", "evaluator.kind", "timestamp"} <= names  # visualizations can resolve their fields
    found = httpx.get(OSD + "/api/saved_objects/dashboard/ee-overview", timeout=10).json()
    assert found["attributes"]["title"] == "elastic-evals overview"
    again = import_objects(OSD, prefix=opensearch.prefix)  # idempotent
    assert again["successCount"] == res["successCount"]
