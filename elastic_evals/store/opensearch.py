"""OpenSearch event store (DESIGN.md §5.3).

Layout: for each kind (obs, payload, scores, traces, embeddings, metrics) and each index pool
('shared' for pooled tenants, or a dedicated tenant's own pool) there is a write alias
`{prefix}-{kind}-{pool}` over time-based indices `{prefix}-{kind}-{pool}-000001, -000002, ...`
that ISM rolls over by primary shard size. Not data streams: we need custom _id for idempotent
writes (`op_type=create` with deterministic ids) and cheap `delete_by_query` for GDPR.

Because an _id is only unique inside one index, a document rewritten after a rollover can
exist twice; readers dedupe by _id (`dedupe`), keeping the newest copy.
"""

import json
import os
from importlib import resources

import httpx

KINDS = ("obs", "payload", "scores", "traces", "embeddings", "metrics")

# days hot, days until delete (DESIGN.md §7). Scores and metrics outlive payloads.
DEFAULT_RETENTION = {
    "obs": (14, 180), "payload": (14, 30), "scores": (14, 400), "traces": (14, 400),
    "embeddings": (14, 180), "metrics": (14, 800),
}


def schema(name: str) -> dict:
    return json.loads(resources.files("elastic_evals.store").joinpath(f"schema/{name}.json").read_text())


class OpenSearchError(RuntimeError):
    pass


class OpenSearch:
    def __init__(self, url: str | None = None, prefix: str = "ee", auth: tuple[str, str] | None = None,
                 verify: bool | str = True, timeout: float = 30):
        self.url = (url or os.environ.get("EE_OPENSEARCH_URL", "http://localhost:9200")).rstrip("/")
        self.prefix = prefix
        self.http = httpx.Client(base_url=self.url, auth=auth, verify=verify, timeout=timeout)

    # -- low level -------------------------------------------------------------------------------
    def req(self, method: str, path: str, body=None, ok=(200, 201), **params):
        if isinstance(body, (list, tuple)):  # NDJSON
            content = "".join(json.dumps(x, separators=(",", ":"), default=str) + "\n" for x in body)
            r = self.http.request(method, path, content=content, params=params,
                                  headers={"Content-Type": "application/x-ndjson"})
        else:
            r = self.http.request(method, path, json=body, params=params)
        if r.status_code not in ok:
            raise OpenSearchError(f"{method} {path} -> {r.status_code}: {r.text[:800]}")
        return r.json() if r.content else {}

    def alias(self, kind: str, pool: str = "shared") -> str:
        return f"{self.prefix}-{kind}-{pool}"

    def pattern(self, kind: str, pool: str | None = None) -> str:
        return f"{self.prefix}-{kind}-{pool or '*'}-*"

    # -- setup -----------------------------------------------------------------------------------
    def setup(self, pools=("shared",), shards: int = 1, replicas: int = 0, retention: dict | None = None,
              rollover_size: str = "40gb", rollover_age: str = "30d", kinds=KINDS):
        """Idempotent: ISM policies, component + index templates, and the first index per (kind, pool)."""
        retention = {**DEFAULT_RETENTION, **(retention or {})}
        for kind in kinds:
            spec = schema(kind)
            hot, delete = retention[kind]
            policy = json.dumps(schema("ism")).replace("{kind}", kind).replace("{pattern}", self.pattern(kind)) \
                .replace("{rollover_size}", rollover_size).replace("{rollover_age}", rollover_age) \
                .replace('"{hot_days}d"', f'"{hot}d"').replace('"{delete_days}d"', f'"{delete}d"')
            policy = json.loads(policy)
            policy.pop("_comment", None)
            self._put_policy(f"{self.prefix}-{kind}", policy)
            settings = {"number_of_shards": shards, "number_of_replicas": replicas, **spec.get("settings", {})}
            self.req("PUT", f"/_component_template/{self.prefix}-{kind}",
                     {"template": {"settings": settings, "mappings": spec["mappings"]}})
            for pool in pools:
                self.req("PUT", f"/_index_template/{self.alias(kind, pool)}", {
                    "index_patterns": [self.pattern(kind, pool)],
                    "priority": 200,
                    "composed_of": [f"{self.prefix}-{kind}"],
                    "template": {"settings": {"plugins.index_state_management.rollover_alias": self.alias(kind, pool)}},
                })
                alias = self.alias(kind, pool)
                if self.http.head(f"/_alias/{alias}").status_code == 404:
                    self.req("PUT", f"/{alias}-000001", {"aliases": {alias: {"is_write_index": True}}},
                             ok=(200, 201, 400))

    def _put_policy(self, name, policy):
        r = self.http.get(f"/_plugins/_ism/policies/{name}")
        if r.status_code == 200:
            cur = r.json()
            self.req("PUT", f"/_plugins/_ism/policies/{name}", policy,
                     if_seq_no=cur["_seq_no"], if_primary_term=cur["_primary_term"])
        else:
            self.req("PUT", f"/_plugins/_ism/policies/{name}", policy)

    def teardown(self):
        """Delete every index, template and policy with our prefix (tests and dev only)."""
        self.req("DELETE", f"/{self.prefix}-*", ok=(200, 404), expand_wildcards="all")
        for kind in KINDS:
            for path in (f"/_index_template/{self.prefix}-{kind}-*", f"/_component_template/{self.prefix}-{kind}",
                         f"/_plugins/_ism/policies/{self.prefix}-{kind}"):
                self.http.delete(path)

    # -- writes ----------------------------------------------------------------------------------
    def bulk(self, kind: str, docs, pool: str = "shared", op: str = "index", refresh: bool | str = False):
        """docs: iterable of (id, doc, routing). op='create' makes re-delivery idempotent (409 = already there).
        id None lets OpenSearch assign one (required by append-only indices such as metrics).
        Returns (written, duplicates); raises on any other error."""
        lines = []
        for _id, doc, routing in docs:
            meta = {"_index": self.alias(kind, pool)}
            if _id is not None:
                meta["_id"] = _id
            if routing:
                meta["routing"] = routing
            lines += [{op: meta}, doc]
        if not lines:
            return 0, 0
        res = self.req("POST", "/_bulk", lines, refresh=str(refresh).lower() if refresh else "false")
        written = dup = 0
        errors = []
        for item in res["items"]:
            r = next(iter(item.values()))
            if r.get("status") in (200, 201):
                written += 1
            elif r.get("status") == 409:
                dup += 1
            else:
                errors.append(r.get("error"))
        if errors:
            raise OpenSearchError(f"bulk {kind}: {len(errors)} errors, first: {json.dumps(errors[0])[:500]}")
        return written, dup

    def refresh(self, kind: str | None = None):
        self.req("POST", f"/{self.pattern(kind) if kind else self.prefix + '-*'}/_refresh")

    def delete_by_query(self, kind: str, query: dict, pool: str | None = None):
        return self.req("POST", f"/{self.pattern(kind, pool)}/_delete_by_query", {"query": query},
                        conflicts="proceed", refresh="true")

    # -- reads -----------------------------------------------------------------------------------
    def search(self, kind: str, body: dict, pool: str | None = None, routing: str | None = None):
        params = {"routing": routing} if routing else {}
        return self.req("POST", f"/{self.pattern(kind, pool)}/_search", body, ignore_unavailable="true", **params)

    def hits(self, kind: str, body: dict, pool: str | None = None, routing: str | None = None):
        res = self.search(kind, body, pool, routing)
        return dedupe(res["hits"]["hits"])

    def count(self, kind: str, query: dict | None = None, pool: str | None = None) -> int:
        return self.req("POST", f"/{self.pattern(kind, pool)}/_count", {"query": query or {"match_all": {}}},
                        ignore_unavailable="true")["count"]


def dedupe(hits, newest_field: str | None = None):
    """Collapse copies of the same _id that live in different indices after a rollover."""
    best = {}
    for h in hits:
        cur = best.get(h["_id"])
        if cur is None or h["_index"] > cur["_index"]:  # later index in the rollover sequence wins
            best[h["_id"]] = h
    order = {h["_id"]: i for i, h in enumerate(hits)}
    return [dict(h["_source"], _id=h["_id"]) for h in sorted(best.values(), key=lambda h: order[h["_id"]])]


def tenant_filter(tenant_id: str) -> dict:
    """The mandatory filter on every read: document-level security is never the only control."""
    return {"term": {"tenant_id": tenant_id}}
