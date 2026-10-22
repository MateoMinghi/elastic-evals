"""MCP server tests. No services needed: the API is replaced by a recording transport, and the requests
it records are checked against the routes of the real FastAPI app, so a renamed route or parameter fails here."""

import inspect
import json

import httpx
import pytest

pytest.importorskip("mcp")
pytest.importorskip("fastapi")

from fastapi.routing import APIRoute  # noqa: E402
from mcp import Client  # noqa: E402
from starlette.routing import Match  # noqa: E402

from elastic_evals.api import create_app  # noqa: E402
from elastic_evals.mcp_server import EvalsAPI, build_server  # noqa: E402

ARGS = {  # the smallest valid call of every tool
    "get_conversation": {"conversation_id": "c1"}, "similar_conversations": {"conversation_id": "c1"},
    "get_suite": {"suite_id": "s"}, "get_calibration": {"calibration_id": "k"}, "shadow_comparison": {"suite_id": "s"},
    "list_queue_items": {"queue_id": "q"}, "label_agreement": {"label_set_id": "l"}, "get_dataset_items": {"dataset_id": "d"},
    "compare_experiments": {"run_a": "a", "run_b": "b"}, "put_suite": {"suite_id": "s", "yaml": "x: 1", "agent_name": "bot"},
    "start_calibration": {"suite_id": "s", "label_set_id": "l"}, "start_shadow": {"calibration_id": "k"},
    "promote_calibration": {"calibration_id": "k"}, "reject_calibration": {"calibration_id": "k"},
    "run_drift_check": {"suite_id": "s"}, "rescore": {"conversation_ids": ["c1"], "since": "2026-10-01"},
    "create_annotation_queue": {"suite_id": "s", "name": "n", "checks": ["c"]},
    "add_to_annotation_queue": {"queue_id": "q", "conversation_ids": ["c1"]}, "fill_annotation_queue": {"queue_id": "q"},
    "create_dataset": {"name": "n"}, "add_dataset_items": {"dataset_id": "d", "from_conversations": ["c1"]},
    "list_conversations": {"q": "refund", "flag": "hallucination", "since": "2026-10-01", "error": True},
    "score_summary": {"since": "2026-10-01", "agent": "bot"},
}
SUITE = {"versions": [{"version": "v2", "created_at": "t2", "yaml": "y" * 5000}, {"version": "v1", "created_at": "t1", "yaml": "old"}],
         "calibrations": []}


class Recorder:
    def __init__(self, replies=None):
        self.requests, self.replies = [], replies or {}

    def __call__(self, request: httpx.Request):
        self.requests.append(request)
        reply = self.replies.get(request.url.path, {})
        if isinstance(reply, httpx.Response):
            return reply
        return httpx.Response(200, json=reply)


def server(replies=None, read_only=False, key="ek_1.secret"):
    rec = Recorder(replies)
    http = httpx.AsyncClient(transport=httpx.MockTransport(rec), base_url="http://ee",
                             headers={"Authorization": f"Bearer {key}"})
    return build_server(EvalsAPI(client=http), read_only), rec


async def tools(srv):
    async with Client(srv) as c:
        return {t.name: t for t in (await c.list_tools()).tools}


def routes():
    app = create_app(pg=object(), os_=object(), registry=object(), auth=False)
    return [r for r in app.routes if isinstance(r, APIRoute)]


def route_for(request: httpx.Request):
    scope = {"type": "http", "method": request.method, "path": request.url.path}
    for r in routes():
        if r.matches(scope)[0] == Match.FULL:
            return r
    return None


async def test_every_tool_hits_a_real_route_with_parameters_it_accepts():
    srv, rec = server({"/v1/suites/s": SUITE})
    async with Client(srv) as c:
        for name in (await tools(srv)):
            await c.call_tool(name, ARGS.get(name, {}))
    assert len(rec.requests) >= 30
    for req in rec.requests:
        route = route_for(req)
        assert route is not None, f"{req.method} {req.url.path} is not an API route"
        d = route.dependant
        extra = set(req.url.params) - {p.alias or p.name for p in d.query_params}
        assert not extra, f"{req.method} {req.url.path}: unknown query parameters {extra}"
        if req.content:
            extra = set(json.loads(req.content)) - {p.alias or p.name for p in d.body_params}
            assert not extra, f"{req.method} {req.url.path}: unknown body fields {extra}"


async def test_one_tool_per_job_and_no_tool_for_ground_truth_or_platform_administration():
    names = set(await tools(server()[0]))
    assert {"list_conversations", "get_conversation", "put_suite", "promote_calibration", "score_summary"} <= names
    for banned in ("correct_score", "submit_labels", "create_tenant", "create_api_key", "put_model_prices", "reprice"):
        assert banned not in names
    scopes = {r.path for r in routes()}  # every tool's route exists; none targets the platform routes
    assert "/v1/tenants" in scopes and "/v1/api-keys" in scopes


async def test_read_only_offers_no_tool_that_changes_anything():
    full, only = await tools(server()[0]), await tools(server(read_only=True)[0])
    assert set(only) < set(full)
    assert all(t.annotations.read_only_hint for t in only.values())
    writes = {n for n, t in full.items() if not t.annotations.read_only_hint}
    assert {"put_suite", "promote_calibration", "rescore"} <= writes and not writes & set(only)
    assert full["promote_calibration"].annotations.destructive_hint
    assert not full["put_suite"].annotations.destructive_hint


async def test_the_api_key_is_sent_and_never_exposed_to_the_agent():
    srv, rec = server({"/v1/me": {"tenant": "acme"}})
    async with Client(srv) as c:
        res = await c.call_tool("whoami", {})
    assert rec.requests[0].headers["authorization"] == "Bearer ek_1.secret"
    assert "secret" not in str(res)
    assert "key" not in inspect.signature(EvalsAPI.call).parameters


async def test_api_errors_reach_the_agent_with_the_reason():
    reply = httpx.Response(409, json={"detail": {"message": "candidate does not yet agree", "comparison": {"promote": False}}})
    srv, _ = server({"/v1/calibrations/k/promote": reply})
    async with Client(srv) as c:
        res = await c.call_tool("promote_calibration", {"calibration_id": "k"})
    assert res.is_error
    assert "409" in res.content[0].text and "does not yet agree" in res.content[0].text
    forbidden = httpx.Response(403, json={"detail": "missing scope suites:write"})
    srv, _ = server({"/v1/suites/s": forbidden})
    async with Client(srv) as c:
        res = await c.call_tool("put_suite", {"suite_id": "s", "yaml": "x: 1"})
    assert res.is_error and "missing scope suites:write" in res.content[0].text


async def test_long_text_is_clipped_but_a_suite_is_returned_whole():
    long = "word " * 2000
    srv, _ = server({"/v1/conversations/c1": {"conversation": {"turns": [long]}, "spans": [{"content": long}], "scores": []},
                     "/v1/suites/s": SUITE})
    async with Client(srv) as c:
        conv = (await c.call_tool("get_conversation", {"conversation_id": "c1", "include_spans": True})).structured_content
        suite = (await c.call_tool("get_suite", {"suite_id": "s"})).structured_content
        old = (await c.call_tool("get_suite", {"suite_id": "s", "version": "v1"})).structured_content
        missing = await c.call_tool("get_suite", {"suite_id": "s", "version": "v9"})
    assert "more characters" in conv["conversation"]["turns"][0] and len(conv["spans"][0]["content"]) < 2100
    assert suite["version"] == "v2" and suite["yaml"] == "y" * 5000 and len(suite["versions"]) == 2
    assert old["yaml"] == "old"
    assert missing.is_error


async def test_spans_are_left_out_unless_asked_for():
    srv, _ = server({"/v1/conversations/c1": {"conversation": {}, "spans": [{"a": 1}, {"a": 2}], "scores": []}})
    async with Client(srv) as c:
        out = (await c.call_tool("get_conversation", {"conversation_id": "c1"})).structured_content
    assert out["n_spans"] == 2 and "spans" not in out


async def test_page_size_is_capped():
    srv, rec = server({"/v1/conversations": {"items": []}})
    async with Client(srv) as c:
        await c.call_tool("list_conversations", {"size": 100000})
    assert rec.requests[0].url.params["size"] == "100"


async def test_an_unreachable_api_is_reported_not_hidden():
    def down(request):
        raise httpx.ConnectError("refused", request=request)
    srv = build_server(EvalsAPI(client=httpx.AsyncClient(transport=httpx.MockTransport(down), base_url="http://ee")))
    async with Client(srv) as c:
        res = await c.call_tool("whoami", {})
    assert res.is_error and "cannot reach the elastic-evals API at http://ee" in res.content[0].text
