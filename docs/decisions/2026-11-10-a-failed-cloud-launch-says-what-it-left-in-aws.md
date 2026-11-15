# A failed cloud launch says what it left in AWS, and says "removed" only for a stack that existed

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-10

## Decision

After a failed provision the dashboard still runs its rollback delete, and when the launch
engine reports that no stack existed and the delete confirms, the card says
"The EC2 stack kc-… was never created, so it is not billing." instead of "was removed so it
stops billing"; an engine that cannot tell keeps the "removed" line. After a failed deploy
`kirocrew cloud launch` names the stack it left with its status, says a `ROLLBACK_COMPLETE`
stack does not bill and any other status may, prints
`kirocrew cloud destroy --tag <tag> --region <region>` (plus `--profile` when one is set), and
no longer words the `--keep-on-failure` tip as if nothing were kept.

## Why

- Both lines told a new user something false about what was in their account: a removal
  that never happened, and silence about a stack that was there.
- Not "Nothing was created in your account": a first launch creates the account's shared
  instance permissions boundary before the stack, and that policy stays.
- The engine check is optional, so the `teardown` contract every engine and provisioner
  implements is unchanged; the CLI names the stack instead of deleting it, so its failure
  path gains no delete.
- The delete runs even when the check says absent, so a lookup that misreads an unrelated
  error cannot turn a "may still be billing" into "not billing".

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/32228#issuecomment-6779957630 -- the
  maintainer's ruling.
- https://github.com/kirodotdev/KiroCrew/issues/32228 -- the issue.
