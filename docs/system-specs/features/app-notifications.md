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

`website/src/components/notifications/NotificationDetailPanel.tsx` and `NotificationFeed.tsx` render navigation actions only after `safeInternalUrl` rechecks a dashboard-internal URL. Unacknowledged approval notes render Approve and Reject controls that use the approval API. `NotificationFeed` collapses notes sharing a `group_key` within a date group to the newest row and expands the stack on demand. `NotificationsBellButton` sends the unread attention count through `badge:set`; `electron/badge.js` clamps it before `app.setBadgeCount`.
