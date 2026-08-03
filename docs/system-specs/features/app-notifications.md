# App Notification Producers (Push Endpoint) — Design Document

Last Updated: 2026-07-22 (initial: RFC local notification bus, Phase 2)

## Overview

Phase 2 of the local notification bus (`docs/request-for-change/rfc-local-notification-bus.md`): installed apps become first-class notification producers. An app declares its channels in `app.json`, then pushes notifications through `POST /api/notifications/push` authenticated with its app token. The gateway resolves the producer identity server-side from the verified token (never from the request body), enforces manifest-declared channels, and applies a per-app rate limit. Delivered notes flow through the Phase 1 `NotificationBus` sink (`DashboardState._deliver_note`), which redacts, broadcasts to SSE clients, and persists.

## API

### POST /api/notifications/push

App-token-only endpoint (dashboard-user tokens are rejected with 403: they have no app identity, and the endpoint is for producers). Registered in `server.py` `_register_mcp_routes`, so it serves both the dashboard app and the headless gateway.

Request body (JSON, max 64 KB — enforced on Content-Length AND incrementally on the raw stream so chunked transfer-encoding cannot bypass it):

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `channel` | string | yes | A channel id declared in the app's manifest (bare id, not the full dotted name) |
| `title` | string | yes | Validated by `NotificationPayload.validate()` (length caps) |
| `body` | string | yes | Length-capped |
| `priority` | string | no | `critical` / `default` / `passive`; defaults to the channel's `defaultPriority` |
| `group_key`, `url`, `icon`, `ttl`, `actions`, `meta` | — | no | Passed through payload validation; `url` must be a dashboard-internal path. Each `actions[]` entry requires `id` + `label`; an entry MAY carry a `url`, validated with the same dashboard-internal-path rule at persistence time (the trust root — no stored action can hold an external link) |

The note's `source` is always `app:<name>` resolved from the verified token; `channel` becomes `<app-name>.<channel-id>`.

Responses: `200` with `{"ok": true, "note": {...}}` where `note` is the enriched note (resolved `source`, full `channel`, effective `priority`, server timestamp `ts`); `400` (validation failure — including a corrupt on-disk manifest `defaultPriority`), `403` (no app token / app disabled or unknown / channel not declared), `413` (oversized body), `429` (rate limited), `500` (delivery or persistence failure, SEL-audited). A `200` is a durability guarantee: the handler awaits the persist job before acknowledging, so an accepted push cannot be silently lost to a disk failure. Legacy system producers remain best-effort fire-and-forget.

### Request pipeline order

`validate -> register-channel-once -> rate-limit token -> bus.push`, chosen so that:

- Invalid payloads and corrupt-manifest 400s never consume a rate-limit token (the budget caps DELIVERED notifications).
- Channel registration happens at most once per channel (re-registering every push would stomp future runtime priority overrides).
- A `NotificationValidationError` escaping `bus.push` (unreachable today; safety net) refunds the token. A sink failure (e.g. disk full) deliberately does NOT refund: delivery may be partial, and throttling a producer while the gateway is failing is protective.

## Manifest schema: `notifications.channels`

```json
{
  "notifications": {
    "channels": [
      { "id": "sync-status", "name": "Sync status", "defaultPriority": "passive" }
    ]
  }
}
```

Validation (`apps/manifest.py`): max 8 channels per app, kebab-case ids, unique ids, `defaultPriority` in the priority enum. The `notifications` block is covered by `signing_payload()` when non-empty, so channel declarations (including any `critical` default priority) are tamper-evident post-signing; manifests without channels produce a byte-identical signing payload to pre-Phase-2 manifests (backward compatible).

## Authorization

- **Token gate** (`token_auth.py` `app_token_path_allowed`): app tokens are deny-by-default; a single carve-out grants every app token exactly `/api/notifications/push`. Deliberately NOT `/api/notifications` — that path also serves GET (read history) and DELETE, which app tokens must not reach.
- **Handler checks**: app must be installed AND enabled (`is_app_enabled`, a read-only accessor safe for `asyncio.to_thread` — unlike `get_app` it has no version-sync write side effect), and the channel must be declared in the manifest. Manifest reads run off-loop via `asyncio.to_thread` (TOCTOU window accepted: one final notification from a just-disabled app).
- **Auditing**: every denial, error, and grant emits SEL `log_api_access` with the caller identity.

## Rate limiting

`notifications/rate_limit.py` `AppRateLimiter`: per-app token bucket, 30 notifications per 5 minutes with burst 10. State-owned (`DashboardState.notification_rate_limiter`), not a module global, so its lifecycle matches the gateway instance and tests get isolation. Buckets are never evicted; growth is bounded because only installed, enabled apps reach the limiter (unknown apps 403 earlier). `refund(app_name)` returns one token (capped at burst) for requests that consumed a token but delivered nothing.

## Delivery and event-loop safety

`bus.push` delivers synchronously into `DashboardState._deliver_note` (redact, append to in-memory log, SSE broadcast). Persistence (JSONL append + trim) does blocking file I/O; because the sink is externally drivable in Phase 2, ALL notification file mutations run on a dedicated single-worker executor when a loop is running: appends from the delivery sink AND whole-file rewrites from delete/ack/unack/clear. Single-worker execution means strict submission order — a rewrite submitted after an append can never be overtaken by that append, so a deleted notification cannot be resurrected by a still-queued persist. Mutation endpoints (`delete_notification`, `ack_notification`, `unack_notification`, `clear_notifications`) are async and await durability before responding; the delivery sink stays fire-and-forget (a push response does not need to wait for disk). Snapshot copies are handed to the executor so later loop-side mutation (e.g. ack flags) cannot race serialization. No locks are taken on the event loop and no file I/O runs on it. Sync callers (tests, CLI) persist inline.

## Testing

`test/test_notifications_push.py` (39 tests): auth bypass attempts, undeclared channels, oversized/chunked bodies, rate limit + refund semantics, falsy-but-valid fields, signing-payload coverage, register-once behavior, sink-failure 500. `test/test_dashboard.py` covers persistence, load-time redaction, and the executor-offloaded persist path.

## Channel lifecycle

Channels register lazily on an app's first push to each declared channel. On app uninstall or disable, `NotificationBus.unregister_app_channels(app_name)` removes every `<app>.*` channel from the registry (wired in `apps/routes.py`); re-enabling re-registers lazily on the next push. System channels are never removable. The app name `system` is rejected at manifest validation (`RESERVED_APP_NAMES`) AND denied defense-in-depth at the push endpoint, so app channels can never shadow the reserved `system.*` namespace.

## Per-channel settings (Phase 3)

User preferences per channel, stored in `~/.kiro/crew/notification_settings.json` (`notifications/settings.py`, state-owned `ChannelSettings`, atomic-rename writes, corrupt file falls back to defaults):

- **Mute**: the note still lands in history (mute silences, it does not destroy) but is stamped `silenced: true` with priority forced to `passive`, so every attention surface skips it — the unread badge counts only non-passive rows, and sound/banner/feed styling key off the same fields.
- **Priority override**: the user's value replaces the producer-requested priority and channel default.
- **Protected channels** (`system.approval`): cannot be muted and cannot have priority lowered — enforced at the settings API (400) AND at apply time, so even a hand-edited settings file cannot silence approvals (RFC exit criteria: approval still interrupts while heartbeat can be silenced everywhere).

Settings are applied at the delivery sink (`_deliver_note`), before append/broadcast, so SSE clients and disk both see the user's view; the bus stays pure.

API (dashboard-user only; app tokens have no grant here):
- `GET /api/notifications/channels` — every registered channel plus channels with stored settings (even if currently unregistered, e.g. app disabled), each with `source`, `default_priority`, `protected`, and `settings`.
- `PUT /api/notifications/channels/settings` — body `{"channel", "muted"?, "priority"?}`; `priority: null` clears the override. Changes broadcast over WS as `notification_channel_settings`.

## Phase 5: cleanup and agent tool (delivered)

- **`send_notification` MCP tool**: agent sessions publish schema-v2 notifications through the fixed `system.agent` channel via `POST /api/notifications/agent` (`source`/`channel` are server-fixed, never body-supplied; full payload validation applies — internal-path urls, action caps, length caps; a `200` awaits the persist like the app push; app tokens are refused at the handler — apps publish through their own token-verified endpoint). Governed by the same messaging capability gate as `send_message`. Caller identity resolves strictly, in order: (0) the gateway-injected per-call caller context (`_meta.kirocrew.caller`, installed by the stdio dispatch loop via `mcp_caller.set_current_caller` — the authoritative identity in the pooled/warm-pool topology, where gatewayd strips client-forged blocks and injects its own from the claim-push at `rekey()`), (1) `KIROCREW_SESSION_KEY` env, (2) HMAC-verified `KIROCREW_HOST_PID` pid file; the tool fails closed with no verified identity. Channel agents (`channel:*` caller keys) are denied at MCP dispatch for BOTH `send_message` and `send_notification` — the interactive `channel.py` guard only fires on permission-request events, which auto-approved calls never emit.
- **TTL sweeper**: passive notes carrying a positive integer `ttl` (seconds) expire once `ts + ttl` passes. Swept in-memory at load and lazily on every delivery (the log is capped, so the scan is cheap); disk catches up on the next full rewrite. Only passive notes sweep — critical/default history has recall value. Ambiguous timestamps are kept, never destroyed.
- **Delivery to additional channels: planned, superseding the earlier Slack-escalation design**. A per-channel routing preference (`deliver_to`: a set of connected chat transports — Slack/Discord/Telegram/Webex/WeCom — plus a minimum-priority filter) will fan matching notes out to those transports, governed per transport through the `channels/<transport>` allowlist via the shared `vet_and_audit` seam. The earlier Slack-only, away-detection escalation shape was rejected (transport-specific and presence-sniffing); it never shipped.
- **Kind-fallback cleanup: deferred (precondition unmet)**. The RFC gates removal on telemetry showing no legacy rows in the active window, but all system producers still route through the legacy `notify()` adapter by design (backward-compat table: "Signature unchanged; adapter forwards to bus indefinitely"), so legacy-shaped rows are permanent in the active window. The `kind` field remains load-normalized and presentation-only.

## Phase 4: inline actions and grouping (delivered)

- **Action contract**: an `actions[]` entry is `{id, label, url?}`. `id` + `label` are required non-empty strings; `url` is optional and validated at persistence time with the dashboard-internal-path rule (same validator as the note-level `url` — `_validate_internal_url` in `notifications/bus.py`). Caps at validation: at most 4 actions per notification, `id` ≤ 64 chars, `label` ≤ 40 chars, `url` ≤ 500 chars (every action renders as a button on every surface, so unbounded counts/lengths would distort feed layout). The frontend renders an action as a navigation button only when it carries a `url` (`safeInternalUrl` in `notifMeta.tsx` re-checks client-side as defense-in-depth, plus string-type guards for legacy persisted rows); **url-less actions are legal, persist, and render nothing today** — they reserve dispatch semantics for a future phase.
- **Approval inline resolution**: unacked `approval` notes render one-click Approve/Reject in the feed (bell popover and page), dispatching to the existing `POST /api/approvals/{id}/{action}` endpoint.
- **`group_key` stacking**: notes sharing a `group_key` within a date group collapse to the newest head. The mac sheet renders a layered deck (click the collapsed head to expand, quiet "Show less" capsule); the panel variant uses an explicit "N more" pill.
- **Deep links**: the detail panel renders an Open button for a note-level `url` and buttons for url-carrying actions (same validation).
- **Dock badge**: the renderer mirrors the unread (non-passive, non-silenced) count to `app.setBadgeCount` via a `badge:set` IPC bridge; clamp lives in `electron/badge.js`.

## Related: sidebar nav status (`app_nav_status`)

A sibling app→user signal, but a **glanceable status** rather than a
notification. An app reports a runtime state that renders as a small colored dot
on its sidebar icon (issue #520), instead of (or alongside) a feed notification.

- API: backend `AppContext.set_nav_status(tone, label=None)`
  (`apps/context.py`). `tone ∈ {neutral, busy, positive, caution, critical}`
  (unknown → `neutral`); `label` is optional tooltip text, capped at 48 chars.
  The app owns its domain state and maps it to a tone; the core validates
  render-safety only.
- Gating: manifest `permissions.events: ["app_nav_status"]` wires `ctx.events`;
  `set_nav_status` no-ops otherwise. `permissions.storage: true` persists the
  last status so `GET /api/apps` returns a `nav_status` field for fresh-load
  hydration before the next live push.
- Transport: publishes an `app_nav_status` EventBus frame
  (`{type, app, data:{tone,label}}`) → `useWebSocket` → `appStatusSlice` →
  `AppStatusIndicator` (corner dot when the rail is collapsed, right-edge dot
  when expanded; `busy` pulses; label is the hover tooltip). Tones map to the
  active theme's tokens via `--nav-status-*`, so no per-theme edits are needed.
- Full contract: `docs/app-kit/manifest-reference.md` and
  `docs/app-kit/api-reference.md`.

## Known follow-ups

- Phase 3 frontend: settings UI section (channel list grouped by source), priority tiers wired through sound/native banner/feed styling, provisional keep/mute prompt for the first notification from a new app channel.
- Phase 4 follow-ups: surface inline-approval failures as a per-row transient error state (currently a console diagnostic; the row stays retryable); hoist dock-badge mirroring from `NotificationsBellButton` to an app-level effect with badge cleanup on teardown.
- Phase 5 follow-ups: run the TTL sweep in the GET handler too (today it runs at load and on delivery); propagate sweep removals to connected clients (broadcast expired timestamps over WS/SSE or trigger a list refetch — today an active dashboard retains an expired passive row until reload); add `ttl` to the `send_notification` tool schema (purely additive); per-channel delivery routing to additional chat transports (see the planned bullet above).
