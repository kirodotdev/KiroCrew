# A loop at its cycle cap wakes within two minutes of a turn

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-25

## Decision

After a turn in its slot ends (the dashboard turn end, or a channel loop's own re-arm after its turn), a prompt loop whose cycle cap is spent wakes within a two-minute grace, so the existing cycle-cap branch runs then instead of one interval later; the loop's deadline itself is unchanged. It does not wake the moment the turn ends, and an arm that follows no turn (a revival through an update, a restart, a reconciler rescue) keeps the deadline.

## Why

- Until the cap branch runs, a capped loop reads as running, sends no stop notice and is still there when a new `monitor_start` is refused, for up to a day.
- Research Lab ends its own loop a few seconds after its last cycle and still gets there first.
- An app that revives a cap-stopped loop is not stopped and notified every two minutes.

## Evidence

- Maintainer decision: https://github.com/kirodotdev/KiroCrew/issues/26764#issuecomment-7163207415
- Issue: https://github.com/kirodotdev/KiroCrew/issues/26764
- The runtime-budget half, and why the cap half was left open: https://github.com/kirodotdev/KiroCrew/pull/35437
