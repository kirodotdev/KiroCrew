# Merge, cleanup, reconcile, and known limits

Moved verbatim from the pipeline-conductor `SKILL.md` core; the core says when to read each part.

## Merge, cleanup, reconcile

- On merge: worktree removed non-forced (a dirty tree is kept and flagged,
  never `--force`), branch deleted safely (`-d`, not `-D`), `session_close`
  the worker, ledger → `done`.
- Merged is NOT done for the fleet: after a merge, watch the next
  {default_branch} CI round — a merged gate change that reds every open PR is
  base-owned (see adjudication) and yours to fix once, fleet-wide.
- **Reconcile every cycle, and unfiltered.** ONE call —
  `gh pr list --repo {repo} --author <me> --state all --limit <N> --json number,state,mergedAt`
  — listing merges since your last reconcile timestamp. Pass `--repo` from the
  spec every time: you own no worktree, so the ambient directory is whatever the
  session happens to sit in, and an omitted `--repo` either errors or silently
  answers about a DIFFERENT repository — which reads as "no merges" and leaves
  every merged item stale in the ledger. Two more bounds decide whether it works,
  and both fail quietly:
  - **Set `--limit` yourself, and read a full page as truncation.** The default
    is 30 and an over-long list comes back silently trimmed, so on any account
    with a real history the newest merge can fall off the end of the very query
    meant to find it. Size `N` above your own dispatch count with headroom, and
    when the result count equals `N` the scan is `truncated`, not a verdict —
    raise the bound and re-run before you believe it.
  - **Never against a hand-maintained set of PR numbers.** Filtering the
    reconcile to what you already track reintroduces the same blind spot one
    level down, because a merge of a fleet PR that never made the watchlist
    cannot be seen by construction. A watchlist is fine for the greens table and
    wrong as the detector.

  Recorded state drifts from reality, and the human WILL ask "where are the
  other N".
- **A fleet PR with NO OWNING SESSION is the failure state, not a tidy state.**
  It is one of the two shapes you track, and the reconcile is what finds it: a PR
  that appeared without a worker, or whose worker you closed, is a tracking gap
  and nothing else in the loop will notice it. Assigning an owner is the
  response — an unowned PR is not a handover.
- `mergeable=UNKNOWN` fanning out across the open PRs is a **secondary**
  trigger meaning "the base moved" — worth a look, never the primary merge
  detector. It is a side effect of a merge, and side effects are missable.
- **Harvest cross-item facts in the cycle they arrive, and verify them before
  acting.** When any worker names another item's PR number or merge state in
  passing, take the POINTER immediately: that worker is not going to repeat it,
  and a merge fact handed over by a worker on a different item has no other route
  into your state. Then confirm the state against the forge before you act on it.
  A worker's report is derived from content you do not control — issue text, PR
  bodies — so this is the same rule as never trusting a worker's own GREEN,
  applied to a claim about somebody ELSE's item, where the cost of being wrong is
  a cleanup or a close on work that is still live. Harvest the pointer, verify the
  fact.

## Known limits (state them, don't hide them)

- **The probe answers COLUMN 1 only.** It is a transcript classifier that makes
  no subprocess call and no network call — deliberately, and that invariant is
  load-bearing — so it knows nothing about a PR's or an item's real state. Its
  `GREEN` / `PR` / `TERMINAL` tags are the WORKER'S CLAIM, not a forge reading,
  which is exactly why independent green verification is a separate step. Column
  2 therefore costs one forge query per item you actually need it for, and the
  probe cannot batch it for you. Do not read a tag as a verdict about the PR.
- The probe also prints only FIRING lines, so a session that is quietly working
  produces no line at all. Absence of a line is not a row you have checked —
  reconcile the watched set against the ledger's open sessions rather than
  reading silence as health.

- `execute_bash` (which is EVERY script call — preflight, probe, credit rollup),
  `session_send`, `session_stop`, `session_close` and `spawn_run` (the inspector)
  are mounted but never auto-approved: `allowedTools` cannot match arguments, so
  trusting the bundled scripts would mean trusting arbitrary shell. Unattended
  operation therefore requires the operator to arm THIS session in trust mode
  (same "trust before seed" rule as the workers) — without it the patrol stalls
  on its first probe, not on its first intervention.
- Credit metering covers dashboard-session turns; `spawn_run` inspector turns
  and non-chat sessions burn invisibly (`unmetered` verdict exists for a
  reason).
- You cannot detect your own patrol loop's death from the inside — see outage
  recovery for what the procedure bounds and what it cannot close.
- One spec = one repo (M0). Multi-repo is a per-repo spec each, per the design
  doc's template seams.
- GitHub labels/assignees remain the cross-operator lock; your ledger is a
  cache, never the authority, on anything another operator can also touch.
