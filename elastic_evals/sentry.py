"""Sentry integration (DESIGN.md §8). Sentry handles exceptions and the issue workflow; we handle
traces, content and scores; one W3C trace id links them. No conversation content, no scores
and no span events ever go to Sentry.

Agent side:
    import sentry_sdk
    from elastic_evals.sentry import before_send
    sentry_sdk.init(dsn=..., before_send=before_send(ui_url="https://evals.example.com"),
                    send_default_pii=False)       # no traces_sample_rate: OTel owns tracing
    # (call it after the OTel tracer provider is set, or pass tracer_provider=)

  `before_send` tags the event with the active OTel trace id, conversation id and agent, adds a
  deep link, writes `ee.sentry.event_id` on the active span and marks it ERROR (a handled
  exception otherwise leaves the span OK and its content could be dropped by retention).

Server side (the bridge, mounted on the API when EE_SENTRY_CLIENT_SECRET is set):
  POST /v1/sentry/webhook   HMAC-verified Sentry integration webhooks:
      installation          remembers the installation uuid
      issue / error /       links the issue to its trace (sentry_links, traces-* rollup) and
      event_alert           creates an External Issue link ("View agent trace") on the issue;
                            subscribe to both issue and error: issue.created fires once per issue,
                            error.created for every event (each later conversation that hits it)
      issue resolved/...    copies the issue status back
  quality_regression()      drift alerts become Sentry messages with a stable fingerprint per
                            check, suite and tenant, so Sentry alerting covers quality
"""

import hashlib
import hmac
import json
import logging
import os
import time

import httpx

log = logging.getLogger("elastic_evals.sentry")


# -- agent side ------------------------------------------------------------------------------------
try:
    from opentelemetry.sdk.trace import SpanProcessor as _SpanProcessor
except ImportError:  # the server side does not need the OTel SDK
    _SpanProcessor = object


class ConversationIndex(_SpanProcessor):
    """OTel span processor remembering each trace's conversation id and agent name. Usually only the
    root span carries them, and errors are raised in child spans (tool calls, model calls)."""

    def __init__(self, size: int = 10000):
        from collections import OrderedDict
        self.size, self.by_trace = size, OrderedDict()

    def on_start(self, span, parent_context=None):
        attrs = span.attributes or {}
        conv, agent = attrs.get("gen_ai.conversation.id"), attrs.get("gen_ai.agent.name")
        if conv or agent:
            tid = span.get_span_context().trace_id
            old = self.by_trace.pop(tid, (None, None))
            self.by_trace[tid] = (conv or old[0], agent or old[1])
            while len(self.by_trace) > self.size:
                self.by_trace.popitem(last=False)

    def get(self, trace_id: int):
        return self.by_trace.get(trace_id, (None, None))



def before_send(ui_url: str | None = None, tenant_id: str | None = None, tracer_provider=None):
    """`tracer_provider` (default: the global one) gets a ConversationIndex, so events raised in child
    spans still carry the conversation id and agent."""
    from opentelemetry import trace

    index = ConversationIndex()
    provider = tracer_provider or trace.get_tracer_provider()
    if hasattr(provider, "add_span_processor"):
        provider.add_span_processor(index)

    def hook(event, hint):
        span = trace.get_current_span()
        ctx = span.get_span_context()
        if not ctx.is_valid:
            return event
        trace_id = format(ctx.trace_id, "032x")
        attrs = getattr(span, "attributes", None) or {}
        known_conv, known_agent = index.get(ctx.trace_id)
        conv = attrs.get("gen_ai.conversation.id") or known_conv
        agent = attrs.get("gen_ai.agent.name") or known_agent
        tags = event.setdefault("tags", {})
        tags["trace_id"] = trace_id
        if conv:
            tags["gen_ai.conversation.id"] = conv
        if agent:
            tags["gen_ai.agent.name"] = agent
        if tenant_id:
            tags["ee.tenant_id"] = tenant_id
        link = {"trace_id": trace_id, "conversation_id": conv or trace_id}
        if ui_url:
            link["url"] = f"{ui_url.rstrip('/')}/conversations/{conv or trace_id}"
        event.setdefault("contexts", {})["elastic_evals"] = link
        if event.get("event_id"):
            span.set_attribute("ee.sentry.event_id", event["event_id"])
        span.set_status(trace.Status(trace.StatusCode.ERROR))
        return event

    return hook


# -- server side -----------------------------------------------------------------------------------
def verify(secret: str, body: bytes, signature: str | None) -> bool:
    if not signature:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


class Bridge:
    def __init__(self, client_secret: str, token: str | None = None, base_url: str = "https://sentry.io",
                 org: str | None = None, ui_url: str | None = None, installation: str | None = None,
                 http: httpx.Client | None = None, default_tenant: str | None = None, web_url: str | None = None):
        self.secret, self.token, self.base = client_secret, token, base_url.rstrip("/")
        self.web = (web_url or base_url).rstrip("/")  # what a browser reaches, when the API URL is internal
        self.org, self.ui_url, self.installation = org, ui_url, installation
        self.http = http or httpx.Client(timeout=10)
        self.default_tenant = default_tenant
        self._lookup_cache = {}

    @classmethod
    def from_env(cls):
        return cls(os.environ["EE_SENTRY_CLIENT_SECRET"], os.environ.get("EE_SENTRY_TOKEN"),
                   os.environ.get("EE_SENTRY_URL", "https://sentry.io"), os.environ.get("EE_SENTRY_ORG"),
                   os.environ.get("EE_UI_URL"), os.environ.get("EE_SENTRY_INSTALLATION"),
                   default_tenant=os.environ.get("EE_SENTRY_DEFAULT_TENANT"), web_url=os.environ.get("EE_SENTRY_WEB_URL"))

    def issue_url(self, issue_id) -> str | None:
        return f"{self.web}/organizations/{self.org}/issues/{issue_id}/" if self.org else None

    def _api(self, method, path, **kw):
        if not self.token:
            raise RuntimeError("EE_SENTRY_TOKEN is not set")
        r = self.http.request(method, f"{self.base}/api/0/{path.lstrip('/')}",
                              headers={"Authorization": f"Bearer {self.token}"}, **kw)
        r.raise_for_status()
        return r.json() if r.content else {}

    # Sentry -> us -------------------------------------------------------------------------------
    @staticmethod
    def _tags(event):
        tags = event.get("tags") or []
        if isinstance(tags, dict):
            return tags
        return {t[0]: t[1] for t in tags if isinstance(t, (list, tuple)) and len(t) == 2} | \
               {t["key"]: t["value"] for t in tags if isinstance(t, dict) and "key" in t}

    def _trace_of_issue(self, issue_id):
        try:
            ev = self._api("GET", f"issues/{issue_id}/events/latest/")
        except (httpx.HTTPError, RuntimeError) as exc:
            log.warning("cannot read latest event of issue %s: %s", issue_id, exc)
            return None, None, None
        tags = self._tags(ev)
        ctx = (ev.get("contexts") or {}).get("trace") or {}
        return (tags.get("trace_id") or ctx.get("trace_id"), tags.get("ee.tenant_id"), ev.get("eventID") or ev.get("id"))

    def link(self, pg, registry, os_, trace_id, issue, tenant=None, event_id=None):
        tenant = tenant or self.default_tenant
        if not (trace_id and tenant):
            return False
        issue_id = str(issue["id"])
        status = issue.get("status", "unresolved")
        pg.exec("""INSERT INTO sentry_links (tenant_id, trace_id, sentry_issue_id, sentry_event_id, project_slug, status)
                   VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (tenant_id, trace_id, sentry_issue_id)
                   DO UPDATE SET status=EXCLUDED.status, sentry_event_id=coalesce(EXCLUDED.sentry_event_id, sentry_links.sentry_event_id),
                   updated_at=now()""", tenant, trace_id, issue_id, event_id,
                (issue.get("project") or {}).get("slug"), status)
        self._update_rollups(registry, os_, tenant, [trace_id], issue_id, status)
        link = pg.one("SELECT external_issue_created FROM sentry_links WHERE tenant_id=%s AND trace_id=%s AND sentry_issue_id=%s",
                      tenant, trace_id, issue_id)
        if self.installation and self.ui_url and not link["external_issue_created"]:
            try:
                self._api("POST", f"sentry-app-installations/{self.installation}/external-issues/", json={
                    "issueId": int(issue_id) if issue_id.isdigit() else issue_id,
                    # Sentry keeps one External Issue per issue and integration, so it opens every
                    # conversation linked to the issue rather than the first trace
                    "webUrl": f"{self.ui_url.rstrip('/')}/conversations?sentry_issue={issue_id}",
                    "project": "elastic-evals", "identifier": "agent conversations"})
                pg.exec("UPDATE sentry_links SET external_issue_created=true WHERE tenant_id=%s AND trace_id=%s AND sentry_issue_id=%s",
                        tenant, trace_id, issue_id)
            except (httpx.HTTPError, RuntimeError) as exc:
                log.warning("external issue link failed for %s: %s", issue_id, exc)
        return True

    def set_status(self, pg, registry, os_, issue_id, status):
        rows = pg.all("UPDATE sentry_links SET status=%s, updated_at=now() WHERE sentry_issue_id=%s RETURNING tenant_id, trace_id",
                      status, str(issue_id))
        by_tenant = {}
        for r in rows:
            by_tenant.setdefault(r["tenant_id"], []).append(r["trace_id"])
        for tenant, traces in by_tenant.items():
            self._update_rollups(registry, os_, tenant, traces, str(issue_id), status)
        return len(rows)

    @staticmethod
    def _update_rollups(registry, os_, tenant, trace_ids, issue_id, status):
        if os_ is None:
            return
        os_.req("POST", f"/{os_.pattern('traces', registry.pool(tenant))}/_update_by_query", {
            "query": {"bool": {"filter": [{"term": {"tenant_id": tenant}}, {"terms": {"trace_ids": trace_ids}}]}},
            "script": {"lang": "painless", "params": {"issue": issue_id, "status": status}, "source": (
                "if (ctx._source.sentry_issue_ids == null) { ctx._source.sentry_issue_ids = []; } "
                "if (!ctx._source.sentry_issue_ids.contains(params.issue)) { ctx._source.sentry_issue_ids.add(params.issue); } "
                "ctx._source.sentry_status = params.status;")}}, conflicts="proceed", refresh="true",
            ignore_unavailable="true")

    def handle(self, pg, registry, os_, resource: str, payload: dict) -> dict:
        action, data = payload.get("action"), payload.get("data") or {}
        if resource == "installation":
            inst = payload.get("installation") or data.get("installation") or {}
            self.installation = inst.get("uuid") or self.installation
            return {"installation": self.installation}
        if resource in ("event_alert", "error"):
            # error.created arrives for every error event, including new events of an existing issue
            # (issue.created fires once per issue, so it alone misses every later conversation)
            event = data.get("event") or data.get("error") or {}
            tags = self._tags(event)
            trace_id = tags.get("trace_id") or ((event.get("contexts") or {}).get("trace") or {}).get("trace_id")
            issue = {"id": event.get("issue_id") or event.get("groupID"), "status": "unresolved",
                     "project": {"slug": event.get("project")}}
            ok = issue["id"] is not None and self.link(pg, registry, os_, trace_id, issue, tags.get("ee.tenant_id"),
                                                       event.get("event_id"))
            return {"linked": bool(ok)}
        if resource == "issue":
            issue = data.get("issue") or {}
            out = {}
            if action in ("resolved", "ignored", "unresolved", "archived"):
                status = {"archived": "ignored"}.get(action, action)
                out["updated"] = self.set_status(pg, registry, os_, issue.get("id"), status)
            if action in ("created", "unresolved"):
                # a new issue, or a regression: link the conversation of the event that opened it
                # (Sentry need not send error.created for a regression event)
                trace_id, tenant, event_id = self._trace_of_issue(issue.get("id"))
                out["linked"] = self.link(pg, registry, os_, trace_id, dict(issue, status="unresolved"), tenant, event_id)
            if out:
                return out
            return {"ignored": action}
        return {"ignored": resource}

    # us -> Sentry ---------------------------------------------------------------------------------
    def issues_for_trace(self, trace_id: str, ttl: float = 300) -> list[dict]:
        """Fallback when no ee.sentry.event_id was recorded: search issues by trace (partly undocumented,
        so failures degrade to an empty list)."""
        hit = self._lookup_cache.get(trace_id)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        try:
            res = self._api("GET", f"organizations/{self.org}/issues/", params={"query": f"trace:{trace_id}"})
        except (httpx.HTTPError, RuntimeError) as exc:
            log.info("issue lookup for %s failed: %s", trace_id, exc)
            res = []
        out = [{"id": i.get("id"), "title": i.get("title"), "status": i.get("status"), "permalink": i.get("permalink")}
               for i in res]
        self._lookup_cache[trace_id] = (time.time(), out)
        return out

    def quality_regression(self, alert: dict):
        """A drift alert as a Sentry message: aggregated, no content, stable fingerprint."""
        import sentry_sdk
        with sentry_sdk.new_scope() as scope:
            scope.fingerprint = ["ee-quality", alert["tenant_id"], alert["suite_id"], alert["check_name"], alert.get("lang") or ""]
            scope.set_tag("ee.tenant_id", alert["tenant_id"])
            scope.set_tag("ee.check", alert["check_name"])
            scope.set_tag("ee.suite", alert["suite_id"])
            scope.set_context("drift", {k: alert[k] for k in ("baseline", "observed", "n", "calibration_id", "lang")})
            sentry_sdk.capture_message(
                f"Evaluator drift: {alert['check_name']} agreement {100 * alert['observed']:.1f}% "
                f"(baseline {100 * alert['baseline']:.1f}%)", level="warning")

    # FastAPI router -------------------------------------------------------------------------------
    def router(self, pg, registry, os_):
        from fastapi import APIRouter, HTTPException, Request
        r = APIRouter()

        @r.post("/v1/sentry/webhook")
        async def webhook(request: Request):
            body = await request.body()
            if not verify(self.secret, body, request.headers.get("sentry-hook-signature")):
                raise HTTPException(401, "bad signature")
            resource = request.headers.get("sentry-hook-resource", "")
            from starlette.concurrency import run_in_threadpool
            payload = json.loads(body or b"{}")
            result = await run_in_threadpool(self.handle, pg, registry, os_, resource, payload)
            log.info("sentry webhook %s.%s -> %s", resource, payload.get("action"), result)
            return result

        return r
