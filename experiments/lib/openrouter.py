"""Minimal OpenRouter client: chat completions + Jev Decisions API.

Every request is cached on disk (keyed by the request body), so re-running an
experiment never pays twice. Spend is recorded in a ledger and a hard budget cap
stops new paid requests once it is reached.
"""

import asyncio
import hashlib
import json
import os
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT / ".cache" / "openrouter"
LEDGER = ROOT / ".cache" / "ledger.jsonl"
BUDGET_USD = float(os.environ.get("EVAL_BUDGET_USD", "5"))

CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"


class BudgetExceeded(RuntimeError):
    pass


class CacheMiss(RuntimeError):
    """Raised in offline mode (EVAL_OFFLINE=1) when a request is not already cached."""


def _api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is not set (load it from an env file outside git)")
    return key


def spent_usd() -> float:
    if not LEDGER.exists():
        return 0.0
    return sum(json.loads(line)["cost"] for line in LEDGER.read_text().splitlines() if line.strip())


def _cache_path(url: str, body: dict) -> Path:
    digest = hashlib.sha256((url + json.dumps(body, sort_keys=True)).encode()).hexdigest()
    return CACHE_DIR / digest[:2] / f"{digest}.json"


class OpenRouter:
    def __init__(self, concurrency: int = 8):
        self._sem = asyncio.Semaphore(concurrency)
        self._client = httpx.AsyncClient(timeout=120)
        self._lock = asyncio.Lock()

    async def close(self):
        await self._client.aclose()

    async def _post(self, url: str, body: dict, tag: str) -> dict:
        path = _cache_path(url, body)
        if path.exists():
            cached = json.loads(path.read_text())
            cached["_cached"] = True
            return cached
        if os.environ.get("EVAL_OFFLINE") == "1":
            raise CacheMiss(url)
        async with self._sem:
            if spent_usd() >= BUDGET_USD:
                raise BudgetExceeded(f"budget of ${BUDGET_USD} reached")
            for attempt in range(10):
                t0 = time.perf_counter()
                try:
                    res = await self._client.post(
                        url, json=body, headers={"Authorization": f"Bearer {_api_key()}"}
                    )
                except httpx.TransportError:
                    await asyncio.sleep(2**attempt)
                    continue
                latency = time.perf_counter() - t0
                in_flight = res.status_code == 402 and "openrouter_in_flight_budget" in res.text
                if res.status_code == 429 or res.status_code >= 500 or in_flight:
                    await asyncio.sleep(float(res.headers.get("Retry-After") or 2**attempt))
                    continue
                data = res.json()
                if res.status_code != 200 or "error" in data:
                    raise RuntimeError(f"{url} {res.status_code}: {json.dumps(data)[:500]}")
                break
            else:
                raise RuntimeError(f"{url}: retries exhausted")
        data["_latency_s"] = latency
        cost = float((data.get("usage") or {}).get("cost") or 0)
        async with self._lock:
            LEDGER.parent.mkdir(parents=True, exist_ok=True)
            with LEDGER.open("a") as f:
                f.write(json.dumps({"tag": tag, "model": body.get("model"), "cost": cost}) + "\n")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data))
        data["_cached"] = False
        return data

    async def chat(self, model: str, messages: list, tag: str, **params) -> dict:
        body = {"model": model, "messages": messages, **params}
        return await self._post(CHAT_URL, body, tag)

    async def decide(self, model: str, state, questions: dict, tag: str) -> dict:
        body = {"model": model, "state": state, "questions": questions}
        return await self._post(DECISIONS_URL, body, tag)


def load_env(path: str | None = None):
    """Load KEY=VALUE lines from an env file kept outside the repository."""
    path = path or os.environ.get("EVAL_ENV_FILE")
    if not path or not Path(path).exists():
        return
    for line in Path(path).read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
