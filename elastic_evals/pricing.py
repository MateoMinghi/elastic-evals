"""Model prices and re-pricing (DESIGN.md §5.2 step 4; Langfuse pain point "pricing fixed at ingestion").

The collector prices each span at ingest from a prices file; `PriceBook` implements the same rules
in Python (regex on the model, newest start date wins within a priority, tiers by input size,
exclusive token buckets), so stored spans can be re-priced after a price changes:

    python -m elastic_evals reprice --tenant acme --from 2026-10-01 [--to ...]

`obs` documents are immutable; re-pricing rewrites the cost on the `traces-*` rollups and records
the price ids used. The collector picks up a changed prices file within `prices_reload_interval`.
"""

import datetime as dt
import re
from dataclasses import dataclass


@dataclass
class Price:
    id: str
    pattern: re.Pattern
    start: dt.datetime
    priority: int
    tiers: list


class PriceBook:
    def __init__(self, rows: list[dict]):
        prices = []
        for r in rows:
            start = dt.datetime.fromisoformat(str(r.get("start_date") or "1970-01-01")).replace(tzinfo=dt.timezone.utc)
            prices.append(Price(r.get("id") or f"{r['model_pattern']}@{r.get('start_date', '')}",
                                re.compile(r["model_pattern"]), start, int(r.get("priority", 0)),
                                sorted(r["tiers"], key=lambda t: t.get("above_input_tokens", 0))))
        self.prices = sorted(prices, key=lambda p: (-p.priority, -p.start.timestamp()))

    def cost(self, model: str | None, usage: dict, at: dt.datetime):
        """(usd, price id) or (None, None) when no price matches."""
        if not model:
            return None, None
        for p in self.prices:
            if not p.pattern.search(model) or at < p.start or not p.tiers:
                continue
            tier = p.tiers[0]
            for t in p.tiers:
                if usage.get("input_tokens", 0) >= t.get("above_input_tokens", 0):
                    tier = t
            cache_r, cache_w = usage.get("cache_read_tokens", 0), usage.get("cache_write_tokens", 0)
            plain = max(0, usage.get("input_tokens", 0) - cache_r - cache_w)
            usd = (plain * tier.get("input", 0) + cache_r * tier.get("cache_read", 0)
                   + cache_w * tier.get("cache_write", 0) + usage.get("output_tokens", 0) * tier.get("output", 0)) / 1e6
            return usd, p.id
        return None, None


def rows_from_db(pg) -> list[dict]:
    return [{"id": f"price-{r['id']}", "model_pattern": r["model_pattern"], "start_date": str(r["start_date"]),
             "priority": r["priority"], "tiers": r["tiers"]}
            for r in pg.all("SELECT * FROM model_prices ORDER BY id")]


def reprice(os_, pool: str, tenant: str, book: PriceBook, since: str, until: str | None = None, page: int = 500) -> dict:
    """Recompute the cost of every conversation started in [since, until) from its spans' usage."""
    from .conversation import ns
    rng = {"gte": since, **({"lt": until} if until else {})}
    after, changed, seen = None, 0, 0
    while True:
        body = {"size": page, "_source": ["conversation_id", "trace_ids", "cost_usd"],
                "query": {"bool": {"filter": [{"term": {"tenant_id": tenant}}, {"range": {"start_time": rng}}]}},
                "sort": [{"conversation_id": "asc"}]}
        if after:
            body["search_after"] = after
        res = os_.search("traces", body, pool)
        hits = res["hits"]["hits"]
        if not hits:
            break
        after = hits[-1]["sort"]
        trace_ids = sorted({t for h in hits for t in h["_source"].get("trace_ids", [])})
        spans = os_.hits("obs", {"size": 10000, "_source": ["trace_id", "usage", "request_model", "response_model", "start_time"],
                                 "query": {"bool": {"filter": [{"term": {"tenant_id": tenant}}, {"terms": {"trace_id": trace_ids}},
                                                               {"exists": {"field": "usage"}}]}}}, pool)
        per_trace, ids_used = {}, {}
        for s in spans:
            at = dt.datetime.fromtimestamp(ns(s["start_time"]) / 1e9, dt.timezone.utc)
            usd, pid = book.cost(s.get("response_model") or s.get("request_model"), s.get("usage") or {}, at)
            per_trace[s["trace_id"]] = per_trace.get(s["trace_id"], 0.0) + (usd or 0.0)
            if pid:
                ids_used.setdefault(s["trace_id"], set()).add(pid)
        updates = []
        for h in hits:
            src = h["_source"]
            seen += 1
            cost = sum(per_trace.get(t, 0.0) for t in src.get("trace_ids", []))
            if abs(cost - (src.get("cost_usd") or 0.0)) > 1e-12:
                changed += 1
                updates.append((h["_id"], {"doc": {"cost_usd": cost, "repriced_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                                                   "price_ids": sorted({p for t in src.get("trace_ids", []) for p in ids_used.get(t, ())})}},
                                None, h["_index"]))
        if updates:
            os_.update(updates)
    return {"conversations": seen, "repriced": changed}
