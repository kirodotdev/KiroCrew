# A cron run's transcript reopened from History is read-only

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-19

## Decision

A sequence agent's (`cron_<job id>_<agent>`) or a stateless run's
(`cron_<job id>_<run id>`) transcript that the dashboard user reopens from
History with no linked session opens as a read-only tab: it shows the
transcript, refuses a reply with a visible reason (409 `cron_run_read_only`, or
an error row when a turn starts by any other path), and does not offer Edit,
Regenerate, Continue or Resume all; it is not bound to the run's session.

## Why

- Such a tab could never save a reply, so a typed turn ran and then vanished
  without a word; a refusal with a reason is strictly better.
- Binding these transcripts needs retention and notice-routing rules a run or a
  sequence step does not have today; read-only rides the read-only tab path the
  dashboard already has and stays reversible.
- It matches the cron spec, which already says a stateless job's run can be read
  in History but not replied to.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/36032#issuecomment-7007164756 --
  the decision, option (a).
