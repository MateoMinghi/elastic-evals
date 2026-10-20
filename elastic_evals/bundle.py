"""Load a suite and a calibration directory (calibration.json + heads.pkl, as `calibrate --out` writes)."""

import json
import pickle
from pathlib import Path

from .evaluators import LocalHeadEvaluator, build
from .suite import Suite


def load(suite_path, calib_dir, client=None):
    suite = Suite.load(suite_path)
    cal = json.loads((Path(calib_dir) / "calibration.json").read_text())
    suite.policy.allow_hosted = cal["policy"]["allow_hosted"]
    if suite.policy.allow_hosted and client is None:
        from .providers import OpenRouter
        client = OpenRouter()
    evaluators = build(suite, client)
    heads_file = Path(calib_dir) / "heads.pkl"
    heads = pickle.loads(heads_file.read_bytes()) if heads_file.exists() else {}
    for e in evaluators:
        if isinstance(e, LocalHeadEvaluator):
            e.heads = heads.get(e.id, {})
    return suite, cal, evaluators


def save(calib_dir, cal, evaluators):
    out = Path(calib_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "calibration.json").write_text(json.dumps(cal, indent=1, default=str))
    heads = {e.id: e.heads for e in evaluators if isinstance(e, LocalHeadEvaluator)}
    (out / "heads.pkl").write_bytes(pickle.dumps(heads))
