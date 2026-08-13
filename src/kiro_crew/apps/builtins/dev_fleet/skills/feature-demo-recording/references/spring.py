#!/usr/bin/env python3
"""
spring.py — spring-eased interpolation, ported from preston176/screen-demo-skill's
springInterpolate.ts (which wraps Remotion's spring()).

Config defaults match the skill: damping=200, stiffness=100, mass=1, overshootClamping=True
-> heavily overdamped: a smooth, monotonic ease-out-from-rest with NO overshoot. We solve
the damped-harmonic ODE analytically and normalize so the spring SETTLES exactly at
durationInFrames, then return from+(to-from)*t.
"""
import math

DAMPING, STIFFNESS, MASS = 200.0, 100.0, 1.0


def _spring_progress(tau, zeta, omega0):
    if zeta > 1.0:
        s = math.sqrt(zeta * zeta - 1.0)
        r1 = -omega0 * (zeta - s)
        r2 = -omega0 * (zeta + s)
        A = -r2 / (r2 - r1)
        B = r1 / (r2 - r1)
        x = A * math.exp(r1 * tau) + B * math.exp(r2 * tau)
    elif abs(zeta - 1.0) < 1e-9:
        x = (-1.0 - omega0 * tau) * math.exp(-omega0 * tau)
    else:
        omega1 = omega0 * math.sqrt(1.0 - zeta * zeta)
        x = math.exp(-zeta * omega0 * tau) * (-math.cos(omega1 * tau)
                                              - (zeta * omega0 / omega1) * math.sin(omega1 * tau))
    return 1.0 + x


def _settle_tau(zeta, omega0, eps=1e-3):
    tau, step = 0.0, 0.01
    last = 0.0
    for _ in range(100000):
        tau += step
        p = _spring_progress(tau, zeta, omega0)
        if p >= 1.0 - eps and p >= last:
            return tau
        last = p
    return tau


def spring_interpolate(frame, fps, frm, to, duration_in_frames,
                       damping=DAMPING, stiffness=STIFFNESS, mass=MASS,
                       overshoot_clamping=True):
    if duration_in_frames <= 0:
        return float(to)
    zeta = damping / (2.0 * math.sqrt(stiffness * mass))
    omega0 = math.sqrt(stiffness / mass)
    settle = _settle_tau(zeta, omega0)
    frac = max(0.0, min(1.0, frame / duration_in_frames))
    p = _spring_progress(frac * settle, zeta, omega0)
    if overshoot_clamping:
        p = max(0.0, min(1.0, p))
    return frm + (to - frm) * p


if __name__ == "__main__":
    fps, dur = 60, 18
    vals = [spring_interpolate(f, fps, 0.0, 1.0, dur) for f in range(dur + 1)]
    mono = all(b >= a - 1e-9 for a, b in zip(vals, vals[1:]))
    print("monotonic:", mono, "start:", round(vals[0], 4), "end:", round(vals[-1], 4))
    print("samples:", [round(v, 3) for v in vals[::3]])
