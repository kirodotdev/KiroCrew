# The `monitor_start` call for the Phase 3 poll

Read from SKILL.md's Phase 3 step 5, exit **10**, before you arm the loop. SKILL.md
keeps the fixed arguments and the rules for them; this file holds the call itself.

```
monitor_start(
  message="Check https://github.com/owner/repo/pull/123 with green_age.py "
          "--pr 123 then pr_status.py --reviewers <profile reviewer names>. "
          "green_age 30: re-sync, run the related tests, push, post one "
          "Rebased comment. pr_status 10: stay silent. Exit 20: read "
          "pr_findings.py and triage; a red main follows When main is red; "
          "Kiro Crew AI repairs MUST follow kirocrew-prepare-pr Review repair routing "
          "with model-pinned subagents, then parent verification and Phases "
          "1 -> 2 -> 3. Push only if authorized. Exit 0: Phase 4, arm "
          "auto-merge (omit when the merge is on hold), keep watching; "
          "on a conflict rebase, push, re-arm. "
          "Merged, terminal state, user stop, blocker or spent budget: "
          "report the outcome, overrides and open findings, then call "
          "autonudge_stop.",
  interval_secs=300, max_cycles=288, max_runtime_secs=86400, judge=False,
  banner="kirocrew-prepare-pr: polling PR #123")
```

Replace the example URL with the real one. Under a merge hold, rewrite the
`Exit 0` clause to `Exit 0: report review-ready and keep watching; do NOT arm
auto-merge` — the message is re-injected verbatim every cycle and outlives
the turn that heard the hold. A hold heard after arming goes in with
`monitor_update(message=...)` that turn (`references/rationale.md`). The loop
is gated: an unchanged PR costs no turn and any change wakes it.
