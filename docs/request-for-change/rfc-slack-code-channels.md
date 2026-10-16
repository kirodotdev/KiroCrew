---
title: Slack Code — agent sessions and code channels for Kiro Crew
status: in-progress
author: kseam
created: 2026-10-16
last-audited: 2026-10-16
audited-at: c1302956a1
doc-pr:
implementation-prs: []
tracking-issues: [15175]
supersedes: []
superseded-by: []
---

# RFC: Slack Code — agent sessions and code channels for Kiro Crew

- Status: in-progress. Phase 1 (settings, manifest, update flow) is in the pull
  request that carries this document; Phases 2 and 3 stack on its branch
  (`feat/slack-code-settings`). Every switch defaults off, so no default changes.
- Author: kseam
- Created: 2026-10-16
- Prior art: issue [#15175](https://github.com/kirodotdev/KiroCrew/issues/15175)
  and pganapathy80's `slack-code-channels-preview` branch
  (`7888cb2ec3` agent sessions, `ae2545752c` opt-in code channels,
  `8b243f0b1d` `create_code_channel`, `21fc677d5d` views and the plan canvas),
  also open as [#15259](https://github.com/kirodotdev/KiroCrew/pull/15259).
  The Rust reference implementation is `crates/kiro-bot` on kiro-cli's
  `codex/kiro-bot-development` branch.
- Slack documentation: [Slack Code](https://docs.slack.dev/ai/slack-code/),
  [agent sessions](https://docs.slack.dev/ai/agent-sessions.md), the
  `agents.sessions.*` and `agents.conversations.*` method references.

## Summary

Slack Code is Slack's surface for coding agents. It has two parts:

* **Agent sessions**: an app declared as an agent (`features.agent_view`) shows
  each thread as a session with a status (`processing`, `active`, `suspended`,
  `closed`), a title and a native Stop button.
* **Code channels**: the agent opens a channel per coding task. The whole channel
  is one session, with a context bar (repo, branch, PR), up to five views (a diff,
  HTML, Block Kit, a canvas, a PR), channel commands and an archive step that
  posts a summary back to the message the work started from.

This RFC brings both to Kiro Crew behind two switches and one block of defaults:

```json
"slack": {
  "agent_sessions": false,
  "code_channels": false,
  "code_channel_defaults": {
    "folder": null, "agent": null, "model": null, "reasoning_effort": null
  }
}
```

The person flips a switch in Settings → Slack, picks a default folder and/or
agent (with a model and effort), and the page shows exactly which manifest lines
change and whether their installed app already carries them.

## Motivation

Today a Slack thread is a plain thread. A long coding turn shows a reaction and a
streaming message, and Stop is a Kiro Crew button. Code work, which spans many
turns, a worktree, a diff and a PR, has nowhere in Slack to live except a thread
that scrolls away.

The preview branch proved the runtime works, but it is 2,800 commits behind main,
ships code channels with no settings surface (the user edits the manifest by
hand from the setup guide), and has no answer for the person who already has a
Slack app installed: Slack grants scopes only at install time, so an app created
before the switch silently lacks `code_channels:manage` and every call fails.

## Goals

1. One switch per Slack feature, default off, with the manifest following the
   switches automatically.
2. An existing install learns exactly what to change: the scopes, events and
   feature blocks, a copy action, a link to the app's App Manifest page, and a
   status that reads "Up to date" once the app is reinstalled.
3. Code-channel defaults that "just work": pick a folder, an agent, or both, and a
   new code channel files there and runs that agent on that model and effort.
4. Agent sessions: live status, title and native Stop for every Slack thread.
5. Code channels: the agent opens one for coding work, works in a worktree, keeps
   the diff and the context bar current, and archives with a summary.

## Non-goals

* Pushing a manifest into the user's Slack app programmatically (see
  "Alternatives considered").
* HTTP (Events API over a public URL) transport. Kiro Crew is Socket Mode only,
  and the reference implementation refuses code channels over HTTP for the same
  reasons: cross-worker Stop and slash routing.
* Multi-user Slack. Slack stays owner-only; a code channel inherits the
  authorization of the conversation it was opened from.

## Design

### 1. Settings (Phase 1)

**Config.** `config/sections_slack.py` adds `SlackConfig.agent_sessions` and
`SlackConfig.code_channels` (bool, default `False`) and
`SlackConfig.code_channel_defaults` (`CodeChannelDefaultsConfig`). All three are
ordinary Slack settings on the Slack settings page, not a Developer or Feature
Preview flag, and all three hot-apply: the runtime reads them per event, behind
the capability guard below, so a flip needs no restart. The builder treats
anything that is not a real `True` as off and normalizes every default string,
with `None` as the one "not set" spelling and an effort outside `low…max`
dropped.

**Two layers: toggle and capability.** A feature runs only when its toggle is on
AND the installed app can use it:

* *capability* (`agent_sessions_available()` / `code_channels_available()`,
  `availability(feature) -> (ok, reason)`): the bot scopes Slack reports in
  `auth.test`'s `x-oauth-scopes` header include the feature's scope
  (`assistant:write` / `code_channels:manage`), and Slack has not answered a call
  for the feature with `feature_disabled`, `missing_scope` or
  `not_allowed_token_type`. Those answers latch the feature off for the process
  (`latch_unavailable`): one dashboard notification, one log line, and a silent
  fallback to today's Slack behaviour with no error in Slack. The scopes are read
  on every Socket Mode `hello` (the first connect and each reconnect), which also
  clears the latches, and again whenever the settings page checks the install;
  a reinstall that grants a missing scope lifts its `missing_scope` latch.
* *toggle* (`agent_sessions_toggle(cfg)` / `code_channels_toggle(cfg)`): what
  the manifest renders. The manifest follows the toggle, never the capability,
  because the manifest is how an install gains the capability.

**One read path.** `kiro_crew/slack/features.py` is what every consumer reads:
`agent_sessions_enabled(cfg)` / `code_channels_enabled(cfg)` (toggle AND
capability; what the runtimes call), the two layers above,
`code_channel_defaults(cfg) -> CodeChannelDefaults` (frozen), `manifest_features`,
and the pure resolver `resolve_code_channel_session(defaults, folders,
slack_agent=, origin_project_dir=)`.

**Resolution order** for a new code-channel session, implemented once in that
resolver:

| Value | Order |
|---|---|
| agent | `code_channel_defaults.agent` > the folder's `default_agent` > the agent Slack already uses (`agent.default_agent`) |
| model, effort | `code_channel_defaults` only; unset keeps the agent's own |
| project | the folder's `project_dir` (nearest ancestor that declares one) > the project bound to the thread the channel was opened from > none |
| folder | `code_channel_defaults.folder` (id or unique `Parent/Child` path); the session also inherits the folder's steering |

A folder deleted after it was saved is reported (`folder_missing`) and ignored:
the session is unfiled and every other default still applies. An agent removed
after it was saved is reported (`agent_missing`) and the resolver falls through.

**API.** `GET /api/slack/config` returns the switches, the defaults, the effective
resolution and the warnings. `PUT` validates: a folder must name exactly one
sidebar folder and is stored by id (a rename keeps it); an agent must exist in
the user scope or the folder's project scope; a model goes through the same
guard the crew editor uses (`_validate_role_model`), with `auto` stored as unset;
an effort must be on the ladder. Every refusal and every write is SEL-audited as
`slack.config.update`, like the rest of the Slack save, and the save stays
loopback-only.

**Manifest.** `slack_manifest.render(alias, features=…)` is the single choke
point. Template lines ending in `#@agent_sessions`, `#@code_channels` or
`#@a|b` render only when that switch is on:

| Switch | Feature block | Bot scopes | Bot events |
|---|---|---|---|
| `agent_sessions` | `agent_view` (`agent_description`) | `assistant:write` | `app_context_changed`, `agent_session_stopped`, `agent_session_title_changed` |
| `code_channels` | `code_channels: {enabled: true}` | `code_channels:manage`, `chat:write.customize`, `canvases:write`, `channels:write.invites`, `groups:write.invites` | `agent_session_stopped` |

`channels:read`, `groups:read`, `groups:history` and `message.groups` are already
in the base manifest. No `slash_command_url` is set: Socket Mode delivers channel
commands. `kirocrew manifest`, `GET /api/slack/manifest` and the exfil
redactor's own-link exemption all render from the saved switches; the exemption
rebuilds one exact pattern per switch combination (four), so the carve-out does
not widen.

**Drift.** `GET /api/slack/manifest/status` calls `auth.test` with the stored bot
token and reads the granted scopes from its `x-oauth-scopes` header, then names
the app through `bots.info` (for the App Manifest deep link). Status:

* `up_to_date`: every required bot scope is granted;
* `needs_update`: some are missing (listed);
* `feature_disabled`: scopes are granted but Slack refuses the feature. For code
  channels this is probed with a read-only `agents.conversations.listViews` on a
  channel id no workspace issues; for agent sessions it is the latch the runtime
  sets with `features.latch_unavailable` when `setStatus` answers
  `feature_disabled`;
* `unknown`: no scopes header, or a network failure;
* `token_rejected` / `not_configured`.

Slack does not report an app's event subscriptions to a bot token, so the status
checks scopes only, and the page says so. Scopes the install holds that the
switches no longer need are listed as optional cleanup, never as drift.

**UI.** Settings → Slack gains a "Slack Code" section: the Agent sessions switch
with the irreversibility warning and a confirm before it turns on; the Code
channels switch; a defaults card (folder picker showing the folder's project and
agent, agent picker, the shared model picker, effort picker, a live "a new code
channel runs with" preview, the resolution order); and the "Update your Slack
app" panel (status, the lines each switch adds with missing scopes marked, copy
manifest, open App Manifest, the reinstall steps, Check again).

**Irreversibility.** Slack cannot switch an app from `agent_view` back to
`assistant_view`. Kiro Crew's manifest has never declared `assistant_view`, but
the switch is still one-way on Slack's side once saved there, so the UI says so
before enabling and the confirm restates it. Turning the Kiro Crew switch off
again only stops the gateway using agent sessions; the Slack app stays an agent.

### 2. Agent sessions runtime (Phase 2)

Behind `agent_sessions_enabled(cfg)`, reusing the preview branch's `7888cb2ec3`:

* On a turn's start the thread session is `agents.sessions.setStatus(processing,
  title=…)`, re-sent every ~50 minutes on a long turn (Slack times `processing`
  out to `active` after an hour); on a question or tool approval it is
  `suspended`; on the turn's end `active`.
* `agent_session_stopped` cancels the in-flight turn through the existing
  `slack_stop_in_flight` path, then moves the status away from `processing` (Slack
  does not), stopping the streams named in `streaming_message_ts`.
* `agent_session_title_changed` updates the session title in the dashboard.
* Runs only while `agent_sessions_enabled(cfg)` holds. A `feature_disabled`,
  `missing_scope` or `not_allowed_token_type` answer from `setStatus` is passed to
  `latch_unavailable("agent_sessions", …)`, and the runtime falls back to today's
  thread behaviour until the next reconnect re-probes.

### 3. Code channels runtime (Phase 3)

Behind `code_channels_enabled(cfg)`, reusing `ae2545752c`, `8b243f0b1d` and
`21fc677d5d`, and the reference implementation's ten design decisions.

* **Routing.** A directed human message (not a bot copy) in an ordinary
  conversation may open a code channel. The decision is the agent's: it gets a
  `create_code_channel` tool and opens one for work that needs multiple steps,
  code changes, builds, tests or a PR; when unsure it stays. A deterministic
  `session_id = sha256(app_id, team_id, conversation, source_ts)` makes the
  `agents.conversations.create` call idempotent across retries and restarts.
* **Creation.** `agents.conversations.create(name, session_id,
  origin_channel_id, origin_message_ts)`, privacy inherited from the origin.
  Slack's own copy of the origin message (the `agent_channel_origin_context`
  block) is recognised by a strict match on `origin_link` and not answered.
* **Default-bound sessions.** The new channel's session is one gateway session
  (`slack:<channel_id>`), created with `resolve_code_channel_session` (§1):
  filed in the default folder, on the resolved agent, model and effort, with the
  resolved project directory.
* **Authorization.** A code channel's authority is the origin conversation's,
  restored from the durable origin binding even with a cold cache; a channel
  Kiro Crew did not create is recognised through `conversations.info`
  (`record_type == "agent_channel"` and this bot among its agents), re-checked on
  every message.
* **Worktree, diff and context bar.** When the project is a git repository the
  session works in its own worktree off the project's default branch. After each
  turn the gateway publishes the unified diff against the base as the channel's
  `diff` view (one per channel, capped at 1 MB, truncated with a note past it) and
  sets the context bar (≤5 items: repo, branch, PR, CI) with
  `agents.conversations.setProperties`, resending all items because the bar is
  full replacement.
* **Agent tools.** Pinned to the session's own channel server-side:
  `set_code_channel_view` (html, block_kit, canvas), `publish_plan_canvas` (the
  comment-only plan loop: `setView canvas/comment` → `getCanvas` →
  `setCanvasContent`), `set_code_channel_status`, `archive_code_channel`. No tool
  can target another channel or open a second one.
* **Commands.** `agents.conversations.setCommands` registers `/summarize`,
  `/run-tests` and `/create-pr`, delivered over Socket Mode and turned into a
  prompt for the channel's session.
* **Archive.** When the work is done the agent posts a summary and calls
  `agents.conversations.archive(summary_message_ts)`, which shares it back as a
  reply on the origin message.
* **Status.** The channel's session status follows the same lifecycle as an agent
  session (§2), with no `thread_ts`.

## Migration plan

| Phase | Scope | Exit criteria |
|---|---|---|
| 1 | Config, `slack/features.py`, the toggle-aware manifest, the drift probe, the settings API and UI, docs | Every switch combination renders a manifest that the exfil scanner exempts; the API refuses a missing folder, a missing agent, an unavailable model and an off-ladder effort; the drift probe reports each status against recorded payloads; with both switches off the deep-link payload is byte-identical to before |
| 2 | Agent sessions runtime | A turn sets `processing` then `active`; Stop cancels it and leaves the session `active`; `feature_disabled` falls back to plain threads |
| 3 | Code channels runtime | A coding request opens one channel (idempotent on retry), the session files under the default folder on the resolved agent, the diff view and context bar follow each turn, archive posts the summary to the origin |

Each phase is shippable alone. Phase 1 changes nothing for a user who leaves both
switches off.

## Backward compatibility

Both switches default off. With both off, the rendered manifest and the deep-link
payload are byte-identical to before (pinned by a test), so an existing app needs
no change and `kirocrew manifest` prints what it always printed. With a switch on
but its scope not yet granted, or Slack answering `feature_disabled`, the runtime
behaves exactly as with the switch off and the settings page says why (also
pinned). The
`_slack_manifest_payload_re` helper returns a tuple of patterns instead of one; it
is private to the security package.

## Security considerations

* **No new credential.** The drift probe uses the bot token Kiro Crew already
  holds, only for `auth.test`, `bots.info` and a read-only `listViews`. The token
  is never logged or returned; the cache keys on its digest.
* **Exfil exemption unchanged in strength.** The own-link carve-out still requires
  the payload to equal a rendered manifest exactly; it now accepts each of four
  exact renderings, and a mixed rendering (a feature block without its scope) is
  refused (tested).
* **Settings writes stay loopback-only and SEL-audited**, and validation runs
  before any write, so a refused field changes nothing.
* **Code channels widen the bot's scopes** (`code_channels:manage`,
  `chat:write.customize`, `canvases:write`, `channels:write.invites`,
  `groups:write.invites`), visible on Slack's consent screen and
  listed before the user reinstalls. The scope-ratchet test pins the exact set
  per switch.
* **Phase 3 authorization** is the origin conversation's, never the new channel's
  membership, and every agent tool is pinned to its own channel.

## Alternatives considered

**Push the manifest with `apps.manifest.update`.** Rejected for now. The method
needs an app configuration token, which is minted per *workspace and user*,
expires every 12 hours and rotates through a single-use refresh token
(`tooling.tokens.rotate`). That token can rewrite every app the user can manage in
the workspace, not only Kiro Crew's, so storing it, even in `.env` behind the
vault, adds a credential far broader than anything Kiro Crew holds today, plus a
rotation writer that must never lose the refresh token. The method also
**replaces the whole app configuration**, so a push would silently drop anything
the user added by hand (an extra scope, a shortcut, a renamed bot), and it would
make the irreversible `agent_view` switch with one click rather than through
Slack's own manifest editor. Copy, paste and reinstall keeps Slack's review screen
in the loop, and the drift probe gives the same closing signal ("Up to date").
A future revision could revisit this with `apps.manifest.export` → merge →
update and a short-lived token the user pastes per push, never stored.

**One manifest with everything, always.** Rejected: it would widen every install's
consent screen with scopes a user who never turns Slack Code on does not need,
which the scope ratchet exists to prevent.

**Optional regex segments in the exfil pattern** instead of four exact patterns.
Rejected: it would accept combinations no emitter renders.

**A model classifier to decide when to open a code channel** (the reference
implementation's sandboxed router). Deferred: the agent already decides with a
tool, as on the preview branch, and a second model call per message costs a turn
on every message in every tracked channel.

## Open questions

1. Should code channels require agent sessions? Slack's docs say
   `agents.conversations.create` needs "a recognized agent-session app", which
   suggests `agent_view` is a prerequisite in practice. Phase 1 keeps them
   independent, as the contract defines them; Phase 3 should confirm against a
   pilot workspace and, if needed, make the code-channels switch imply the agent
   view.
2. `code_channel_action` (context-bar action items) has no published event
   payload; Phase 3 ships link items only until it does.
