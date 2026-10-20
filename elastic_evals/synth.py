"""Synthetic labelled test sets (DESIGN.md §6.9, lesson L13).

A spec file describes the domain records (built in code, so the ground truth is known), the
checks and how to plant each one or its hard negative, and the generator model (choose a model
family different from your judges). Labels come from the spec, never from a judge; structural
checks can be re-derived from the generated turns with a rule (`verify`). Every conversation is
validated structurally and regenerated once if malformed.

Synthetic sets are for cold start and CI regression sets, always followed by real labels.

    python -m elastic_evals synth --spec examples/banking_synth.yaml --n 50 --out synth.jsonl
"""

import asyncio
import json
import random
from pathlib import Path

import yaml

from .evaluators import resolve_rule


def _value(spec, rng):
    if isinstance(spec, list):
        return rng.choice(spec)
    if isinstance(spec, dict) and "range" in spec:
        lo, hi = spec["range"]
        step = spec.get("step", 1)
        return rng.randrange(lo, hi + 1, step)
    return spec


def make_item(spec: dict, rng: random.Random, i: int) -> dict:
    templates = spec["records"]
    tpl = rng.choices(templates, weights=[t.get("weight", 1) for t in templates])[0]
    record = {k: _value(v, rng) for k, v in tpl["fields"].items()}
    labels, instructions, skipped = {}, [], set()
    for name, chk in spec["checks"].items():
        if chk.get("applies_to") and record.get("operation") not in chk["applies_to"]:
            labels[name] = 0
            skipped.add(name)
            continue
        present = rng.random() < chk.get("rate", 0.3)
        if present and chk.get("when_present"):
            for field, allowed in chk["when_present"].items():
                if record.get(field) not in allowed:
                    record[field] = rng.choice(allowed)
        labels[name] = int(present)
    for name, chk in spec["checks"].items():
        if name in skipped:
            continue
        text = chk["present"] if labels[name] else chk.get("absent")
        if text:
            instructions.append("- " + text.format(**record))
    lo, hi = spec.get("turns", [6, 12])
    return {"id": f"{spec.get('prefix', spec['name'])}-{i:04d}", "lang": rng.choice(spec.get("languages", ["en"])),
            "record": record, "labels": labels, "_instructions": instructions, "_n_turns": rng.randint(lo, hi)}


def prompt(spec: dict, item: dict) -> list[dict]:
    lang = {"es": "Spanish (Rioplatense)", "en": "English", "pt": "Portuguese"}.get(item["lang"], item["lang"])
    user = (f"System-of-record entry for the operation (ground truth):\n{json.dumps(item['record'], ensure_ascii=False)}\n\n"
            f"Write one conversation in {lang}, about {item['_n_turns']} turns, including tool calls where natural "
            f"(tools: {', '.join(spec.get('tools', []))}). It must satisfy ALL of these:\n" + "\n".join(item["_instructions"]) +
            "\n\nReturn only JSON: {\"turns\": [{\"role\": \"user\"|\"assistant\", \"text\": \"...\"} | "
            "{\"role\": \"tool\", \"name\": \"...\", \"args\": {...}, \"result\": \"...\"}]}")
    return [{"role": "system", "content": spec["brief"].strip()}, {"role": "user", "content": user}]


def valid_turns(turns) -> bool:
    if not isinstance(turns, list) or len(turns) < 2:
        return False
    for t in turns:
        if not isinstance(t, dict) or t.get("role") not in ("user", "assistant", "tool"):
            return False
        if t["role"] == "tool":
            if not t.get("name") or not isinstance(t.get("args", {}), dict):
                return False
        elif not isinstance(t.get("text"), str) or not t["text"].strip():
            return False
    roles = {t["role"] for t in turns}
    return "user" in roles and "assistant" in roles


async def generate_async(spec: dict, n: int, client, seed: int = 0, concurrency: int = 8):
    rng = random.Random(seed)
    items = [make_item(spec, rng, i) for i in range(n)]
    model = spec["generator"]
    params = {"temperature": spec.get("temperature", 0.9), "max_tokens": spec.get("max_tokens", 3000),
              "response_format": {"type": "json_object"}}
    if spec.get("reasoning") is False:
        params["reasoning"] = {"enabled": False}
    sem = asyncio.Semaphore(concurrency)

    async def one(item):
        async with sem:
            for attempt in range(2):
                msgs = prompt(spec, item)
                if attempt:
                    msgs[-1]["content"] += "\n(Previous answer was malformed. Follow the JSON format exactly.)"
                try:
                    res = await client.chat(model, msgs, tag="synth", **params)
                except Exception:  # noqa: BLE001 - one failed item must not stop the set
                    continue
                content = res["choices"][0]["message"]["content"] or ""
                try:
                    turns = json.loads(content[content.find("{"): content.rfind("}") + 1])["turns"]
                except (json.JSONDecodeError, KeyError, TypeError):
                    continue
                if valid_turns(turns):
                    return turns, float((res.get("usage") or {}).get("cost") or 0)
        return None, 0.0

    results = await asyncio.gather(*(one(it) for it in items))
    out, cost, failed = [], 0.0, 0
    for item, (turns, usd) in zip(items, results):
        cost += usd
        if turns is None:
            failed += 1
            continue
        conv = {k: v for k, v in item.items() if not k.startswith("_")}
        conv["turns"] = turns
        for name, chk in spec["checks"].items():  # structural labels: trust the rule over the instruction
            if chk.get("verify"):
                conv["labels"][name] = int(resolve_rule(chk["verify"])(conv) >= 0.5)
        conv["synthetic"] = {"generator": model, "spec": spec["name"], "seed": seed}
        out.append(conv)
    return out, {"requested": n, "generated": len(out), "failed": failed, "usd": cost}


def generate(spec_path, n, client, seed=0):
    spec = yaml.safe_load(Path(spec_path).read_text())
    return asyncio.run(generate_async(spec, n, client, seed))
