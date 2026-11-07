# Every task a Bot hands to a worker goes into its work ledger, a single task too

Decided by: Joe Guo (maintainer, @iamwhatever)
Date: 2026-11-06

## Decision
Where a Bot's chat mounts the work ledger, every task it hands to a worker is an item in that ledger, a single task too, the same as on the Ultra Mode conductor; the `[Worker report]` wake stays, and a Bot without the ledger keeps its old text.

## Why
- Every task should go into the ledger.
- The Bot and Ultra Mode must work the same way on this.
- A task that skips the ledger has no item, so the Bot's Sessions tab shows "No report yet" for it.

## Evidence
- The pull request that makes the change: https://github.com/kirodotdev/KiroCrew/pull/31770
- The maintainer's restatement on that pull request, the on-record artifact this entry rests on: https://github.com/kirodotdev/KiroCrew/pull/31770#issuecomment-6696538193
- The behaviour it changes: issue https://github.com/kirodotdev/KiroCrew/issues/27930 ("Quick single tasks keep today's `[Worker report]` flow") and PR https://github.com/kirodotdev/KiroCrew/pull/27933.
