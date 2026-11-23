# Optional GitHub helper

Read from the babysit skill's "Read state from the host" section.

If kirocrew-prepare-pr is installed, run its complete script bundle from the TARGET repo,
not the skill directory. Resolve an absolute `SKILL_DIR` first from the active
installation; never use an unresolved default expansion as a path argument.

```bash
python3 "$SKILL_DIR/scripts/pr_status.py" <pr#> --json --reviewers <known-lanes>
python3 "$SKILL_DIR/scripts/pr_findings.py" <pr#>
python3 "$SKILL_DIR/scripts/green_age.py" --pr <pr#>
```

Pin a known fleet with `--reviewers` / `PREPARE_PR_REVIEWERS`; discovery mode only
checks stamps it finds and cannot detect an absent lane. Set the repo's aggregate
via `--readiness-context` / `PREPARE_PR_READINESS_CONTEXT` where needed. The scripts
require sibling `_review_contract.py`; missing scripts are not permission to
pretend the helper ran. Use the host interfaces above for general monitoring.

- `0`: checks clean, but still apply the readiness conditions below.
- `10`: genuinely running. End the cycle silently, without rereading bot bodies,
  logs or diffs; it neither counts nor resets the stall streak.
- `20`: use findings and the reason before acting. Terminal PR state means stop;
  stale reviewer stamps can mean a comment-triggered bot has not posted yet,
  not a code defect. `?`/JSON `null` threads mean unknown, never zero.
- `2`: environment error, escalate rather than loop on it.

`green_age.py` answers the separate question of whether a clean rollup still
describes today's base: `0` fresh, `30` the base moved in files this PR also
touches since the commit this head's CI ran on (a local rebase and re-check,
pushed only when it conflicts or goes red), `2` could not measure — which is neither fresh nor stale.
`pr_status.py --json` carries a reading in `advisory.green_age` measured on the
PR's published head, so it keeps saying 30 after a clean re-check that stayed
local. Take 30 or 0 from `green_age.py` run in the loop's own checkout, which
measures the local head. Keep it out of the stall
key: on a busy repository the base moves constantly, and a commit count in the
key would reset the streak forever.

`--json` appends a last-line object without changing the exit code. Compare ONLY
`progress_key` for stalls. Its `advisory` fields include unresolved threads,
findings, stale/blocking reviewers and `elided_stamp_reviewers`. Report an elided
stamp once per head as a lane-quality note, not a new blocker; the script already
checks whether that shortened stamp matches the current head.

Always use the target checkout even for a full PR URL: thread lookup also uses
cwd, so a foreign checkout can mix two repositories. An unreadable/discarded CI
rollup is an environment blocker, not a bug count or a passing empty list.
