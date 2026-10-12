# Dynamic Traverse Gate 0: FHWA circular-intersection-v1

Source basis: Federal Highway Administration, *Bridge Geometry Manual*,
FHWA-HIF-22-034 (2022), horizontal alignment equations and Appendix B.
Distribution statement: no restrictions. This page defines an experimental
calculation contract; it does not establish full road-design compliance.

## Retrieval aliases

- 动态导线
- 动态导线法
- 交点法圆曲线
- dynamic traverse
- circular-intersection-v1
- FHWA-HIF-22-034

## Scope and units

This is **Gate 0**, the circular-curve intersection-method kernel used when a
PI is moved and the whole downstream alignment is recomputed. It is not a
complete dynamic-traverse method: it excludes transition spirals, superelevation,
and multi-PI optimization. Coordinates use `x = Easting`, `y = Northing`.
All coordinates, radii, lengths, and stations use feet. Angles used in formulas
and returned as `delta_rad` use radians.

## Inputs

An ordered point chain `P0 ... P(n+1)`, radii `R1 ... Rn` for interior PIs,
and initial station `S0` at `P0`.

## Strict calculation order

For each segment `j = 1 ... n+1`:

1. `v_j = P_j - P_(j-1)`
2. `B_j = hypot(v_j.x, v_j.y)`
3. `u_j = v_j / B_j`

For each curve `i = 1 ... n`:

4. `cross_i = u_i.x*u_(i+1).y - u_i.y*u_(i+1).x`
5. `dot_i = u_i.x*u_(i+1).x + u_i.y*u_(i+1).y`
6. `delta_i = atan2(cross_i, dot_i)`; positive is left, negative is right.
7. `Delta_i = abs(delta_i)`
8. `T_i = R_i * tan(Delta_i / 2)`
9. `L_i = R_i * Delta_i`
10. `PC_i = P_i - T_i*u_i`
11. `PT_i = P_i + T_i*u_(i+1)`
12. `nL_i = (-u_i.y, u_i.x)`
13. `CC_i = PC_i + sign(delta_i)*R_i*nL_i`

Compute tangent gaps only after every `T_i` is known:

14. `q_0 = B_1 - T_1`
15. `q_i = B_(i+1) - T_i - T_(i+1)` for `i = 1 ... n-1`
16. `q_n = B_(n+1) - T_n`

Propagate stations in route order; each result feeds the next formula:

17. `S_PC1 = S0 + q_0`
18. `S_PTi = S_PCi + L_i`
19. `S_PC(i+1) = S_PTi + q_i`
20. `S_PIi = S_PCi + T_i`
21. `S_POE = S_PTn + q_n`

Do not use chord length in place of arc length. Do not convert `atan2` output
to degrees before computing `T` or `L`. Do not discard the sign of `delta`
when computing the center.

## Required back-substitution checks

For every curve, calculate and report the maximum absolute residual of:

- `abs(norm(PC_i - P_i) - T_i)`
- `abs(norm(PT_i - P_i) - T_i)`
- `abs(norm(PC_i - CC_i) - R_i)`
- `abs(norm(PT_i - CC_i) - R_i)`
- `abs(dot(PC_i - CC_i, u_i))`
- `abs(dot(PT_i - CC_i, u_(i+1)))`

The last PT center-radius and tangent-orthogonality checks are independent
back-substitutions; they catch sign/order mistakes in `CC_i`.

## Public worked example (Curve 1 only)

For `P0=(500,2500)`, `P1=(3340,660)`, `P2=(4340,5000)`, and `R1=1000`:

- `turn = L`
- `delta_rad = 1.919222667006`
- `T = 1427.176520611`
- `L = 1919.222667006`
- `PC = (2142.237995, 1436.014820)`
- `PT = (3660.446123, 2050.736173)`
- `CC = (2685.979298, 2275.267700)`

With `S0=10000`, `S_PC1=11956.785654`, `S_PI1=13383.962175`, and
`S_PT1=13876.008321`.

No Curve 2 or Curve 3 expected outputs are included; those are held out for
external validation.
