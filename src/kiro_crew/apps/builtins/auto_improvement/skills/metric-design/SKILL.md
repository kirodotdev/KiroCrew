---
name: metric-design
description: Phase 1 of the auto-improvement loop — analyze the target repository, then design and calibrate the ruler (the trustworthy metric) BEFORE any optimization. Produces a low-variance primary metric, frozen anchors, a calibrated noise band, guardrails, reward-hack guards, and a mandatory canary that must clear the band or the ruler is rejected.
always: false
triggers: design the ruler, calibrate metric, metric design, noise band, canary check
---

# metric-design — build the ruler (Phase 1)

Build the ruler before you measure anything with it. This skill drives Phase 1:
analyze the target repository, then design a metric the keep-or-revert loop can
trust. A loop optimizing a noisy or wrong ruler "wins" on fiction, and that is
the dominant risk this app is built to eliminate — so Phase 2 does not start
until Phase 1 has proven itself.

## The output — the ruler, as durable config plus a reviewable doc

Writes `data/ruler/`: the calibrated ruler config and a human-readable
metric-design document to review **before any optimization runs**. The ruler the
active target profile supplies:

- **Primary metric** — a low-variance number where the two measurement arms
  cancel as much shared cost as possible (label + unit + direction). Never
  hard-coded in the UI; it is read from the profile.
- **Attributable sub-stages** — so a win is pinned to a named stage rather than
  hand-waved as a whole-system improvement.
- **Frozen anchors** — reference measurements with provenance (for example a
  pinned floor plus a shipped-defaults baseline), recorded in `data/ruler/`.
- **Guardrails** — metrics that must not regress beyond a stated tolerance.
- **Reward-hack guards** — checks the build/test gate structurally cannot see,
  such as "no silent capability shrink" or "a held-out functional probe still
  passes".

## Calibration — the trust gate

1. **Noise band** — around 30 repetitions of the untouched baseline under the
   full harness, then `noise_band = max(2σ, floor)`. Any delta inside the band
   is **no change**, not a small win.
2. **Canary (mandatory)** — a known or deliberately forced win that MUST clear
   the band. If it cannot, the harness is broken and the run **halts**. This is
   the Phase-1 gate: no Phase-2 cycle is trusted until the canary passes. The UI
   disables Start until the ruler reports `status: "calibrated"`, and the backend
   independently refuses to run on an uncalibrated ruler — two checks because a
   UI-only guard is bypassable.

## Why a rejected ruler is a good outcome

Halting on a failed canary feels like a failure and is the opposite. It means the
measurement system caught its own untrustworthiness before spending a night
optimizing noise. Report it plainly and say what would make the harness
measurable instead.
