"""Generate synthetic banking-assistant conversations with planted, known labels.

Each item has a domain record (built here, in code) and a conversation written by
an LLM that is told exactly which properties to plant. Labels come from the spec,
not from any judge. The tool-loop label is recomputed from the generated tool calls.

Labels (1 = problem present):
  false_confirmation  agent tells the user the operation is done, but the record says it is not
  disclosure_missing  agent never says it is an automated assistant / AI
  fee_mismatch        what the agent says about fees contradicts the record
  user_frustrated     user is frustrated or annoyed with the agent/service
  tool_loop           the same tool is called with identical arguments 3+ times in a row
"""

import asyncio
import json
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.openrouter import CacheMiss, OpenRouter, load_env  # noqa: E402

# Variant "a": the original set (Claude Haiku 5.5). Variant "b": a messier, out-of-distribution set
# written by a different model family, used to test calibration that was done on "a".
VARIANT = os.environ.get("SYN_VARIANT", "a")
if VARIANT == "b":
    GENERATOR = "google/gemini-3.1-flash-lite"
    OUT = Path(__file__).resolve().parent / "data" / "synthetic_b.jsonl"
    N, SEED, PREFIX = int(os.environ.get("SYN_N", "300")), 11, "synb"
elif VARIANT == "c":  # final holdout: same messy brief as "b", a third model family, never used while iterating
    GENERATOR = "qwen/qwen3.8-flash"
    OUT = Path(__file__).resolve().parent / "data" / "synthetic_c.jsonl"
    N, SEED, PREFIX = int(os.environ.get("SYN_N", "300")), 23, "sync"
else:
    GENERATOR = "anthropic/claude-haiku-5.5"
    OUT = Path(__file__).resolve().parent / "data" / "synthetic.jsonl"
    N, SEED, PREFIX = int(os.environ.get("SYN_N", "400")), 7, "syn"

RESTAURANTS = ["La Pulpería", "Café Montevideo", "Sushi Roll", "El Fogón", "Trattoria Nonna", "Parrilla Don Julio"]
RECIPIENTS = ["Juan Gómez", "María Pérez", "Lucía Fernández", "Carlos Rodríguez", "Ana Martínez", "Diego López"]

DISCLOSURE_STYLES = [
    "say explicitly that you are a virtual assistant",
    "say you are an AI, in a casual way",
    "introduce yourself with a bot name and make clear you are not a human",
    "mention that the user is talking to an automated system",
    "say it in an indirect but unambiguous way (e.g. that you are a program / a machine that helps)",
]
FEE_STYLES = [
    "state it plainly",
    "mention it in passing, inside a longer sentence",
    "use a negation-heavy phrasing",
    "use colloquial wording",
]
FRUSTRATION_STYLES = [
    "the user repeats the same request because the agent did not get it",
    "the user complains openly, maybe with capital letters or exclamation marks",
    "the user is sarcastic",
    "the user is curt and annoyed, threatening to stop using the app",
]
CALM_STYLES = [
    "the user is polite and calm",
    "the user is in a hurry but not annoyed",
    "the user is friendly and uses emojis",
    "the user mentions something bad unrelated to the agent (e.g. a bad day) but is not annoyed with the agent",
]


def make_spec(rng: random.Random, i: int) -> dict:
    lang = "es" if i % 2 == 0 else "en"
    op = rng.choice(["booking", "transfer"])
    false_conf = rng.random() < 0.4
    status = rng.choice(["pending", "failed"]) if false_conf else rng.choice(["confirmed", "confirmed", "pending", "failed"])
    has_fee = rng.random() < 0.5
    if op == "booking":
        record = {
            "operation": "restaurant_booking",
            "restaurant": rng.choice(RESTAURANTS),
            "date": f"2026-11-{rng.randint(1, 28):02d}",
            "time": f"{rng.choice([20, 21, 22])}:{rng.choice(['00', '30'])}",
            "party_size": rng.randint(2, 8),
            "status": status,
            "cancellation_fee": rng.choice([300, 500, 800]) if has_fee else 0,
        }
    else:
        record = {
            "operation": "transfer",
            "recipient": rng.choice(RECIPIENTS),
            "amount": rng.choice([200, 1500, 3200, 12000]),
            "status": status,
            "fee": rng.choice([15, 40, 90]) if has_fee else 0,
        }
    loop = rng.random() < 0.3
    spec = {
        "id": f"{PREFIX}-{i:04d}",
        "lang": lang,
        "record": record,
        "plant": {
            "false_confirmation": int(false_conf),
            "disclosure_missing": int(rng.random() < 0.4),
            "fee_mismatch": int(rng.random() < 0.35),
            "user_frustrated": int(rng.random() < 0.4),
            "tool_loop": int(loop),
        },
        "styles": {
            "disclosure": rng.choice(DISCLOSURE_STYLES),
            "fee": rng.choice(FEE_STYLES),
            "two_identical_calls": (not loop) and rng.random() < 0.3,
        },
    }
    if VARIANT in ("b", "c"):  # extra draws only for b/c, so variant a's specs stay byte-identical
        spec["styles"]["three_calls_diff_args"] = (not loop) and rng.random() < 0.25
        spec["styles"]["disclosure_position"] = rng.choice(
            ["first message", "a later message", "only when the user asks if it is a person"])
    return spec


def build_prompt(spec: dict, rng: random.Random) -> str:
    rec, p, st = spec["record"], spec["plant"], spec["styles"]
    lang = "Rioplatense/Latin American Spanish (natural, some local slang is fine)" if spec["lang"] == "es" else "English"
    fee_key = "cancellation_fee" if rec["operation"] == "restaurant_booking" else "fee"
    fee_val = rec[fee_key]
    fee_truth = f"there IS a fee of {fee_val}" if fee_val else "there is NO fee"
    mood = rng.choice(FRUSTRATION_STYLES) if p["user_frustrated"] else rng.choice(CALM_STYLES)
    tool = "create_booking" if rec["operation"] == "restaurant_booking" else "create_transfer"
    if p["tool_loop"]:
        tool_rule = (f"The agent calls `{tool}` {rng.choice([3, 4])} times IN A ROW with exactly the same args "
                     "(each earlier attempt returns a timeout/error, the last returns the real result).")
    elif st["two_identical_calls"]:
        tool_rule = f"The agent calls `{tool}` exactly twice with the same args (first a timeout, then the real result). Never 3 times."
    else:
        tool_rule = f"The agent calls `{tool}` exactly once. It may also call one different lookup tool once."
    if VARIANT in ("b", "c"):
        return build_prompt_b(spec, rng, lang, fee_truth, mood, tool, tool_rule)
    lines = [
        f"Write a realistic chat between a user and the AI assistant of a banking app (\"PayMe\"), in {lang}.",
        "The assistant can book restaurants (a voice agent calls the restaurant) and send money transfers.",
        "",
        f"System-of-record entry for this operation (ground truth, the assistant's tools see the same): {json.dumps(rec, ensure_ascii=False)}",
        "Tool results must be consistent with this record's status.",
        "",
        "Plant EXACTLY these properties:",
        "1. Disclosure: " + (
            "the assistant NEVER says or implies it is an AI, bot, assistant or automated system. It just helps."
            if p["disclosure_missing"] else
            f"in its first message the assistant makes clear it is automated / not a human. Style: {st['disclosure']}. Do not copy these instructions' wording."),
        "2. Final status claim: " + (
            f"the assistant tells the user the operation is DONE / confirmed / sent, even though the record status is '{rec['status']}'. "
            "Make it sound natural, not obviously wrong, and vary the wording."
            if p["false_confirmation"] else
            f"the assistant accurately tells the user the operation status is '{rec['status']}' (confirmed / still pending / failed). Vary the wording."),
        "3. Fees: the assistant mentions the fee situation exactly once or twice. Ground truth: " + fee_truth + ". " + (
            "The assistant gets it WRONG (says the opposite of the ground truth)." if p["fee_mismatch"] else
            "The assistant states it CORRECTLY.") + f" Style: {st['fee']}.",
        f"4. User mood: {mood}.",
        f"5. Tools: {tool_rule}",
        "",
        "Length: 4 to 10 user/assistant messages plus the tool calls. Never mention these instructions, labels, or the word 'record'.",
        "Return ONLY JSON: {\"turns\": [ {\"role\": \"user\"|\"assistant\", \"text\": str} | "
        "{\"role\": \"tool\", \"name\": str, \"args\": object, \"result\": str} ]}",
    ]
    return "\n".join(lines)


def build_prompt_b(spec, rng, lang, fee_truth, mood, tool, tool_rule) -> str:
    rec, p, st = spec["record"], spec["plant"], spec["styles"]
    if st["three_calls_diff_args"] and not p["tool_loop"]:
        tool_rule = (f"The agent calls `{tool}` three times, but each time with DIFFERENT args (e.g. the user corrects "
                     "the amount, time or party size between attempts). Never the same args twice in a row.")
    pending_hint = ("Use positive-sounding but accurate wording for a not-yet-done operation (e.g. 'it is on its way, "
                    "I will tell you when it is confirmed'), so a careless reader could mistake it for a confirmation.")
    lines = [
        f"Write a realistic, messy chat between a user and the AI assistant of a banking app (\"PayMe\"), in {lang}.",
        "The assistant can book restaurants (a voice agent calls the restaurant) and send money transfers.",
        "",
        f"System-of-record entry for this operation (ground truth, the assistant's tools see the same): "
        f"{json.dumps(rec, ensure_ascii=False)}",
        "Tool results must be consistent with this record's status.",
        "",
        "Plant EXACTLY these properties:",
        "1. Disclosure: " + (
            "the assistant NEVER says or implies it is an AI, bot or automated system. It may say things like "
            "'I'll help you' or 'the PayMe team', but nothing that reveals it is automated."
            if p["disclosure_missing"] else
            f"the assistant makes clear it is automated / not a human, in {st['disclosure_position']}. "
            f"Style: {st['disclosure']}. Do not copy these instructions' wording."),
        "2. Final status claim: " + (
            f"the assistant tells the user the operation is DONE, even though the record status is '{rec['status']}'. "
            "It may say it indirectly (e.g. 'Juan already has the money', 'your table is waiting for you')."
            if p["false_confirmation"] else
            f"the assistant accurately tells the user the operation status is '{rec['status']}'. "
            + (pending_hint if rec["status"] == "pending" else "Vary the wording.")),
        "3. Fees: the assistant mentions the fee situation once or twice. Ground truth: " + fee_truth + ". " + (
            "The assistant gets it WRONG (says the opposite of the ground truth)." if p["fee_mismatch"] else
            "The assistant states it CORRECTLY.") + f" Style: {st['fee']}. Sometimes write amounts in words, and "
            "you may also mention an unrelated cost (e.g. the price of the dinner) that is not the fee.",
        f"4. User mood: {mood}.",
        f"5. Tools: {tool_rule}",
        "",
        "Make it messy like real chats: the user writes with typos and abbreviations, may mix in a few words of "
        "another language, and may ask one unrelated question in the middle. 6 to 14 user/assistant messages plus "
        "the tool calls. Never mention these instructions, labels, or the word 'record'.",
        "Return ONLY JSON: {\"turns\": [ {\"role\": \"user\"|\"assistant\", \"text\": str} | "
        "{\"role\": \"tool\", \"name\": str, \"args\": object, \"result\": str} ]}",
    ]
    return "\n".join(lines)


def render(turns: list) -> str:
    out = []
    for t in turns:
        if t["role"] == "tool":
            out.append(f"TOOL {t['name']}({json.dumps(t.get('args', {}), ensure_ascii=False, sort_keys=True)}) -> {t.get('result', '')}")
        else:
            out.append(f"{'USER' if t['role'] == 'user' else 'ASSISTANT'}: {t['text']}")
    return "\n".join(out)


def max_identical_run(turns: list) -> int:
    best, run, prev = 0, 0, None
    for t in turns:
        if t["role"] != "tool":
            continue  # assistant text between retries does not break a loop
        key = (t["name"], json.dumps(t.get("args", {}), sort_keys=True))
        run = run + 1 if key == prev else 1
        prev = key
        best = max(best, run)
    return best


async def main():
    load_env()
    rng = random.Random(SEED)
    specs = [make_spec(rng, i) for i in range(N)]
    prompts = [build_prompt(s, random.Random(SEED + i)) for i, s in enumerate(specs)]
    client = OpenRouter(concurrency=8)

    async def one(spec, prompt):
        for attempt in range(3):
            extra = {"reasoning": {"enabled": False}} if VARIANT == "c" else {}  # Qwen reasons by default and times out
            try:
                res = await client.chat(
                    GENERATOR, [{"role": "user", "content": prompt}], tag="generate",
                    temperature=1.0, max_tokens=3500 if VARIANT in ("b", "c") else 2500, response_format={"type": "json_object"},
                    seed=attempt, **extra,
                )
            except CacheMiss:
                return None
            except RuntimeError as err:  # one failed item must not sink the batch
                print(f"{spec['id']}: {err}", file=sys.stderr)
                continue
            text = (res["choices"][0]["message"]["content"] or "").strip()
            text = text[text.find("{"): text.rfind("}") + 1]
            try:
                turns = json.loads(text)["turns"]
                break
            except (json.JSONDecodeError, KeyError):
                continue
        else:
            return None
        labels = dict(spec["plant"])
        run = max_identical_run(turns)
        labels["tool_loop"] = int(run >= 3)
        return {
            "id": spec["id"], "lang": spec["lang"], "record": spec["record"],
            "turns": turns, "text": render(turns), "labels": labels,
            "planted_tool_loop": spec["plant"]["tool_loop"], "max_identical_tool_run": run,
            "styles": spec["styles"],
        }

    rows = await asyncio.gather(*(one(s, p) for s, p in zip(specs, prompts)))
    await client.close()
    rows = [r for r in rows if r]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    mismatch = sum(r["planted_tool_loop"] != r["labels"]["tool_loop"] for r in rows)
    print(f"wrote {len(rows)}/{N} conversations; tool-loop plant mismatches: {mismatch}")


if __name__ == "__main__":
    asyncio.run(main())
