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

- Status: in-progress. Phase 1 (the switch registry, the manifest built from the
  switches, the update flow, the capability latch and the scope probe, plus the
  Agent sessions switch and its runtime, first opened as
  [#21422](https://github.com/kirodotdev/KiroCrew/pull/21422)) is in the pull
  request that carries this document. Phase 2 (code channels: their switch, their
  defaults and their runtime) stacks on it, starting with
  [#21410](https://github.com/kirodotdev/KiroCrew/pull/21410). Every switch
  defaults off, so no default changes.
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

This RFC brings both to Kiro Crew behind two switches and one block of defaults
(each setting ships with the phase that reads it: `agent_sessions` in Phase 1,
`code_channels` and `code_channel_defaults` in Phase 2):

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

**Config.** `config/sections_slack.py` adds `SlackConfig.agent_sessions` (bool,
default `False`). It is an ordinary Slack setting on the Slack settings page,
not a Developer or Feature Preview flag, and it hot-applies: the agent-sessions
runtime (§2) reads it per event, behind the capability guard below, so a flip
needs no restart. Each phase ships only the settings it reads: saving an
`agent_view` manifest in Slack is one-way, so `slack.agent_sessions` arrives
with its runtime, and `slack.code_channels` and `slack.code_channel_defaults`
arrive with the code-channels runtime in Phase 2 (§3). The builder treats
anything that is not a real `True` as off.

**Switch registry.** `slack_manifest.FEATURE_SCOPES` maps each switch name to the
bot scope it cannot run without; the name is also its `slack.<name>` config key
and its template gate. Everything downstream iterates it: the render, every
combination the exfil validator rebuilds, the requirement diffs, the drift probe,
the capability record, the settings API's switch fields (`<name>`,
`<name>_capability`) and the update panel. Registering a switch is therefore:

1. its `FEATURE_SCOPES` entry and its `#@<name>` lines in `slack-manifest.yaml`;
2. its `SlackConfig` field and builder read (`slack.<name>`, regenerating
   `config-baseline.json`);
3. in the dashboard, its key in the `SlackCodeSwitchKey` union and its
   `SLACK_CODE_SWITCH_LABEL` entry (`pages/settings/slackCodeSwitches.ts`), its
   toggle in `SlackCodePanel`, and its catalog strings.

**Two layers: toggle and capability.** A feature runs only when its toggle is on
AND the installed app can use it:

* *capability* (`available(feature)`, `availability(feature) -> (ok, reason)`):
  the bot scopes Slack reports in `auth.test`'s `x-oauth-scopes` header include
  the feature's registered scope (`assistant:write` for agent sessions), and Slack has not answered a call
  for the feature with `feature_disabled`, `missing_scope` or
  `not_allowed_token_type`. Those answers latch the feature off for the process
  (`latch_unavailable`): one dashboard notification, one log line, and a silent
  fallback to today's Slack behaviour with no error in Slack. The scopes are read
  on every Socket Mode `hello` (the first connect and each reconnect), which also
  clears the latches, and again whenever the settings page checks the install;
  a reinstall that grants a missing scope lifts its `missing_scope` latch.
* *toggle* (`toggle(cfg, feature)`): what
  the manifest renders. The manifest follows the toggle, never the capability,
  because the manifest is how an install gains the capability.

**One read path.** `kiro_crew/slack/features.py` is what every consumer reads:
`enabled(cfg, feature)` / `agent_sessions_enabled(cfg)` (toggle AND capability;
what the runtimes call), the two layers above, and `manifest_features`. Phase 2
adds the code-channels readers to the same module (§3).

**API.** `GET /api/slack/config` returns each registered switch (`<name>`) and
what the install can do with it (`<name>_capability`). `PUT` accepts only
registered switches, each a real boolean. Every refusal and every write is
SEL-audited as `slack.config.update`, like the rest of the Slack save, and the
save stays loopback-only.

**Manifest.** `slack_manifest.render(alias, features=…)` is the single choke
point. Template lines ending in `#@<switch>` render only when that switch is on.
Phase 1 registers `agent_sessions`; the `code_channels` row is what Phase 2
registers:

| Switch | Feature block | Bot scopes | Bot events |
|---|---|---|---|
| `agent_sessions` | `agent_view` (`agent_description`) | `assistant:write` | `app_context_changed`, `agent_session_stopped`, `agent_session_title_changed` |
| `code_channels` (Phase 2) | `code_channels: {enabled: true}` | `code_channels:manage`, `chat:write.customize`, `canvases:write`, `channels:write.invites`, `groups:write.invites` | `agent_session_stopped` |

`channels:read`, `groups:read`, `groups:history` and `message.groups` are already
in the base manifest. No `slash_command_url` is set: Socket Mode delivers channel
commands. `kirocrew manifest`, `GET /api/slack/manifest` and the exfil
redactor's own-link exemption all render from the saved switches; the exemption
rebuilds one exact pattern per switch combination (two in Phase 1, four once
Phase 2 registers its switch), so the carve-out does not widen. Phase 2 extends
the gate to `#@a|b` for the one line two switches share (`agent_session_stopped`,
which code channels also need).

**Drift.** `GET /api/slack/manifest/status` calls `auth.test` with the stored bot
token and reads the granted scopes from its `x-oauth-scopes` header, then names
the app through `bots.info` (for the App Manifest deep link). Status:

* `up_to_date`: every required bot scope is granted;
* `needs_update`: some are missing (listed);
* `feature_disabled`: scopes are granted but Slack refuses the feature. Agent
  sessions have no read-only probe, so this is the latch the runtime sets with
  `features.latch_unavailable` when Slack answers `feature_disabled`; Phase 2
  probes code channels directly with a read-only
  `agents.conversations.listViews` on a channel id no workspace issues;
* `unknown`: no scopes header, or a network failure;
* `token_rejected` / `not_configured`.

Slack does not report an app's event subscriptions to a bot token, so the status
checks scopes only, and the page says so. Scopes the install holds that the
switches no longer need are listed as optional cleanup, never as drift.

**UI.** Settings → Slack gains a "Slack Code" section: the Agent sessions switch
and the "Update your Slack app" panel (status, the lines each switch adds with
missing scopes marked, copy manifest, open App Manifest, the reinstall steps,
Check again).

The update panel lists every registered switch, so Phase 2's switch appears
there with no change to the panel.

### 2. Agent sessions runtime (Phase 1)

Phase 1 registers the `agent_sessions` switch (§1, Switch registry:
`assistant:write`, the `agent_view` block and the agent-session events) together
with the runtime that handles it, reusing the preview branch's `7888cb2ec3`. It
was first opened on its own as
[#21422](https://github.com/kirodotdev/KiroCrew/pull/21422) and folded in so
that every switch ships with its reader. It runs behind
`agent_sessions_enabled(cfg)` (`enabled(cfg, "agent_sessions")`).

* **Irreversibility.** Slack cannot switch an app from `agent_view` back to
  `assistant_view`. Kiro Crew's manifest has never declared `assistant_view`, but
  saving the agent-view manifest in Slack is still one-way. The switch only
  changes the generated manifest, so the warning sits at the update steps, before
  the manifest is saved in Slack. Turning the Kiro Crew switch off
  again only stops the gateway using agent sessions; the Slack app stays an agent.

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

### 3. Code channels (Phase 2)

**Settings.** Phase 2 adds `SlackConfig.code_channels` (bool, default `False`)
and `SlackConfig.code_channel_defaults` (`CodeChannelDefaultsConfig`: folder,
agent, model, effort; `None` is the one "not set" spelling and an effort
outside `low…max` is dropped), registers `code_channels` in `FEATURE_SCOPES`, and
adds to `slack/features.py` `code_channels_enabled(cfg)`,
`code_channel_defaults(cfg) -> CodeChannelDefaults` (frozen) and the pure
resolver `resolve_code_channel_session(defaults, folders, slack_agent=,
origin_project_dir=)`. The settings page gains the Code channels switch and a
defaults card (folder picker showing the folder's project and agent, agent
picker, the shared model picker, effort picker, a live "a new code channel runs
with" preview, the resolution order). `GET /api/slack/config` adds the defaults,
the effective resolution and the warnings; `PUT` validates them: a folder must
name exactly one sidebar folder and is stored by id (a rename keeps it); an
agent must be one the picker offers (below) in the user scope or the folder's
project scope; a model goes through the same guard the crew editor uses
(`_validate_role_model`), with `auto` stored as unset; an effort must be on the
ladder.

**Resolution order** for a new code-channel session, implemented once in that
resolver:

| Value | Order |
|---|---|
| agent | `code_channel_defaults.agent` > the folder's `default_agent` > the agent Slack already uses (`agent.default_agent`) |
| model, effort | `code_channel_defaults` only; unset keeps the agent's own |
| project | the folder's `project_dir` (nearest ancestor that declares one) > the project bound to the thread the channel was opened from > none |
| folder | `code_channel_defaults.folder` (id or unique `Parent/Child` path) > the auto folder: a top-level sidebar folder named `Code channels` (`features.AUTO_FOLDER_NAME`). The session also inherits the folder's steering |

**The auto folder.** With no default folder set, a code channel still gets one,
so a person who touches no setting gets a working channel. The resolver stays
pure: when no top-level `Code channels` folder exists (exactly one matches,
case-insensitively, or none counts) it returns `folder_id=None`,
`folder_path="Code channels"` and `folder_auto_create=True`, and resolves the
agent as if the folder had `default_agent = kirocrew-conductor`
(`features.AUTO_FOLDER_AGENT`). The Phase 2 runtime creates that folder the
first time a channel needs it, through the sidebar's own folder-create path
(`create_folder_record`, mkdir -p semantics, SEL-audited like the sidebar),
with that `default_agent`, then files the session into its id. Once it exists
its own settings apply, so a person can change or clear its agent. The
settings page shows it as the default folder row, "Code channels (created
automatically · kirocrew-conductor)", and its "New folder…" row opens the
sidebar's folder-create modal and selects the new folder.

A folder deleted after it was saved is reported (`folder_missing`) and ignored:
the session is unfiled, not moved to the auto folder (the person chose a
folder, and the page says it is gone), and every other default still applies.
An agent removed after it was saved is reported (`agent_missing`) and the
resolver falls through.

**Which agents.** The default-agent picker and the `PUT` check offer the same
list (`code_channel_agents` in the `GET`): the chat picker's template choices
(`agent_catalog._choosable_templates`, so `kirocrew-conductor`,
`kirocrew-team-lead` and every other shipped agent a person can run), minus the
agents that are not for a conversation: `kirocrew-lite` (titles, compaction and
the routing classifier; no tools), `kirocrew-guest` (a trust boundary), the
dashboard manager, `kirocrew-heartbeat` (unattended polling), Kiro Crew's
generated specs (the side-turn read-only spec, skill-view aliases) and
crewmates' private copies.

**Runtime.** Behind `code_channels_enabled(cfg)`, reusing `ae2545752c`, `8b243f0b1d` and
`21fc677d5d`, and the reference implementation's ten design decisions.

* **Routing (a model classifier, in scope for this stack).** A directed human
  message (not a bot copy) in an ordinary conversation may open a code channel,
  and a model decides whether it does, as Slack's lifecycle guidance asks: "This decision
  is better made by the model reading the request than by a keyword check in
  front of it" ([lifecycle of a code channel](https://docs.slack.dev/ai/slack-code/lifecycle)).
  The reference implementation does the same in kiro-bot's `code_channel_routing`.
  * **The call.** One sandboxed, tool-less model call: no tools, no MCP servers,
    no hooks, no resources. Its input is the request plus a bounded window of
    recent context from the origin conversation (the last few messages, capped
    in count and characters). Its output is a fixed JSON contract,
    `{"route": "stay" | "code_channel", "title"?: string}`, parsed strictly. A
    `title` names the new channel, capped and stripped like any channel name;
    without one Slack names it from the origin message.
  * **Stays in place on doubt.** An ambiguous request (the model's own `stay`),
    output that does not parse or breaks the contract, a model error, and a
    timeout all leave the conversation where it is, and the request is answered
    in place as it is today. The timeout is at most 12 s. This follows Slack's
    "when it's genuinely ambiguous, reply in place and offer to go deeper in a
    code channel".
  * **Never a silent wait.** Before the classifier runs, the origin thread gets
    its immediate acknowledgement, the same working status and reaction a turn
    shows today, so the user sees at once that the request was received.
  * **Latency budget.** The acknowledgement is immediate (sent before the model
    call); the route decision's p95 is at most the timeout. The live p50 and p95
    are recorded from the Phase 2 live test run.
  * **Cost.** One extra model call per directed human message, and only in an
    ordinary conversation while `code_channels_enabled(cfg)` is true. A message
    inside a code channel, a bot copy, and every message with the switch off or
    unavailable skip the classifier entirely.
  * **Idempotent creation.** A deterministic
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
| 1 | Config (`agent_sessions`), `slack/features.py`, the switch registry and toggle-aware manifest, the drift probe, the settings API and UI, the agent-sessions runtime, docs | Every switch combination renders a manifest that the exfil scanner exempts; the API refuses an unregistered or non-boolean switch; the drift probe reports each status against recorded payloads; with the switch off the deep-link payload is byte-identical to before; a turn sets `processing` then `active`; Stop cancels it and leaves the session `active`; `feature_disabled` falls back to plain threads |
| 2 | Code channels: config (`code_channels`, `code_channel_defaults`), their manifest lines, settings and runtime | The API refuses a missing folder, a missing agent, an unavailable model and an off-ladder effort; the classifier routes a coding request to a code channel and stays in place for an ambiguous, unparseable, failed or timed-out (≤12 s) call; the origin thread is acknowledged before the classifier runs; live p50/p95 of the route decision are recorded; one channel opens per request (idempotent on retry), the session files under the default folder on the resolved agent, the diff view and context bar follow each turn, archive posts the summary to the origin |

Each phase is shippable alone. Phase 1 changes nothing for a user who leaves the
switch off.

## Backward compatibility

Every switch defaults off. With all of them off, the rendered manifest and the deep-link
payload are byte-identical to before (pinned by a test), so an existing app needs
no change and `kirocrew manifest` prints what it always printed. With a switch on
but its scope not yet granted, or Slack answering `feature_disabled`, the runtime
behaves exactly as with the switch off and the settings page says why (also
pinned). The
`_slack_manifest_payload_re` helper returns a tuple of patterns instead of one; it
is private to the security package.

## Security considerations

* **No new credential.** The drift probe uses the bot token Kiro Crew already
  holds, only for `auth.test`, `bots.info` and, from Phase 2, a read-only
  `listViews`. The token
  is never logged or returned; the cache keys on its digest.
* **Exfil exemption unchanged in strength.** The own-link carve-out still requires
  the payload to equal a rendered manifest exactly; it now accepts each
  combination's exact rendering, and a mixed rendering (a feature block without its scope) is
  refused (tested).
* **Settings writes stay loopback-only and SEL-audited**, and validation runs
  before any write, so a refused field changes nothing.
* **Code channels (Phase 2) widen the bot's scopes** (`code_channels:manage`,
  `chat:write.customize`, `canvases:write`, `channels:write.invites`,
  `groups:write.invites`), visible on Slack's consent screen and
  listed before the user reinstalls. The scope-ratchet test pins the exact set
  per switch.
* **Phase 2 authorization** is the origin conversation's, never the new channel's
  membership, and every agent tool is pinned to its own channel.
* **The routing classifier holds no capability.** It runs with no tools, MCP
  servers, hooks or resources, so text in the request or its context window can
  at most choose `stay` or `code_channel` and suggest a title. The title is
  capped and sanitised like any channel name, and anything outside the JSON
  contract is treated as `stay`.

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
A hand-pasted full manifest replaces the configuration just as a push would; the
difference is that the person does it in Slack's editor, which shows what
changes, and the panel says so before they do: it warns that replacing the YAML
drops hand-made additions and offers adding only the listed lines instead. The
copied manifest also keeps the app's own slash command and name (the alias is
derived from the saved `slack.command`), so pasting it does not rename an
existing app's `/kirocrew-<alias>` command.
A future revision could revisit this with `apps.manifest.export` → merge →
update and a short-lived token the user pastes per push, never stored.

**One manifest with everything, always.** Rejected: it would widen every install's
consent screen with scopes a user who never turns Slack Code on does not need,
which the scope ratchet exists to prevent.

**Optional regex segments in the exfil pattern** instead of one exact pattern per combination.
Rejected: it would accept combinations no emitter renders.

**Letting the agent open the channel itself with a `create_code_channel` tool**
(the preview branch's approach) instead of a classifier. Rejected by the owner
for this stack in favour of the model classifier (§3, Routing): the tool fires
only after a full agent turn has started in the origin thread, so the decision
is made late and inside a session that has every tool, while the classifier is
a bounded, tool-less call that decides before any turn starts and falls back to
staying in place.

## Open questions

1. Should code channels require agent sessions? Slack's docs say
   `agents.conversations.create` needs "a recognized agent-session app", which
   suggests `agent_view` is a prerequisite in practice. Phase 1 keeps them
   independent, as the contract defines them; Phase 2 should confirm against a
   pilot workspace and, if needed, make the code-channels switch imply the agent
   view.
2. `code_channel_action` (context-bar action items) has no published event
   payload; Phase 2 ships link items only until it does.
