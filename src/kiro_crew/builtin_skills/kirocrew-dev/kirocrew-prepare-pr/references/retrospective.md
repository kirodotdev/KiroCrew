# Retrospective verdicts

Read from SKILL.md's *Iteration budget and retrospective* on `pr_findings.py --rounds`
exit **30**, after the read-only retrospective returns its per-mechanism verdicts.
The retrospective is a step, not a stop: rule on every mechanism and continue
Phase 1 → 2 → 3 in the same turn.

First that holds: **remove** (goal survives, defect stays fixed); **revert** (mostly
beyond the goal since that head); **smaller replacement** (removal reopens the defect);
**keep** plus the one invariant that makes the span unreachable. In doubt, smaller
wins. Post a class-level `> ` disposition for each subtraction.

Why the loop decides and continues rather than asking: `references/rationale.md`.
