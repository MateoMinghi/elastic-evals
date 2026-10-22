"""Harness optimisation: a loop that edits an agent's harness and keeps what the evals prove better
(DESIGN.md §6.11).

The harness is a directory of files the agent is built from: system prompts, tool descriptions,
skills, configuration. Everything else is the kernel and is never edited: the agent's code, the
suite, the calibration, the dataset and this loop. Each iteration:

  1. pick a parent from the accepted candidates: one that no other candidate dominates on the
     validation items, weighted by the items it is best on (GEPA's per-item Pareto front)
  2. run the parent on a train minibatch; the conversations it failed (flagged checks with their
     `ask`, failed assertions, transcript, record) and the outcome of earlier attempts go to the
     proposer, which returns new contents for some harness files
  3. writes outside the mutable surface (path globs, size limit) are refused
  4. the child runs on the same minibatch and must beat the parent there, then on the validation
     split it must clear three gates against its parent:
       improvement   paired mean gain over the validation items >= min_z standard errors
       regressions   no check's flagged rate rises by more than `tolerance` (critical: not at all)
       assertions    no more failed assertions than the parent
  5. accepted children join the archive; every attempt, accepted or not, is logged with its lineage

The proposer sees train items only. Validation decides; the test split is scored once at the end,
for the seed and the best candidate, with the audit judge on every item when the suite allows
hosted evaluators: a gain the cascade sees but the independent judge does not is the signature of
a harness that learned the evaluators' blind spots instead of the task.

Nothing is deployed: the best candidate is a directory to review, run as an experiment against
production traffic, and ship like any other change.

    python -m elastic_evals evolve --suite S --calib calib/ --items items.jsonl --harness harness/ \\
        --agent my_pkg.agent:build --model <proposer model> --out evo/ [--iterations 10]
"""

import asyncio
import copy
import dataclasses
import fnmatch
import json
import math
import random
import shutil
from collections import Counter
from pathlib import Path, PurePosixPath

from . import experiments as exps
from .engine import drift_report, unit_hash

# Text and configuration only by default: code in the harness runs in the loop's process, so
# letting the proposer write it (--mutable '*.py') belongs in a sandbox.
MUTABLE = ["*.md", "*.txt", "*.yaml", "*.yml", "*.json", "*.j2", "*.jinja"]

SYSTEM = """You improve the harness of an AI agent: the prompts, tool descriptions, skills and configuration files \
it is built from. You get the current files, conversations from a training set in which the agent failed checks \
(each check's `ask` says what counts as a failure), and the outcome of earlier attempts.

Propose one focused change that fixes a class of failures, not a single conversation. Never copy names, amounts, \
ids or other specifics of the examples into the harness. Prefer generalising an existing instruction to adding a \
new one, and do not repeat a change that was rejected. Only edit files whose paths match the mutable patterns.

Return only JSON: {"rationale": "<what fails, why, and what the change does>", "files": {"<path>": "<full new \
content>"}}"""


def split(items, val_share=0.3, test_share=0.2):
    """Deterministic by item id, so adding items never moves the others between splits."""
    out = {"train": [], "val": [], "test": []}
    for it in items:
        h = unit_hash(f"evolve|{it['id']}")
        out["test" if h < test_share else "val" if h < test_share + val_share else "train"].append(it)
    return out["train"], out["val"], out["test"]


def item_score(it, suite, critical_weight=3.0) -> float:
    """Share of an item's checks and assertions that are clean; critical checks weigh more."""
    total = clean = 0.0
    for q in suite.questions:
        if q.multiclass or q.name not in it["decisions"]:
            continue
        w = critical_weight if q.critical else 1.0
        total += w
        clean += w * (it["decisions"][q.name] != 1)
    for a in it["assertions"]:
        total += 1
        clean += bool(a["pass"])
    return clean / total if total else 1.0


def paired(parent: dict, child: dict) -> tuple[float, float]:
    """Mean and standard error of the per-item gain of child over parent."""
    d = [child[i] - parent[i] for i in parent if i in child]
    n = len(d)
    if n < 2:
        return (d[0] if d else 0.0), math.inf
    mean = sum(d) / n
    return mean, math.sqrt(sum((x - mean) ** 2 for x in d) / (n - 1) / n)


def regressions(parent: dict, child: dict, suite, tolerance) -> list[str]:
    out = []
    for q in suite.questions:
        p, c = parent["rates"].get(q.name), child["rates"].get(q.name)
        if p is not None and c is not None and c > p + (0.0 if q.critical else tolerance) + 1e-12:
            out.append(q.name)
    if child["failed_assertions"] > parent["failed_assertions"]:
        out.append("assertions")
    return out


def harness_files(root: Path, mutable) -> dict:
    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if p.is_file() and any(fnmatch.fnmatch(rel, g) for g in mutable):
            out[rel] = p.read_text()
    return out


def apply(parent_dir: Path, child_dir: Path, files: dict, mutable, max_bytes) -> tuple[list, list]:
    """Copy the parent and write the proposed files that lie inside the mutable surface."""
    shutil.copytree(parent_dir, child_dir)
    changed, refused = [], []
    for rel, text in (files or {}).items():
        p = PurePosixPath(rel)
        if (p.is_absolute() or ".." in p.parts or not isinstance(text, str) or len(text.encode()) > max_bytes
                or not any(fnmatch.fnmatch(rel, g) for g in mutable)):
            refused.append(rel)
            continue
        target = child_dir / rel
        if target.exists() and target.read_text() == text:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        changed.append(rel)
    return changed, refused


def transcript(conv, max_chars=600) -> str:
    lines = []
    for t in conv["turns"]:
        if t["role"] == "tool":
            line = f"tool {t.get('name')}({json.dumps(t.get('args', {}), ensure_ascii=False)}) -> {t.get('result', '')}"
        else:
            line = f"{t['role']}: {t.get('text', '')}"
        lines.append(line if len(line) <= max_chars else line[:max_chars] + " …")
    return "\n".join(lines)


def failures(run, suite, limit=12) -> list[dict]:
    """What the proposer learns from: the spec of each failed check, never its rule, route or thresholds."""
    asks = {q.name: q for q in suite.questions}
    out = []
    for it in run["items"]:
        flagged = [c for c, v in it["decisions"].items() if v == 1 and c in asks and not asks[c].multiclass]
        failed = [a for a in it["assertions"] if not a["pass"]]
        if flagged or failed:
            out.append({"item_id": it["item_id"], "record": it["conversation"].get("record"),
                        "transcript": transcript(it["conversation"]),
                        "failed_checks": [{"check": c, "ask": asks[c].ask, "critical": asks[c].critical} for c in flagged],
                        "failed_assertions": failed})
    return out[:limit]


def pick_parent(pool, val_scores, rng):
    ids = list(val_scores[pool[0]])

    def dominated(c):
        return any(all(val_scores[o][i] >= val_scores[c][i] for i in ids)
                   and any(val_scores[o][i] > val_scores[c][i] for i in ids) for o in pool)
    front = [c for c in pool if not dominated(c)]
    wins = Counter()
    for i in ids:
        best = max(val_scores[c][i] for c in front)
        wins.update(c for c in front if val_scores[c][i] == best)
    cands = sorted(wins)
    return rng.choices(cands, weights=[wins[c] for c in cands])[0]


class LLMProposer:
    """Proposer backed by a chat model (OpenRouter or any OpenAI-compatible URL). Choose a model family
    different from the suite's judges, so the proposer does not share their blind spots."""

    def __init__(self, model, client=None, **params):
        if client is None:
            from .providers import OpenRouter
            client = OpenRouter()
        self.model, self.client, self.params = model, client, params

    async def __call__(self, request: dict) -> dict:
        res = await self.client.chat(self.model, [{"role": "system", "content": SYSTEM},
                                                  {"role": "user", "content": json.dumps(request, ensure_ascii=False)}],
                                     tag="evolve", **self.params)
        content = res["choices"][0]["message"]["content"] or ""
        try:
            return json.loads(content[content.find("{"): content.rfind("}") + 1])
        except json.JSONDecodeError:
            return {"rationale": "unparseable proposal", "files": {}}


async def _evaluate(build, harness_dir, items, suite, cal, evaluators, critical_weight, concurrency, audit=False):
    agent = build(Path(harness_dir))
    run = await exps.run_async(agent, items, suite, cal, evaluators, concurrency=concurrency, audit=audit)
    s = run["summary"]
    scores = {it["item_id"]: item_score(it, suite, critical_weight) for it in run["items"]}
    stats = {"mean": sum(scores.values()) / max(1, len(scores)), "rates": s["rates"], "eval_usd": s["cost_usd"],
             "failed_assertions": sum(not a["pass"] for it in run["items"] for a in it["assertions"])}
    if audit:
        stats["audit_agreement"] = drift_report(run["items"])
    return run, scores, stats


async def evolve_async(build, seed_dir, items, suite, calibration, evaluators, proposer, out, iterations=10,
                       minibatch=8, mutable=None, val_share=0.3, test_share=0.2, tolerance=0.02, min_z=1.0,
                       critical_weight=3.0, max_bytes=50_000, history=8, concurrency=4, seed=0, log=print):
    """build(harness_dir) -> agent(item_input) -> conversation. proposer(request) -> {"rationale", "files"}
    (sync or async). Writes candidates/, archive.jsonl and report.json under `out`; returns the report."""
    mutable = mutable or MUTABLE
    out = Path(out)
    (out / "candidates").mkdir(parents=True, exist_ok=True)
    train, val, test = split(items, val_share, test_share)
    if not train or not val:
        raise ValueError(f"need train and validation items (got {len(train)} train, {len(val)} validation)")
    rng = random.Random(seed)
    ev = dict(suite=suite, cal=calibration, evaluators=evaluators, critical_weight=critical_weight,
              concurrency=concurrency)
    path = lambda cid: out / "candidates" / cid  # noqa: E731
    archive_file = (out / "archive.jsonl").open("w")

    def record(entry):
        archive.append(entry)
        archive_file.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        archive_file.flush()
        log(f"{entry['id']:6s} parent {entry['parent'] or '-':6s} {entry['outcome']}")

    archive, val_scores, val_stats = [], {}, {}
    shutil.copytree(seed_dir, path("c000"))
    _, val_scores["c000"], val_stats["c000"] = await _evaluate(build, path("c000"), val, **ev)
    record({"id": "c000", "parent": None, "outcome": "seed", "accepted": True, "val": val_stats["c000"]})
    pool = ["c000"]

    for k in range(1, iterations + 1):
        cid, pid = f"c{k:03d}", pick_parent(pool, val_scores, rng)
        batch = rng.sample(train, min(minibatch, len(train)))
        prun, pscores, _ = await _evaluate(build, path(pid), batch, **ev)
        fails = failures(prun, suite)
        entry = {"id": cid, "parent": pid, "accepted": False, "minibatch": [it["id"] for it in batch]}
        if not fails:
            record(dict(entry, outcome="skipped: parent clean on the minibatch"))
            continue
        request = {"files": harness_files(path(pid), mutable), "mutable": mutable,
                   "checks": {q.name: {"ask": q.ask, "critical": q.critical} for q in suite.questions},
                   "failures": fails,
                   "history": [{f: e.get(f) for f in ("id", "parent", "rationale", "changed", "outcome", "gain")}
                               for e in archive[1:]][-history:]}
        proposal = proposer(request)
        if asyncio.iscoroutine(proposal):
            proposal = await proposal
        changed, refused = apply(path(pid), path(cid), proposal.get("files"), mutable, max_bytes)
        entry.update(rationale=proposal.get("rationale"), changed=changed, refused=refused)
        if not changed:
            record(dict(entry, outcome="rejected: no change inside the mutable surface"))
            continue
        _, cscores, _ = await _evaluate(build, path(cid), batch, **ev)
        entry["minibatch_gain"] = (sum(cscores.values()) - sum(pscores.values())) / len(batch)
        if entry["minibatch_gain"] <= 0:
            record(dict(entry, outcome="rejected: no gain on the minibatch"))
            continue
        _, scores, stats = await _evaluate(build, path(cid), val, **ev)
        gain, se = paired(val_scores[pid], scores)
        regs = regressions(val_stats[pid], stats, suite, tolerance)
        entry.update(val=stats, gain=gain, gain_se=se, regressions=regs)
        if regs:
            outcome = f"rejected: regressed {', '.join(regs)}"
        elif gain <= 0 or gain < min_z * se:
            outcome = f"rejected: validation gain {gain:+.4f} under {min_z:g} SE ({se:.4f})"
        else:
            outcome = f"accepted: validation gain {gain:+.4f} (SE {se:.4f})"
            entry["accepted"] = True
            pool.append(cid)
            val_scores[cid], val_stats[cid] = scores, stats
        record(dict(entry, outcome=outcome))
    archive_file.close()

    best = max(pool, key=lambda c: (val_stats[c]["mean"], c == "c000"))
    lineage, c = [], best
    while c:
        lineage.append(c)
        c = next(e["parent"] for e in archive if e["id"] == c)
    report = {"best": best, "lineage": lineage[::-1], "splits": {"train": len(train), "val": len(val), "test": len(test)},
              "val": {"seed": val_stats["c000"]["mean"], "best": val_stats[best]["mean"]},
              "attempts": len(archive) - 1, "accepted": len(pool) - 1}
    if test:
        audit = suite.policy.allow_hosted and suite.policy.audit_judge is not None
        tsuite = copy.copy(suite)
        tsuite.policy = dataclasses.replace(suite.policy, audit_rate=1.0)
        report["test"] = {}
        for name, cand in (("seed", "c000"), ("best", best)):
            _, scores, stats = await _evaluate(build, path(cand), test, **dict(ev, suite=tsuite), audit=audit)
            report["test"][name] = dict(stats, scores=scores)
        report["test"]["gain"], report["test"]["gain_se"] = paired(report["test"]["seed"].pop("scores"),
                                                                   report["test"]["best"].pop("scores"))
        if audit:
            drop = [q for q, a in report["test"]["seed"]["audit_agreement"].items()
                    if report["test"]["best"]["audit_agreement"].get(q, a)["agreement"] < a["agreement"] - tolerance]
            report["test"]["audit_agreement_dropped"] = drop
    (out / "report.json").write_text(json.dumps(report, indent=1, default=str))
    return report


def evolve(*args, **kw):
    return asyncio.run(evolve_async(*args, **kw))
