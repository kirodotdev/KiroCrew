# Your dashboard page, and worker debates and grills

Moved verbatim from the goal-conductor `SKILL.md` core; the core says when to read each part.

## User-visible Dynamic Dashboard

You own your dashboard page; it shows ledger evidence; workers report to you and
never publish your board page. For a long run load the `dashboard` skill, which
carries the page, its flow and its honesty rules. The page is the one the
`[DASHBOARD]` block names. Where no board page is named there is nothing for a
reader to open, so the `task-dashboard` artifact stays as it is today: one
authorized descendant may publish it for this goal, with milestone updates to the
same slug, read as a presentation of ledger evidence and never another ledger or
an acceptance result. The UI already aggregates this session's
descendants, questions and tool approvals; Normal permission does not require
the user to inspect every worker. Your no-file-writing role and the four
non-delegable jobs above do not change, and a dashboard is never a reason to
bypass a tool approval or escalate approval mode.
Before you `dashboard_write` a board page's `needs_you` or `asks` field, or
call `panel_publish`, read `<this skill's dir>/references/dashboard-contract.md`: it gives each page's
fields.

## Worker debate

When one open question — a design, a root cause, a plan, a review — needs
several workers to argue it before anyone builds on the answer, run a worker
debate: proposers and critics with one role each argue in files, you relay and
referee, and the human rules when it is stuck. Read
`<this skill's dir>/references/worker-debate.md` and follow it. Its
participants read each other's files by design, the one exception to
Independent above, and the ledger carries only a short summary and the path to
the debate note.

**Offer a debate only for a costly, contested choice** (two or more plausible
approaches AND a wrong choice is costly), or when the owner asks. Before you
offer one, read "When to offer a debate" in `<this skill's dir>/references/worker-debate.md`.

When your turn's context carries the "Worker grill preview is on" paragraph,
it replaces the unasked offer above: offer a fast worker grill as that
paragraph says, not a debate, and its moderator follows
`<this skill's dir>/references/worker-grill.md`. A debate then runs only when
the owner names one.
