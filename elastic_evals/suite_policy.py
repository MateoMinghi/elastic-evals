"""What a tenant-supplied suite may reference (DESIGN.md §9).

Suites registered through the API come from tenants, and the platform's workers run them. So a
suite must not make a worker import code, send a credential to a URL of the tenant's choosing, or
load model weights the operator has not vetted. Operators widen this with environment variables:

    EE_RULE_MODULES     modules whose functions suites may name as rules ("pkg.mod:fn"); default none
    EE_JUDGE_URLS       URL prefixes an `llm` evaluator's base_url may point at; default none (OpenRouter)
    EE_JUDGE_KEY_ENVS   environment variables an evaluator may name as api_key_env; default OPENROUTER_API_KEY
    EE_HF_MODELS        Hugging Face model ids `hf_classifier` / `hf_zero_shot` may load; default none

Suites given to the CLI by the operator are trusted and not checked.
"""

import os

from .evaluators import TYPES, builtin_rules


def _allowed(name: str, default: str = "") -> list[str]:
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


def problems(suite) -> list[str]:
    out = []
    rules = builtin_rules()
    rule_modules = _allowed("EE_RULE_MODULES")
    for q in suite.questions:
        if not q.rule:
            continue
        if ":" in q.rule:
            module = q.rule.split(":", 1)[0]
            if not any(module == m or module.startswith(m + ".") for m in rule_modules):
                out.append(f"{q.name}: rule {q.rule!r} is not built in, and module {module!r} is not allowed "
                           "by the operator (EE_RULE_MODULES)")
        elif q.rule not in rules:
            out.append(f"{q.name}: unknown rule {q.rule!r} (built-in rules: {', '.join(sorted(rules))})")
    urls, keys, hf = _allowed("EE_JUDGE_URLS"), _allowed("EE_JUDGE_KEY_ENVS", "OPENROUTER_API_KEY"), _allowed("EE_HF_MODELS")
    for e in suite.evaluators:
        eid, kind = e.get("id", "?"), e.get("type")
        if kind not in TYPES:
            out.append(f"evaluator {eid}: unknown type {kind!r} (types: {', '.join(sorted(TYPES))})")
        if e.get("base_url") and not any(e["base_url"].startswith(u) for u in urls):
            out.append(f"evaluator {eid}: base_url {e['base_url']!r} is not allowed by the operator (EE_JUDGE_URLS)")
        if e.get("api_key_env") and e["api_key_env"] not in keys:
            out.append(f"evaluator {eid}: api_key_env {e['api_key_env']!r} is not allowed (EE_JUDGE_KEY_ENVS)")
        if kind in ("hf_classifier", "hf_zero_shot") and e.get("model") not in hf:
            out.append(f"evaluator {eid}: model {e.get('model')!r} is not allowed by the operator (EE_HF_MODELS)")
    return out
