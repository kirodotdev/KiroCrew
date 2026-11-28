# A usage answer with no credit limit says so instead of reporting a failed balance read

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-18

## Decision

When the Kiro usage service answers with credits used and no credit limit anywhere
in its answer, and no earlier balance for the same account is still held, the credit
pill, the Kiro Account panel and the chat's `/usage` say that the service reports
credits used but no credit limit for the signed-in account, so there is no balance
to show, instead of saying the balance could not be read; which pool is read as the
plan does not change, and no figure is shown.

## Why

- The person asked to see the balance, or the reason it cannot be shown. An answer
  with no limit has no balance, and "could not read your balance" names a failure
  that did not happen.
- Reading a figure from another credit pool on a guess could show a confidently
  wrong balance, which the earlier ruling on two credit pools and the first decision
  on this issue both refused.
- The reporter's own kiro-cli `/usage` shows credits used and no limit for their
  organization account, which is consistent with such an answer; the notice is
  decided from the answer itself, never from that report.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/35702#issuecomment-6987789952 -- the
  maintainer's ruling.
- https://github.com/kirodotdev/KiroCrew/issues/35702#issuecomment-6968598761 -- the
  reporter's kiro-cli `/usage` answer.
- https://github.com/kirodotdev/KiroCrew/issues/35702#issuecomment-6866695238 -- the
  first decision on the issue: no pool is read on a guess.
- https://github.com/kirodotdev/KiroCrew/issues/8945#issuecomment-5872227106 -- the
  earlier ruling on two credit pools.
