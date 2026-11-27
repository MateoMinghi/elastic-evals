"""Small statistics shared by calibration, experiments and the component measurements."""

import numpy as np

# Below this many labelled examples a per-language (or per-entity) rate says little: with n = 30 and 95%
# measured, the interval is still about +-8 points. Reports mark such numbers instead of presenting them as facts.
MIN_N = 30


def wilson(k, n, z=1.96):
    """95% Wilson interval of a proportion; honest on the small datasets evaluations use."""
    if not n:
        return None
    p = k / n
    centre, half = p + z * z / (2 * n), z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return [float(max(0.0, (centre - half) / (1 + z * z / n))), float(min(1.0, (centre + half) / (1 + z * z / n)))]
