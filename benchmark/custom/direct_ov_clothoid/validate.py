#!/usr/bin/env python3
"""Validate A/B/C result JSON for the held-out G1 clothoid task."""

import json
import math
import sys


GOLD = {
    "length_m": 93.99956255425538,
    "curv_start_1pm": -0.003499717371910190,
    "curv_end_1pm": 0.006839624679112094,
    "curvature_rate_1pm2": 0.000109993512417194,
    "E_m": 468241.1856253988,
    "N_m": 3468155.3005138465,
    "azimuth_deg": 7.162353734641609,
}


def normalized(raw):
    values = raw.get("output", raw)
    sample = raw.get("sample", values)
    residuals = raw.get("independent_numerical_integration", raw.get("validation", values))
    return {
        "length_m": values["length_m"],
        "curv_start_1pm": values["curv_start_1pm"],
        "curv_end_1pm": values["curv_end_1pm"],
        "curvature_rate_1pm2": values["curvature_rate_1pm2"],
        "E_m": sample["E_m"],
        "N_m": sample["N_m"],
        "azimuth_deg": sample["azimuth_deg"],
        "position_residual_m": residuals["position_residual_m"],
        "heading_residual_rad": residuals.get(
            "heading_residual_rad", residuals["circular_heading_residual_rad"]
        ),
    }


def validate(path):
    with open(path, encoding="utf-8") as stream:
        values = normalized(json.load(stream))
    errors = {key: abs(values[key] - gold) for key, gold in GOLD.items()}
    application_position = math.hypot(errors["E_m"], errors["N_m"])
    application_heading = abs(math.radians(math.remainder(
        values["azimuth_deg"] - GOLD["azimuth_deg"], 360
    )))
    gates = {
        "P1_G1": values["position_residual_m"] <= 1e-3
        and values["heading_residual_rad"] <= 1e-5,
        "P2_CANONICAL": errors["length_m"] <= 1e-4
        and errors["curv_start_1pm"] <= 1e-8
        and errors["curv_end_1pm"] <= 1e-8,
        "P3_APPLICATION": application_position <= 1e-3
        and application_heading <= 1e-5,
    }
    gates["STRICT_ALL"] = all(gates.values())
    return {"path": path, "errors": errors, "gates": gates}


def main():
    paths = sys.argv[1:] or [
        "/private/tmp/direct_ov_clothoid/a/result.json",
        "/private/tmp/direct_ov_clothoid/b/result.json",
        "/private/tmp/direct_ov_clothoid/c/result.json",
    ]
    results = [validate(path) for path in paths]
    print(json.dumps(results, indent=2, sort_keys=True))
    raise SystemExit(0 if all(item["gates"]["STRICT_ALL"] for item in results) else 1)


if __name__ == "__main__":
    main()
