# Sync and review holds

Read from the babysit skill's "Read state from the host" section.

Conflict, `BEHIND`, or `green_age.py` exit 30 requires an authorized sync, not
another unchanged poll (for exit 30, a local rebase and re-check, pushed only when
it conflicts or goes red):
GitHub cannot build a conflicted merge ref, so `pull_request` checks may never
start while old checks look green. `BEHIND` is the weakest of the three and on
some repositories never appears at all: `mergeStateStatus` reports it only under
strict up-to-date protection, so where that is off, a base that moved underneath
a green head is visible only through the green-age reading in
[github-helper.md](github-helper.md). Rebase
unambiguous conflicts under the repo's history rules, re-verify and push only
with authorization; escalate ambiguous conflicts. A draft or `CHANGES_REQUESTED`
also survives waiting. Read the reviewer:
a product hold needs a human decision, not repeated patches. Report it once and
stop with the blocking review quoted.
