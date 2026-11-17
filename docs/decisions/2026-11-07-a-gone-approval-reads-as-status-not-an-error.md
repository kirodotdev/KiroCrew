# A press that finds its approval gone is shown as status, not as an error, on every dashboard approval surface

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-07

## Decision

When a press on an approval's buttons finds the approval gone (`isTerminalApprovalRefusal`:
expired, decided elsewhere, or replaced), every dashboard approval surface reports it as status
instead of an `ErrorNotice`, and `errors-use-error-notice` names it as a case it does not flag; a
decide that failed while the approval may still be pending (a network error, a 5xx, a 403 that
asks for a sign-in) stays an `ErrorNotice`.

## Why

- Nothing the user did failed, so an error notice for it reports a failure that did not
  happen.
- Before this, the composer's tool approval bar already showed it as status while most other
  approval surfaces showed it as an error.
- The issue asked for the status register on these surfaces and named a carve-out in
  `errors-use-error-notice` as one way to make the rule agree with it.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/18203 -- the issue.
- https://github.com/kirodotdev/KiroCrew/issues/18203#issuecomment-6711955383 -- the maintainer
  decision.
