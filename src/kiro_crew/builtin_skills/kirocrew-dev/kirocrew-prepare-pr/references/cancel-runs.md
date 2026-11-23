# Cancelling the old head's runs

Moved verbatim from the kirocrew-prepare-pr `SKILL.md` core (Phase 3).

**Do not push while the previous head still has runs in flight** — amend into the
pending head. **Once you have decided to change code, cancel the old head's
in-flight runs BEFORE you edit**, after harvesting what you need (the failing log,
each reviewer lane's verdict — a cancelled lane never posts):

```bash
OLD_SHA=$(gh pr view <pr#> --repo <owner>/<repo> --json headRefOid --jq .headRefOid)
gh api "repos/<owner>/<repo>/actions/runs?head_sha=$OLD_SHA&per_page=100" \
  --jq '.workflow_runs[] | select(.status=="queued" or .status=="in_progress") | .id' \
  | while read -r run_id; do gh run cancel "$run_id" --repo <owner>/<repo>; done
```

Leave a reviewer lane running while its verdict still decides *whether* to fix; a
flake gets a rerun after *Before you rerun a red test* below, not a cancel.
