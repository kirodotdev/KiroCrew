# Overriding a false positive or over-engineering

Moved verbatim from the kirocrew-prepare-pr `SKILL.md` core.

A **writer** of the repository may record a judgment that turns a lane green for
one head:

```
/ai-review override <fable|gpt|design|ux|first-principles|scope|all> <current-head-sha> <class>[ #N]: <reason, 20 to 500 characters>
```

Use `false-positive` or `not-applicable` for what follows; the other classes need an
issue or a link ([Risk release records](https://github.com/kirodotdev/KiroCrew/blob/main/docs/ci/ci-and-reviews.md#risk-release-records)).

`fable` is the Opus lane; `scope` is Security Scope Review. Decide it yourself —
do not ask — when all four hold:

1. You verified against the code that the finding is a false positive, or a
   demand the three questions judge disproportional (over-engineering).
2. The `rebutted` disposition with its evidence is already posted.
3. It is not a security, data-loss or corruption finding (any lane, including
   the ones the GPT lane fences), a crash, or a removed guard. A real one gets
   fixed. One you judge *not a defect* is still a human's call: the override
   record is read as proof a person checked it, so draft the `> ` rationale and
   the one-line reason and ask a writer to verify and post it.
4. Your account has write access. Otherwise (an outside author) post the
   disposition and ask a maintainer, with the one-line reason ready to paste.

Start the reason with `agent:` so a reader can tell it from a person's ruling,
and post it with `gh pr comment <n> --body "<the command>"`, naming one lane
(`all` only when one ruling covers every lane). It binds that SHA alone: a
new push needs a fresh judgment. Fork lanes honour it like same-repo ones (the
handler re-runs the bound fork lane run), and PR Readiness counts this lane's
same-head BLOCKs posted before the record as adjudicated: the only same-head exit,
better than a new head. **Report
every override** (lane, span, SHA, reason) in the Phase 4 report.
