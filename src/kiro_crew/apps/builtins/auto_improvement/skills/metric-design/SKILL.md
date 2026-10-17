---
name: metric-design
description: Phase 1 of the auto-improvement loop — how the app designs and calibrates the ruler (the trustworthy metric) BEFORE any optimization; calibration itself is backend code, read its result with get_ruler. Covers a low-variance primary metric, frozen anchors, a calibrated noise band, guardrails, reward-hack guards, and a mandatory canary that must clear the band or the ruler is rejected.
always: false
triggers: design the ruler, calibrate metric, metric design, noise band, canary check
---

# metric-design — build the ruler (Phase 1)

Build the ruler before you measure anything with it. This skill explains Phase 1:
how the app designs and proves a metric the keep-or-revert loop can trust. A loop
optimizing a noisy or wrong ruler "wins" on fiction, and that is the dominant risk
this app is built to eliminate — so Phase 2 does not start until Phase 1 has
proven itself.

Calibration is backend code, not an agent task. It runs from the owner-only
`POST /api/apps/auto-improvement/calibrate` route, and again as the pre-flight of
every perf-track run. No agent tool calibrates or writes the ruler, and the
discovery agent must never touch it; read it with the `get_ruler` MCP tool.

## The output — the ruler record

Calibration writes `ruler/ruler.json` under the active repository+branch
workspace (`data/repos/<workspace-key>/ruler/`): `status` (`calibrated` or
`canary_failed`), the primary metric, the noise band, a `baseline` anchor (the
median sample), the canary result and the raw samples. There is no separate
metric-design document. The ruler the active target profile supplies:

- **Primary metric** — a low-variance number where the two measurement arms
  cancel as much shared cost as possible (label + unit + direction). Never
  hard-coded in the UI; it is read from the profile.
- **Attributable sub-stages** — so a win is pinned to a named stage rather than
  hand-waved as a whole-system improvement.
- **Frozen anchors** — reference measurements. Calibration records one, the
  `baseline` median, in `ruler.json`; the shipped GitHub profile declares no
  others.
- **Guardrails** — metrics that must not regress beyond a stated tolerance.
- **Reward-hack guards** — checks the build/test gate structurally cannot see,
  such as "no silent capability shrink" or "a held-out functional probe still
  passes".

## Calibration — the trust gate

1. **Noise band** — repetitions of the untouched baseline under the full
   harness (`calibrationReps`, default 5; a run's pre-flight clamps it to 2–10
   because each rep is a full suite run), then `noise_band = max(2σ, floor)`.
   Any delta inside the band is **no change**, not a small win.
2. **Canary (mandatory by default)** — a known or deliberately forced win that
   MUST clear the band. If it cannot, the harness is broken and the run
   **halts**. This is the Phase-1 gate: every perf-track run re-proves the ruler
   in its own pre-flight before any Phase-2 cycle, whatever `ruler.json` says.
   The bug track skips pre-flight. `canaryAdvisory: true` is an explicit
   operator opt-out that downgrades a failed canary to a warning.

## Why a rejected ruler is a good outcome

Halting on a failed canary feels like a failure and is the opposite. It means the
measurement system caught its own untrustworthiness before spending a night
optimizing noise. Report it plainly and say what would make the harness
measurable instead.
