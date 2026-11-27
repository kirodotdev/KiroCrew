# Phase 4: rules for fixes that narrow or widen

Moved verbatim from the kirocrew-prepare-pr `SKILL.md` core (Phase 4).

A recurring-span retrospective's disposition names the span and its hit count
in a `> ` line.

- **A fix that narrows one branch of a fallback or resolution chain must come with a table of every branch** and why each is now correct.
- **Never decline a reviewer's wider scope without a failing test proving the narrower scope is sufficient.**
- **Widening a fix is itself a code change** — re-check the widened sites for the OPPOSITE failure mode.
