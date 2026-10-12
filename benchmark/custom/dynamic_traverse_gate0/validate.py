#!/usr/bin/env python3
"""Validate raw A/B responses against an independent complex-number oracle."""

import json
import math
import statistics
from pathlib import Path


ROOT = Path(__file__).parent
RESULTS = ROOT / "results"
TOL = 5e-6
COMPILED_URI = (
    "viking://user/default/peers/dynamic-b/memories/entities/dynamic-traverse/"
    "dynamic_traverse_动态导线_circular-intersection-v1_计算规.md"
)
POINTS = [500 + 2500j, 3340 + 660j, 4340 + 5000j, 7600 + 4560j, 8480 + 2010j]


def oracle(radii: list[float]) -> dict:
    segments = [b - a for a, b in zip(POINTS, POINTS[1:])]
    lengths = [abs(segment) for segment in segments]
    directions = [segment / length for segment, length in zip(segments, lengths)]
    curves = []
    for index, radius in enumerate(radii):
        incoming, outgoing = directions[index : index + 2]
        rotation = incoming.conjugate() * outgoing
        delta = math.atan2(rotation.imag, rotation.real)
        tangent = radius * math.tan(abs(delta) / 2)
        arc = radius * abs(delta)
        pi = POINTS[index + 1]
        pc = pi - tangent * incoming
        pt = pi + tangent * outgoing
        center = pc + math.copysign(radius, delta) * 1j * incoming
        curves.append(
            {
                "turn": "L" if delta > 0 else "R",
                "delta_rad": delta,
                "T": tangent,
                "L": arc,
                "PC": [pc.real, pc.imag],
                "PT": [pt.real, pt.imag],
                "CC": [center.real, center.imag],
            }
        )

    gaps = [lengths[0] - curves[0]["T"]]
    gaps.extend(
        lengths[index + 1] - curves[index]["T"] - curves[index + 1]["T"]
        for index in range(len(curves) - 1)
    )
    gaps.append(lengths[-1] - curves[-1]["T"])
    station_pc = 10000 + gaps[0]
    for index, curve in enumerate(curves):
        curve["station_PC"] = station_pc
        curve["station_PI"] = station_pc + curve["T"]
        curve["station_PT"] = station_pc + curve["L"]
        if index + 1 < len(curves):
            station_pc = curve["station_PT"] + gaps[index + 1]
    return {
        "curve2": curves[1],
        "curve3": curves[2],
        "station_POE": curves[-1]["station_PT"] + gaps[-1],
    }


def close(actual: float, expected: float, tolerance: float = TOL) -> bool:
    return math.isfinite(float(actual)) and abs(float(actual) - expected) <= tolerance


def check_oracle() -> None:
    published = {
        "curve2": {
            "turn": "R",
            "delta_rad": -1.478492667762,
            "T": 1139.635576705,
            "L": 1848.115834703,
            "PC": [4084.115884, 3889.462938],
            "PT": [5469.395067, 4847.566310],
            "CC": [5302.199416, 3608.798529],
            "station_PC": 15762.913772,
            "station_PI": 16902.549349,
            "station_PT": 17611.029607,
        },
        "curve3": {
            "turn": "R",
            "delta_rad": -1.104336565388,
            "T": 585.287898832,
            "L": 1049.119737118,
            "PC": [7019.971367, 4638.286073],
            "PT": [7790.932128, 4006.730765],
            "CC": [6892.902672, 3696.822560],
            "station_PC": 19175.665372,
            "station_PI": 19760.953271,
            "station_PT": 20224.785109,
        },
        "station_POE": 22337.070194,
    }
    calculated = oracle([1000, 1250, 950])
    errors = compare(calculated, published, residual_required=False)
    assert not errors, "oracle failed FHWA published control: " + "; ".join(errors)


def parse_message(message: str) -> dict:
    start, end = message.find("{"), message.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in response")
    return json.loads(message[start : end + 1])


def compare(actual: dict, expected: dict, *, residual_required: bool = True) -> list[str]:
    errors = []
    for curve_name in ("curve2", "curve3"):
        got, want = actual.get(curve_name, {}), expected[curve_name]
        if got.get("turn") != want["turn"]:
            errors.append(f"{curve_name}.turn={got.get('turn')} expected {want['turn']}")
        for field in ("delta_rad", "T", "L", "station_PC", "station_PI", "station_PT"):
            try:
                ok = close(got[field], want[field])
            except (KeyError, TypeError, ValueError):
                ok = False
            if not ok:
                errors.append(f"{curve_name}.{field} mismatch")
        for field in ("PC", "PT", "CC"):
            try:
                ok = len(got[field]) == 2 and all(
                    close(value, target) for value, target in zip(got[field], want[field])
                )
            except (KeyError, TypeError, ValueError):
                ok = False
            if not ok:
                errors.append(f"{curve_name}.{field} mismatch")
        if residual_required:
            try:
                ok = 0 <= float(got["max_back_substitution_residual"]) <= TOL
            except (KeyError, TypeError, ValueError):
                ok = False
            if not ok:
                errors.append(f"{curve_name}.max_back_substitution_residual invalid")
    try:
        poe_ok = close(actual["station_POE"], expected["station_POE"])
    except (KeyError, TypeError, ValueError):
        poe_ok = False
    if not poe_ok:
        errors.append("station_POE mismatch")
    return errors


def validate_file(path: Path, expected: dict) -> dict:
    record = json.loads(path.read_text())
    response = record["response"]
    calls = [
        str(event.get("data", ""))
        for event in response.get("events") or []
        if event.get("type") == "tool_call"
    ]
    tool_results = [
        str(event.get("data", ""))
        for event in response.get("events") or []
        if event.get("type") == "tool_result"
    ]
    try:
        value = parse_message(response.get("message", ""))
        errors = compare(value, expected)
    except (ValueError, json.JSONDecodeError) as error:
        errors = [str(error)]
    geometry_station_errors = [
        error for error in errors if ".turn=" not in error and ".delta_rad" not in error
    ]
    return {
        "arm": record["arm"],
        "rep": record["rep"],
        "pass": not errors,
        "geometry_station_pass": not geometry_station_errors,
        "errors": errors,
        "tokens": response.get("token_usage", {}),
        "elapsed_seconds": record["elapsed_seconds"],
        "memory_hit": COMPILED_URI in (response.get("relevant_memories") or ""),
        "web_calls": sum(
            call.startswith("web_search(") or call.startswith("web_fetch(") for call in calls
        ),
        "official_source_hit": any(
            "fhwa.dot.gov/bridge/pubs/hif22034.pdf" in result for result in tool_results
        ),
        "openviking_reads": sum(call.startswith("openviking_multi_read(") for call in calls),
        "tool_calls": len(calls),
    }


def main() -> None:
    check_oracle()
    expected = oracle([1000, 1234.5, 987.25])
    rows = [validate_file(path, expected) for path in sorted(RESULTS.glob("[ab][1-3].json"))]
    arms = {arm: [row for row in rows if row["arm"] == arm] for arm in ("A", "B")}
    paired_pass = [
        rep
        for rep in range(1, 4)
        if all(next(row for row in arms[arm] if row["rep"] == rep)["pass"] for arm in ("A", "B"))
    ]
    a_official_source_hits = sum(row["official_source_hit"] for row in arms["A"])
    summary = {
        "oracle_control": "PASS",
        "tolerance": TOL,
        "protocol_readiness": {
            "status": "BLOCKED_WEB_SOURCE_RETRIEVAL",
            "a_official_source_hits": a_official_source_hits,
            "required_hits": len(arms["A"]),
            "valid_for_token_comparison": a_official_source_hits == len(arms["A"]),
            "reason": (
                "A never retrieved the official FHWA source, while B received a private "
                "21-step compiled contract."
            ),
        },
        "rows": rows,
        "aggregate": {
            arm: {
                "pass_count": sum(row["pass"] for row in arm_rows),
                "memory_hit_count": sum(row["memory_hit"] for row in arm_rows),
                "web_call_count": sum(row["web_calls"] for row in arm_rows),
                "median_total_tokens": statistics.median(
                    row["tokens"]["total_tokens"] for row in arm_rows
                ),
                "median_elapsed_seconds": statistics.median(
                    row["elapsed_seconds"] for row in arm_rows
                ),
            }
            for arm, arm_rows in arms.items()
        },
        "paired_double_pass_reps": paired_pass,
        "strict_token_reduction": None,
    }
    if paired_pass:
        reductions = []
        for rep in paired_pass:
            a = next(row for row in arms["A"] if row["rep"] == rep)["tokens"]["total_tokens"]
            b = next(row for row in arms["B"] if row["rep"] == rep)["tokens"]["total_tokens"]
            reductions.append({"rep": rep, "tokens_saved": a - b, "percent": (a - b) / a})
        summary["strict_token_reduction"] = reductions
    (RESULTS / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
