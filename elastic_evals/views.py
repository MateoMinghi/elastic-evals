"""Conversation helpers: rendering and the per-question text views local models see.

A conversation is a dict: {"id", "lang", "turns": [{"role": "user"|"assistant", "text"} |
{"role": "tool", "name", "args", "result"}], "record": {...}}.

View syntax: "<part>[|<part>...]", parts joined with a space:
  user                 all user messages
  assistant            all assistant messages
  first_assistant:N    first N assistant messages
  last_assistant:N     last N assistant messages
  tools                the TOOL lines
  trajectory           the agent's steps in order, with errors
  record:a/b           "record a: <value>." using the first field present (a, else b)
"""

import json


def render(turns: list) -> str:
    out = []
    for t in turns:
        if t["role"] == "tool":
            args = json.dumps(t.get("args", {}), ensure_ascii=False, sort_keys=True)
            out.append(f"TOOL {t['name']}({args}) -> {t.get('result', '')}")
        else:
            out.append(f"{'USER' if t['role'] == 'user' else 'ASSISTANT'}: {t['text']}")
    return "\n".join(out)


def text_of(conv: dict) -> str:
    return conv.get("text") or render(conv["turns"])


def view(conv: dict, spec: str) -> str:
    turns = conv["turns"]
    asst = [t["text"] for t in turns if t["role"] == "assistant"]
    parts = []
    for part in spec.split("|"):
        name, _, arg = part.partition(":")
        if name == "user":
            parts.append(" ".join(t["text"] for t in turns if t["role"] == "user"))
        elif name == "assistant":
            parts.append(" ".join(asst))
        elif name == "first_assistant":
            parts.append(" ".join(asst[: int(arg or 1)]))
        elif name == "last_assistant":
            parts.append(" ".join(asst[-int(arg or 1):]))
        elif name == "tools":
            parts.append(" ".join(line for line in text_of(conv).splitlines() if line.startswith("TOOL")))
        elif name == "trajectory":
            from .trajectory import render, steps
            parts.append(render(steps(conv)))
        elif name == "record":
            fields = arg.split("/")
            label = fields[-1]
            value = next((conv["record"][f] for f in fields if f in conv["record"]), None)
            parts.append(f"record {label}: {value}.")
        else:
            raise ValueError(f"unknown view part {part!r}")
    return " ".join(parts)
