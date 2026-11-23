# When main is red

Moved verbatim from the kirocrew-prepare-pr `SKILL.md` core.

A red check is main's, not yours, when the same check fails on `origin/<base>`:
read `gh run list --branch <base> --event push --workflow <file> --limit 5`, open
the newest red run's failing log, or reproduce the node id on a clean
`origin/<base>` worktree. A `cancelled` run is not a red; a runner or network
outage is a rerun later, not a code fix.

1. **Is someone fixing it?** Search open PRs and issues for the test id, file or
   error line (`gh pr list --state open --search "<term>"`, the same for
   `gh issue list`, plus the open `ratchet-audit` issue). Found one → link it from
   a PR comment and wait for it inside the loop.
2. **Nobody is** → open a `tier:T1` issue (symptom, failing node id, first bad main
   commit if known, log excerpt; label it `tier:T1` if you can, else the Captain
   tiers it). Fix it on a new branch cut from fresh `origin/<base>`, smallest fix,
   and run this skill's full loop on that PR — it is a `fix` PR, so it carries
   `## Pattern harvest`.
3. **The original PR waits.** No push and no override for that red. When the fix
   merges, Phase 1 rebases onto it; push, re-run, continue to green, then Phase 4.
4. A fix that needs a design decision is a pause reason, not a T1.
