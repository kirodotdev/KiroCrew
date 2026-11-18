---
title: Slack owner claim — the owner sends the bot a DM and confirms it on the local Settings page
status: draft
author: zejiangg (drafted with an agent session)
created: 2026-11-18
last-audited: 2026-11-18
audited-at: 539737a2a
doc-pr: null
implementation-prs: []
tracking-issues: [38899]
supersedes: []
superseded-by: []
---

# RFC: Slack owner claim

Status: draft. Nothing is built and this document changes no code. It answers [#38899](https://github.com/kirodotdev/KiroCrew/issues/38899). Acceptance is requested from a maintainer, who records it on this Status line. Every code reference below was read at `539737a2a`.

## Summary

Today the last step of Slack setup is to copy your own Slack member ID out of the Slack client and paste it into Settings → Messaging channels → Slack → Owner Slack member ID. Until it is set, the gateway refuses to connect to Slack at all.

This RFC proposes an **owner claim**: on the local Settings page the person clicks "Claim in Slack", which opens a 10-minute claim window and only then connects Socket Mode. The person sends the bot any direct message. The local Settings page lists that sender as a candidate, with the message text and time. The person clicks Confirm on the local page; Crew writes `KIROCREW_OWNER_ID`, reconnects, and the bot sends a welcome DM. There is no pairing code: the security rests on the confirm click, which only a direct-local request can make.

The change moves a security boundary — the "no owner, no Slack" invariant in `docs/system-specs/modules/slack-gateway.md` (the **Deny-by-default** bullet, line 2098) — so per `CONTRIBUTING.md` ("moves a security, data or trust boundary") it goes RFC-first, as its own pull request.

## Motivation

### Current state

- With no owner, startup refuses Slack: `init_socket_mode` sets `_slack_connect_error = "KIROCREW_OWNER_ID is not set"`, `_slack_enabled = False`, `slack = None` and returns (`init_socket_mode` in `src/kiro_crew/slack/events.py`).
- Reconnect with no owner answers `owner_id_missing` (`_bring_up` in `src/kiro_crew/slack/reconnect.py`, `OWNER_ID_MISSING`).
- Reconnect already accepts a **first** owner without a restart, because "a gateway with no owner yet has admitted nothing" (`_reconnect` in `src/kiro_crew/slack/reconnect.py`). It refuses a **changed** owner; that needs a restart.
- Save answers `reconnect_required: true` for a first owner and `restart_required` for a changed one (`_slack_config_save_locked` in `src/kiro_crew/dashboard/messaging_api/slack_settings.py`).
- Every Slack settings write, and Reconnect, is gated on `is_direct_local_request` (`is_direct_local_request` in `src/kiro_crew/dashboard/origin.py`, checked by `_slack_config_save_locked` and `api_slack_reconnect`). Remote read-only viewers get `read_only: true` (`api_slack_config_get`).
- `is_owner` returns False for an empty owner and cross-matches the `W`/`U` prefix (`is_owner` in `src/kiro_crew/slack/handler_runtime/access.py`).

### Problem

1. The member ID is the one value a new user has to dig out of the Slack client (avatar → Profile → ⋮ → Copy member ID). It is the third manual paste after the bot token (`xoxb-`) and the app token (`xapp-`).
2. Because the gateway refuses to connect without it, a person who has just saved two valid tokens sees "Needs setup" and no connection, and often restarts the gateway for nothing.
3. The value cannot be found any other way: an installed bot token does not say who installed the app, and the local OAuth path that would identify the user is not open to a self-built app (bot scopes are not allowed on desktop PKCE redirects).

## Goals

- Replace the member-ID paste with "send the bot a DM, then confirm on this machine".
- Keep the window during which an ownerless gateway is connected to Slack as short as possible: it opens only on a local click and closes on expiry, cancel, confirm or any credential change.
- An ownerless gateway still admits nothing and sends nothing to anyone, window open or closed.
- With the window closed, behaviour is exactly today's.

## Non-goals

- No pairing code, alone or in addition to the confirm click.
- No change of owner. Replacing an existing owner stays "edit the value and restart" (`_reconnect` answers `OWNER_CHANGE_NEEDS_RESTART`).
- No claim from the CLI. `kirocrew setup --slack` keeps asking for the member ID.
- No multi-user access, no shared-crew relay.
- No setup wizard UI, slash-command or manifest changes; those are separate pull requests.
- No change to the Slack app manifest scopes. The claim uses `users:read`, which the manifest already requests (`src/kiro_crew/slack-manifest.yaml`, bot scopes).

## Design

### 1. Lifecycle and connection

| State | Socket Mode | Entered by | Left by |
|---|---|---|---|
| **Ownerless, closed** (today) | Not connected; connect error `KIROCREW_OWNER_ID is not set` | Startup or Reconnect with no owner; window close | Local "Claim in Slack" |
| **Claim window open** | Connected; only the claim hook acts on events | Local "Claim in Slack" (§2) | Expiry (10 min), Cancel, Confirm, any Slack credential write (§7) |
| **Owned** (today) | Connected as today | Confirm (§6), or the manual field + Reconnect as today | — |

- Saving tokens only verifies them (`auth.test` for `xoxb-`, `apps.connections.open` for `xapp-`, as today). It does **not** connect an ownerless gateway. Reconnect with no owner and no open window still answers `owner_id_missing`.
- Only the local "Claim in Slack" click connects an ownerless gateway, and only for the life of the window.
- When the window closes without a confirm, the gateway closes the Socket Mode connection and returns to the ownerless-closed state: `_slack_enabled = False`, `slack = None`, connect error `KIROCREW_OWNER_ID is not set`. It never stays "connected but dropping everything".
- Window state and candidates live in process memory only. A restart drops them; nothing about a claim is persisted except the confirmed owner ID.

### 2. Opening the window

`POST /api/slack/owner-claim/start`:

- Direct-local only (`is_direct_local_request`), the same gate as Save and Reconnect. Anything else is 403.
- 409 when **either** the gateway's owner (`orch._owner_id`) **or** the saved owner (`KIROCREW_OWNER_ID` in `.env`) is non-empty. The two differ after a manual Save before Reconnect (`_slack_config_save_locked`), so checking one is not enough.
- 409 when a window is already open.
- Refused when both tokens are not saved, or when `auth.test` fails.
- **Refused for an org-wide Enterprise Grid install**: when the install's identity is an `E…` enterprise ID rather than a `T…` team ID (`_team_of` in `src/kiro_crew/slack/reconnect.py`). The manifest sets `org_deploy_enabled: false` (`src/kiro_crew/slack-manifest.yaml`), so this is rare; it fails closed.
- On success, pins the workspace team from `auth.test` for the window, connects Socket Mode in claim mode and answers with the expiry time.

### 3. What the claim hook accepts

The hook sits in `_on_event` in `events.py`, after the Enterprise Grid envelope-team override and its `missing_team_id` rejection, and immediately before `await _route_message(...)`. At that point the existing `bot_id` / `subtype` handling and the envelope `team_id` override have run. In claim mode the hook calls `owner_claim.offer(...)` and returns; the event never reaches `_route_message`.

A message becomes a candidate only when **all** hold; anything else is dropped and counted as "ignored":

| Check | Rule |
|---|---|
| Channel | `channel_type == "im"` |
| Sender kind | no `bot_id`, no `subtype` (trusted-bot admission does not apply in claim mode) |
| Workspace | envelope `team_id` equals the team pinned at window open |
| Org gate | passes `check_message_origin` (`src/kiro_crew/slack/enterprise.py`) |
| Team presence (fail closed) | at least one of the event's `user_team` and `users.info`'s `team_id` (on Grid, `enterprise_user.enterprise_id` compared with the install's enterprise) is present, and every one present matches. If both are missing, reject. |
| User record | `users.info` succeeds and the user is not `deleted`, `is_bot`, `is_app_user`, `is_restricted`, `is_ultra_restricted` or `is_stranger` |

`users.info` is called at most once per sender per window; the answer is cached for the window.

### 4. Candidates

- Counted per sender, with no global failure cap: one sender's latest message replaces their earlier one, so a flood from one person adds one row, not many.
- At most 3 distinct senders are kept; a fourth drops the oldest.
- Each row shows avatar, display name, `@handle`, workspace name, member ID, **the message text and the time it arrived**. The person recognises what they just typed, which a look-alike name and avatar cannot copy.
- With more than one candidate, the page shows a prominent warning: more than one person sent a message; check the text before confirming.
- The page also shows the count of ignored messages, without their content.

### 5. Silence while ownerless

- The bot replies to nobody during the window: no reply to the candidate, no ephemeral "not authorized" to anyone, no Home tab publish.
- Other inbound surfaces (slash commands, interactions, Home tab, channel joins) are acknowledged at the Socket Mode envelope level as Slack requires and otherwise dropped silently.
- Every outbound path that reads `_owner_id` stays silent while it is empty. Today's known readers include `_task_notify` in `gateway_runtime/service_boot.py` (owner DM, already guarded), `gateway_runtime/delivery.py`, `gateway_runtime/result_egress.py` and `gateway_runtime/turn_approvals.py`; the implementation pins each with a test (§Tests).

So the premise behind Reconnect's first-owner rule — an ownerless gateway has admitted nothing (the comment in `_reconnect`) — still holds after a claim window.

### 6. Confirm

`POST /api/slack/owner-claim/confirm` with the candidate's member ID:

1. Direct-local only; 403 otherwise. The ID must be one of the current candidates; otherwise 404.
2. **Compare, then write.** Immediately before writing, re-read the saved owner from `.env` and the gateway's owner. If either is non-empty, answer 409 and write nothing. The check and the write run under `_get_config_lock()`, the lock `api_slack_config_save` already holds for every Save, so a concurrent manual Save cannot slip in between.
3. Write `KIROCREW_OWNER_ID` through the existing credential writer (`_write_env_or_roll_back`, as Save does). The value is `event.user` **exactly as Slack sent it**: no `W`→`U` or `U`→`W` rewrite (`is_owner` cross-matches prefixes, so a rewrite would hide a mismatch rather than fix it).
4. Close the window, then run the existing Reconnect, which accepts a first owner.
5. Post one welcome DM to the new owner. That puts the app in the owner's DM list on desktop and mobile.

`POST /api/slack/owner-claim/cancel` (direct-local only) closes the window. `GET /api/slack/owner-claim` (direct-local only; 403 otherwise) returns window state and candidates.

### 7. Closing the window

The window closes, and Socket Mode disconnects unless the close is a confirm, on any of:

- expiry after 10 minutes;
- Cancel;
- Confirm;
- **any write of a Slack credential** (bot token, app token or owner ID) through Settings or the CLI's writer. A new token may point at another workspace, and the window is pinned to the old team.

### 8. Visibility and logging

- Candidates appear only in the direct-local `GET` answer. They are never in `GET /api/slack/config`, any other public status, any page push, or any audit body.
- The audit log records only `claim_window_opened`, `candidate_seen`, `claim_confirmed`, `claim_closed(reason)` and a hash of the member ID.
- Ordinary logs never contain a candidate's display name, handle or message text.

### 9. After the claim

- The Slack panel shows "Owner: @handle (workspace)" with a Clear button. The manual Owner Slack member ID field stays as a fallback.
- **Clear** only removes `KIROCREW_OWNER_ID` from `.env` and tells the person to restart the gateway. It does not change the running gateway's owner: Reconnect refuses an owner change (`_reconnect` answers `OWNER_CHANGE_NEEDS_RESTART`). Until the restart, "Claim in Slack" still answers 409 because the gateway owner is set (§2).

### 10. CLI

`kirocrew setup --slack` does not claim. It keeps asking for the member ID, accepts an empty answer, and then says to use "Claim in Slack" on the Settings page.

### 11. Code layout

| File | Change |
|---|---|
| `src/kiro_crew/slack/owner_claim.py` (new) | Window, pinned team, filters (§3), candidates (§4), confirm (§6), close (§7). All claim logic lives here. |
| `src/kiro_crew/slack/events.py` | Hooks only: the ownerless early return in `init_socket_mode` connects in claim mode when a window is open, and one call at the point named in §3. `events.py` is 1958 lines against the 2000-line cap (`scripts/check_file_line_cap.py`), so no logic goes here. |
| `src/kiro_crew/slack/reconnect.py` | Claim-mode connect and disconnect; `owner_id_missing` unchanged when no window is open. |
| `src/kiro_crew/dashboard/messaging_api/slack_settings.py` | The four endpoints; confirm under `_get_config_lock()`; window close on credential write. |
| Settings UI and every locale file | Claim panel. |
| `src/kiro_crew/cli_setup.py` | The §10 hint. |
| `docs/system-specs/modules/slack-gateway.md` | Rewrite the Deny-by-default bullet: Slack is disabled at startup with no owner, and stays disconnected unless a direct-local claim window is open; while it is open the gateway is connected but admits nothing and sends nothing. |

The spec text changes in the implementation pull request, not here, because the spec describes the code on main.

## Migration plan

### Phase 1: owner claim (one pull request)

Entry: this RFC is `accepted`.

Exit criteria:

- With no window open, an ownerless gateway is not connected and Reconnect answers `owner_id_missing`, as today.
- Every test in §Tests passes.
- `events.py` stays at or under 2000 lines.
- `slack-gateway.md`'s Deny-by-default bullet matches the shipped behaviour.
- The RFC's status, `implementation-prs`, `last-audited` and `audited-at` are updated in that pull request.

The setup wizard, which puts this panel on its own screen, is a separate pull request and does not block this one.

## Tests

| Area | Case |
|---|---|
| Gate | `start`, `cancel`, `confirm`, `GET` each answer 403 to a request that is not direct-local (forwarded headers, SSM-proxied) |
| Visibility | Candidates never appear in `GET /api/slack/config` or any page push; audit and log lines carry no display name or text |
| Open | 409 when the gateway owner is set; 409 when only the saved owner is set (manual Save, no Reconnect); 409 when a window is open; refused for an `E…` org install; refused without both tokens |
| Connection | Saving tokens with no owner does not connect; `start` connects; expiry disconnects and restores `KIROCREW_OWNER_ID is not set`; cancel disconnects |
| Filters | one rejection test each: not `im`; `bot_id`; `subtype`; other envelope team; `user_team` mismatch; both team fields missing; `check_message_origin` refuses; `users.info` fails; `deleted`, `is_bot`, `is_app_user`, `is_restricted`, `is_ultra_restricted`, `is_stranger` |
| Candidates | one sender flooding keeps one row and does not push out another sender; a fourth sender drops the oldest; rows carry text and time; more than one candidate sets the warning flag; `users.info` called once per sender |
| Silence | a candidate message never reaches `_route_message`; the bot posts nothing during the window; slash, interaction and Home tab events post nothing; `delivery.py`, `result_egress.py`, `turn_approvals.py` and `service_boot.py` outbound paths send nothing with an empty owner |
| Confirm | an ID not in the candidates is 404; compare-then-write answers 409 when a manual Save set the owner after `start`; the written value equals `event.user` byte for byte, including a `W…` ID; Reconnect then succeeds and `is_owner` is true; one welcome DM is posted |
| Close | a bot-token write, an app-token write and an owner write each close an open window |
| Clear | Clear writes only `.env`, answers restart-required, and `start` still answers 409 until restart |
| UI | a read-only viewer sees no Claim, Confirm or Cancel controls |

## Backward compatibility

Compatible. With no window open, every path behaves as today. The only value written is the existing `KIROCREW_OWNER_ID` key in `.env`, in its existing format. No config key, file format or public API is removed or renamed. The manual member-ID field and the CLI prompt stay.

## Rollback

- Code: revert the implementation pull request. Nothing is persisted except `KIROCREW_OWNER_ID`, which a manual entry would have written the same way, so a revert needs no migration.
- A wrong owner was confirmed: Clear (or delete `KIROCREW_OWNER_ID` from `~/.kiro/crew/.env`), then restart the gateway, then claim again or enter the ID by hand.
- A window misbehaves: restart the gateway. Window state is in memory only, so a restart returns to the ownerless-closed state.

## Security considerations

### Threat model

Assets: who becomes the owner, and what an ownerless gateway admits or sends. Actors: a remote read-only dashboard viewer (shared crew or relay visitor), other members of the same Slack workspace, Slack Connect or guest users, other bots and apps, and a person who holds a leaked bot token. A local user with direct dashboard access can already write the owner ID today; this RFC gives them nothing new.

| # | Threat | Mitigation |
|---|---|---|
| T1 | A remote read-only viewer claims ownership | Every claim endpoint is direct-local only (§2, §6); candidates are only in the direct-local `GET` (§8). There is no code to read or replay. |
| T2 | A workspace member sends a DM during the window, copying the owner's name and avatar | The owner must click Confirm locally; rows show the message text and time; more than one candidate triggers a warning (§4). |
| T3 | A Slack Connect, guest or foreign-team user is offered | Envelope team pin, `user_team` / `users.info` team match with fail-closed on missing fields, guest and stranger flags, org gate (§3). |
| T4 | A bot or app is offered | `bot_id`, `subtype`, `is_bot`, `is_app_user` rejected; trusted-bot admission off in claim mode (§3). |
| T5 | Flooding pushes the owner out or burns a shared limit | Per-sender counting, 3 distinct senders, no global failure cap, one `users.info` per sender (§3, §4). |
| T6 | A long-lived ownerless connection | Connects only on a local click, 10-minute expiry, disconnect on close (§1, §7). |
| T7 | The token changes to another workspace mid-window | Any Slack credential write closes the window (§7). |
| T8 | A race with a manual Save sets two owners | 409 on either owner at open; compare-then-write under a lock shared with Save (§2, §6). |
| T9 | An ownerless gateway leaks output or runs a turn | No routing, no replies, all outbound paths silent, each pinned by a test (§5). |
| T10 | Candidate data leaks through logs | Hash-only audit, no names or text in logs (§8). |
| T11 | The wrong ID form is stored | `event.user` written verbatim (§6). |
| T12 | An org-wide install widens who can DM | Refused (§2). |
| T13 | A leaked bot token is used to claim | Not enough: claiming needs the local click, and a window pinned to that token's team. |

### Boundary change, stated plainly

Today: no owner means no Slack connection. After this RFC: no owner means no Slack connection, **except** for up to 10 minutes after a direct-local click, during which the gateway is connected, admits nothing into any session, and sends nothing.

## Alternatives considered

- **Pairing code** (type a 6-digit code shown locally into the Slack DM). Rejected: the code is one more thing that can leak to a remote viewer, a global failure cap lets anyone void it, and it adds typing. The local confirm alone covers what the code was for.
- **Pairing code plus local confirm.** Rejected: the confirm already decides; the code adds a step and a leak surface without a new guarantee.
- **Connect as soon as tokens are saved, then wait for a claim.** Rejected: it leaves an ownerless gateway connected for as long as the person takes, instead of a bounded window.
- **Identify the user through OAuth.** Not possible for a self-built app on a local machine: bot scopes are not allowed on desktop PKCE redirects, and a non-PKCE redirect must be HTTPS.
- **Keep only the manual field.** Leaves the hardest setup step in place.

## Open questions

None blocking. Two values are proposed defaults a reviewer may change without reshaping the design: the 10-minute window, and the 3-candidate limit.
