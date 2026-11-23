---
title: Slack Code — agent sessions and code channels for Kiro Crew
status: partial
author: kseam
created: 2026-10-16
last-audited: 2026-11-20
audited-at: 4158830b79
doc-pr:
implementation-prs: [21425, 21410, 22001, 21993, 22370, 38112, 38181, 38240, 38243, 38998]
tracking-issues: [15175]
supersedes: []
superseded-by: []
---

# RFC: Slack Code — agent sessions and code channels for Kiro Crew

- Status: partial. Phase 1 (the switch registry, the manifest built from the
  switches, the update flow, the capability latch and the scope probe, plus the
  Agent sessions switch and its runtime, first opened as
  [#21422](https://github.com/kirodotdev/KiroCrew/pull/21422)) shipped in
  [#21425](https://github.com/kirodotdev/KiroCrew/pull/21425). Phase 2 (code channels: their switch, their
  defaults and their runtime) shipped in
  [#21410](https://github.com/kirodotdev/KiroCrew/pull/21410), with helper-agent
  dispatch and restart resume in
  [#22001](https://github.com/kirodotdev/KiroCrew/pull/22001). Every switch
  defaults off, so no default changes; the 2026-11-16 amendment changes the
  agent a code channel with no settings runs, and turns helper agents on with a
  first enable of Code channels (Backward compatibility).
- Built since (checked at `0c5d8bd5f3`): the 2026-11-12 Stop amendment ([#38243](https://github.com/kirodotdev/KiroCrew/pull/38243),
  `slack/code_channels/stop_helpers.py`) and every item of the 2026-11-16 amendment:
  helper agents on a first enable ([#38112](https://github.com/kirodotdev/KiroCrew/pull/38112)), the default agent and **Always use
  conductor mode** ([#38240](https://github.com/kirodotdev/KiroCrew/pull/38240)), the bar actions and canvas comments ([#22370](https://github.com/kirodotdev/KiroCrew/pull/22370)), the
  tabs ([#38998](https://github.com/kirodotdev/KiroCrew/pull/38998)) and the board's per-row PR and CI and wrap-up outcomes
  ([#38181](https://github.com/kirodotdev/KiroCrew/pull/38181)). Not built: the §3 "Stop", "What happens next" brake, open in
  [#38882](https://github.com/kirodotdev/KiroCrew/pull/38882). The notes below record each amendment as it was written.
- Amended 2026-11-12: Stop in a code channel also stops the channel's helper
  agents (§3, "Stop"). The owner left this call to the Slack Code team lead, who
  decided it; it takes effect when the owner merges this amendment. Nothing is
  built yet: on main `6ded84fc02`, `_stop_bound` in
  `slack/code_channels/routing.py` stops only the channel's own session; when that
  session is idle, `!stop` answers "Nothing running." and Slack's Stop button says
  nothing, whatever its helper agents are doing. The code follows in its own pull
  request, after
  [#21993](https://github.com/kirodotdev/KiroCrew/pull/21993).
- Amended 2026-11-16: records the owner's decisions on what a code channel shows
  and runs, each in the section named. They take effect when the owner merges this
  amendment. Verified at `09e5482863`, which has the work board and no reactions
  (built in [#21993](https://github.com/kirodotdev/KiroCrew/pull/21993)) and
  none of the rest, as each item below says; each part's code follows in its own
  pull request.
  * Helper agents turn on with a first enable of Code channels (§3, "Helper
    agents"). Decided 2026-11-13. On main, turning Code channels on never saves
    `slack.code_channel_dispatch`.
  * A code channel runs the owner's default agent unless told otherwise, and
    **Always use conductor mode** makes every new code channel run the conductor
    mode agent (§3, "Resolution order", "The auto folder", "Always use conductor
    mode"). Decided 2026-11-13; the switch took the conductor mode name on
    2026-11-16. On main the auto folder is created with `default_agent =
    kirocrew-conductor` (`features.AUTO_FOLDER_AGENT`), so a code channel with no
    settings runs the conductor.
  * The context bar has two action items, Create PR and Stop, a Workers item and
    one pull-request slot with its CI in the label, and Plan-canvas comments reach
    the agent (§3, "Context bar", "Plan-canvas comments"; open question 2).
    Decided 2026-10-23. On main the bar has link items only and nothing reads the
    canvas's comments back.
  * Native pull-request tabs, tab deep links, a Workers tab with a per-helper Stop
    and Open in dashboard, and which tab gives way at Slack's five-tab limit (§3,
    "Tabs"). The owner's design, built in
    [#22928](https://github.com/kirodotdev/KiroCrew/pull/22928); decided
    2026-11-16 to ship on main in its own pull request, after #22370. On main a
    code channel has no tab beyond the Plan canvas, the Code tab and the agent's
    own views, and nothing makes room at the limit.
  * A bound code channel shows no reaction ladder and no queued-message hourglass;
    its progress is the channel's session status and the pinned work board (§3,
    "Work board"). Built in #21993 under the owner's
    [decision of 2026-11-13](https://github.com/kirodotdev/KiroCrew/pull/21993#issuecomment-6841005122).
    Also built there: the board files no helper agents and session creation
    files them (§3, "Work board"), as the 2026-10-27 conductor mode decision
    keeps a conductor mode chat's crew out of folders.
  * The work board shows each helper agent's PR and CI, the Code tab adds each
    helper agent's own commits, the wrap-up lists each helper agent's outcome, and
    an idle `!stop` names and links up to five running helper agents (§3, "Work
    board"). These are
    the parts of the owner's work board that #21993 left for a second pull request.
    On main a row shows no PR or CI, the Code tab shows only the channel's own
    diff, the wrap-up lists only the channel's own PR, and an idle `!stop` gives a
    count of running helper agents and points at the board.
- Amended 2026-11-20: a shared user may open and drive a code channel of their
  own (Non-goals; §3, "Authorization" and "Stop"; §4). The owner decided it, and it takes
  effect when the owner merges this amendment. The decision, the rules and the
  security trade-off are recorded in
  [rfc-slack-shared-access](rfc-slack-shared-access.md), "A shared user's own code
  channel". Not built: on main `4158830b79`, `_route_message` in `slack/events.py`
  skips `code_channel_dispatch` for a shared sender, who is refused in every code
  channel. The
  code is open in [#40181](https://github.com/kirodotdev/KiroCrew/pull/40181).
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

The person flips a switch in Settings → Messaging Channels → Slack, picks a default folder and/or
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
* Multi-user code channels. A code channel inherits the authorization of the
  conversation it was opened from, and membership of the channel admits nobody.
  Amended 2026-11-20: with `slack.shared_access` on, a confirmed shared user may
  open a code channel of their own, which admits the owner and that opener only;
  its settings, Trust this channel, the helper-agent Turn-on, its slash commands
  and Archive stay the owner's, and the owner's own code channels stay the
  owner's alone ([rfc-slack-shared-access](rfc-slack-shared-access.md), "A shared
  user's own code channel").

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
| `code_channels` (Phase 2) | `code_channels: {enabled: true}` | `code_channels:manage`, `chat:write.customize`, `canvases:write`, `channels:write.invites`, `groups:write.invites`, `pins:write` | `agent_session_stopped` (`code_channel_action` removed 2026-11-18: Slack's manifest editor refuses it as a bot event, open question 2) |

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

**UI.** Settings → Messaging Channels → Slack gains a "Slack Code" section: the Agent sessions switch
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

**Helper agents** (amended 2026-11-16). **Let code channels start helper agents**
(`slack.code_channel_dispatch`, the sub-toggle under Code channels) lets a code
channel's session start helper agents. It reads off when unset. A settings save
that newly turns Code channels on also saves it on, and the sub-toggle's text
says so, but only when Code channels are not already saved on and no value for
it is stored. A stored value is kept, so an explicit off survives and an upgrade
alone changes nothing; an off chosen in the enabling save is stored
as an explicit off. A missing, unreadable or malformed value reads off, and when
`config.local.json` sets the key or cannot be read, the save writes nothing for
helper agents (the Code channels part of the save still goes through). The owner
can still turn it off under Code channels. The owner-only, workspace and
approval-mode limits on helper agents do not change. Rejected: turning it on for
existing installs by changing the stored default.

**Always use conductor mode** (amended 2026-11-16).
`slack.code_channel_defaults.conductor_mode`, off when unset, is a switch on the
defaults card above the default agent, shown only where conductor mode is
offered: every build but LTS
([2026-11-14 decision](../decisions/2026-11-14-conductor-mode-graduates-from-feature-previews.md)).
When on, every new code channel runs `agent.crew_mode_agent`
(`kirocrew-conductor` unless set,
[2026-11-15 decision](../decisions/2026-11-15-conductor-mode-runs-on-the-conductor-by-default.md))
over the code-channel default agent and the folder's agent. The channel's
session is a conductor mode root by its agent, but it does not carry the stamp a
dashboard chat that turned conductor mode on carries (`crew_mode_agent`), so its
helper agents stay filed in the channel's folder ("Work board", below). While the
switch is on, the settings page disables the default-agent picker and keeps its
value, and shows a display-only warning while helper agents are off; the switch
never turns helper agents on. A missing or malformed value, the LTS edition, or a
`config.local.json` that cannot be read, reads as off. A session already bound
keeps its agent, and a pick in the kickoff picker or `/kirocrew agent` still
applies within the existing conductor mode lock. The `PUT` accepts only a boolean
or null; when `config.local.json` sets the key or cannot be read, the switch is
disabled and a `PUT` that names it is refused (409).

**Resolution order** for a new code-channel session, implemented once in that
resolver:

| Value | Order |
|---|---|
| agent | with `code_channel_defaults.conductor_mode` on, where conductor mode is offered: `agent.crew_mode_agent`, over every other source when the channel is created (amended 2026-11-16). Otherwise `code_channel_defaults.agent` > the folder's `default_agent` (the auto folder's legacy `kirocrew-conductor` reads as unset) > the agent Slack already uses (`agent.default_agent`) |
| model, effort | `code_channel_defaults` only; unset keeps the agent's own |
| project | the folder's `project_dir` (nearest ancestor that declares one) > the project bound to the thread the channel was opened from > none |
| folder | `code_channel_defaults.folder` (id or unique `Parent/Child` path) > the auto folder: a top-level sidebar folder named `Code channels` (`features.AUTO_FOLDER_NAME`). The session also inherits the folder's steering |

**The auto folder.** With no default folder set, a code channel still gets one,
so a person who touches no setting gets a working channel. The resolver stays
pure: when no top-level `Code channels` folder exists (exactly one matches,
case-insensitively, or none counts) it returns `folder_id=None`,
`folder_path="Code channels"` and `folder_auto_create=True`, and resolves the
agent as if the folder had no `default_agent` (amended 2026-11-16; it was
`kirocrew-conductor` before). The Phase 2 runtime creates that folder the
first time a channel needs it, through the sidebar's own folder-create path
(`create_folder_record`, mkdir -p semantics, SEL-audited like the sidebar),
with no `default_agent`, then files the session into its id. Once it exists
its own settings apply, so a person can change or clear its agent. A top-level
`Code channels` folder whose agent is exactly `kirocrew-conductor` counts as no
folder agent, whether an earlier release gave it that value or the owner set it
later: a folder record keeps no edit history, so the value is the only signal.
That is read-time only and the stored folder is not rewritten; any other value,
or the conductor on any other folder, is the owner's and is kept. Rejected:
clearing the legacy value on disk, which rewrites user data with no undo. The
settings page shows it as the default folder row,
"Code channels (created automatically)", and its "New folder…" row opens the
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
  every message. Amended 2026-11-20: a channel opened from a confirmed shared
  user's request records them as its opener (`opener` on the route and binding)
  and admits the owner and that opener; a channel with no opener, including one
  Slack's own UI opened and one bound before the field existed, is the owner's.
* **Worktree, diff and context bar.** When the project is a git repository the
  session works in its own worktree off the project's default branch. After each
  turn the gateway publishes the unified diff against the base as the channel's
  `diff` view (one per channel, capped at 1 MB, truncated with a note past it) and
  sets the context bar (≤5 items, "Context bar" below) with
  `agents.conversations.setProperties`, resending all items because the bar is
  full replacement.
* **Agent tools.** Pinned to the session's own channel server-side:
  `set_code_channel_view` (html, block_kit, canvas), `publish_plan_canvas` (the
  comment-only plan loop: `setView canvas/comment` → `getCanvas` →
  `setCanvasContent`), `code_channel_read_plan_canvas` (amended 2026-11-18: the
  plan text and open comments read back after a restart or compaction), `set_code_channel_status`, `archive_code_channel`. No tool
  can target another channel or open a second one.
* **Commands.** `agents.conversations.setCommands` registers `/summarize`,
  `/run-tests` and `/create-pr`, delivered over Socket Mode and turned into a
  prompt for the channel's session.
* **Archive.** When the work is done the agent posts a summary and calls
  `agents.conversations.archive(summary_message_ts)`, which shares it back as a
  reply on the origin message.
* **Status.** The channel's session status follows the same lifecycle as an agent
  session (§2), with no `thread_ts`.
* **Context bar** (amended 2026-11-16). Slack keeps the first five items of a
  longer list and only warns, so the gateway budgets the bar itself, in priority
  order, each item at most once:
  1. `workers`: in a conductor-led channel with helper agents, the work board's
     tally in short words (`2 working · 1 needs you`, `3 of 3 done`), linked to
     the pinned board message ("Work board", below);
  2. `repo` (the folder's name when the project is not a git repository);
  3. `branch`;
  4. `stop`: the Stop action, only while the channel's own session runs a turn
     (decided when the bar is sent); it runs the same Stop as Slack's Stop button
     ("Stop", below);
  5. ONE pull-request slot: the channel's own PR with its CI folded into the
     label (`PR #42 is open · tests passing`); else, when the channel can open a
     pull request, has no PR, and has commits on its branch past the Code tab's
     base, the `create-pr` action, which runs `/create-pr`.

  A slot nothing above wanted goes to the agent's own items
  (`code_channel_set_context_bar`), then to Open in dashboard (a plain link, never
  a sign-in token). CI shares the PR's item because five slots cannot hold repo,
  branch, PR, CI and the actions. Each helper agent's PR is on the work board, not
  in an item of its own. Once the board carries each helper agent's PR ("Work
  board"), the channel's own PR keeps the slot while it is open, and after it
  merges or closes while no helper-agent PR is open; otherwise the slot shows a
  single helper-agent PR the same way, or several as one item linked to the board
  (`3 PRs open`, `3 PRs: 2 open · 1 merged`). Create
  PR and Stop are `item_type: "action"` items with no URL; Slack's docs say a
  click delivers a `code_channel_action` event (how it arrives is open question
  2), and a click the gateway receives that way runs an existing path with
  that path's own checks and audit. Any other key is refused, and the agent
  cannot set an action key.
* **Plan-canvas comments** (amended 2026-11-16). Slack sends no event for a
  comment on the Plan canvas, so the gateway reads the comments with
  `agents.conversations.getCanvas` when a turn of the channel's session ends and
  by a poll while the session is idle. New comments reach the session as one
  message per commenter, routed exactly like a message that person typed in the
  channel (so the same authorization applies), redacted like all channel text,
  and framed as feedback to weigh, not instructions. Each comment is delivered
  once (remembered across restarts once delivered), at most 20 per read, each cut
  to 1,000 characters. Slack keeps only the newest 100 threads and 100 replies
  each (`has_more_comments`, `has_more_replies`); a message says so when it
  delivers comments from such a listing (amended 2026-11-18). A
  `setCanvasContent` that changes nothing (`sections_changed_count` 0) is
  reported to the agent as unchanged. `agent_content_hash` is not sent: with the
  Plan tab comment-only no human edits the text for it to detect.
* **Work board** (built in
  [#21993](https://github.com/kirodotdev/KiroCrew/pull/21993); amended
  2026-11-16). In a conductor-led code channel the gateway posts one work board
  message, pins it and edits it in place: a counts line, then one task per helper
  agent with its state, what it is doing now and for how long. A helper agent
  waiting on the owner gets one threaded reply under the board that mentions the
  owner. `docs/system-specs/modules/slack-gateway.md` has the details.
  * **No reactions.** While Code channels are on, a bound code channel adds no
    reaction ladder to messages and no hourglass to a queued message. Its
    progress is the channel's session status (between the conductor's own turns
    it follows the helper agents) and the work board. DMs, threads and ordinary
    channels keep both. The owner's
    [decision of 2026-11-13](https://github.com/kirodotdev/KiroCrew/pull/21993#issuecomment-6841005122).
  * **Filing.** The board files no helper agent: it writes no `folder_id` and
    follows helper agents by lineage, wherever they are filed. Session creation
    files them: `session_create` files each helper agent in the channel's own
    folder at creation, and a channel whose session turned conductor mode on (it
    carries the conductor mode stamp) keeps its helper agents out of folders, as
    conductor mode does everywhere
    ([2026-10-27 decision](../decisions/2026-10-27-crew-mode-is-a-composer-switch-to-the-conductor-template.md)).
    A session born on the conductor through **Always use conductor mode** carries
    no stamp, so its helper agents are filed in the channel's folder.
  * **PR and CI per row.** Each row shows its helper agent's PR (`PR #12`, with
    its state when not open) and, for a live PR, its CI (`CI 4/6 passing · 2
    running`); the counts line adds `· N PRs open`. A PR is looked up only in the
    channel's own repository, so a helper agent working in another repository
    shows no branch, PR or CI. A failed forge read keeps the PR last read for that
    row, or else shows only the number the helper agent reported, unlinked and
    without state or CI; the board still goes out.
  * **Combined Code tab.** With a board, the Code tab shows the channel's own
    diff, then one group per helper agent that has commits of its own: from its
    branch's merge base with the channel's branch to its branch, and only for a
    branch whose tip the helper agent's own tree reaches. Each group passes the
    same screen and redaction as the channel's diff, and all share one size
    budget; a helper agent left out adds the truncation note.
  * **Wrap-up outcomes.** The wrap-up adds one line per helper agent (`• Fix
    login: PR #12 merged`, `rejected, PR #9 closed`, `lost, no PR`), at most
    1,800 characters, then `+N more`. The lines are left out when the board's
    scope refuses the channel at the post or at any later edit of the message.
  * **Idle `!stop`.** Until the amended Stop ("Stop", below) is built, a `!stop`
    while the channel's session is idle but helper agents run says so and names
    up to five of them by their board title, each linked to its chat (no sign-in
    token); the rest fold into "and N more", pointed at the lead session.
* **Tabs** (amended 2026-11-16). Slack holds at most five views per code channel.
  Besides the Plan canvas, the Code tab and the agent's own views:
  * **Pull-request tabs.** A native `pull_request` tab (`pr-<n>`, "PR #<n>") for
    each of the channel's two newest open PRs: its own, and its helper agents'
    once the board carries each row's PR. A PR tab is removed, by the view id
    recorded when it opened, only on evidence: its PR seen merged or closed, or
    two newer open PRs seen. A failed read removes nothing.
  * **Workers tab.** A `workers` block_kit tab drawn from the work board's own
    snapshot and redaction: for each open helper agent (up to 14, then `+N
    more`) its title, status, PR and CI, **Open in dashboard** and, while it
    works, a **Stop** behind a confirm dialog; settled helper agents fold into
    one line each. It is written when what it shows changes, or every 5 minutes
    for elapsed times, and at most every 15 s.
  * **The Workers tab's Stop** stops that one helper agent's running turn and
    nothing else; it does not keep it stopped (the hold in "Stop" belongs to the
    channel's Stop). It is authorized as the context bar's Stop, accepted only
    from this channel's recorded Workers tab, and its target is resolved by the
    gateway from an item id on this channel's board. Audited as
    `slack.code_channel.stop`.
  * **Deep links.** The board's header links the Plan, Workers and Code tabs, each
    board task's PR links its PR tab, and the wrap-up links the Plan tab, the Code
    tab and the open PR tabs (the Plan link amended 2026-11-18). Context-bar items take only a URL, so the bar keeps GitHub
    links.
  * **The limit.** Three tiers: (1) the Plan canvas, the Code tab and the Workers
    tab, never removed to make room; (2) the PR tabs, at most two; (3) the
    agent's own html and block_kit tabs. When Slack refuses a new tab with
    `too_many_views`, the gateway removes ONE view of its own and retries the
    write once: the least recently written tier-3 view first, for any write; a PR
    tab (the oldest PR first) only to fit a tier-1 tab. Only a view the gateway
    recorded opening is ever removed, never another agent's. With nothing of its
    own to remove, the write fails with `too_many_views` and the caller says so.
    So with the Plan canvas, the Code tab, the Workers tab and two PR tabs open,
    a new agent view is refused.
  * **Reserved keys.** `code_channel_set_view` refuses the keys `workers`,
    `diff` and `pr-<n>`, as it already refuses `plan`.
* **Restart resume.** A code channel's binding survives a gateway restart, so its
  next message reaches the same session; a turn that was running does not. Once
  per gateway start, after Slack connects, the gateway continues each interrupted
  turn of a code channel's own session and of that session's own workers, and
  nothing else. This is the owner's ask ("Yes, add the Resume button after
  restarts, Auto-resume without asking").
  * **Whose work.** Only a channel admitted right now (bound, not archived,
    wrapped up, stopped or fenced, in an admitted workspace, admitted by the
    per-channel policy ceiling `allowed_channel_ids`, let in by channels
    governance), its session by strict identity (binding, Slack link and slot
    agree), and that session's own workers: those the store recorded as its
    workers at birth or the crew log lists under it, whose live creator still
    names it and which are linked to no channel. "Not archived" is asked of
    Slack itself (`conversations.info`), since an archive made while the gateway
    was offline sent no event; a confirmed archive is recorded as the event
    would have, and an answer that cannot be had runs nothing by itself (the
    work is held for the Resume button, which asks Slack again). Identity and
    admission are re-checked under the slot lock right before each continuation.
  * **At most once per turn.** The interrupted turn is recorded in the
    write-protected code-channel store before it is continued, and a recorded
    turn is never continued automatically again, so a turn that takes the
    gateway down cannot loop. A turn whose identity cannot be proven, or whose
    record cannot be saved, is not continued automatically.
  * **Which turn.** A turn's identity comes only from what the dispatcher
    produced, never from how a transcript row looks: in the running process the
    session's turn generation, and across a restart the turn marker written at
    admission, which names the row the dispatcher started the turn from. A steer,
    a message queued behind a running turn, a replayed or restored row, or a row
    of an unknown kind opens no turn. No identity is read from a transcript row
    or the metadata line, both of which the session's own agent can write: at
    every code-channel turn's admission the gateway records the opener it
    started the turn from (its own copy) in the store, and removes it when the
    turn ends; each resume made here records the recovery row it dispatched.
    The restart names the cut turn from those records only, and a session with
    no recorded opener, or whose cut turn is that resume's own run, is held for
    the owner's Resume. A turn that had already failed before the
    restart is not resumed automatically, because the restart did not interrupt
    it (the dashboard's Resume still offers it).
  * **One notice.** The channel gets one top-level notice: "I resumed where I
    left off" with an owner-only Stop for what was resumed, or the reason the
    work is paused with an owner-only Resume. Stop reaches only the sessions that
    notice resumed, at the dispatch that resumed them: a turn dispatched after it
    is not stopped. Resume re-runs every identity and
    admission check, and is given back when nothing could run. In a Trust channel
    the notice says the resumed work acts without asking first.
  * **Switch.** Restart resume is part of code channels and runs only while they
    are on; it has no switch of its own.
* **Stop** (amended 2026-11-12). Slack's Stop and `!stop` in a code channel stop
  the channel's own session and its helper agents. Stop is the person's brake: a
  Stop that leaves agents editing code is wrong.
  * **What it stops.** The running turn of the channel's session, the running
    turn of every helper agent that the gateway's own records tie to that session
    (the code-channel store records each helper agent the session starts, when it
    starts it), and the sub-agents those turns started. It never undoes anything an
    agent wrote: files, commits, pushes, messages and work-ledger entries stay as
    they are.
  * **Who may press it.** Unchanged: only Kiro Crew's owner, in a channel whose
    origin conversation still admits the bot, from an admitted workspace. Anyone
    else is refused and audited, and nothing is stopped. Amended 2026-11-20: in a
    code channel a shared user opened, its opener may also use Slack's Stop, the
    context bar's Stop (which runs the same check) and `!stop`, under the same
    channel and workspace checks. The context bar's Create PR, the Workers tab's
    per-helper Stop and the restart notice's Stop and Resume stay the owner's.
  * **What the channel says.** How many it stopped (none, when nothing was
    running), and that nothing starts again until someone sends a message here.
  * **What happens next.** Nothing starts again on its own. The channel's session
    starts no turn by itself (no patrol or timer wake, no wake from a helper
    agent's report, no scheduled job) until a person sends it a message, in Slack
    or in the dashboard. A stopped helper agent stays stopped, and work queued for
    it before the Stop waits, until a person sends it a message or the channel's
    session, working on a person's message, starts it again; a message to one
    helper agent releases only that one. Whether a message is a person's is read
    from the gateway's own record of who sent it, never from its text. Restart
    resume does not resume a turn a Stop ended, including one still ending when
    the gateway restarted.

### 4. Approval modes in code channels

A code channel's session has an approval mode like any session: Normal (look-ups
run on their own, everything else asks), Reads (also searches of memory, past
chats, skills and the knowledge library) or Trust (every tool runs without asking).

* **Default.** Settings → Messaging Channels → Slack → Code channels sets the mode
  a new code channel's session starts in; unset is Normal.
* **Who changes it.** Only Kiro Crew's owner (or a user the owner explicitly
  authorizes) can raise or lower a code channel's mode, checked by the identity of
  the person clicking or sending, never by being in the channel, a wider allow-list
  (shared users included, the opener of a shared user's code channel too) or a
  value carried in the request. That holds on every
  path: the kickoff's Approval mode select (Reads and Trust ask first, privately),
  its confirmation, `/kirocrew approval normal|reads|trust`, **Trust this channel**
  and the dashboard. Anyone else is refused with a private notice and an audit row;
  every change is audited and posts a note.
* **Trust this channel.** A code channel's own session offers an owner-only
  **Trust this channel** on its approval cards, behind a confirmation, which puts the
  channel on Trust and approves that call. This is an intentional exception to the
  group-channel rule in `docs/system-specs/modules/slack-gateway.md` (approval cards
  in group channels offer Approve / Reject only, no Trust, to limit blast radius),
  which every other group channel keeps: a code channel is a single task with a
  single owner, not a shared room. The owner's decision is recorded on the PR that
  adds it (#21991). It stays owner-only in a code channel a shared user opened
  (amended 2026-11-20).
* **Workers.** Approval prompts of workers the channel's session started are
  relayed into the channel, answered by the same owner-only buttons; a mode change
  reaches them through the creator-mode cascade, and lowering always reaches them.
  Amended 2026-11-20: in a code channel a shared user opened, its opener may also
  answer Approve / Reject on these prompts and on the channel session's own, never
  Trust.

## Migration plan

| Phase | Scope | Exit criteria |
|---|---|---|
| 1 | Config (`agent_sessions`), `slack/features.py`, the switch registry and toggle-aware manifest, the drift probe, the settings API and UI, the agent-sessions runtime, docs | Every switch combination renders a manifest that the exfil scanner exempts; the API refuses an unregistered or non-boolean switch; the drift probe reports each status against recorded payloads; with the switch off the deep-link payload is byte-identical to before; a turn sets `processing` then `active`; Stop cancels it and leaves the session `active`; `feature_disabled` falls back to plain threads |
| 2 | Code channels: config (`code_channels`, `code_channel_defaults`), their manifest lines, settings and runtime | The API refuses a missing folder, a missing agent, an unavailable model and an off-ladder effort; the classifier routes a coding request to a code channel and stays in place for an ambiguous, unparseable, failed or timed-out (≤12 s) call; the origin thread is acknowledged before the classifier runs; live p50/p95 of the route decision are recorded; one channel opens per request (idempotent on retry), the session files under the default folder on the resolved agent, the diff view and context bar follow each turn, archive posts the summary to the origin |

Each phase is shippable alone. Phase 1 changes nothing for a user who leaves the
switch off.

## Backward compatibility

Every switch defaults off. The one setting that follows a switch is helper agents
(`slack.code_channel_dispatch`, amended 2026-11-16): a first enable of Code channels
saves it on, a stored value is kept, and an unset one reads off, so an existing
install changes nothing on upgrade. One default changes for a code channel with no
settings (amended 2026-11-16): it runs the owner's default agent, not the
conductor, including one filed in an auto folder that still holds the legacy
`kirocrew-conductor`; a session already bound keeps its agent. The manifest
subscribes no event for the context bar's action items (amended 2026-11-18):
Slack's App Manifest editor refuses `code_channel_action` as a bot event (open
question 2). With every switch off, the rendered manifest and the deep-link
payload are byte-identical to before (pinned by a test), so an existing app needs
no change and `kirocrew manifest` prints what it always printed. With a switch on
but its scope not yet granted, or Slack answering `feature_disabled`, the runtime
behaves exactly as with the switch off and the settings page says why (also
pinned). The
`_slack_manifest_payload_re` helper returns a tuple of patterns instead of one; it
is private to the security package. A shared user's own code channel (amended
2026-11-20) adds no switch: a route or binding stored before its `opener` field
existed reads as the owner's, so a channel already bound stays the owner's.

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
  `groups:write.invites`, `pins:write`), visible on Slack's consent screen and
  listed before the user reinstalls. The scope-ratchet test pins the exact set
  per switch.
* **Phase 2 authorization** is the origin conversation's, never the new channel's
  membership, and every agent tool is pinned to its own channel. With shared
  access on (amended 2026-11-20), a shared user's own code channel admits its
  opener, re-checked at every act; the trade-off is recorded in
  [rfc-slack-shared-access](rfc-slack-shared-access.md), "A shared user's own code
  channel".
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
   payload. Slack's [code channel properties](https://docs.slack.dev/ai/slack-code/code-channel-properties/)
   page documents only the trigger: an item with `item_type: "action"` (no
   `url`) delivers a `code_channel_action` event to the app when clicked.
   Decided 2026-10-23 (amended 2026-11-16): Phase 2 ships two action items,
   Create PR and Stop, on that documented trigger ("Context bar", §3), and reads
   the channel, the person, the item key and the workspace from the spellings
   Slack uses on its other events. A click whose person, channel or key cannot
   be read is refused and SEL-audited, and two fields naming different people
   make the click nobody's. Only the field names of the first click are logged,
   never values, so the real shape can be pinned down from a live workspace.
   This question closes once Slack publishes the body; then only the documented
   fields are read.
   Amended 2026-11-18: the manifest no longer subscribes `code_channel_action`.
   Pasted into Slack's App Manifest editor on 2026-11-18, the manifest with Code
   channels on was refused: "Scopes could not be verified for
   `code_channel_action` as it is not a valid bot user event type". Slack
   documents no subscribable event for the click: its events reference lists
   none, and its interaction payloads reference lists no code-channel payload
   type. Whether a click on Create PR or Stop in the bar reaches the app is not
   yet confirmed in a live workspace. The click handling stays as it is, so a
   `code_channel_action` event Slack sends with no subscription still runs.
   `/create-pr`, `!stop` and Slack's own Stop button do not depend on it.
