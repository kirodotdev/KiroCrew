# Before you rerun a red test

Moved verbatim from the kirocrew-prepare-pr `SKILL.md` core (Phase 3).

**Before you rerun a red test.** A flaky test is a defect someone owns, so a rerun
comes after a record, never instead of one:

- **Prove the red is not yours.** Reproduce the exact node id on a clean
  `origin/<base>` worktree (`kirocrew-worktree-dev`), or show the same node id red on
  `main` or on another head in CI history (a Windows- or macOS-only red cannot be
  reproduced from Linux). A red only your branch shows is yours to fix.
- **Record it in the flake ledger**: the open issues titled `Flaky: <test name> ...`,
  labelled `area: tests`. Search for the test's function name
  (`gh issue list --state open --search "<test name> in:title"`). Comment on the match
  with the run URL, the OS and the failing line, or open one in that form with the same
  three facts.
- **Rerun at most once**, only what failed (`gh run rerun <run-id> --failed`, or
  `--job <job-id>`).
- **Rerun the job; never re-push an identical tree** to re-trigger it
  (`references/ci.md`, *Rerun, don't re-push*).
- **A test with two or more ledger reports in 14 days is fixed before more feature work
  lands on top of it**, not rerun again.
