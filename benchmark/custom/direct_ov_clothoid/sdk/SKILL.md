---
name: g1-clothoid-road-sdk
description: Compute a canonical single-segment G1 Hermite clothoid from road survey E/N coordinates and north-clockwise azimuths; use for G1 clothoid, single spiral, 回旋线, and 道路线形 fitting.
---

# G1 Clothoid Road SDK

Use the bundled `scripts/calculate.py`; do not rewrite the formulas.

1. Download the script with `ov get` if it is not already local.
2. Run its smoke test once: `python calculate.py --self-test`.
3. Run `python calculate.py E0 N0 AZ0 E1 N1 AZ1 --fraction F`.
4. Return the emitted JSON unchanged. A nonzero exit or failed residual is a failure.

The script requires Python with SciPy. It implements the canonical method from
Bertolazzi and Frego, https://arxiv.org/abs/1305.6644, and contains no task-specific
inputs or expected outputs.
