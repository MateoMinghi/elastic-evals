"""Trajectories and observations (DESIGN.md §6.5 scopes).

A trajectory is the ordered list of an agent's steps (model calls, tool calls, agent spans) with
their status; conversations rebuilt from spans carry one (`conversation.build`), and plain JSONL
conversations get one derived from their tool turns.

  scope: trajectory    a check over the whole trajectory: rules below (step budget, tool errors,
                       forbidden tools, expected sequence) or any evaluator, which also sees the
                       rendered trajectory
  scope: observation   a check on single steps (`select` picks them, e.g. {"type": "TOOL"}): every
                       selected step becomes an item scored and calibrated like a conversation, and
                       its scores carry `observation_id` = the span id. Labels: "check@<span_id>".
"""

import json
import re

ITEM_SEP = "#"


def steps(conv: dict) -> list[dict]:
    if conv.get("trajectory") is not None:
        return conv["trajectory"]
    out = []
    for i, t in enumerate(conv.get("turns", [])):
        if t["role"] == "tool":
            out.append({"span_id": f"t{i}", "type": "TOOL", "name": t["name"], "tool": t["name"],
                        "args": t.get("args", {}), "result": t.get("result", ""), "status": t.get("status")})
        elif t["role"] == "assistant":
            out.append({"span_id": f"t{i}", "type": "GENERATION", "name": "assistant", "output": t["text"]})
    return out


def render(steps_: list[dict]) -> str:
    lines = []
    for i, s in enumerate(steps_, 1):
        flag = f" [{s['status']}{': ' + s['error_type'] if s.get('error_type') else ''}]" if s.get("status") == "ERROR" else ""
        if s["type"] == "TOOL":
            args = json.dumps(s.get("args", {}), ensure_ascii=False, sort_keys=True)
            lines.append(f"{i}. TOOL {s.get('tool') or s['name']}({args}) -> {s.get('result', '')}{flag}")
        elif s["type"] == "GENERATION":
            lines.append(f"{i}. MODEL {s.get('model') or ''} -> {(s.get('output') or '')[:300]}{flag}".replace("  ", " "))
        else:
            lines.append(f"{i}. {s['type']} {s['name']}{flag}")
    return "\n".join(lines)


def selected(step: dict, select: dict | None) -> bool:
    if not select:
        return True
    for key, want in select.items():
        have = step.get(key)
        if isinstance(want, list):
            if have not in want:
                return False
        elif isinstance(want, str) and want.startswith("~"):
            if not re.search(want[1:], str(have or "")):
                return False
        elif have != want:
            return False
    return True


def items(conv: dict, questions) -> list[dict]:
    """One item per step selected by any observation question; the item looks like a conversation."""
    out = []
    for s in steps(conv):
        if not any(selected(s, q.select) for q in questions):
            continue
        if s["type"] == "TOOL":
            turns = [{"role": "tool", "name": s.get("tool") or s["name"], "args": s.get("args", {}), "result": s.get("result", "")}]
        else:
            turns = [{"role": "assistant", "text": s.get("output") or s["name"]}]
        item = {k: conv.get(k) for k in ("tenant_id", "trace_id", "session_id", "trace_ctx", "experiment", "lang", "record")}
        item.update({"id": f"{conv['id']}{ITEM_SEP}{s['span_id']}", "turns": turns, "span": s,
                     "conversation_id": conv["id"], "observation_id": s["span_id"],
                     "applies": [q.name for q in questions if selected(s, q.select)]})
        out.append(item)
    return out


def item_labels(convs, labels: dict | None = None) -> dict:
    """Labels for observation items, from "check@span_id" keys on conversations."""
    out = {}
    for c in convs:
        for key, v in (c.get("labels") or {}).items():
            if "@" in key:
                check, span = key.split("@", 1)
                out[(f"{c['id']}{ITEM_SEP}{span}", check)] = v
    for (cid, q), v in (labels or {}).items():
        if ITEM_SEP in cid:
            out[(cid, q)] = v
    return out


# -- rules over trajectories (scope: trajectory) and steps (scope: observation) -------------------
def tool_errors(conv, min_errors: int = 1) -> float:
    return float(sum(s["type"] == "TOOL" and s.get("status") == "ERROR" for s in steps(conv)) >= min_errors)


def step_budget(conv, max_tool_calls: int = 10) -> float:
    return float(sum(s["type"] == "TOOL" for s in steps(conv)) > max_tool_calls)


def forbidden_tools(conv, tools: list | None = None) -> float:
    banned = set(tools or [])
    return float(any(s["type"] == "TOOL" and (s.get("tool") or s["name"]) in banned for s in steps(conv)))


def expected_sequence(conv, sequence: list | None = None) -> float:
    """1 when the tools in `sequence` were not called in that order (others may come between)."""
    want = list(sequence or [])
    for s in steps(conv):
        if want and s["type"] == "TOOL" and (s.get("tool") or s["name"]) == want[0]:
            want.pop(0)
    return float(bool(want))


def step_failed(item) -> float:
    """Observation rule: the step itself reported an error (exact when the span has a status)."""
    status = (item.get("span") or {}).get("status")
    if status is None or status == "UNSET":
        return 0.5 if str((item.get("span") or {}).get("result", "")).lower().startswith("error") else 0.0
    return float(status == "ERROR")
