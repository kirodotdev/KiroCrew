---
title: The batch write guard is on by default
status: implemented
author: kirocrew agent session, directed by iamwhatever
created: 2026-11-09
last-audited: 2026-11-09
audited-at: 87defc8a5f
doc-pr: 33570
implementation-prs: [33582]
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: The batch write guard is on by default

Status: implemented in
[#33582](https://github.com/kirodotdev/KiroCrew/pull/33582). The repository owner (iamwhatever) asked for this on
2026-11-09 through the conductor chat that dispatched the work. This document
landed first, and the code change rebased onto it. Every citation below was read
on `origin/main` at `1e3b894d87`. Citations name a file and a symbol, not a line
number. Paths are under `src/kiro_crew/` unless they start with `test/` or
`docs/`.

This document lands on its own because it changes a default, and
[#29612](https://github.com/kirodotdev/KiroCrew/pull/29612), which added the
guard, left the default flip to "a later RFC". This is that RFC.

## 1. Summary

`hooks.batch_write_guard` becomes `true` unless the user sets it to `false`.
Nothing else about the guard changes: what it refuses, how it groups a batch
and which calls count as writes stay as on main.

## 2. Current state

- The setting is `HooksConfig.batch_write_guard` in
  `hook_runtime/hook_types.py`, default `False`. `HooksConfig.from_dict` turns it
  on only for a JSON `true`.
- The schema entry is `hooks.batch_write_guard` in `config/loader_config.py`,
  `"default": False`, mirrored in `config-baseline.json`.
- `acp/batch_guard.py` `guard_settings` reads the setting. Both transports call
  `screen_approval` after the security floor allows a request.
- With the guard on, a write-class call is refused when it shares a parallel
  tool batch with a read-class call (or a call that mints ids) AND its arguments
  quote a token the session has not seen: a sha, an `it_`, `chat-` or `art_` id,
  a long numeric id, or a PR or run number. This token rule came from
  [#31274](https://github.com/kirodotdev/KiroCrew/pull/31274).

## 3. Motivation

Agents still send a write in the same response as the read whose answer it
quotes. The write's arguments are written before the read returns, so the sha,
id or number in it is a guess. Prompt rules and saved lessons have not stopped
this ([#32861](https://github.com/kirodotdev/KiroCrew/issues/32861) records one
conductor session doing it after the prompt-only fix in [#30189](https://github.com/kirodotdev/KiroCrew/pull/30189)).

Recent slips in the dispatching conductor's own crew, on 2026-11-08:

- a conductor sent a worker message quoting CI reds and a head sha in the same
  batch as the `gh` read that would produce them;
- a worker filed a progress report naming a PR head in the same batch as its
  push and read, reported the wrong head, and had to send a correction.

Both were writes quoting a value the batch had not returned yet: the exact case
the guard refuses. Both installs had the guard off, because it is off by
default.

## 4. Why on is now safe enough

Before #31274 the guard refused every write next to a read. An offline replay
over about 500 recorded sessions refused 1,107 steps, and 60% of a hand-judged
sample were false refusals (the write did not use the read). #31274 changed the
rule to refuse only a write that quotes an unseen token. Its replay of the
shipped code refused 154 of 90,839 writes (0.17%), with about 15% false
refusals in a 20-case sample.

Cost of one refusal, true or false: one extra model step. The refusal is
steered into the running turn with the reason and the values it found, and the
same write sent alone in the next step passes.

Opt-out: set `hooks.batch_write_guard` to `false` in `config.json`.

## 5. Design

- `HooksConfig.batch_write_guard` defaults to `True`.
- `HooksConfig.from_dict` reads the key with `_coerce_bool(..., default=True)`,
  the helper already used for the other opt-out flags. The documented opt-out
  is the JSON boolean `false`: a `config.json` load drops a quoted `"false"` in
  validation, with a hint, so it reads as the default (on). A missing key or an
  unreadable value keeps it on.
- The schema default in `config/loader_config.py` and `config-baseline.json`
  becomes `true`, and the help text says "On by default".
- `guard_settings` still returns off when `config.json` cannot be read: on a
  load error, and when the load reports `config.json` or its overlay
  unreadable (`_base_unreadable`, `_overlay_unreadable`), since the empty
  `hooks` it then returns cannot carry an operator's `false`. The
  guard is a discipline guard, not a security control, so a broken config
  keeps the old behaviour rather than refusing calls.
- Docs that say "off by default" or "opt-in" are updated in the same code PR
  (`src/kiro_crew/docs/configuration.md`, `docs/system-specs/modules/acp-client.md`,
  `docs/system-specs/modules/config.md`, `docs/system-specs/modules/sel.md`,
  the module docstring of `acp/batch_guard.py`).

## 6. Non-goals

- Any change to what the guard refuses, how it groups calls or which tools are
  write-class.
- The gaps listed in section 7. They are design-level changes tracked in
  #32861 and stay out of this change.

## 7. Known limits (unchanged by this RFC)

Related: [#32861](https://github.com/kirodotdev/KiroCrew/issues/32861). Its
maintainer comment (bolichen97, 2026-11-08) names three gaps. All stay as they
are:

- `dashboard_write` is not counted as a write, so a dashboard write that quotes
  an unseen value is not refused.
- A batch is grouped by when calls settle, not by the model response that sent
  them. A harness that announces one response's calls one at a time can split
  them into separate batches.
- `nothing_to_do` ends the turn even when it shares a response with another
  call.

Behaviour that turning the guard on already brings, and that now reaches every
install that does not opt out (from `src/kiro_crew/docs/configuration.md`
`hooks.batch_write_guard`):

- On the KAS backend, Kiro Crew's own write tools (ledger, report and send)
  that an agent's `allowedTools` auto-approves come back as approval requests
  Kiro Crew answers itself (`batch_guard.withhold_write_auto_approval`). A
  write sent alone still runs with no prompt.
- Under a configured governance profile or a matching `confirm_tools` pattern,
  such a write now goes through those checks, so it can prompt where it ran
  silently before.
- A native KAS sub-agent, or a child session with its own session id, has no
  Kiro Crew answer for those requests, so its spec-approved writes ask a person,
  or are refused when nobody is there. An install that relies on such
  sub-agents writing unattended should set the guard to `false`.
- A kiro-cli session still approves its spec's `allowedTools` itself, so those
  writes are not seen by the guard (the `shortcut:` note on
  `withhold_write_auto_approval`).

## 8. Backward compatibility

A two-way door: a `git revert` of the code PR restores the old default, and no
stored format changes. An install that already sets the key keeps its value. An
install that never set it gets the guard on from its next session.

## 9. Tests

- The default test in `test/test_batch_write_guard.py` pins on-by-default,
  `false` turning it off, and an unreadable value keeping it on.
- `guard_settings` with an empty `hooks` section returns on.
- Existing tests that stub `guard_settings` to `(False, ())` keep pinning the
  off path.

## 10. Alternatives considered

- Keep it opt-in and add more prompt rules. Rejected: #32861 shows prompt rules
  did not stop the slips.
- Turn it on only for conductors and workers. Rejected for now: it needs a
  per-agent setting nobody asked for, and the refusal costs every agent the same
  one step.
