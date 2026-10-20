"""OpenSearch Dashboards saved objects for the operational views we do not build ourselves
(DESIGN.md §1 non-goals, §10): index patterns, visualizations and an overview dashboard.

    python -m elastic_evals setup --dashboards http://dashboards:5601      # import (idempotent)
    python -m elastic_evals dashboards --out elastic-evals.ndjson         # or write the NDJSON
"""

import json

import httpx

VERSION = "3.9.0"


def _search(index_ref=True, query="", filters=None):
    src = {"query": {"query": query, "language": "kuery"}, "filter": filters or []}
    if index_ref:
        src["indexRefName"] = "kibanaSavedObjectMeta.searchSourceJSON.index"
    return {"searchSourceJSON": json.dumps(src)}


def _vis(vid, title, pattern_id, vis_type, aggs, params, query=""):
    vis_state = {"title": title, "type": vis_type, "aggs": aggs, "params": params}
    return {"type": "visualization", "id": vid,
            "attributes": {"title": title, "visState": json.dumps(vis_state), "uiStateJSON": "{}", "description": "",
                           "version": 1, "kibanaSavedObjectMeta": _search(query=query)},
            "references": [{"name": "kibanaSavedObjectMeta.searchSourceJSON.index", "type": "index-pattern", "id": pattern_id}]}


def _agg(i, kind, schema, **params):
    return {"id": str(i), "enabled": True, "type": kind, "schema": schema, "params": params}


def _line(category_axis="Time"):
    return {"type": "line", "grid": {"categoryLines": False}, "addTooltip": True, "addLegend": True,
            "legendPosition": "right", "times": [], "addTimeMarker": False,
            "categoryAxes": [{"id": "CategoryAxis-1", "type": "category", "position": "bottom", "show": True,
                              "labels": {"show": True, "truncate": 100}, "title": {"text": category_axis}}],
            "valueAxes": [{"id": "ValueAxis-1", "name": "LeftAxis-1", "type": "value", "position": "left", "show": True,
                           "labels": {"show": True, "truncate": 100}, "title": {}}],
            "seriesParams": [{"show": True, "type": "line", "mode": "normal", "data": {"id": "1"},
                              "valueAxis": "ValueAxis-1", "drawLinesBetweenPoints": True, "showCircles": True}]}


def _bar():
    p = _line("")
    p["type"] = "histogram"
    p["seriesParams"][0].update(type="histogram", mode="stacked")
    return p


def _table():
    return {"perPage": 10, "showPartialRows": False, "showMetricsAtAllLevels": False, "showTotal": False,
            "totalFunc": "sum", "percentageCol": ""}


def objects(prefix: str = "ee") -> list[dict]:
    patterns = {"traces": "start_time", "scores": "timestamp", "obs": "start_time", "metrics": "ts"}
    out = []
    ids = {}
    for kind, time_field in patterns.items():
        pid = f"{prefix}-{kind}-pattern"
        ids[kind] = pid
        out.append({"type": "index-pattern", "id": pid,
                    "attributes": {"title": f"{prefix}-{kind}-*", "timeFieldName": time_field}, "references": []})
    hist = {"field": None, "interval": "auto", "min_doc_count": 1, "extended_bounds": {}}
    vis = [
        _vis("ee-vis-conversations", "Conversations over time", ids["traces"], "line",
             [_agg(1, "count", "metric"), _agg(2, "date_histogram", "segment", **dict(hist, field="start_time"))], _line()),
        _vis("ee-vis-flag-rate", "Flag rate by check (production decisions)", ids["scores"], "horizontal_bar",
             [_agg(1, "avg", "metric", field="value", customLabel="flag rate"),
              _agg(2, "terms", "segment", field="check", size=30, order="desc", orderBy="1")], _bar(),
             query="decision:true and sample_reason:route"),
        _vis("ee-vis-decided-by", "Decided by layer", ids["scores"], "pie",
             [_agg(1, "count", "metric"), _agg(2, "terms", "segment", field="evaluator.kind", size=10, order="desc", orderBy="1")],
             {"type": "pie", "addTooltip": True, "addLegend": True, "legendPosition": "right", "isDonut": True,
              "labels": {"show": True, "values": True, "last_level": True, "truncate": 100}},
             query="decision:true and sample_reason:route"),
        _vis("ee-vis-llm-cost", "Agent LLM cost per day (USD)", ids["obs"], "histogram",
             [_agg(1, "sum", "metric", field="cost_usd"), _agg(2, "date_histogram", "segment", **dict(hist, field="start_time", interval="d"))],
             _bar()),
        _vis("ee-vis-eval-cost", "Evaluation cost per day (USD)", ids["traces"], "histogram",
             [_agg(1, "sum", "metric", field="eval_cost_usd"), _agg(2, "date_histogram", "segment", **dict(hist, field="start_time", interval="d"))],
             _bar()),
        _vis("ee-vis-errors", "Errors by type", ids["obs"], "table",
             [_agg(1, "count", "metric"), _agg(2, "terms", "bucket", field="error_type", size=20, order="desc", orderBy="1",
                                               missingBucket=True, missingBucketLabel="(no type)")], _table(),
             query="status:ERROR"),
        _vis("ee-vis-latency", "Model latency p50 / p95 (ms) by model", ids["obs"], "table",
             [_agg(1, "percentiles", "metric", field="duration_ms", percents=[50, 95]),
              _agg(2, "terms", "bucket", field="request_model", size=20, order="desc", orderBy="_key")], _table(),
             query="type:GENERATION"),
    ]
    out += vis
    panels, refs = [], []
    for i, v in enumerate(vis):
        panels.append({"panelIndex": str(i + 1), "gridData": {"x": (i % 2) * 24, "y": (i // 2) * 15, "w": 24, "h": 15,
                                                              "i": str(i + 1)},
                       "version": VERSION, "panelRefName": f"panel_{i}", "embeddableConfig": {}})
        refs.append({"name": f"panel_{i}", "type": "visualization", "id": v["id"]})
    out.append({"type": "dashboard", "id": "ee-overview",
                "attributes": {"title": "elastic-evals overview", "description": "Conversations, checks, layers, cost, errors, latency",
                               "panelsJSON": json.dumps(panels), "optionsJSON": json.dumps({"useMargins": True, "hidePanelTitles": False}),
                               "version": 1, "timeRestore": False, "kibanaSavedObjectMeta": _search(index_ref=False)},
                "references": refs})
    return out


def ndjson(prefix: str = "ee") -> str:
    return "".join(json.dumps(o) + "\n" for o in objects(prefix))


def import_objects(url: str, prefix: str = "ee", auth=None) -> dict:
    r = httpx.post(url.rstrip("/") + "/api/saved_objects/_import", params={"overwrite": "true"},
                   headers={"osd-xsrf": "true"}, auth=auth, timeout=60,
                   files={"file": ("elastic-evals.ndjson", ndjson(prefix).encode(), "application/ndjson")})
    r.raise_for_status()
    res = r.json()
    if not res.get("success"):
        raise RuntimeError(f"saved objects import failed: {json.dumps(res.get('errors'))[:800]}")
    # imported index patterns carry no field list, and visualizations cannot resolve their fields
    # without one: fill it from the mappings (run `setup` first, so the indices exist)
    for o in objects(prefix):
        if o["type"] != "index-pattern":
            continue
        f = httpx.get(url.rstrip("/") + "/api/index_patterns/_fields_for_wildcard", auth=auth, timeout=30,
                      params={"pattern": o["attributes"]["title"], "meta_fields": ["_source", "_id", "_index"]})
        f.raise_for_status()
        u = httpx.put(url.rstrip("/") + f"/api/saved_objects/index-pattern/{o['id']}", headers={"osd-xsrf": "true"},
                      auth=auth, timeout=30, json={"attributes": dict(o["attributes"], fields=json.dumps(f.json()["fields"]))})
        u.raise_for_status()
    return res
