"""Fake evaluators and data for offline engine tests.

Each fake reads its answers from the conversation itself (conv["fake"][evaluator_id][check]),
so a test can construct exactly the accuracy profile it needs.
"""

import random

from elastic_evals.evaluators import Evaluator, register


class FakeHosted(Evaluator):
    hosted = True

    async def evaluate(self, conv, questions):
        ans = conv.get("fake", {}).get(self.id, {})
        return ({q.name: ans.get(q.name) for q in questions}, self.cfg.get("usd", 0.0001),
                self.cfg.get("ms", 500) / 1000)


class FakeLocal(Evaluator):
    def evaluate(self, conv, questions):
        ans = conv.get("fake", {}).get(self.id, {})
        return {q.name: ans[q.name] for q in questions if q.name in ans}, 0.0, 0.001


register("fake_classifier", FakeHosted, 2)
register("fake_judge", FakeHosted, 3)
register("fake_local", FakeLocal, 1)


def rule_from_fake(conv):
    return conv["fake"]["rules"]["structural"]


def rule_heuristic(conv):
    return conv["fake"]["rules"].get("style")


SUITE_YAML = """
name: test-suite
judge_preamble: 'Answer with JSON: '
questions:
  structural:
    ask: Is the structure broken?
    view: assistant
    rule: tests.fakes:rule_from_fake
    critical: true
  style:
    ask: Is the user annoyed?
    view: user
    rule: tests.fakes:rule_heuristic
evaluators:
  - {id: rules, type: rules}
  - {id: clf, type: fake_classifier, usd: 0.00004, ms: 200}
  - {id: judge_fast, type: fake_judge, usd: 0.0001, ms: 900}
  - {id: judge_slow, type: fake_judge, usd: 0.00008, ms: 2500}
  - {id: auditor, type: fake_judge, usd: 0.0002, ms: 1500}
policy:
  target_accuracy: 0.95
  audit_rate: 0.5
  audit_judge: auditor
"""


def noisy(y, acc, rng, confident=True):
    right = rng.random() < acc
    v = y if right else 1 - y
    if confident:
        return float(v)
    return 0.8 if v else 0.2


def make_convs(n=300, seed=0, langs=("en", "es")):
    """structural: the rule is exact. style: the rule is a weak heuristic (0.5 = unsure on half),
    the classifier is 88% accurate, judge_fast 96%, judge_slow 98%, auditor 97%."""
    rng = random.Random(seed)
    convs = []
    for i in range(n):
        ys, yt = rng.random() < 0.3, rng.random() < 0.4
        ys, yt = int(ys), int(yt)
        lang = langs[i % len(langs)]
        style_rule = float(yt) if rng.random() < 0.5 else 0.5
        convs.append({
            "id": f"c{i}", "lang": lang, "turns": [], "record": {},
            "labels": {"structural": ys, "style": yt},
            "fake": {
                "rules": {"structural": float(ys), "style": style_rule},
                "clf": {"structural": noisy(ys, 0.9, rng, False), "style": noisy(yt, 0.88, rng, False)},
                "judge_fast": {"structural": noisy(ys, 0.97, rng), "style": noisy(yt, 0.96, rng)},
                "judge_slow": {"structural": noisy(ys, 0.99, rng), "style": noisy(yt, 0.98, rng)},
                "auditor": {"structural": noisy(ys, 0.99, rng), "style": noisy(yt, 0.97, rng)},
            },
        })
    return convs


def labels_of(convs):
    return {(c["id"], q): v for c in convs for q, v in c["labels"].items()}
