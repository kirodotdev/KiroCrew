# The `monitor_start` call for the Phase 3 poll

Read from SKILL.md's Phase 3 step 5, exit **10**, before you arm the loop. SKILL.md
keeps the fixed arguments and the rules for them; this file holds the call itself.

**This `message` is the whole instruction a cycle gets.** It is re-injected
verbatim and each wake is a fresh turn, so a woken cycle knows only what is in
this string: a rule that lives in SKILL.md and not here does not reach the loop
at all. That is not theoretical — the cap and the in-flight guard below were
once left out, and a loop on a PR whose base kept moving rebased away a head
that had already gone green on every lane with auto-merge armed.

```
monitor_start(
  message="Check https://github.com/owner/repo/pull/123 with green_age.py "
          "--pr 123 then pr_status.py --reviewers <profile reviewer names>. "
          "green_age 30: re-sync, run the related tests, push. Do NOT push "
          "while the head still has queued or in_progress runs -- wait a "
          "cycle. Then post ONE comment opening with exactly "
          "'Rebased: main moved in' and naming the files and both bases; "
          "that opening is the string the cap counts, so do not reword it. "
          "green_age 31: the re-sync cap is spent. Do NOT rebase: report "
          "which files the base keeps moving in and call autonudge_stop. "
          "pr_status 10: stay silent. Exit 20: read "
          "pr_findings.py and triage; a red main follows When main is red; "
          "once you have decided to change code, harvest the failing log and "
          "each reviewer lane's verdict (a cancelled lane never posts), then "
          "cancel that head's runs before editing. "
          "Kiro Crew AI repairs MUST follow kirocrew-prepare-pr Review repair routing "
          "with model-pinned subagents, then parent verification and Phases "
          "1 -> 2 -> 3. Push only if authorized. Exit 0: Phase 4, arm "
          "auto-merge (omit when the merge is on hold), keep watching; "
          "on a conflict rebase, push, re-arm. "
          "Merged, terminal state, user stop, blocker or spent budget: "
          "report the outcome, overrides and open findings, then call "
          "autonudge_stop.",
  interval_secs=600, max_cycles=144, max_runtime_secs=86400, judge=False,
  banner="kirocrew-prepare-pr: polling PR #123")
```

Replace the example URL with the real one. Under a merge hold, rewrite the
`Exit 0` clause to `Exit 0: report review-ready and keep watching; do NOT arm
auto-merge` — the message is re-injected verbatim every cycle and outlives
the turn that heard the hold. A hold heard after arming goes in with
`monitor_update(message=...)` that turn (`references/rationale.md`). The loop
is gated: an unchanged PR costs no turn and any change wakes it.

Two of the clauses above are load-bearing and easy to lose when editing this
call, so they are named here as well as written into it:

- **`green_age` 31 is not 30.** 30 says rebase; 31 says the PR has spent its
  re-sync budget and a person takes it from here. The script counts the PR
  `Rebased: main moved in` comments posted by the PR's author or by a member of
  the project, because a cycle remembers nothing; never re-derive the cap by
  reading prose.
- **The comment's opening words are an interface, not a label.** `green_age.py`
  matches `Rebased: main moved in` at the start of the body, so a cycle that
  writes its own phrasing ("Rebased onto main", "Re-synced: ...") is counted as
  nothing, the cap never trips, and the loop rebases a green head for as long as
  the PR stays open. That is the very failure this clause exists to stop, and it
  looks exactly like a PR whose base never moves, so the literal string is in
  the `message` above rather than left to the cycle to recall.
- **A STALE answer does not license a push into a running head, and never a
  cancel.** SKILL.md's no-push-while-runs-are-in-flight rule still holds, so
  exit 30 waits a cycle. Cancelling the head's in-flight runs belongs to exit
  20, after a decision to change code — never to a re-sync.

Keep the whole call inside the host's argument budget (**3800 characters** on
the dashboard, counted across every argument, not just `message`). A longer call
is refused outright and nothing is armed, so a `banner` is not optional padding
here: it keeps the long `message` out of the per-cycle transcript row.
