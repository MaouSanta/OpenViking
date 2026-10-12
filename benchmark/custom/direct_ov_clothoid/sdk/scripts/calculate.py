#!/usr/bin/env python3
"""Canonical G1 Hermite clothoid solver for road-survey coordinates."""

import argparse
import json
import math

from scipy.integrate import quad


def normalize(angle):
    return (angle + math.pi) % (2 * math.pi) - math.pi


def solve(e0, n0, az0, e1, n1, az1, fraction=0.37):
    if not 0 <= fraction <= 1:
        raise ValueError("fraction must be in [0, 1]")
    dx, dy = e1 - e0, n1 - n0
    r = math.hypot(dx, dy)
    if r == 0:
        raise ValueError("endpoints must differ")

    theta0 = math.pi / 2 - math.radians(az0)
    raw_theta1 = math.pi / 2 - math.radians(az1)
    theta1 = theta0 + normalize(raw_theta1 - theta0)
    phi = math.atan2(dy, dx)
    phi0 = normalize(theta0 - phi)
    phi1 = normalize(theta1 - phi)
    delta = phi1 - phi0

    a = 3 * (phi0 + phi1)
    for iterations in range(1, 101):
        phase = lambda z: a * z * z + (delta - a) * z + phi0
        g = quad(lambda z: math.sin(phase(z)), 0, 1, epsabs=1e-13)[0]
        if abs(g) <= 1e-12:
            break
        dg = quad(
            lambda z: math.cos(phase(z)) * (z * z - z),
            0,
            1,
            epsabs=1e-13,
        )[0]
        if not math.isfinite(dg) or abs(dg) < 1e-15:
            raise RuntimeError("Newton derivative vanished")
        a -= g / dg
    else:
        raise RuntimeError("Newton iteration did not converge")

    phase = lambda z: a * z * z + (delta - a) * z + phi0
    h = quad(lambda z: math.cos(phase(z)), 0, 1, epsabs=1e-13)[0]
    length = r / h
    if not math.isfinite(length) or length <= 0:
        raise RuntimeError("non-canonical non-positive length")
    kappa0 = (delta - a) / length
    dkappa = 2 * a / (length * length)

    def point(s):
        heading = lambda u: theta0 + kappa0 * u + 0.5 * dkappa * u * u
        e = e0 + quad(lambda u: math.cos(heading(u)), 0, s, epsabs=1e-12)[0]
        n = n0 + quad(lambda u: math.sin(heading(u)), 0, s, epsabs=1e-12)[0]
        theta = heading(s)
        return e, n, theta

    station = fraction * length
    e_f, n_f, theta_f = point(station)
    e_end, n_end, theta_end = point(length)
    return {
        "length_m": length,
        "curv_start_1pm": kappa0,
        "curv_end_1pm": kappa0 + dkappa * length,
        "curvature_rate_1pm2": dkappa,
        "fraction": fraction,
        "station_m": station,
        "E_m": e_f,
        "N_m": n_f,
        "azimuth_deg": (90 - math.degrees(theta_f)) % 360,
        "position_residual_m": math.hypot(e_end - e1, n_end - n1),
        "heading_residual_rad": abs(normalize(theta_end - theta1)),
        "newton_residual": abs(g),
        "iterations": iterations,
    }


def self_test():
    out = solve(0, 0, 0, 0, 10, 0, 0.5)
    assert abs(out["length_m"] - 10) < 1e-10
    assert abs(out["curv_start_1pm"]) < 1e-12
    assert abs(out["curv_end_1pm"]) < 1e-12
    assert out["position_residual_m"] < 1e-10


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("values", nargs="*", type=float)
    parser.add_argument("--fraction", type=float, default=0.37)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        print('{"self_test":"PASS"}')
        return
    if len(args.values) != 6:
        parser.error("expected E0 N0 AZ0 E1 N1 AZ1")
    print(json.dumps(solve(*args.values, args.fraction), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
