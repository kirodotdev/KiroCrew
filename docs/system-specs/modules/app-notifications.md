# App notification producers

## Overview

Installed apps declare notification channels in `app.json` and publish through `POST /api/notifications/push` with an app token. `dashboard.handlers.notifications_push.api_push_notification` resolves the producer from the verified token rather than the request body, requires a manifest-declared channel, and uses the state-owned rate limiter. `NotificationBus.push` enriches the payload and calls `DashboardState._deliver_note`, which redacts, applies channel settings, appends the note, broadcasts it, and queues persistence.

## API

### POST /api/notifications/push

This endpoint requires an app token. Dashboard-user tokens carry no `request["app"]` identity and `api_push_notification` rejects them. `dashboard.server._register_mcp_routes` registers the route for both dashboard and headless gateway servers.

The JSON object contains:

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `channel` | string | yes | A bare channel id declared in the app manifest. |
| `title` | string | yes | `NotificationPayload.validate` applies the title cap. |
| `body` | string | yes | `NotificationPayload.validate` applies the body cap. |
| `priority` | string | no | `critical`, `default`, or `passive`; absent values use the channel default. |
| `group_key`, `url`, `icon`, `ttl`, `actions`, `meta` | — | no | `NotificationPayload.validate` validates the payload. Note and action URLs must be dashboard-internal paths, making persistence the trust root: no stored action can carry an external link. |

`read_bounded_json` enforces the request-size bound before decoding, both from `Content-Length` and while incrementally reading a chunked stream; `test_notifications_push.py::test_body_size_boundary_exact` and `::test_oversized_chunked_body_rejected` pin that boundary. `api_push_notification` sets `source` to `app:<name>` from the verified token and expands `channel` to `<app-name>.<channel-id>`.

The endpoint returns the enriched note on success, including resolved source, full channel, effective priority, and `ts`. Validation and registration failures return `400`, including undeclared channels and an invalid manifest channel priority; missing or disabled app identity returns `403`; oversized bodies return `413`; exhausted budgets return `429`; and delivery or persistence failures return `500`. `test_notifications_push.py::TestPushDurability` pins the durability invariant: the handler awaits `DashboardState.last_notification_persist`, so it does not return success when the queued persist fails. Legacy `DashboardState.notify` remains best-effort.

### Deep-linking a push back to its notification

`NotificationBus.push` creates `ts` as the note store id. The push response and notification envelope carry it, and the per-note mutation APIs key on it. Producers can link to a note with:

```
/notifications?note=<url-encoded ts>
```

`ts` is an ISO-8601 UTC value, so callers must percent-encode it: an unencoded `+` decodes as a space and cannot match the stored value. `website/src/pages/NotificationsPage.tsx` exports `NOTE_DEEP_LINK_PARAM`, captures and removes the parameter with history replacement, and resolves it through the same selection path as a tapped row. That path preserves acknowledgement, mobile detail, stack expansion, and scroll behavior; an unmatched id leaves the page unselected without an error.

### Request pipeline order

`api_push_notification` performs bounded parsing and app/channel resolution, registers an unregistered channel while holding `app_lifecycle_lock`, validates the payload, consumes a rate-limit token, then calls `NotificationBus.push`. The order is load-bearing:

- `test_invalid_payload_does_not_consume_rate_token` and `test_corrupt_manifest_400_does_not_consume_rate_token` ensure non-delivering `400` paths do not drain the budget.
- `test_register_once_does_not_stomp_runtime_priority_override` ensures lazy registration cannot reset a runtime channel priority.
- The lifecycle lock serializes enablement and registration with disable/uninstall. If a channel becomes unavailable before `NotificationBus.push`, the handler fails the push and refunds the token (`notifications_push.py`).
- A `NotificationValidationError` from `NotificationBus.push` refunds the consumed token. Delivery and persistence errors do not refund because the note may already have been broadcast.

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

`apps.manifest.NotificationsConfig.validate` requires unique kebab-case ids, names, and enum priorities; `test_notifications_push.py::TestNotificationsManifest::test_channel_cap_enforced` pins the channel-count bound. `AppManifest.signing_payload` includes non-empty channel declarations, so signed declarations and their defaults are tamper-evident. `test_no_channels_keeps_pre_phase2_payload_shape` pins the empty-channel payload shape.

## Authorization

- `dashboard.token_auth.app_token_path_allowed` denies app-token paths by default and explicitly permits `/api/notifications/push`; it does not grant `/api/notifications`, which includes notification-history reads and deletes.
- `_resolve_app_channels` requires an installed, enabled app and manifest declaration. It runs in `asyncio.to_thread` and uses the read-only `is_app_enabled`/`get_app_manifest` path rather than `get_app`, whose version synchronization can write metadata.
- `api_push_notification` SEL-audits token-identity, disabled/unknown-app, undeclared-channel, rate-limit, delivery, persistence, and successful-grant outcomes. Bounded-body, channel-registration, and payload-validation responses return directly without a `log_api_access` call.

## Rate limiting

`notifications.rate_limit.AppRateLimiter` maintains a per-app token bucket. `DashboardState.notification_rate_limiter` owns the limiter, keeping lifecycle and test isolation scoped to a gateway instance; `test_rate_limiter_is_state_owned_not_module_global` enforces that invariant. `test_burst_allowed_then_limited` and `test_refund_returns_token_capped_at_burst` pin the bucket configuration and refund ceiling. The handler reaches the limiter only after installed/enabled authorization, so its never-evicted buckets are bounded by authorized app names.

## Delivery and event-loop safety

`DashboardState._deliver_note` redacts the note, applies settings, appends it to the in-memory log, broadcasts it, and queues `_persist_notification`. On a running event loop, delivery appends and rewrite mutations share `_notification_io_executor`, a single-worker executor; `DashboardState._rewrite_notifications_async` awaits rewrites for delete, acknowledgement, unacknowledgement, clear, and acknowledge-all paths. Submission order is load-bearing: a rewrite queued after an append cannot be overtaken, preventing deleted rows from reappearing. Snapshot copies prevent later loop-side mutations from changing the data being serialized. `test_dashboard.py::test_deliver_note_offloads_persist_on_running_loop` and `::test_ack_persists_durably_before_return` cover these guarantees; synchronous callers persist inline.

## Testing

`test/test_notifications_push.py` covers app-token authorization, manifest channel enforcement, bounded/chunked bodies, rate-limit and refund semantics, falsy valid fields, signing-payload coverage, lazy registration, and sink/persistence failure paths. `test/test_dashboard.py` covers persistence, load-time redaction, ordered executor persistence, and durable rewrite behavior. `test/test_notification_settings.py` covers settings persistence, protected channels, sink application, badge behavior, and settings APIs.

## Channel lifecycle

Channels register lazily on the first push to each declared channel. App lifecycle routes call `NotificationBus.unregister_app_channels(app_name)` while holding the app lifecycle lock; disabling or uninstalling an app removes its registered `<app>.*` channels, and a later enabled push registers them again. `test_notifications_push.py::TestUnregisterAppChannels` pins boundary-safe removal and preservation of system channels. `RESERVED_APP_NAMES` rejects `system` during manifest validation, and `_resolve_app_channels` rejects it again, preventing app channels from shadowing `system.*`.

## Per-channel settings

`notifications.settings.ChannelSettings` is state-owned, writes atomically, and loads an invalid settings file as empty defaults. `ChannelSettings.apply` runs in `DashboardState._deliver_note` before append and broadcast, so disk and clients receive the same user view while `NotificationBus` remains policy-free.

- A muted non-protected channel remains in history but receives `silenced: true` and passive priority. `test_apply_mute_forces_passive_and_silenced` and `test_muted_channel_excluded_from_badge` pin the visibility and badge invariant.
- A priority override replaces the effective producer or channel priority.
- `system.approval` is protected. `ChannelSettings.update` rejects muting or lowering it, and `ChannelSettings.apply` enforces the same floor for hand-edited settings; `test_protected_channel_cannot_be_muted_or_lowered` and `test_apply_ignores_noncritical_override_on_protected_channel` cover both boundaries.

Dashboard-user settings routes expose the union of registered channels and stored settings through `api_notification_channels`; `api_notification_channel_settings` accepts mute and priority updates, clears an override for `priority: null`, and broadcasts `notification_channel_settings`.

## Agent notifications and expiration

`mcp_tools.messaging.send_notification` requires a verified caller identity, applies the messaging governance gate, and denies channel-agent callers. `dashboard.handlers.messaging.api_notification_agent_push` fixes agent notes to the `system.agent` channel and server-derived source before `NotificationPayload` validation. The agent endpoint and the app push handler both await queued persistence before returning success.

`DashboardState.sweep_expired_notifications` removes only passive notes with a positive integer `ttl` whose parseable timestamp has elapsed; ambiguous timestamps and other priorities remain. `DashboardState` invokes the sweep while loading persisted history and before each delivery. The in-memory sweep becomes durable on a later full rewrite, so already-open clients retain an expired row until their next reload or refresh.

## Inline actions and grouping

`NotificationPayload.validate` accepts action entries with non-empty `id` and `label`, and validates each optional action URL at the persistence trust root. `test_notification_bus.py::test_action_count_capped` and `::test_action_field_lengths_capped` pin action bounds. URL-less actions persist but do not render; `test_action_without_url_accepted` pins that contract.

`website/src/components/notifications/NotificationDetailPanel.tsx` and `NotificationFeed.tsx` render navigation actions only after `safeInternalUrl` rechecks a dashboard-internal URL. Unacknowledged approval feed rows render inline Approve and Reject that resolve through the approvals endpoint (the one-click path `rfc-local-notification-bus.md` Phase 4 shipped). Every approval row -- read or unread, because reading a pending request must not shrink it -- renders the notification body in full through the same markdown renderer and per-item error boundary as the detail panel: no slice, clamp or hidden overflow, because a control that authorizes a command must sit next to the whole command, and a truncated excerpt turns two lines into one harmless-looking line. The producer tags the command fence `approval-command` (`lib/approvalNotificationBody.ts`), a dashboard-own tag `CodeBlock` soft-wraps like `error-report`, so a line wider than the feed column wraps instead of scrolling off the edge. Both surfaces render the body with `readOnlyCode`, so the command carries a copy control but no edit affordance: `EditableCodeBlock`'s scratch editor changes only a local copy, and a pencil beside Approve would let a reader authorize the original command while looking at their edit. Every other row keeps the flattened one-line excerpt. This contract applies to the full page and bell popover, including the mac feed variant. `NotificationFeed` collapses notes sharing a `group_key` within a date group to the newest row and expands the stack on demand. `NotificationsBellButton` sends the unread attention count through `badge:set`; `electron/badge.js` clamps it before `app.setBadgeCount`.

## Plain-text previews

The native notification body, feed-row preview and transcript turn minimap share
`website/src/components/notifications/notifMeta.tsx::stripMd`. It unwraps paired
emphasis and code delimiters, keeps code contents literal, and preserves unpaired
markers and intraword underscores. Heading, blockquote and list prefixes (`-`,
`+`, `*`, ordered) are removed only at line starts and only when whitespace
follows the marker, so `*emphasis*` and `**bold**` at a line start are unwrapped
as emphasis rather than deleted as bullets; links/images retain labels/alt text,
and fenced code loses its language tag. A single prose newline collapses to a space; a
paragraph break (two or more newlines, blank lines may hold whitespace) in prose
becomes ` · ` — the detail panel's own separator idiom — so an approval reads
`Source: agent · <command> · <purpose>` and a skill note's paragraphs stay
distinct instead of running together. Empty paragraphs are dropped, so the
separator never leads, trails or doubles. Whitespace inside code regions stays
literal, including indentation, repeated spaces, tabs and blank lines; only the
fence wrapper's final line ending is removed. A multiline command remains a
multiline string in the preview. Backtick fences
close only on a standalone run at least as long as their opening run; shorter
runs inside code remain literal. An inline span pairs runs of EQUAL length, so a
longer or shorter run inside one stays literal content. One deliberate deviation
from CommonMark: a newline ends an unclosed inline span rather than continuing
it, because in a preview a stray backtick would otherwise pair with another far
below and hold every line between as code, suppressing flattening for that whole
region — the deviation costs only multi-line inline spans, which no producer
writes. Approval bodies use
`website/src/lib/approvalNotificationBody.ts::approvalNotificationBody` to combine
a formatted source label with a literal command in a fence longer than any
backtick run in that command (minimum three). Empty input adds no fence. The
live WebSocket event appends its optional purpose; reconciliation keeps its
source-and-command-only content. This preserves balanced globs, home paths,
redirects and command backticks in both previews. The feed slices the flattened
text to 80/140 characters, so wrapper fences do not consume its excerpt budget;
the detail panel renders the fenced input as one code block. That body is the
only surface naming the requesting system: the detail panel's metadata row
prints the note's kind (`KIND_META[...].label`) under the `pages.artifactsPage.kind`
label, so its label and the body's `Source:` label are distinct fields.

The shared contracts live in `website/src/test/notifMeta.stripMd.test.ts` (with
the code-region scan in `website/src/test/notifMeta.codeScan.test.ts`) and
`website/src/test/approvalNotificationBody.test.tsx`; native banner formatting is
pinned in `website/integration/AppNotification.integration.test.tsx`. WebSocket
producer coverage pins the differing purpose policies, and the feed tests pin
both excerpt lengths.

## Notification sound (client)

Notification sound is produced entirely on the client and is independent of the
notification feed, the bell badge, and OS notification-center toasts. The
WebAudio layer is the **single source of sound**: `website/src/hooks/useNotificationSound.ts`
synthesizes tones through the Web Audio API (no audio files) and is the only
component that emits sound. Both page-context `Notification` constructors —
`website/src/hooks/useNativeNotification.ts` (feed toast) and the approval toast
in `website/src/hooks/useWebSocket.ts` — pass `silent: true`, so the OS toast
never adds its own system chime on top of the WebAudio tone. A browser that
ignores `silent` degrades to the prior double-sound behavior and no worse.

### Sound events

Two sound kinds are synthesized by the websocket layer. `TURN_DONE_KIND`
(`'turn'`, on `chat_done`) is sound-only: it never appears in the feed (no Redux
entry, no toast, no badge). `APPROVAL_KIND` (`'approval'`, on an `approval`
frame) is synthesized for sound, but the same approval frame *separately* adds an
approval notification to the feed — so approval both chimes and shows a feed
entry, and the two are independent. Both chimes are suppressed during reconnect
catch-up replay, and `shouldChimeOnTurnDone` also suppresses slot-less turn
completions. A real feed `notification` frame fires `MC_NOTIFICATION_EVENT` with
its own `kind`, except when the note is muted-channel (`silenced`) or `passive`.

### Settings and resolution

Settings persist in `localStorage` under `mc-notification-sound`
(`{ enabled, volume, perCategory }`). `presetForKind(kind, settings)` resolves
the preset for a kind, in order:

1. `enabled === false` → `'none'` (primary switch; WebAudio never plays).
2. An explicit per-category override in `perCategory[kind]`.
3. Global `perCategory.all === 'none'` → `'none'`. An explicit global silence
   wins over any built-in category default, so `all='none'` truly silences every
   category that has no explicit override — **including** approval.
4. A built-in, non-persisted category default (`BUILTIN_CATEGORY_DEFAULTS`,
   currently `approval → pulse`). Reached only when the global fallback is
   audible. Not written to `localStorage`, so a "Use default" reset cannot clear
   it.
5. The global fallback `perCategory.all ?? 'chime'`.

`NotificationsPanel.tsx` previews the effective per-category preset by calling
`presetForKind` (not a naive `perCategory[cat] ?? fallback`), so the settings
row, its Test button, and runtime playback always agree — notably for approval,
whose built-in `pulse` default the naive form did not show.

### Persistence and cross-surface sync

`saveSoundSettings` writes through `safeSetItem` (quota-defensive) and returns a
boolean. It fires the same-window `MC_SOUND_SETTINGS_CHANGED_EVENT` **only on a
successful persist**; a quota-dropped write returns `false` and stays silent, so
no mounted `useNotificationSound` reloads and reads the old value.
`NotificationsPanel` adopts a change into local state only when the save returns
`true`, leaving the UI showing the persisted truth on failure.

`useNotificationSound` stays in sync three ways: the same-window
`MC_SOUND_SETTINGS_CHANGED_EVENT`, and a cross-tab DOM `storage` listener that
filters by `storageArea === localStorage` and by the `mc-notification-sound`
key (a `null` key, i.e. `clear()`, is also honored) then reloads through
`loadSoundSettings` so validation and clamping are reused. Notification playback
is debounced to one tone per 300 ms.

## In-app banner (client)

`website/src/components/notifications/NotificationBanner.tsx`, mounted once by
the bell button in `App.tsx` and portalled beside the bell's sheet, shows a
macOS Notification Center-style card under the top bar for a **live**
notification. The card body is `NotificationCard.tsx`, the ONE rendering the
bell popover's mac rows and the banner both use (kind-tinted 26 px icon square,
one-line title, two-line body, relative time with the unread dot, hover-reveal
close, quiet capsule actions); its `elevation` prop is the only difference —
`popover` (72 % card tint, the theme's `--shadow-md`) versus `banner` (88 %
tint, `--shadow-lg`); shadows are theme tokens, never literal alphas. The
card's `body` prop replaces the two-line clamp: the feed passes the full
read-only approval render for every approval row, because the popover card
keeps one-click Approve/Reject and a clamped excerpt hides the tail of the
command they authorize. The banner, which offers only Review, keeps the
excerpt. A critical note is signalled only by its danger dot and the approval
icon tint, never an edge or a label. Nothing about the banner is persisted
server-side.

### Trigger

The banner listens to `MC_LIVE_NOTIFICATION_EVENT` (`hooks/notificationEvent.ts`),
which `useWebSocket` fires for a `notification` frame received on a live
connection. It never reads the Redux list: the boot `fetchNotifications`
snapshot and reconnect refetches fill the store with history, and history is
never bannered. `useWebSocket` withholds the event during a reconnect catch-up
(`reconnectingRef`), the same window that mutes the turn-done chime.

### Priorities

| Priority | Banner |
|---|---|
| `critical` | stays until clicked, dismissed, or acted on; the live region is `role="alert"` while one is pending |
| `default` | auto-hides after `BANNER_AUTO_HIDE_MS` (6 s). Every pending default card shares ONE timer, restarted by each default arrival and paused while the stack is hovered or holds focus |
| `passive`, or `silenced` (`isSilencedNote`) | never |

Auto-hide does **not** acknowledge: the note stays unread in the bell, and the
unread dot is the visible continuation of the card. A body click or a url
action acknowledges (the popover's selection effect for the former,
`ackNotification` for the latter). A url action runs entirely inside the
navigation leave guard and awaits the ack: a user who answers "stay" keeps an
unread note and the card; a rejected ack (`ackNotification.rejected` flips
`acked` back in the slice) keeps the card and shows an `ErrorNotice` under its
actions, the action itself being the retry. The rollback is held to the same
per-write stamp rule as the confirmation: a rejection carrying a stamp a newer
ack has since moved (a second press that succeeded) changes nothing. The bell
popover's own open-a-note auto-ack asks once per selection so that flip cannot
loop it.

### Suppression (never banner)

`shouldBannerNote` in `hooks/notificationBanner.ts`, in order: the preference is
off; the note is passive or silenced; the bell popover is open (or closing); the
route is `/notifications`; the note describes what is already on screen —
`targetsCurrentView`: while the window is focused, a note whose `slot` is the
active chat on a chat route, or whose `url` path is the current route. Opening
the popover, landing on the inbox page, or switching the preference off also
retires every pending card.

### Stack

Newest on top. Beyond the top card, up to `BANNER_DECK_DEPTH` (2) older cards
peek as a deck of BLANK shells (card material only, no text, icon or time;
4/8 px offset, .98/.96 scale, .8/.55 opacity), so nothing prints through the
translucent top card. Each shell and the "Show N more" pill on the top card's
corner are the same control (`Show N more notifications`) that expands to a
vertical list of at most `BANNER_EXPANDED_MAX` (4) cards plus a "+N more in your
inbox" line that goes to `/notifications` (through the navigation leave guard) —
the same place the popover's "Open inbox" goes, so "inbox" names one place. On the mobile breakpoint only the newest card renders,
full width, with its close visible at rest (no hover on touch).

### Motion

Enter: slide in from the right with a fade (~220 ms). Exit, for auto-hide and
dismiss alike: the card shrinks about its top-right corner and travels to the
bell (`computeExitDelta` measures the vector from the card's own rect to
`bellRef`'s) while fading (~260 ms) — the relocation animates the same element
into its new home rather than swapping it out. Under `prefers-reduced-motion`
(`useReducedMotion`) enter and exit are plain fades and the deck/list switch
does no layout animation. Escape dismisses the topmost card; arrival never moves
focus.

### Setting

Settings › Notifications › Desktop alerts › "Show a banner for new
notifications", default ON, `localStorage` key `mc-notification-banner`
(`loadBannerEnabled` / `saveBannerEnabled`). A flip is announced same-window via
`MC_BANNER_SETTING_CHANGED_EVENT` and cross-tab via the DOM `storage` event, so
a mounted banner honours it immediately.

### System-notification permission surfaces

`hooks/useNotificationPermission.ts` exposes `Notification.permission` as state
(`unsupported | default | granted | denied`), re-read on window focus and after
its own `request()` settles. Two user-gesture surfaces call `request()`:

- **Settings › Notifications › Desktop alerts › System notifications**
  (`SystemNotificationsRow`): `granted` shows "Allowed" with a check and no
  button; `default` offers "Allow system notifications"; `denied` states in
  plain language that the browser blocked it and where to turn it back on.
  Absent entirely when `Notification` is undefined.
- **Bell popover hint** (`NotificationPermissionHint`, in the mac controls
  card): one row — bell-ring icon, "Get alerted when you're away", "Allow",
  "Not now" — shown only while permission is `default`, the store holds at
  least one notification, and the user has not pressed "Not now"
  (`mc-notification-permission-hint-dismissed`). Any verdict after "Allow"
  retires it too. The row leaves only once the dismissal is on disk; a failed
  write keeps it with an `ErrorNotice`, the buttons being the retry.

`useNativeNotification`'s effect-time `requestPermission()` on a first unacked
arrival is left in place as best effort; browsers refuse a prompt with no
gesture behind it, which is why the two surfaces above exist.
