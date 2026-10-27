"""MCP server: lets an AI agent operate elastic-evals (inspect conversations and scores, manage suites,
calibrations, annotation queues, datasets) through the same HTTP API the UI and CLI use.

It holds no state and touches no database. Every tool is one API call made with one API key, so tenant
isolation, role scopes and the audit log are exactly the API's: a key that may not write suites cannot
write them through MCP either. Give an agent the narrowest role that does its job (`viewer` to look,
`engineer` to manage), or start the server with --read-only to not offer the write tools at all.

Left out on purpose: tenants, API keys and the price book (platform administration), and human labels
and score corrections. Those are ground truth for calibration; an agent writing them would be
grading its own work.

    EE_API_URL=http://localhost:8000 EE_API_KEY=ek_x.secret python -m elastic_evals mcp
"""

import os
from typing import Any

import httpx

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

CLIP = 2000  # conversation text and span payloads can be megabytes; an agent's context cannot take that

INSTRUCTIONS = """\
Operates an elastic-evals deployment: observability and calibrated evaluation for production AI agents.
Conversations are scored by checks defined in a suite; a calibration picks the evaluator chain per check
from human labels. Typical flow: put_suite -> start_calibration (needs a label set with 20+ labelled
conversations) -> start_shadow -> shadow_comparison -> promote_calibration. Promotion is refused until the
candidate agrees with the audit judge at least as well as the active calibration. Humans label; you
cannot. Long text is clipped; use get_conversation with include_spans for detail."""


class EvalsAPI:
    """The elastic-evals HTTP API as the one tenant and role its key belongs to."""

    def __init__(self, url: str | None = None, key: str | None = None, tenant: str | None = None,
                 client: httpx.AsyncClient | None = None):
        url = (url or os.environ.get("EE_API_URL", "http://localhost:8000")).rstrip("/")
        key = key or os.environ.get("EE_API_KEY")
        tenant = tenant or os.environ.get("EE_TENANT")
        headers = {"Authorization": f"Bearer {key}"} if key else ({"X-EE-Tenant": tenant} if tenant else {})
        self.http = client or httpx.AsyncClient(base_url=url, headers=headers, timeout=120)

    async def call(self, method: str, path: str, *, params: dict | None = None, body: dict | None = None) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            res = await self.http.request(method, path, params=params, json=body)
        except httpx.HTTPError as exc:  # otherwise the agent only sees "Error executing tool"
            raise ToolError(f"cannot reach the elastic-evals API at {self.http.base_url}: {type(exc).__name__}: {exc}") from exc
        if res.status_code >= 400:
            try:
                detail = res.json().get("detail", res.text)
            except ValueError:
                detail = res.text
            raise ToolError(f"{method} {path}: HTTP {res.status_code}: {detail}")
        return clip(res.json())

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, **kw):
        return self.call("POST", path, **kw)

    def put(self, path, **kw):
        return self.call("PUT", path, **kw)


def clip(x: Any) -> Any:
    if isinstance(x, str) and len(x) > CLIP:
        return f"{x[:CLIP]}… [{len(x) - CLIP} more characters]"
    if isinstance(x, list):
        return [clip(v) for v in x]
    if isinstance(x, dict):
        return {k: v if k == "yaml" else clip(v) for k, v in x.items()}  # a suite is edited whole: never hand back half
    return x


def build_server(api: EvalsAPI | None = None, read_only: bool = False) -> MCPServer:
    api = api or EvalsAPI()
    mcp = MCPServer("elastic-evals", instructions=INSTRUCTIONS)

    def tool(*, write: bool = False, destructive: bool = False):
        def deco(fn):
            if write and read_only:
                return fn
            return mcp.tool(annotations=ToolAnnotations(
                read_only_hint=not write, destructive_hint=destructive if write else None,
                idempotent_hint=None if write else True, open_world_hint=False))(fn)
        return deco

    get, post, put = api.get, api.post, api.put

    # -- look ------------------------------------------------------------------------------------
    @tool()
    async def whoami() -> dict[str, Any]:
        """The tenant, actor and scopes this connection has. Check it first: write tools need the matching scope."""
        return await get("/v1/me")

    @tool()
    async def list_conversations(q: str | None = None, flag: str | None = None, agent: str | None = None,
                                 error: bool | None = None, lang: str | None = None, experiment: str | None = None,
                                 sentry_issue: str | None = None, since: str | None = None, until: str | None = None,
                                 size: int = 20, search_after: str | None = None) -> dict[str, Any]:
        """Search conversations, newest first. `q` is full text over the messages; `flag` is a check name that fired;
        since/until are ISO timestamps. Returns items, total, facets (flags, agents, languages) and `next`: pass it
        back as search_after for the next page."""
        return await get("/v1/conversations", params={
            "q": q, "flag": flag, "agent": agent, "error": error, "lang": lang, "experiment": experiment,
            "sentry_issue": sentry_issue, "from": since, "to": until, "size": min(size, 100), "search_after": search_after})

    @tool()
    async def get_conversation(conversation_id: str, include_spans: bool = False) -> dict[str, Any]:
        """One conversation: the transcript, every score with the evaluator that decided it, the tracker state and
        linked Sentry issues. A trace id also works. Spans (tool calls, LLM calls, raw payloads) are large, so
        they are left out unless include_spans is true."""
        out = await get(f"/v1/conversations/{conversation_id}")
        if not include_spans:
            out["n_spans"] = len(out.pop("spans", None) or [])
        return out

    @tool()
    async def similar_conversations(conversation_id: str, k: int = 10) -> list[Any]:
        """The k conversations nearest in embedding space (needs workers started with --embed)."""
        return await get(f"/v1/conversations/{conversation_id}/similar", params={"k": k})

    @tool()
    async def score_summary(since: str | None = None, until: str | None = None, agent: str | None = None,
                            calibration_id: str | None = None, experiment: str | None = None,
                            model: str | None = None, interval: str = "day") -> dict[str, Any]:
        """Per check: conversations scored, how many were flagged, the rate, which evaluator tier decided them,
        the rate by language, the cost, and the rate over time (interval: hour, day, week). Production decisions
        only, unless `experiment` names a run. `model` keeps conversations where the agent ran on that model."""
        return await get("/v1/scores/summary", params={
            "from": since, "to": until, "agent": agent, "calibration_id": calibration_id,
            "experiment": experiment, "model": model, "interval": interval})

    @tool()
    async def model_summary(since: str | None = None, until: str | None = None,
                            agent: str | None = None) -> dict[str, Any]:
        """Per model the agent ran on in production: conversations, cost per conversation (task) and per
        successful one, success rate, tokens per task, effective and list cost per 1M tokens, latency
        p50/p95 and flag rates. Use it to compare the models an agent runs on."""
        return await get("/v1/models/summary", params={"from": since, "to": until, "agent": agent})

    @tool()
    async def list_suites() -> list[Any]:
        """Suites (the checks for one agent) with their latest version and the active and shadow calibrations."""
        return await get("/v1/suites")

    @tool()
    async def get_suite(suite_id: str, version: str | None = None) -> dict[str, Any]:
        """A suite's YAML (latest version unless `version` is given), its version history and its calibrations."""
        out = await get(f"/v1/suites/{suite_id}")
        versions = out["versions"]  # newest first
        pick = next((v for v in versions if v["version"] == version), None) if version else versions[0]
        if pick is None:
            raise ToolError(f"suite {suite_id} has no version {version}")
        return {"id": suite_id, "version": pick["version"], "yaml": pick["yaml"],
                "versions": [{"version": v["version"], "created_at": v["created_at"]} for v in versions],
                "calibrations": out["calibrations"]}

    @tool()
    async def get_calibration(calibration_id: str) -> dict[str, Any]:
        """A calibration: status (candidate, shadow, active, rejected, retired), the route chosen per check, and the
        accuracy, cost and latency report measured on the labels."""
        return await get(f"/v1/calibrations/{calibration_id}")

    @tool()
    async def shadow_comparison(suite_id: str, days: float = 7) -> dict[str, Any]:
        """How a shadow calibration agrees with the audit judge against the active one over the last `days`.
        `promote: true` means promote_calibration will be accepted."""
        return await get(f"/v1/suites/{suite_id}/shadow-comparison", params={"days": days})

    @tool()
    async def drift_alerts(status: str = "open") -> list[Any]:
        """Alerts raised when the audit judge stopped agreeing with the active calibration."""
        return await get("/v1/drift-alerts", params={"status": status})

    @tool()
    async def list_annotation_queues() -> list[Any]:
        """Annotation queues (where humans label conversations) with their checks and progress."""
        return await get("/v1/annotation-queues")

    @tool()
    async def list_queue_items(queue_id: str, status: str | None = None, limit: int = 100) -> list[Any]:
        """Items in a queue; status is one of pending, locked, done, skipped."""
        return await get(f"/v1/annotation-queues/{queue_id}/items", params={"status": status, "limit": limit})

    @tool()
    async def label_agreement(label_set_id: str, calibration_id: str | None = None) -> dict[str, Any]:
        """Human labels against the routed decisions, per check: kappa, accuracy, per-class precision and recall."""
        return await get(f"/v1/label-sets/{label_set_id}/agreement", params={"calibration_id": calibration_id})

    @tool()
    async def list_datasets() -> list[Any]:
        """Datasets (fixed inputs for offline experiments) with their item counts."""
        return await get("/v1/datasets")

    @tool()
    async def get_dataset_items(dataset_id: str, at: str | None = None) -> list[Any]:
        """The items of a dataset, as they were at the ISO timestamp `at` (default: now)."""
        return await get(f"/v1/datasets/{dataset_id}/items", params={"at": at})

    @tool()
    async def list_experiments(dataset_id: str | None = None) -> list[Any]:
        """Experiment runs, newest first, with their summaries."""
        return await get("/v1/experiments", params={"dataset_id": dataset_id})

    @tool()
    async def compare_experiments(run_a: str, run_b: str) -> dict[str, Any]:
        """Per-check differences between two experiment runs."""
        return await get("/v1/experiments/compare", params={"a": run_a, "b": run_b})

    @tool()
    async def audit_log(limit: int = 50) -> list[Any]:
        """Recent mutations in this tenant (who changed what), including those made through MCP."""
        return await get("/v1/audit-log", params={"limit": limit})

    # -- change ----------------------------------------------------------------------------------
    @tool(write=True)
    async def put_suite(suite_id: str, yaml: str, agent_name: str | None = None) -> dict[str, Any]:
        """Register a new version of a suite from its YAML (the checks, the evaluators allowed, the policy). The
        version does nothing to production scoring until a calibration for it is promoted. Rejected with the list
        of problems if the suite uses something the tenant is not allowed to run."""
        return await put(f"/v1/suites/{suite_id}", body={"yaml": yaml, "agent_name": agent_name})

    @tool(write=True)
    async def start_calibration(suite_id: str, label_set_id: str, local_only: bool = False) -> dict[str, Any]:
        """Calibrate the suite's latest version on a label set: picks the cheapest evaluator chain per check that
        meets the target accuracy. Needs 20+ labelled conversations. Runs synchronously and can take minutes;
        hosted evaluators cost money unless local_only. Produces a candidate; it changes nothing in production."""
        return await post(f"/v1/suites/{suite_id}/calibrations", body={"label_set_id": label_set_id, "local_only": local_only})

    @tool(write=True)
    async def start_shadow(calibration_id: str) -> dict[str, Any]:
        """Run a candidate calibration beside the active one on live traffic, scored but not acted on."""
        return await post(f"/v1/calibrations/{calibration_id}/shadow")

    @tool(write=True, destructive=True)
    async def promote_calibration(calibration_id: str) -> dict[str, Any]:
        """Make a calibration the active one: it decides every new conversation of its suite and the previous active
        one is retired. A shadow candidate is refused (409, with the comparison) unless it agrees with the audit
        judge at least as well as the active one. Prefer to confirm with the user first."""
        return await post(f"/v1/calibrations/{calibration_id}/promote")

    @tool(write=True)
    async def reject_calibration(calibration_id: str) -> dict[str, Any]:
        """Discard a candidate or shadow calibration."""
        return await post(f"/v1/calibrations/{calibration_id}/reject")

    @tool(write=True)
    async def run_drift_check(suite_id: str, days: float = 7, min_n: int = 50, threshold: float = 0.05) -> dict[str, Any]:
        """Compare the active calibration's decisions with the audit judge's over the last `days`; opens an alert
        (and a Sentry issue, if connected) when agreement dropped by more than `threshold`."""
        return await post(f"/v1/suites/{suite_id}/drift-check", params={"days": days, "min_n": min_n, "threshold": threshold})

    @tool(write=True, destructive=True)
    async def rescore(conversation_ids: list[str] | None = None, since: str | None = None, until: str | None = None) -> dict[str, Any]:
        """Queue stored conversations to be scored again under the active calibration (give conversation_ids, or a
        `since` date with an optional `until`). Earlier scores are kept, by version. Costs evaluator time and,
        for hosted evaluators, money."""
        return await post("/v1/conversations/rescore", body={"conversation_ids": conversation_ids, "from": since, "to": until})

    @tool(write=True)
    async def create_annotation_queue(suite_id: str, name: str, checks: list[str], fill_rules: list[dict] | None = None,
                                      label_set_id: str | None = None) -> dict[str, Any]:
        """A queue for humans to label conversations on the given checks. fill_rules choose what lands in it
        (see fill_annotation_queue)."""
        return await post("/v1/annotation-queues", body={
            "suite_id": suite_id, "name": name, "checks": checks, "fill_rules": fill_rules or [], "label_set_id": label_set_id})

    @tool(write=True)
    async def add_to_annotation_queue(queue_id: str, conversation_ids: list[str]) -> dict[str, Any]:
        """Put specific conversations in front of the annotators."""
        return await post(f"/v1/annotation-queues/{queue_id}/items", body={"conversation_ids": conversation_ids})

    @tool(write=True)
    async def fill_annotation_queue(queue_id: str, days: float = 1) -> dict[str, Any]:
        """Add conversations from the last `days` that match the queue's fill rules."""
        return await post(f"/v1/annotation-queues/{queue_id}/fill", params={"days": days})

    @tool(write=True)
    async def create_dataset(name: str, description: str | None = None, items: list[dict] | None = None) -> dict[str, Any]:
        """A dataset of fixed inputs for offline experiments; items are {id, input, expected}."""
        return await post("/v1/datasets", body={"name": name, "description": description, "items": items or []})

    @tool(write=True)
    async def add_dataset_items(dataset_id: str, items: list[dict] | None = None,
                                from_conversations: list[str] | None = None) -> dict[str, Any]:
        """Add items, and/or snapshot production conversations (by id) as items."""
        return await post(f"/v1/datasets/{dataset_id}/items", body={"items": items or [], "from_conversations": from_conversations or []})

    return mcp


def serve(transport: str = "stdio", host: str = "127.0.0.1", port: int = 8765, read_only: bool = False,
          url: str | None = None) -> None:
    server = build_server(EvalsAPI(url), read_only)
    if transport == "stdio":
        server.run("stdio")
    else:  # one key serves every client of this process: keep it on localhost or behind your own authentication
        server.run("streamable-http", host=host, port=port)
