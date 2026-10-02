# App notification producers

## Overview

Installed apps declare notification channels in `app.json` and publish through `POST /api/notifications/push` with an app token. `dashboard.handlers.notifications_push.api_push_notification` resolves the producer from the verified token rather than the request body, requires a manifest-declared channel, and uses the state-owned rate limiter. `NotificationBus.push` enriches the payload and calls `DashboardState._deliver_note`, which redacts, applies channel settings, appends the note, broadcasts it, and queues persistence.

### Reaching the endpoint from an entryPoint backend

A `backend.entryPoint` app runs as a separate loopback process, so it must learn the gateway's own address before it can push. The gateway injects that at spawn time as two generic environment variables (see `docs/app-kit/api-reference.md` -> Backend Environment Variables): `KIROCREW_GATEWAY_ORIGIN`, the gateway's `http://127.0.0.1:<bound port>` (`http://[::1]:<bound port>` for a `::1` bind; omitted for a bind to one specific interface — `docs/app-kit/api-reference.md` owns the rule), and `KIROCREW_GATEWAY_ORIGIN_PROOF` (`HMAC-SHA256(app_secret, origin)`, injected only when the app has a secret and the origin is set). The origin is set ONLY from the port the gateway ACTUALLY bound (its exported `KIROCREW_BOUND_PORT`, numeric and in `1..65535`), never the app's own `PORT`, an inherited `KIROCREW_PORT`, a config value, a default, or a request-derived value. Without that bound-port evidence both variables are omitted, so a backend that needs a callback base fails closed (stays dormant) rather than pushing to a guessed address. When the origin is present the backend recomputes the proof with its `KIROCREW_PROXY_SECRET` to confirm the origin is one this gateway minted, then pushes to `POST {KIROCREW_GATEWAY_ORIGIN}/api/notifications/push`, authenticating with its app secret. In-gateway route apps (`backend.routes`) have no separate process and push in-process (see `ops-mission-control` `notify_out`), so they need neither variable.

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

### GET /api/notifications/own and POST /api/notifications/own/ack

An app reads and acknowledges the notes it pushed, and nothing else, so its page can show and clear exactly what its sidebar badge counts (`website/src/appNotificationBadges.ts`). The handlers in `notifications_push.py` match `source` against `app:<name>`, the value `api_push_notification` stamps. For an app caller (`request["app"]` set by token auth) `<name>` is that identity and any `app` the request names is ignored. Any other caller must be the owner's dashboard (`require_owner_dashboard_request`, which also refuses a relayed teammate and an internal caller with no app), where the app's page runs, and names the app with `?app=` or a body `app`; a missing name is `400`. Naming the app gives the owner's dashboard nothing new, because the owner can already read and ack every note through `/api/notifications`; it is not app-to-app isolation, which comes from the app token alone. The list answers `{"notifications": [...], "unread": N}` with `unread_count` over those notes. `ack` takes `{"ts"}` and answers `404` unless the note the bell's `DashboardState.ack_notification` would mark for that `ts` is one of the caller's, so another source's note cannot be told apart from a missing one; it then acks through that same call. There is no server-side ack-all: the SDK's `ackAll()` acks each unread note it holds through `ack`, one at a time, so every ack of an app's note goes through the bell's own ack. Each ack broadcasts `notification_ack`, so the bell follows, and keeps the store's persistence contract: the in-memory log is the live record and the rewrite is best-effort, as for every bell ack. The routes are registered beside the push route in `_register_mcp_routes`, and an app token reaches them only through a `permissions.api` grant (see Authorization). The SDK's `useAppNotifications()` calls them through the scoped client. The routes and the hook are App Kit seam level 4 (`apps/app_sdk_level.APP_SDK_LEVEL`; app-kit-platform.md §32): no release carries level 4 yet, so they join it and the level is not raised, and a page that must also load on an older host feature-detects the hook through a namespace import. `test_notifications_own.py` pins the scoping; `index.coverage.test.tsx` pins the hook.

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

- `dashboard.token_auth.app_token_path_allowed` denies app-token paths by default and explicitly permits `/api/notifications/push`; it does not grant `/api/notifications`, which includes notification-history reads and deletes. `/api/notifications/own` is not implicit either: an app declares it in `permissions.api`, and the grant exposes only that app's own notes.
- `_resolve_app_channels` requires an installed, enabled app and manifest declaration. It runs in `asyncio.to_thread` and uses the read-only `is_app_enabled`/`get_app_manifest` path rather than `get_app`, whose version synchronization can write metadata.
- `api_push_notification` SEL-audits token-identity, disabled/unknown-app, undeclared-channel, rate-limit, delivery, persistence, and successful-grant outcomes. Bounded-body, channel-registration, and payload-validation responses return directly without a `log_api_access` call.

## Rate limiting

`notifications.rate_limit.AppRateLimiter` maintains a per-app token bucket. `DashboardState.notification_rate_limiter` owns the limiter, keeping lifecycle and test isolation scoped to a gateway instance; `test_rate_limiter_is_state_owned_not_module_global` enforces that invariant. `test_burst_allowed_then_limited` and `test_refund_returns_token_capped_at_burst` pin the bucket configuration and refund ceiling. The handler reaches the limiter only after installed/enabled authorization, so its never-evicted buckets are bounded by authorized app names.

## Delivery and event-loop safety

`DashboardState._deliver_note` redacts the note, applies settings, appends it to the in-memory log, broadcasts it, and queues `_persist_notification`. On a running event loop, delivery appends and rewrite mutations share `_notification_io_executor`, a single-worker executor; `DashboardState._rewrite_notifications_async` awaits rewrites for delete, acknowledgement, unacknowledgement, clear, and acknowledge-all paths. Submission order is load-bearing: a rewrite queued after an append cannot be overtaken, preventing deleted rows from reappearing. Snapshot copies prevent later loop-side mutations from changing the data being serialized. `test_dashboard.py::test_deliver_note_offloads_persist_on_running_loop` and `::test_ack_persists_durably_before_return` cover these guarantees; synchronous callers persist inline.

A `meta` value is merged onto the note with every NaN or infinite float, at any depth, read as `null` (`bus._finite_json`): the push route parses with plain `json.loads`, which accepts both, and one stored as is makes the feed response JSON no browser can parse. `test_notification_meta_nonfinite.py` pins it.

`POST /api/notifications/unack` on a cron note takes back the one `acked_items` entry that note's `POST /api/crons/<id>/ack` added, and nothing else. The cron ack gives that entry a fresh id, kept beside it in the job's `acked_item_ids` (aligned by position; written to the store only once some entry has one), and records only the job id and that id under the note's internal `_cron_ack_item` key, never the summary text. The unack hands the id to `unack_job_async(job_id, ack_id)`, which removes exactly that entry: not the job's newest, not a later entry with an equal summary, and nothing at all once the entry has aged out of the job's last 20 or when the ids no longer belong to the items: the job saves a digest of `acked_items` beside the ids (`acked_item_ids_digest`), and a writer that changed the items without the ids, such as an older build, leaves a digest that does not match, so the ids then name no entry. A note's ack and unack run one at a time (`cron_note_transition`), so the already-recorded check, the store write and the record update are one step and a double click adds one entry; a repeat ack re-acks the notification without appending. The record is cleared only once the unack has run, so a busy or unreadable store keeps it for the next unack (`dashboard/cron_note_ack.py`). A note read from the bell or by ack-all added no entry, so its unack leaves the job alone. `test_cron_note_unack_pairing.py` pins it.

## Testing

`test/test_notifications_push.py` covers app-token authorization, manifest channel enforcement, bounded/chunked bodies, rate-limit and refund semantics, falsy valid fields, signing-payload coverage, lazy registration, and sink/persistence failure paths. `test/test_dashboard.py` covers persistence, load-time redaction, ordered executor persistence, and durable rewrite behavior. `test/test_notification_settings.py` covers settings persistence, protected channels, sink application, badge behavior, and settings APIs.

## Channel lifecycle

Channels register lazily on the first push to each declared channel. App lifecycle routes call `NotificationBus.unregister_app_channels(app_name)` while holding the app lifecycle lock; disabling or uninstalling an app removes its registered `<app>.*` channels, and a later enabled push registers them again. `test_notifications_push.py::TestUnregisterAppChannels` pins boundary-safe removal and preservation of system channels. `RESERVED_APP_NAMES` rejects `system` during manifest validation, and `_resolve_app_channels` rejects it again, preventing app channels from shadowing `system.*`.

System channels are fixed in `notifications.bus.SYSTEM_CHANNELS`, including `system.monitor` (see Per-channel settings). A legacy `DashboardState.notify(kind, ...)` call maps `kind` to `system.<kind>`, falling back to `system.agent`, unless the caller passes `channel=`. `payload_from_legacy` raises `NotificationValidationError` when that override names anything but a system channel, and `NotificationCoordinator.notify` drops such a note with a warning; `test_monitor_notice_channel.py::TestLegacyChannelOverride` pins the override and the rejection.

An approval prompt in a dashboard chat that expires unanswered also posts one `system.agent` bell note (`kind` `agent`, title `turn_dispatch.APPROVAL_EXPIRED_NOTE_TITLE`, body `format_approval_expired_note`) from the runner's timeout branch, so someone who is not looking at the session learns the turn went on without the tool. The body names the tool and the session's display title, never the prompt's agent-authored text, and `meta.slot` gives the detail panel its open-session action. It is not `kind` `approval`, which the feed renders with live Approve and Reject buttons. It carries no `publisher_session`, so a muted session's expiry still notifies. A no-budget decline, where nothing was asked, posts none. `test_dashboard_approval_window.py` pins all three.

A chat that held Trust or Read when the gateway stopped and came back without it gets one `system.agent` note at boot (`kind` `agent`, title `chat_trust_restart.note_title`, body `note_body`, `meta.slot` and `meta.approval_mode`), so the owner learns a restart is why its tools ask again. See [session](session.md), "A restart ends a chat's Trust or Read mode". `test_chat_trust_restart_notice.py` pins it.

## Per-channel settings

`notifications.settings.ChannelSettings` is state-owned, writes atomically, and loads an invalid settings file as empty defaults. The file is parsed with `user_json.loads_user_json`, so a UTF-8 BOM reads. After a failed load the first `update` reads the file again; if it still fails, the file is renamed to `notification_settings.json.corrupt-<UTC stamp>` with a WARNING before the write, so no write replaces mutes it never read. `test_notification_settings_unreadable.py` pins it. `ChannelSettings.apply` runs in `DashboardState._deliver_note` before append and broadcast, so disk and clients receive the same user view while `NotificationBus` remains policy-free. A hand-written non-boolean `muted` is read as the boolean it means, with a warning naming it (`_with_bool_mute`): the config's own hand-edit reader `config.fields._coerce_bool` maps `true`/`false`/`1`/`0`/`yes`/`no`/`on`/`off` (case and spacing aside), and any other value keeps its truthiness, so a stored `"muted": "false"` no longer mutes the channel and nothing that read before is refused.

- A muted non-protected channel remains in history but receives `silenced: true` and passive priority. `test_apply_mute_forces_passive_and_silenced` and `test_muted_channel_excluded_from_badge` pin the visibility and badge invariant.
- A priority override replaces the effective producer or channel priority.
- `system.approval` is protected. `ChannelSettings.update` rejects muting or lowering it, and `ChannelSettings.apply` enforces the same floor for hand-edited settings; `test_protected_channel_cannot_be_muted_or_lowered` and `test_apply_ignores_noncritical_override_on_protected_channel` cover both boundaries.
- A note published by a muted session is delivered like a note on a muted channel. The two agent routes, `POST /api/notifications/agent` (`send_notification`) and the bell fallback of `send_message`, pass the caller's attested session key as `NotificationPayload.publisher_session` (a schema field in `_RESERVED_NOTE_KEYS`, so `meta` cannot set it, and empty for an unattested or app caller). `NotificationCoordinator.deliver` pops the key before redaction, so it is never stored or broadcast, applies `ChannelSettings.apply`, and then, when `session_is_muted` holds, stamps `silenced: true` and passive priority. A session is muted by its own `muted` flag, by `apps.<name>.mute_sessions` on the app that opened it (`slot._app`, read off the live config snapshot, so a gateway whose watcher has no snapshot yet mutes no app), or by `mutes_opened` on a session up its `_created_by` chain, walked from its creator, as `isSlotMuted` does on the client; a creator that is gone or a cycle ends the walk unmuted. The rule runs after the channel settings, so a channel priority override can neither undo it nor exempt a note. A note stays audible when the producer asked for `critical` (the structured needs-the-user signal; the approval channel defaults to it), when its channel is protected, or, for the `send_message` bell fallback, when the message offers `[OPTIONS:]` choices, in which case the fallback passes no `publisher_session`. Gateway reports about a session (monitor-loop notices, heartbeat, hook results, error and pause notices) name the session only through `slot` or `session_key` for jump-to-source and are not changed; a producer opts in by passing `publisher_session`. `test_notification_publisher_mute.py` pins the walk, the app mute, the exemptions, the ordering against channel settings, the unforgeable key and both routes.

Settings import writes the file through two more paths. `parse_imported_settings` validates an archive's `notification_settings.json` and re-applies the field and protected-channel rules `update` enforces, dropping and counting what fails them. The dashboard Merge installs the result with `ChannelSettings.install_imported`, only where no settings file exists; a Replace swaps the file inside `ChannelSettings.replacing_file`. See [config](config.md) under "Settings import (dashboard Merge)".

`system.monitor` is a system channel for the gateway's monitoring-loop stop and finish notices. `GatewayOrchestrator._notify_nudge_expired` emits them through `DashboardState.notify(..., channel=MONITOR_CHANNEL)`, and the note keeps the legacy `kind` `agent`. The channel shares the `system.agent` default priority and is not protected. `ChannelSettings._seed_monitor_from_agent` runs at construction: a settings mapping with no `system.monitor` key receives an in-memory copy of its `system.agent` entry, so a boot never writes the file and a fresh install with no stored `system.agent` entry seeds nothing. The next `update()` persists the copy. Every `update()` write keeps a `system.monitor` key, `{}` when the channel has no settings, as the record that the seed ran; a build without this key handling rewrites every dict-valued `channel_settings` entry as-is, so the record survives a downgrade, and removing it re-runs the seed and can re-mute a channel the user unmuted. `all_settings()` omits empty entries, so the channels listing never shows `{}`. `test_notification_settings.py::TestSeedMonitorFromAgent` pins the seed, with `test_unmuted_monitor_survives_downgrade_rewrite` and `test_fresh_install_agent_mute_does_not_seed_monitor` covering the downgrade and fresh-install boundaries; `test_monitor_notice_channel.py` pins channel registration, the legacy `kind` override, notice routing and the settings listing.

`system.apps` is a system channel for "an installed app has a newer version published". `apps.update_notify` emits the notes through `DashboardState.notify(..., channel="system.apps")`, keeping the legacy `kind` `update` — the spelling the gateway's own update notice uses, so a frontend filter on `note.kind` sees one name for "something can be updated". It has its own channel rather than falling back to `system.agent` so muting app-update notices does not also mute the notes agents write for a human decision; Settings → Notifications labels it "App updates" (`CHANNEL_LABELS`), because the bare id "apps" reads as every app's notifications. Default priority, not protected. One other producer shares it with the same `kind`: an update of an enabled app whose re-run `setup.onEnable` failed (`apps.routes_lifecycle._note_on_enable_failed_after_update`, titled `<name> updated, but its setup script failed`, linking the app's detail page); the app stays enabled, and the rule is in [app-kit-platform](app-kit-platform.md).

`handle_registry` (`GET /api/apps/registry`) is where it runs, with the listing rows it has just built — the same rows, carrying the same `updateAvailable` and install status, that the Discover Updates list is built from (the rail badge counts only its enabled apps, through `countUpdatables`). Nothing is fetched and no timer is added: the note rides the listing the store and the shell badge query already request. It runs on CATALOG rows only: when the catalog is unreachable the seed fallback answers, and its rows can name an older version than the catalog, so announcing from them would report a version that is not the newest. Two halves, because they belong in different places: `prepare_app_update_notes` reads and writes the ledger and runs off the loop, then `deliver_app_update_notes` calls `state.notify` ON the serving loop. Off the loop, the coordinator persists a note inline instead of through its FIFO executor, and a rewrite queued by a concurrent delete can then overwrite it — which the ledger, already written, would never let come back. Both halves are total, so a store response can never fail on a note.

A note fires at most once per published version. `announced-app-updates.json` under the data home records the version last announced per app, and only a different version announces again. It is deliberately NOT under `cache/`, because dropping a cache is a supported repair and dropping this would re-announce the whole backlog. An entry is dropped only on evidence it is finished with — the app's row says it is no longer installed, or that the installed version has reached the one recorded. An entry whose app is merely ABSENT from a listing is kept, because absence says nothing about the app and forgetting it would announce the same version again when the row returns. A **missing** ledger is a first observation: the pending backlog is adopted silently, because a fresh install with updates waiting would otherwise open with a note per app about versions that have been out for weeks, and the Discover Updates list already shows that standing backlog. A ledger that is unreadable or of the wrong shape reads as empty instead, which fails toward one duplicate note rather than toward permanent silence. Persist before you publish: the ledger is written BEFORE any note goes out, and a ledger that cannot be written sends nothing — otherwise every later listing would read the stale ledger and announce again. A failed delivery is therefore lost rather than repeated, the same ordering and the same trade as `issue_radar`'s watcher. The whole read-modify-write runs under one exclusive lock (`announced-app-updates.lock`, beside the ledger): the store page and the shell badge query list concurrently, and two unserialized passes reading the same ledger would each write back only their own view, so the second would erase the entry the first committed and that version would announce again. A lock that cannot be taken fails closed: no note, nothing written. Only `lifecycle: "gateway"` apps are announced: an `app`- or `locked`-lifecycle app updates itself, so a note would point at an Update button the store cannot press. The note is titled `<name> <version> available`, version first so the feed's truncation takes the word rather than the version, using the app's validated name — never its display name, which on an external row is content that row's author controls and which a bell note would present as the gateway speaking. For the same reason a published version is repeated only when it is a strict semver of bounded length (`apps.update_status.publishable_version`); `9.0.0-SECURITY ALERT: …` produces no note at all. `test_app_update_notify.py` pins the once-per-version property, the first-observation adoption, the kept entry on an absent row, the unwritable-ledger silence, the locked read-modify-write and its fail-closed refusal, the catalog-only trigger, delivery on the loop thread, the skipped lifecycles and the delivery-failure ordering.

The same listing retires the note. Once a row says the app is installed at the version a note announced, or a later one, `resolve_app_update_notes` marks that note read through `DashboardState.acknowledge_notifications_where` (the coordinator's `acknowledge_where`, which `resolve_skill_reviews` also uses): it is acknowledged, never deleted, the rewrite runs once, and each note broadcasts `notification_ack` so an open bell drops it live. It runs where the listing runs, so it covers an update from any surface (the store, `kirocrew app update`, an agent) with no hook in each: the store's own update announces `mc:apps-changed`, which invalidates the shell's always-active `['registry']` observer, so that refetch retires the note at once, and an update made elsewhere is retired by the next listing. Unlike the announce it also runs on the seed fallback's rows, because what it reads is the installed version, which comes from the local install record whichever source built the row. A match needs the `system.apps` channel, `source` `system`, the note's `latestVersion` and the row's `installedVersion` both passing `publishable_version`, and the installed version equal to the announced one or positively newer (two pre-releases of one release are not ordered, so `2.0.0-beta.1` installed leaves a `2.0.0-beta.2` note unread), so an externally detected app (`installedVersion` `unknown`) retires nothing and the setup-script-failure note, which carries no version, stays unread. Total like the other halves. `test_app_update_notify.py::TestANoteIsMarkedReadOnceTheUpdateIsInstalled` pins it against a real `DashboardState`, through the listing route on both the catalog and the seed rows.

`system.memory` is a system channel for the opt-in weekly note that agent tool failures keep coming back (`dashboard/repeated_failures_notice.py`; the rule is in [memory-skills-hooks](memory-skills-hooks.md), "Recurring tool failures"). Its own channel so muting it leaves `system.agent` alone; the legacy `kind` is `agent`. Default priority, not protected; Settings → Notifications labels it "Repeated failures" (`CHANNEL_LABELS`), because the bare id "memory" reads as memory in general. The note names the dashboard copy `repeated_failures`.

`system.turn` is a system channel for "a chat turn stopped with an error the operator must act on". A turn that dies at a terminal, non-retry arm of `_run_chat` — a provider 5xx/throttle or process give-up, a content-filter refusal, a transport death, a signed-out agent — sets `turn.died_notice` to the copy id of its one-line body (a key of `_TURN_DIED_BODIES`, typed `_TurnDiedNote`); the turn's `finally` fires `dashboard/chat_turn/turn_reply_seams.py::_notify_turn_died` ONCE from that single choke point, so every terminal arm (stop-reason and `except` alike) rings through one site and a future arm is covered by setting the flag. A retry arm, a branch reached only on a user Stop, and the nested-turn (`_prompt_depth > 0`) arm whose parent may absorb the death all leave `died_notice` unset and stay silent; an app-owned chat (`slot._app`) is skipped, since an app owns its chat's continuation. The note is built with `payload_from_legacy(channel="system.turn", ...)` and pushed on `state.notification_bus` directly — the way every other legacy producer stacks a note (`member_message_notes`, `terminal_lifecycle`, `member_goals`), since `group_key` is a payload field the bus drops from `meta` and a second way to pass it through the adapter would be a duplicate mechanism. The legacy `kind` is `agent` — NOT `turn`, which is the frontend's conversation-ready sound category (`presetForKind`), so the channel's own mute/priority govern the sound rather than the hand-back chime. It carries `url=/chat?sid=<slot>` (the deep link), `meta={"slot": <slot>}` so the banner gate scopes to the dying chat, `group_key=turn-died:<session>` so one session's repeated deaths stack into one row, and `publisher_session=<slot key>` so a muted session's alert is delivered silenced — the honest form of "only when it is not the session in focus", without the backend tracking per-client focus. It keys on the slot, not the effective session key, because `session_is_muted` resolves the publisher through `caller_slot_key`, which matches a slot's key but never an unbound channel-born slot's `dashboard:`-prefixed session key. Default priority, not protected; Settings → Notifications labels it "Chat errors" (`CHANNEL_LABELS`), because the bare id "turn" reads as the conversation-handoff sound and "stopped" alone does not say the chat failed. `test_turn_died_notify.py` pins the channel, the schema-field `group_key` through the real bus, the slot meta, the deep link, the app-owned skip, the swallowed delivery failure, the blank-session case, and each copy id's English against the catalog.

Dashboard-user settings routes (in `dashboard/messaging_api/notifications.py`, beside the feed routes) expose the union of registered channels and stored settings through `api_notification_channels`, beside `bridge_transports` (the transports `BridgeDispatcher.transports` has a registered sink for, in `BRIDGE_TRANSPORTS` order); `api_notification_channel_settings` accepts mute, priority and bridge-route updates, clears an override for `priority: null` and a route for `deliver_to: null` or `[]`, and broadcasts `notification_channel_settings`. Settings → Notifications → Sources sends one such PUT per change; a channel's PUTs go out one at a time in click order, each once the previous one for that channel has answered, so the server keeps the later click, while other channels' saves do not wait, and only the latest of a channel's saves re-reads the list when it settles (`NotificationsPanel.channels.test.tsx`).

### Notification bridge (egress seam)

The bridge sends notes to chat transports as owner DMs ([RFC](../../request-for-change/rfc-notification-bridge.md), tracking #589). The bus sink is `notifications.bridge.composite_sink(DashboardState._deliver_note, DashboardState._bridge_note)`: the local sink runs first and synchronously, unchanged, and its failure propagates as before; the bridge runs after, and a bridge failure is logged and never reaches the producer's `push`. `BridgeDispatcher.schedule` matches the route after `ChannelSettings.apply` and the session mute have shaped the note, so the EFFECTIVE priority decides it and a `silenced` note never routes. On a worker thread the local sink queues its delivery on the serving loop and the bridge queues behind it; with no running loop nothing is bridged. Each routed transport gets its own tracked task and its own deep copy of the note, so a slow or failing transport delays or fails no other and never touches the stored note.

The route lives in `ChannelSettings` as two optional fields: `deliver_to`, transport ids from the known set `BRIDGE_TRANSPORTS` (`slack`, `discord`, `telegram`, `webex`, `wecom`, `feishu`), stored deduplicated in order; and `deliver_min_priority` (`critical`, `default`, `all`; `all` adds passive notes), read as `critical` when absent. `update` refuses an unknown transport or floor and a floor with no route, and `deliver_to: []` clears the route and its floor. `parse_imported_settings` drops and counts the same values. `ChannelSettings.apply` never reads the route. Validation is against the known set, not the connected one, so a stored route survives a transport that is down. Transports plug in as `BridgeSink` objects through `register_sink`; a routed transport with no sink is skipped. Loop safety: the bridge holds no reference to the bus and calls only its sinks, so a bridged delivery never becomes a note. `test_notification_bridge.py` pins the composite order, the effective-priority match, the per-transport isolation, the route validation on the store, the import and the PUT, and the no-bus invariant.

The route is edited in Settings → Notifications → Sources (`website/src/pages/settings/NotificationsBridgeRoute.tsx`, mounted on every channel row, the protected approval channel included). Its "Deliver to" checkboxes offer a transport only when `dashboard.status.channels` reports it connected (Slack falls back to `slack_connected` for a gateway that sends no map) AND the channel list's `bridge_transports` names it, so no destination is offered that the dispatcher would skip for want of a sink; a gateway that sends no `bridge_transports` offers nothing new. A transport already in the stored route always shows, and stays checked and reads "unavailable" when it is down or has no sink, so a route never drops from the row while the backend still holds it and the user can still remove it. A row with nothing to offer draws no control. The floor picker appears once a transport is picked and shows `critical` for a route saved without one. Both save through `api_notification_channel_settings` with the panel's optimistic, field-scoped rollback; emptying the route sends `deliver_to: []` and drops the floor with it. `NotificationsPanel.bridgeRoute.test.tsx` pins the connected-and-deliverable list, the retained unavailable transport, the floor, the clear and the rollback.

Every delivery runs one governed pipeline in `BridgeDispatcher._deliver_one`, so no sink can skip it (`notifications/bridge_egress.py`). Governance first: `vet_and_audit` for `capabilities.messaging` and `channels/<transport>`, fail-closed, on `HOST_SESSION_KEY`, on every session the note names, and on the publishing app when its `source` is `app:<slug>` (`vet_and_audit(..., app=<slug>)`). The sessions come from the dispatcher's `subjects` resolver, `dashboard_services.bridge_subjects`: the publisher (a session key or a bare slot key) and the note's `session_key`, `slot` and `history_key` fields (a note offering choices drops its publisher but keeps its slot link; a turn-ceiling notice names its session only in `session_key`), each resolved to its slot's `effective_session_key`; a session key no slot matches is vetted as written, so a name can only add a denial, and a bare slot key no slot holds (a slot closed while its note was in flight) makes the note not bridged. A keyed note is bridged only after its replacement commits: the dispatcher's `committed` hook hands back `last_notification_persist` for a keyed note, and a failed rewrite sends nothing. Any denial denies, an evaluation error denies, and each decision is a SEL record. Then a per-transport `AppRateLimiter` bucket (the app-ingress budget: 30 per 5 minutes, burst 10). Then `redact_exfiltration_urls` and `redact_credentials`, in that order, on the title and body of a copy. Then the sink. The local sink pops `publisher_session`, so `composite_sink` reads it before the local sink runs and passes it to `BridgeDispatcher.schedule(publisher=...)` as an argument; it never rides on the note a sink receives. Each outcome writes one `log_tool_invocation` record named `notification_bridge:<transport>`, attributed to the publisher or the host: `completed`, `denied`, `rate_limited`, `skipped` (the sink raised `BridgeTransportUnavailable`), or `error` (exception type only). A skip refunds the token; an error keeps it, because a failed call may still have posted. The Slack sink (`notifications/bridge_slack.py`, registered by `DashboardState`) reads `slack_client` and `owner_id` off the state at delivery time, opens the owner DM through `slack.retry.open_dm_with_retry`, and posts escaped mrkdwn: an optional `:rotating_light:` marker for a critical note, the bold title, the body and the deep-link path as text. `render_slack_parts` slices the body and link before escaping, so no slice cuts an entity and each message stays under `SLACK_MAX_TEXT` however much escaping grows it; the parts post in order, and a failed part makes the delivery an error. A missing client, owner or DM channel is a skip. `test_notification_bridge_slack.py` pins the redacted owner DM and its paired records, both governance subjects, fail-closed denial, the skip, the error, the budget and the escaping.

Every other bridge transport is a `MessagingTransport` in `DashboardState.channel_transports`, served by one sink class, `notifications/bridge_transport.TransportBridgeSink`; `DashboardState` registers one per id in `TRANSPORT_SINKS` (`discord`, `telegram`, `webex`, `feishu`). It renders compact markdown (an optional 🚨 marker for a critical note, the bold title, the body, the deep-link path) and hands it to the proactive owner-DM leg `_deliver_channel_dm`, which takes the owner from the transport's own configured targets (`sole_direct_target`), re-runs the `channels` gate on the shared send ladder, checks the recipient, applies the display-safe floor and chunks at the transport's own limit. A transport that is not registered, declares `supports_proactive_send=False`, names no single owner DM, or sits outside `constants.CHANNEL_OWNER_DM_NAMESPACES` (whose configured targets may hold learned peers, so the owner cannot be inferred: `weixin`, `wecom`) is an audited skip, and a test pins every `TRANSPORT_SINKS` id inside that roster; a refused or failed send is an audited error. `test_notification_bridge_transport.py` drives each id in `TRANSPORT_SINKS` on its real transport class with only the platform client faked.

Bridge actions (RFC `rfc-notification-bridge-actions.md`, phase B4a) start with a core of their own, which only the Slack path below reaches (B4c): `notifications/bridge_actions.BridgeActionRegistry`. `bind(session_key, request_id, decision, choices=..., ttl_s=...)` binds a live decision future and returns the shipped Slack approval token (`slack.approval_tokens.build_approval_token`, the session-scoped key plus a fresh nonce), with `choices` mapping each answer to the value the decision future takes for it. A bridged prompt offers Approve and Deny only (`BRIDGED_CHOICES`), so a bind offering any other answer, `trust` included, or mapping an answer to the `TRUST` verdict, is refused. The nonce is compared through the shared `messaging.renderer.nonce_eq`, so a malformed forged token answers `unknown` rather than raising. `ttl_s` is required, finite and positive: the deadline is per surface (the Slack race on `DashboardState._APPROVAL_TIMEOUT` / `_BACKGROUND_APPROVAL_TIMEOUT_SECS`, the reply path on `messaging.approval.TEXT_APPROVAL_TIMEOUT_S`), and every bridged expiry is measured on the one seam `bridge_clock()`. `link_message` / `token_for_message` record which posted message carries the token. `resolve(token, choice, from_owner=...)` returns an `ActionOutcome`: `unknown` for a token with no live binding or a wrong nonce (forged, retired by a rebind, or after a restart, since the registry is in-process), `not_owner` without consuming the binding, `already_answered` once the decision resolved on any surface, `expired` past the deadline or once its waiter timed out (a cancelled future), `invalid_choice` for an answer the prompt does not offer (a `trust` reply included), and `admitted` when it sets the decision future. `from_owner` is the caller's owner check: Slack binds through `slack.handler_runtime.access.is_owner`, every other transport must be in `constants.CHANNEL_OWNER_DM_NAMESPACES` and name its owner through `sole_direct_target`. A binding lives until its deadline and is pruned on the next bind, so a second press reads as already answered. `test_notification_bridge_actions.py` pins each outcome by driving `bridge_clock`, and that no module outside the two B4 modules imports them. Phase B4b audits every attempt: `bridge_action_audit.audit_action(registry, token, outcome, transport=..., choice=..., actor=..., channel=...)` (`actor` is who acted, so a refused stranger is recorded) writes a crew-log `approval/bridged` entry, whose `outcome` is the RFC's word (`approved`, `denied`, `expired` or `refused`) and whose `reason` is the `ActionOutcome` value when the action was not admitted, under the crew-log session and turn the binding was given (`bind(..., crew_log_session=, turn=)`, read back through `context_for`), and an SEL `log_api_access` record (`notification_bridge.action:<transport>`) attributed to the bound session, or to the host for a token that names no binding, which gets the SEL record alone. Both records carry the bound `owner` (`bind(..., owner=)`) beside the `actor`, and an admitted action is recorded with the answer the registry admitted, not the caller's word. A binding pruned past its deadline is remembered for its audit (the newest `EXPIRED_MEMORY`), so a late press on it resolves `expired` and is audited against its own session rather than as a forgery. An admitted action writes no `approval/decided` of its own: it resolved the decision future, and the waiter on that future writes the same entry it writes for any surface. Neither write raises. `test_notification_bridge_action_audit.py` drives the real crew-log writer.

Phase B4p gives the bridge a pending approval to carry, behind the default-off feature preview `dashboard.feature_previews.bridge_approvals` (Settings > Developer > Feature previews, "Send pending approvals to chat apps"). When a tool-approval card opens and the prompt is about to wait for it (unanswered, and the turn has budget left to wait; a no-budget decline is never bridged), `dashboard/chat_turn/permission_prompt.py` calls `notifications/bridge_approval_note.schedule_pending_approval`, which starts the hand-off as its own task and is never awaited, so no new await sits between the approval's registration and the `try` that cleans it up. The task is held until done (the gateway's `_background_tasks`, else the module's own set) and any failure is logged. It reads the live config off the loop, skips an approval answered in the meantime, and calls `publish_pending_approval`, which checks the preview and hands one `system.approval` note straight to `DashboardState._bridge_note`: the bridge is its only sink, so the dashboard bell never sees it and keeps ringing only when an approval expires. The note names the request (`approval_request_id`, `slot`, `session_key`) and its producer (`producer: tool_approval`), which is how it is told apart from the config-pending review note on the same channel. When the card was already posted into a linked Slack thread the note says so (`approval_slack_card`), and `SlackBridgeSink` posts nothing for it, so the linked card stays the one to answer; the sink raises `BridgeTransportUnavailable`, so the dispatcher audits it as `skipped` and keeps the rate-limit token. That card has its own buttons, so Slack gets no second copy, but the approval is still bound (B4c) so a reply transport can answer it (B4d). With the preview off nothing is published. `test_notification_bridge_approval_note.py` pins each of these.

Phase B4c lets the owner answer that note from Slack, behind the same preview. The hand-off binds the open approval in `DashboardState.bridge_actions`, the gateway's one in-process `BridgeActionRegistry` (`bridge_approval_note.bind_pending_approval`): choices `DECISION_VALUES` (`approve` → `approved`, `deny` → `rejected`, the values the dashboard's own buttons set), `owner` the configured `owner_id`, the crew-log session and turn the prompt passes, and a deadline of the prompt's own wait window (`permission_prompt` passes `ttl_s=_approval_window`, measured from the prompt on `bridge_clock`, so a slow config read shortens the binding rather than outliving the wait). The token is the shipped approval token with a `secrets.token_urlsafe(16)` nonce; it rides in its own note field, `bridge_action_token`, never in the title or body a sink shows (egress redaction reads only those two). No binding is made with the preview off, no owner, a passed deadline, a deny-rule override card or an unverified child request (`answerable=False`: a plain Approve refuses an override's call, so the dashboard alone answers it, as the Slack mirror already does, and a low-fidelity child's text is agent-authored, which the dashboard row warns about), or nothing to show of the call, and then the note carries no buttons. A bound note also carries `approval_detail`, what the call will run (`event.tool_input`, else its title), so the owner approves what they can read; only the Slack sink renders it. `SlackBridgeSink` posts a note carrying a token and a detail with its last part as a section (a last part longer than Slack's 3,000-character section limit is posted as its own message first, because with blocks present Slack shows a message's text only as the notification fallback, and the buttons then follow under the fallback text; a pending question is posted the same way), the detail redacted and then drawn by the native approval card's own renderer (`slack/approval_card.approval_detail_blocks`: mrkdwn-escaped, backtick runs broken so nothing closes the fence, cut to the section limit), and an actions block below (`bridge_slack.action_blocks`: `bridge_action_approve` and `bridge_action_deny`, the token as each button's `value`, nowhere in text), and links the posted message (`link_message("slack", "<channel>:<ts>", token)`). A press is routed by `slack.interactions.dispatch`, after its allow-list check and ahead of the native approval fallthrough, to `interactions_approvals._handle_bridge_action`. It fails closed: it reads the live preview off the loop (`bridge_actions.actions_enabled()`, off on any read failure, so turning the preview off stops a press on a button already posted), then needs the registry, a string token and a user id, then applies the inbound `channels` gate; any of these failing answers nothing and audits nothing. It then resolves with `from_owner=is_owner(user_id)`, where `user_id` is the payload's `user.id` that Slack writes on the app-token Socket Mode connection, never text in the message, and audits every outcome through `audit_action(..., transport="slack", choice=, actor=, channel=)`, a stranger's or stale press included. A stranger's press changes nothing and is told nothing; the owner's stale press (already answered or expired) gets a private `BRIDGE_ACTION_STALE_NOTICE`. `test_slack_bridge_action_press.py` drives that path end to end, from the hand-off through the real sink to a press through `dispatch`, and `test_notification_bridge_actions.py` pins that only the approval note, the Slack sink and the press handler reach the registry.

Phase B4d lets the owner answer the same note by replying on Telegram, the first reply transport (`bridge_transport.REPLY_TRANSPORTS`), behind the same preview and the same binding: one token per approval, so a Slack press, a Telegram reply and the dashboard settle one decision future and every other surface then reads it answered. The binding's recorded owner is Slack's `owner_id`, else the reply transport's owner DM (`bridge_transport.reply_owner`: in `CHANNEL_OWNER_DM_NAMESPACES`, exactly one available non-learned `user:` target through `sole_direct_target`, read live), so a Telegram-only gateway still binds. `TransportBridgeSink` adds, for a bound note on a reply transport whose owner is known, what the call will run and "Reply approve or deny to this message" (`render_reply_text`, off the loop: the detail display-scrubbed first, so the cut can never sever a credential, then collapsed to one line and cut as Telegram's own approval card cuts it, every backtick followed by a zero-width space so input can neither close the code fence nor render as markup; the cut shrinks until the whole note renders to one Telegram message, so a reply can only quote the message carrying the prompt, and below `_MIN_SHOWN` characters of a cut detail the note carries no prompt and no link). A card already in a linked Slack thread is bound too: Slack posts nothing for it, and the reply transport still answers the same decision. The sink then links the delivered message by `<chat id>:<message id>`, which `_deliver_channel_dm` reports through its optional `posted` list, and only when the note went out as exactly one message: a part without the prompt is not consent, so a note the leg still split answers nothing. `TelegramClient._build_inbound` reads the quoted message's id into `reply_to_message_id`, and `TelegramDispatcher.handle_message` hands the message, after callback admission and the inbound `channels` gate and before any session routing, to `telegram/dispatch/bridge_reply.consume_bridged_reply`, except a message its parser reads as `/stop`, which is never an answer: it stops the running reply instead. That takes a message only when it is a reply, in a private chat, to a message the registry still links under that chat; a group reply, another chat's reply, a reply to any other message and a plain message go on to the ordinary turn path untouched. A message it takes starts no turn. It reads the live preview off the loop (off answers and audits nothing), then resolves with `from_owner` true only when the sender's `from.id` and the private chat id both equal the owner DM's id, never a username or display name. Only an exact `approve` or `deny` (trimmed, case-folded) answers, mapped to `approved` / `rejected` for this one request; `trust`, `always`, `3` and every other word resolve `invalid_choice`, so no reply reaches the `TRUST` verdict. Every outcome is audited through `audit_action(..., transport="telegram", choice=, actor=, channel=)`, the answer recorded as the word when it is a short alphanumeric one and `other` otherwise. A stranger is told nothing; the owner gets "Approved." / "Denied.", "Reply approve or deny to answer this request." for another word, or the stale notice for a reply that is a second one, settled elsewhere or past the deadline. A link outlives its binding while the registry remembers the expired binding (`EXPIRED_MEMORY`), so a late reply resolves `expired` and is audited rather than read as an ordinary message. `test_telegram_bridge_reply.py` drives it end to end through the real dispatcher, the real Telegram transport and the real delivery leg.

Phase B4e lets the owner answer an agent's blocking `ask_question` card from Slack, behind the same preview. When `POST /api/agent-ask/open` shows a card to at least one client, `dashboard/handlers/ask_question.api_agent_ask_open` calls `notifications/bridge_question_note.schedule_pending_question`, scheduled and never awaited. The task reads the live config off the loop, skips a card already settled, and publishes one bridge-only `system.approval` note (producer `agent_question`, `ask_id`, `slot`, `session_key`), so the bell never sees it; it is told apart from the approval note on the same channel by its producer, and every such note links to the chat (`/chat?sid=<slot>`, the parameter the dashboard selects a session by), where the full card, free text included, is answered. A card with exactly one single-select question (`answerable_options`) is bound in `DashboardState.bridge_actions` as `kind="question"`: choices `option_0`… (`bridge_actions.QUESTION_CHOICES`, by option position), each mapped to `{question: option label}`, the answer the dashboard submits for that option; the owner, Slack's `owner_id`; the card's window (`_AGENT_ASK_WINDOW_DEFAULT`). A question binding resolves through `settle`, which calls `DashboardState.resolve_question` instead of setting the future, so the card is retired on every screen and its shared decision settled exactly as a dashboard answer would; `settle` returning False (already settled) reads `already_answered`. The registry refuses a question binding without `settle`, an approval binding with one, and any choice outside its kind's set, so an `approve` on a question token, or an `option_*` on an approval token, resolves `invalid_choice`. Any other card (several questions, multi-select) gets no binding. `SlackBridgeSink` posts a bound question note with its text as a section and one `plain_text` button per option (`bridge_slack.question_blocks`: `bridge_action_option_<i>`, the token as each `value`, labels redacted and cut to Slack's button limit by `button_label`), and links the message. When two options would read the same on their buttons (the cut can leave two long labels one shared prefix), the note posts with no buttons, since the owner could not tell which answer a press sends; the dashboard link answers it. The press goes through the same `interactions_approvals._handle_bridge_action` as B4c, with its preview read, owner check on the signed `user.id`, `channels` gate and audit; a question binding has no crew-log session, so its audit is the SEL record alone. `test_slack_bridge_question_press.py` drives it end to end on a real `DashboardState`.

Phase B4g makes Discord the second reply transport (`REPLY_TRANSPORTS` is `("discord", "telegram")`), behind the same preview and the same binding. The decision a reply makes now lives in one place, `notifications/bridge_reply.answer_bridged_reply`, and each transport's reader only decides whether a message is a candidate and how to pin its owner DM: `telegram/dispatch/bridge_reply` (a reply in a private chat; the chat id equals the owner's id) and `discord/bridge_reply` (a reply outside any thread; the channel's DM pairing, `DiscordTransport.direct_peer_of`, names the owner, because a Discord DM channel id is unrelated to the user id). Everything B4d pins holds for both: the live preview, `from_owner` only for the sender's own user id against `reply_owner`, only `approve` / `deny` for this one request, every outcome audited under the transport's own name, and a stranger told nothing. `DiscordClient` reads the quoted message's id from a REPLY-type (19) message's `message_reference` into `DiscordInbound.reply_to_message_id`, and `DiscordDispatcher.handle_message` hands the message to the reader after callback admission and the inbound `channels` gate and before any session routing, except a `!stop` or a message carrying a file, which stays content for a turn; it reads the gateway state from the session-resume controller, where the Discord gateway wires it. `render_reply_text` takes the transport: the note must fit one Telegram message as rendered HTML (3,500 characters), or one Discord message as sent (1,800 characters after the delivery leg's display pass, whose mention defang lengthens it, below the leg's 1,900 chunk), so a reply can only quote the message carrying the prompt. `test_discord_bridge_reply.py` drives it end to end through the real dispatcher, the real Discord transport and the real delivery leg. Webex is the third reply transport (`REPLY_TRANSPORTS` gains `"webex"`). Webex has no quoted reply: a reply is a message in the thread the note started, so its `parentId` names the note (threads are flat, so any reply in that thread does). The bridge addresses the owner's DM by email (`WebexTransport.resolve_conversation`; Webex opens the 1:1 space from `toPersonEmail`), so a posted note is linked under `<email>:<message id>`, and `webex/bridge_reply` looks a reply up under its sender's lower-cased `personEmail`, only in a direct room; the owner is that email against the one `user:<email>` target, the identity the email allow-list authorizes on. `WebexDispatcher.handle_message` hands it the message after the inbound `channels` gate and before any command or session routing, and before its native approval intercept (which matches a `1` / `approve` by sender alone, so a reply in a bridged note's thread answers that note, never a native prompt open in the same DM), except a `/stop`, a message carrying a file, or a card press. Unlike a quoted reply the thread stays open, so the reader takes only an exact `approve` or `deny` (trimmed, case-folded); every other message in the thread, a follow-up, a command or `trust` included, goes on to the ordinary turn path unaudited. The note must fit one Webex message in UTF-8 bytes after the same display pass (6,000, below `WEBEX_MAX_TEXT`'s 7,000). `test_webex_bridge_reply.py` drives it end to end through the real dispatcher (wired by `register_channel_transport`, as the gateway does), the real Webex transport and the real delivery leg. The reply transports also answer an agent's `ask_question` card (B4g): `bridge_question_note.bind_pending_question` now binds for `bridge_transport.bound_owner` (Slack's `owner_id`, else a reply transport's owner DM, the rule the approval note already used), and `TransportBridgeSink` shows a bound card's options numbered from 1 inside the same code fence (`1. main · 2. …`), with "Reply with the number of your answer to this message.", only when every option shows whole (no cut, and no label carrying the ` · ` separator, which could read as another numbered option); otherwise the card is answered in the dashboard or on Slack. `ActionContext` now records the binding's `kind`, so `answer_bridged_reply` reads a reply to a question as the number `n` (ASCII digits, no leading zero) mapped to `option_<n-1>`, which the registry admits only when the card offers it, and tells the owner "Answered." or to reply with a number; an approval reply reads exactly as before. On Webex, whose thread stays open, a question's thread takes only such a number and an approval's only `approve` / `deny`. `test_bridge_question_reply.py` drives it on all three transports.

## Agent notifications and expiration

`mcp_tools.messaging.send_notification` requires a verified caller identity, applies the messaging governance gate, and denies channel-agent callers. `dashboard.handlers.messaging.api_notification_agent_push` fixes agent notes to the `system.agent` channel and server-derived source before `NotificationPayload` validation. The agent endpoint and the app push handler both await queued persistence before returning success.

### Keyed notes: update and dismiss

A note can carry a `key` that names what it tracks, so a producer can change or withdraw it as that thing moves on, the way Android's `notify(id)` and `cancel(id)` do. `NotificationPayload.key` is a non-empty string of at most `MAX_SHORT_STRING` characters and is in `_RESERVED_NOTE_KEYS`, so `meta` cannot set it. A key names a note within its `source`. `NotificationCoordinator.deliver` publishes every keyed note under the state's lazily created full-rewrite lock, whether or not a note holds its key yet, so a later replacement or dismissal by the same publisher queues behind it. It snapshots committed memory inside the lock, builds a new row list without mutating memory, and rewrites the file on the notification FIFO executor. Only a successful write commits the same keyed delta to current memory, applies the ring cap, and broadcasts the replacement. A keyed note takes its place by arrival, after the notes held when its write started, so current memory and a reloaded file agree. While a keyed write is in flight, an unkeyed note is held without trimming; the keyed commit applies the cap so memory and a reload keep the same notes, and a replacement broadcast carries the `quiet` decision written with the replacement. A failed write changes and broadcasts nothing, so the operation remains retryable and the agent route returns `500`. The replacement is `quiet` unless its priority outranks a note actually replaced (`critical` > `default` > `passive`). Every note that actually replaced one also carries `replaced: true`, written and broadcast with it, so a reload keeps it; `NotificationFeed` shows an "edited" label on the row's time line, before the time (a collapsed deck's head included, apart from its count), through `NotificationCard`'s `timeNote` so the in-app banner never shows it, and names the row "Open edited notification" for a screen reader (a collapsed deck's head keeps its "Expand N grouped notifications" name), so a person who read the old row does not take the replacement for a new note. Every other full rewrite, including ack, unack, delete, clear, ack-all and skill-review updates, uses the same lock and takes its snapshot inside it. An unkeyed note keeps the append path: it mutates memory and submits its append in one synchronous step, while a locked rewrite snapshots memory and submits its rewrite in one synchronous step, so the file ends as the latest snapshot plus appends submitted after it. A note delivered from a worker thread while the gateway serves (`asyncio.to_thread(state.notify, ...)`) is handed to the serving loop first, so its append joins the same FIFO instead of writing the file from the thread. `_rewrite_notifications` carries the file's bounded window of unservable lines forward, bytes intact and ahead of the rows it writes, so no rewrite deletes what the store keeps on ambiguity, and writes through `atomic_write`, so a write that fails part-way leaves the previous file in place. `quiet` and `replaced` are also reserved. Clients mirror replacement in `addNotification`, which drops the item with the same `source` and `key`. Each keyed arrival, each `notification_dismiss` frame (`dismissNotificationByTs`) and each confirmed delete (`deleteNotification`) advances `membershipSeq` and stamps the notes it added or withdrew; `fetchNotifications` records the sequence its request started under, and its merge drops rows withdrawn since and keeps keyed notes that arrived since, so a snapshot taken before a replacement, dismissal or delete cannot bring the old note back or hide the new one. The socket applies `notification_dismiss` through `applyNotificationDismiss`, which refetches the list when the view held `NOTIFICATIONS_RING_CAP` notes: an unkeyed note that arrived while the dismissal was being written made a full view evict its oldest note, which the server, holding that arrival untrimmed, kept. That refetch keeps the rows the view holds (`fetchNotifications({ keepHeld: true })`): a row the response does not list stays, so a local approval row, which the server never stores, and a note that arrived while the request was out survive it, while a held keyed note whose replacement the response lists goes; the ring cap then trims as usual. A replacement needs no refetch: the server's commit and the view's cap leave both with the same notes. The socket layer plays no sound and dispatches no new live banner for a quiet note, and `useNativeNotification` does not toast it. `NotificationBanner` also watches committed keyed membership in the store and retires a pending keyed card after a quiet replacement or `notification_dismiss`; unkeyed cards are unaffected. The native-notification hook toasts a note the first time it sees it rather than when the unread count rises, because a replacement swaps one unread note for another and leaves the count unchanged, and the gateway's `native_notification_tag` derives a keyed note's tag from its source and key, so a replacement takes over the earlier note's banner, on the open tab and in Web Push alike. A keyed note reaches Web Push only once its write commits, and a quiet replacement does not reach it. A quiet note is still unread and still counts in the bell badge.

`send_notification` takes the key, and `POST /api/notifications/agent` passes it to the bus. `dismiss_notification` posts `{key}` to `POST /api/notifications/agent/dismiss`, which refuses the same callers the publish route refuses (an app, a caller without the internal secret, a caller whose slot is gone; `_refuse_unattributable_caller`) and calls `DashboardState.dismiss_notification(source="system", key=...)`. It refuses a key that is missing, not a string, blank or longer than publish accepts (`MAX_SHORT_STRING`, the cap on `NotificationPayload.key`) with `400` and code `invalid_key`, so a key no publish could have stored is refused rather than reported as matching nothing. The coordinator runs dismissal under the full-rewrite lock: it builds and writes a row list without the held note, then removes that note from memory and broadcasts `notification_dismiss` only after the write succeeds. A failed rewrite returns `500` with code `persist_failed`, changes nothing and broadcasts nothing, so a retry still finds the note. Both routes refuse a key that delivery redaction would change (`_refuse_redactable_key`, `400` with code `key_would_be_redacted`): the stored key is what a later publish or dismiss matches, so a key that looks like a credential or a URL would be stored as its redaction, two keys could collapse into one, and the original could never be dismissed. That check is the module's only redactor call, so `security_posture.NON_EGRESS_REDACTION_MODULES` lists `agent_push.py` as a gate, not an egress sink. The frame carries only a `ts`, so `ws_event_scope` lists it with the unattributed notification events. Agent keys are one namespace for every session and cron the person runs: a cron run or a re-armed watch is a new session, so a key scoped to one session could not be updated by the next. A key is a way for one person's sessions to cooperate on a note, not an isolation boundary between them: every agent session acts for the same person, and any of them can already publish any note on `system.agent`. What a key adds is the ability to change or withdraw an agent note that was itself published with a key. Every other note is out of reach, because only `api_notification_agent_push` sets `NotificationPayload.key`: approvals, cron results, monitor and heartbeat notes, crewmate messages and unkeyed agent notes carry none, and an app's notes have another `source`. App sessions and channel agents can reach neither route. App pushes and the other system producers set no key. `test_notification_keys.py` pins the replacement, the quiet rule, the `replaced` stamp, the rewrite, the reserved keys, both routes, their refusals and the tool.

### Link back to the publishing session

A note from either agent route names the session that published it, so the detail panel can open it. `api_notification_agent_push` and the `send_message` bell fallback set the note's `slot` and `history_key` from `_publishing_session_meta` on the same attested key `member` uses (`members.attributable_session_key`), so an app or unattested caller gets no link and no request body can choose one. The resolver answers the key's own slot (`dashboard_slot_key`, which maps a cron run to its `cron-<id>` tab) while `DashboardState.get_slot` holds it, with that slot's transcript key (`slot_history_key`) as `history_key`, and otherwise neither. A cron run with no tab of its own does not borrow the tab of the session that created the cron: the banner treats a note whose `slot` is the focused chat as already on screen, and that chat never shows the cron's output. The fallback links a message that offers `[OPTIONS:]` choices too, although it passes no `publisher_session` for one. `NotificationDetailPanel` shows **Go to chat** while that slot is open, as it does for a heartbeat note. The chat page does not render a Bot's DM thread, so both buttons on a note from one open `/members?member=<name>`, the page that thread's sidebar row opens; the name is the open slot's `agent`, or the note's `member` once the thread has closed, and the Members page holds that Bot's one thread. An open thread whose slot names no Bot opens the bare Members page, as its row does. Otherwise, once the slot closes, **Resume chat** resumes with the note's `history_key`, and with `slot` for a note that has none: a bare slot name names no transcript, so a dashboard chat resumed by it opens empty. A cron tab resumes from its `cron:<id>` transcript, as the History tab resumes it. While the window is focused on that chat, the banner skips the note (see Suppression). `test_notification_session_link.py` pins the resolution, the attestation gate, the body refusal, both routes and a closed session's resume; `NotificationDetailPanel.resumeHistory.test.tsx` pins the key the panel resumes with, and `NotificationDetailPanel.memberNavigation.test.tsx` pins where a Bot's note opens.

### Bot attribution

A note published from a Bot's pinned DM session carries `member: {slug, name}`. `members.member_identity_for_session` derives it from the caller's `X-Session-Key` and the slot's live `dm.json` binding, so the name is the binding's member, never the lossy slug; any other session, or a key whose binding is gone, yields no `member`. The key counts only through `members.attributable_session_key`: the request must not be an app caller (`request["app"]`), and `member_memory_auth.session_key_is_attested` must bind the key to the caller, either by the kernel peer attestation on the Unix socket (`peer_verified`) or by a signed per-session `X-Session-Token` that verifies to that key (what Kiro Crew's own pooled tool backends send on TCP loopback). A bare header is the caller's own word, so an app token, or a caller holding only the internal secret, that declares a Bot's DM key gets an unattributed note. `api_notification_agent_push` (the `send_notification` path) and the `send_message` bell fallback in `messaging_api/proactive_send.py` both stamp it. `member` is a `NotificationPayload` schema field with a closed `{slug, name}` shape and is in `_RESERVED_NOTE_KEYS`, so neither a request body nor an app push's `meta` can attach a Bot's identity to a note. `test_notification_crewmate_member.py` pins the derivation, the transport gate on both producers, the body and meta refusals, and both producers.

`website/src/components/notifications/CrewmateNoteFace.tsx` draws the attribution on `NotificationCard` (bell popover and banner), the panel `NotificationFeed` row and `NotificationDetailPanel`: the Bot's avatar replaces the kind icon and its display label sits above the title. Face and label come from the row of `membersRosterQuery` (the Bots page's query, invalidated by the gateway's `refresh` frame and by every crew save) whose slug and exact name match the note's `member`; while that query loads, or when no such row exists, the kind icon renders and no label shows. When that query fails, `CrewmateRosterError` renders one inline `ErrorNotice` with a Retry that refetches the roster: in a Bot note's detail panel, and at the top of a feed showing a Bot note unless the selected note is a Bot note (its open detail panel already reports it), and in the footer of the first full `NotificationBanner` card showing a Bot note (deck shells draw no face, and the banner is suppressed while the popover or the inbox is on screen, so it never repeats their notice). The notice carries no agent hand-off because all three surfaces are overlays over a page that may hold an unsaved draft; a feed with no Bot note never reads the roster. `NotificationCrewmateFace.test.tsx` pins these states.

### Bot messages

A Bot's message in its DM thread is a notification of its own: `dashboard/member_message_notes.py` publishes one note per landed turn in a member DM slot. The chat runner calls `post_turn_reply` once the turn's reply is saved, with the row ids it recorded as the turn's own replies (`_note_reply_row`, `slot._turn_reply_mids`); the note's body is the newest of those rows that is `assistant` speech (`system_notices.is_speech_row`, the predicate the thread draws by, so the bare U+200B a quiet check-in turn ends on is not a message). Two consequences: an `assistant` row the gateway wrote into the thread (a compaction notice, a cleared-conversation line, a slash command's reply, an injected workflow or cron result) is never a Bot's message, and a turn that appends one reply row per model call (the text before each tool call) pings once, with its answer. A turn the user stopped, and a turn about to re-run (promise, compaction, leak or infrastructure recovery), publish nothing; the re-run's reply is the answer. Tool calls, tool results, injected, subagent and streamed rows never notify. The note goes on the `system.member` channel, labelled "Bot messages" in Settings -> Notifications. Its default priority is `passive`: the note lands in the bell feed with no banner, sound, OS toast or unread count, and the user can raise the channel (to `default` for all four) or mute it on its own there. Muting `system.agent` does not silence it. The note keeps the legacy `kind` `agent`, as `system.monitor` notes do. `member` comes from `members.member_identity_for_session` on the slot key, read off the event loop before the note is pushed back on it, and a member slot with no live binding publishes nothing. The title is the Bot's name, so the card omits the small mate-name label it draws above a title that already says it (`CrewmateNoteName` with `title`), and the body is the message text (capped by `payload_from_legacy`, redacted on delivery); `url` is `/members?member=<exact name>`, `slot` names the thread, `group_key` is the slot key so one Bot's messages stack, and `publisher_session` is the slot, so a muted thread's note is delivered silenced. A message the user is watching is still published; the banner decides whether to show it (see Suppression). `test_member_message_notes.py` pins the producer, the roles, rows and slots that publish nothing, the channel and its passive default, one note for a real `_run_chat` turn with a tool call between two segments, and none for a turn ending in a cancelled stop.

`DashboardState.sweep_expired_notifications` removes only passive notes with a positive integer `ttl` whose parseable timestamp has elapsed; ambiguous timestamps and other priorities remain. `DashboardState` invokes the sweep while loading persisted history and before each delivery. The in-memory sweep becomes durable on a later full rewrite, so already-open clients retain an expired row until their next reload or refresh.

## Inline actions and grouping

`NotificationPayload.validate` accepts action entries with non-empty `id` and `label`, and validates each optional action URL at the persistence trust root. `test_notification_bus.py::test_action_count_capped` and `::test_action_field_lengths_capped` pin action bounds. URL-less actions persist but do not render; `test_action_without_url_accepted` pins that contract.

`website/src/components/notifications/NotificationDetailPanel.tsx` and `NotificationFeed.tsx` render navigation actions only after `safeInternalUrl` rechecks a dashboard-internal URL. Unacknowledged approval feed rows render inline Approve and Reject that resolve through the approvals endpoint (the one-click path `rfc-local-notification-bus.md` Phase 4 shipped). Every approval row -- read or unread, because reading a pending request must not shrink it -- renders the notification body in full through the same markdown renderer and per-item error boundary as the detail panel: no slice, clamp or hidden overflow, because a control that authorizes a command must sit next to the whole command, and a truncated excerpt turns two lines into one harmless-looking line. The producer tags the command fence `approval-command` (`lib/approvalNotificationBody.ts`), a dashboard-own tag `CodeBlock` soft-wraps like `error-report`, so a line wider than the feed column wraps instead of scrolling off the edge. Both surfaces render the body with `readOnlyCode`, so the command carries a copy control but no edit affordance: `EditableCodeBlock`'s scratch editor changes only a local copy, and a pencil beside Approve would let a reader authorize the original command while looking at their edit. A JSON command (a tool call's arguments, such as a Write File's `{path, text}`) opens on the chat approval card's Formatted view (`pages/chat/ToolDetails.tsx::FormattedPayloadCard`, mounted by `MarkdownRenderer` for a closed `approval-command` fence): one row per field, string values JSON-decoded so line breaks and escaped characters show as written, with no height cap. Its Formatted | Raw toggle switches to that same read-only code block, verbatim. A command that does not parse as one whole JSON object (a shell line that starts with `[` or `{`, a cut payload) keeps the code block alone, because the Formatted fallback would turn its `\n` into a line break. The toggle stops its click, so switching views in a feed row does not open the note. `approvalNotificationBody.test.tsx` pins both views. Every other row keeps the flattened one-line excerpt. This contract applies to the full page and bell popover, including the mac feed variant. `NotificationFeed` collapses notes sharing a stack key within a date group to the newest row and expands the stack on demand, the way macOS Notification Center stacks by app. `notificationStacks.stackKeyOf` picks the key: the producer's `group_key` when it set one, else the sender -- the publishing Bot (`member`), then the cron job (`job_id`), then the `channel` narrowed by `session_key` when present. An approval or a critical note without a `group_key` never stacks: an approval is its own decision, and a deck shows only its head's priority, so a critical failure followed by an ordinary note from the same sender would be buried under the ordinary one. A collapsed deck's ⓧ dismisses only its newest note, so the deck shrinks by one, and an expanded stack's rows also close one at a time; clearing a whole stack is the expanded header's two-press clear (or the date header's clear). An expanded stack carries a header row above its newest note, as macOS does: `stackLabelOf` names the sender (the Bot, else a non-system app `source`, else the kind label such as "Cron Job"), and the row holds Show less and the stack's own two-step clear, which deletes every note in the stack; that clear shows the word "Clear" rather than an ⓧ and arms to "Clear this group", so it reads neither as hiding the stack nor as the inbox-wide clear-all. The head card itself carries no Show less, and in the panel variant the "N more" pill appears only on a collapsed stack. Clearing is a two-step control (`NotificationClearButton`, which forwards its ref to the `<button>` so a `uiLocation` spread registers it): the first press turns its idle glyph or label into a confirm capsule whose visible text is also its accessible name, the second clears, and pointer-leave, blur or Escape disarm it, so there is no native `confirm()`. Each date-group header carries its own clear, which deletes that group's visible rows (search and the muted disclosure decide what is visible) except its approvals, read or not, because reading one does not resolve it; they keep waiting on their own Approve/Reject; it arms to "Clear <day>", and a day holding only approvals offers no clear; the controls card's clear-all (a trash can, since an ⓧ in the sheet's top-right corner reads as "close") and the page toolbar's "Clear all" (the same trash can, with visible text, since an ⓧ "Clear" beside the search field reads as "clear the search") empty the inbox after arming to "Clear all", through `/api/notifications/clear`. `NotificationsBellButton` sends the unread attention count through `badge:set`; `electron/badge.js` clamps it before `app.setBadgeCount`.
A failed day, stack or inbox clear, or a failed single-note dismiss (a deck's head included), shows one shared `ErrorNotice` in either feed variant, retains failed rows (restoring a dismissed row's pre-animation styles), and removes the notice after a later successful clear, unless another clear failed while that one was in flight. When a clear unmounts the focused control, focus moves to the open control of the next remaining row (else the previous one), never to `<body>`. Each deletion of a multi-note clear commits separately, so that target is held until every cleared note has left the list, the clear has settled, or the pressed control has unmounted (an expanded stack's header goes when one note is left), not dropped on an intermediate commit while the pressed control is still focused; it is dropped when every deletion failed, when the pressed control survives, or when the user has moved focus to another live element.

A note detail that closes while it holds focus (its Close button, Escape, Back, or an Approve or Reject that removes the note) returns focus to the row that opened it, else the first row, else the bell in the sheet or the feed's first control on the inbox page (`components/notifications/feedFocus.ts`, used by `shell/notifications/notificationSheet.tsx` and `pages/NotificationsPage.tsx`). On the inbox page this covers both layouts: the narrow one, where the detail replaces the feed, and the wide one, where the side detail swaps for the empty state. Approve and Reject close the detail before their delete lands, so the row focus returned to is watched, and when it leaves the feed while focus is on `<body>` the first row takes it. Focus the user already moved to another live control stays there. `notificationSheet.detailCloseFocus.test.tsx` and `NotificationsPage.goneNoteFocus.test.tsx` pin it.

### Feed keyboard stepping

In the bell sheet and the inbox feed, Up and Down on a row's open control press the neighbouring row's open control, in rendered order, and move focus to it. A step does exactly what a click on that row does, so a collapsed stack is one stop and in the bell sheet it expands rather than opening its newest note. Keys from an inner control (dismiss, Approve, a code block), modified arrows and the two ends are left to the browser. The shortcuts reference lists this as "Previous notification" and "Next notification"; those entries are reference-only and bind no global key.

### Detail-panel hand-offs

The detail panel's hand-off buttons ("Continue in Chat", and "View last result" on a cron note) show an inline `ErrorNotice` when the hand-off fails. The sentence names the button by its own label and is scoped to the note it failed on, so it does not follow the reader to another row. The notice appears only where the hand-off did NOT succeed: `/to-chat` is not idempotent — a repeat mints another slot and spawns a second agent on the same `work_dir` — so a succeeded call is never offered or run again automatically.

"Fix with Crew" (every note but an approval) stages a new chat whose composer holds the note fenced as untrusted data (`notificationHandoff.prompt.ts`); nothing is sent. The bus stores a body over its cap as its head plus `…`. A cron note whose body is the job's stored result carries `result_ts`, that result's `last_result_ts` (a 24-hour repeat reminder rewrites the text, so it carries none). When such a note was cut, the panel reads the shared `cronJobsQuery` and the click hands over the job's `last_result` instead (`wholeCronResult`), but only while the job's `last_result_ts` still equals the note's, since the job keeps only its latest result, and only up to 100,000 characters. A result that is not loaded, belongs to another run or is longer leaves the note as stored. `NotificationDetailPanel.fixWithCrew.test.tsx` pins the match, another run, the size bound and a whole body.

## Unread badge (client)

Settings › Notifications › "Mark sessions unread only when they need you" (`localStorage` key `mc-unread-on-attention`, default off, `hooks/unreadOnAttention.ts`) limits which `chat_message` rows mark a background session unread: with it on, only a `permission` row does, while a finished turn and a question card badge on their own paths. A row carrying the watchdog's `session_recycled` notice (`meta.notice`, written by `wire_session_recycle_callback` in `dashboard/state_parts/session_events.py` when an idle session is recycled) never marks the session unread, regardless of this opt-in: `chatMessageMarksUnread` (and `noteMemberThreadRow`) short-circuit it because the notice lands in a session that is not running and has nothing new to read. A slot's `last_ts` is its newest durable row (`slot_projection.py` skips transient roles). The unread watermark is never taken from a transient-role row, because a restarted gateway never again reports a `last_ts` that high and the badge could never clear. The client's role set (`UNSAVED_ROLES`) mirrors `dashboard/state_parts/transcript_rows.py` `_TRANSIENT_ROLES`; `test_to_dict_board_fields.py` pins the mirror.

A member DM thread (`slot.mode === 'member'`, key prefix `member-`, which `slot_registry.py` reserves for that mode) is narrower in both modes: `memberThreadRowMarksUnread` badges it only on a row the Bot wrote to the user, `assistant` or `permission`, with the opt-in still applied on top. Its `tool_call`, `tool_result`, `inject` and streaming rows never badge it: the Bots rail dot means "a Bot said something to you", not "a Bot ran a tool". `isMemberThreadSlot` reads the mode from `dashboard.slots` and falls back to the reserved prefix for a thread the list has not caught up to. Its `chat_done` is narrowed the same way (`turnCompletion.ts`): `chatStream` records every such row on the thread's running turn (`noteMemberThreadRow`, on or off screen), and the finished turn badges only when the turn delivered one (`takeMemberThreadSpoke`, which also clears the record) or paused for input (`needs_input` / a live question card). A patrol turn that only ran tools and ended quietly badges nothing. The question-card path is unchanged.

`GET /api/notifications` returns `{"notifications": [...], "unread": N}`, and `unread` is counted from the notes it returns on every read (`notification_coordinator.unread_count`): a note counts while it is not `acked`, not `passive` and not `silenced`, the bell badge's rule (`shell/topbar/shellHeader.tsx`). So an ack, Mark all read and a delete each lower it, and a restarted gateway reports the persisted feed's true count rather than zero. A relayed teammate's count covers only the notes their view returns. `test_notification_unread_count.py` pins the ack, ack-all and restart cases.

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
the detail panel renders the fenced input as one code block, behind the
Formatted view above when it is a JSON object. That body is the
only surface naming the requesting system: the detail panel's metadata row
prints the note's kind (`KIND_META[...].label`) under the `pages.artifactsPage.kind`
label, so its label and the body's `Source:` label are distinct fields.

The shared contracts live in `website/src/test/notifMeta.stripMd.test.ts` (with
the code-region scan in `website/src/test/notifMeta.codeScan.test.ts`) and
`website/src/test/approvalNotificationBody.test.tsx`; native banner formatting is
pinned in `website/integration/AppNotification.integration.test.tsx`. WebSocket
producer coverage pins the differing purpose policies, and the feed tests pin
both excerpt lengths.

## Localized copy (client)

A note the gateway writes itself can name its dashboard copy, so the bell reads
in the dashboard's language. `NotificationPayload.i18n` is `{id, params?}`: `id`
names an entry of `SYSTEM_NOTE_TITLE_KEY` and `SYSTEM_NOTE_BODY_KEY` in
`notifMeta.tsx`, two `as const` maps of literal catalog keys under
`components.notifications.systemNote` (`en.manual.json`), and the string and
number values of `params` fill their placeholders through `i18nT`, which shows
each value literally. A producer passes it as `state.notify(..., i18n=...)`. It
is a schema field in `_RESERVED_NOTE_KEYS`, so neither an app push nor an agent's
`meta` can name a gateway copy. It is not validated: a malformed one reads as no
copy, and dropping the note over it would lose the alert. The note keeps its
English `title` and `body`, which say what the English copy says: OS banners (the
tab's toast and Web Push, which replaces it by tag and has no catalog), the agent
hand-off and the stored feed read those. `noteText` renders the copy on the bell
card and banner, the feed rows and their search, and the detail panel, and falls
back to the stored English for a note without an id, an id the maps do not hold,
or a persisted row of the wrong shape. The detail panel's button for the note's
`url` reads "Open", unless `SYSTEM_NOTE_LINK_KEY` (resolved by `noteLinkLabel`)
names a label for the id, as it does for `mcp_share_paused` ("Review shared
servers"). Notes that name a copy today: the turn-ceiling
pause (`turn_limit`) and the default-model applied and failed notes
(`default_model_applied`, `default_model_failed`), the folder Default agent note
(`folder_agent_label`), the weekly repeated-failures note (`repeated_failures`), and the dead-turn note on
`system.turn`, one `chat_stopped_*` id per way the turn died under one shared title;
every other producer still writes English only. `test_notification_dashboard_copy.py` pins the turn-ceiling
producer, its English against the catalog and the meta refusal;
`test_config_live_server_appliers.py` pins the default-model ids;
`test_turn_died_notify.py` pins each `chat_stopped_*` id and its English;
`test_folder_agent_member_key.py` pins the folder Default agent note (`folder_agent_label`) and its English;
`NotificationCard.systemNote.test.tsx` pins the zh-CN render of every id and the
fallback.

## Notification sound (client)

Notification sound is produced entirely on the client and is independent of the
notification feed, the bell badge, and OS notification-center toasts. The
WebAudio layer is the **single source of sound**: `website/src/hooks/useNotificationSound.ts`
synthesizes tones through the Web Audio API (no audio files) and is the only
component that emits a built-in sound (a theme pack's own sound is described
under "Theme pack sounds" below). The feed toast's page-context `Notification`
constructor (`website/src/hooks/useNativeNotification.ts`, see "OS toast"
below) passes `silent: true`, so the OS toast never adds its own system chime
on top of the WebAudio tone. A browser that
ignores `silent` degrades to the prior double-sound behavior and no worse.

### Theme pack sounds

An active Level-2 theme pack whose sounds are on and audible (consented, not
muted, no reduced motion) can sound a notification instead of the built-in
tone. `ThemeExperienceLayer.tsx` publishes the pack's playable trigger names
through `themeSound.ts:publishThemeSoundTriggers()`, and both listeners of
`MC_NOTIFICATION_EVENT` resolve the same event through
`useNotificationSound.ts:themeTriggerForNotification()`, so exactly one of them
plays it. The resolution order is: a category the user set in Settings keeps its
own sound (the pack stays silent for it); else the pack's `category:<kind>`
trigger; else its `notification` trigger; else the built-in tone. The `all`
fallback is not treated as a user choice, because a stored value cannot be told
apart from the default. The allowed `category:<name>` triggers are the
`SOUND_CATEGORIES` minus `all` (`theme_validate._THEME_AUDIO_NOTIFICATION_CATEGORIES`),
each capped at `notification`'s 2 seconds. `themeNotificationSound.test.ts`,
`ThemeExperienceLayer.test.tsx` and `test_theme_install_declarations.py` pin the
order, the skipped built-in tone and the validator's allowlist and cap.

### Sound events

Two sound kinds are synthesized by the websocket layer, and a third by the
dictation hook. `TURN_DONE_KIND`
(`'turn'`, on `chat_done`) is sound-only: it never appears in the feed (no Redux
entry, no toast, no badge). `DICTATION_STOPPED_KIND` (`'dictation'`) is
synthesized by `useStreamingStt` when a live dictation stops before the user ends
it: an `error` frame or a socket close while still capturing, or the readiness
buffer filling up. It is sound-only for the same reason as `'turn'`: the hook
already shows the error, and the sound is for someone dictating without watching
the screen. A stop the user makes, a cancel and an unmount stay silent.
`APPROVAL_KIND` (`'approval'`) is synthesized at
three sites:

- an `approval` frame (a coordinator-registry approval: a Slack, cron, or
  sub-agent spawn gate). The same frame *separately* adds an approval
  notification to the feed — so a coordinator approval both chimes and shows a
  feed entry, and the two are independent (the feed entry is also what carries
  the approval to the OS toast);
- a `question_card` frame, once per server card identity;
- a `chat_message` frame carrying the chat runner's `permission` row — an
  INTERACTIVE chat parked on a tool prompt. The runner appends that row
  (delivered once to every dashboard window as this frame), registers its
  future and pushes the slots; no `approval` frame exists for it, and the
  coordinator's own cards are synthesized on the client, so every server row
  of this role is the runner's and its one delivery is the one sound.
  `shouldChimeOnPermissionRow` requests it unless the row carries `resolved`
  or the socket is in reconnect catch-up. A row arrives `resolved` when the
  runner already knows nobody will answer it: the batch-rejection re-append,
  and a turn with no budget left to wait, which the runner decides before the
  append (no I/O is needed) because the in-place mark that closes a prompt
  later reaches the client only as an `approval_resolved` frame, which
  retires the card but cannot un-play a sound. A row pre-declined that way is
  not mirrored to a linked Slack thread: the post is the one cancellable await
  between the prompt's registration and the backstop that retires its future,
  and a turn ceiling landing inside it would strand a future nothing resolves
  (the Board keeps the session Blocked, Continue answers 409) for a card the
  backstop deletes the moment the post returns — so the pre-declined row's
  `resolved` is its final state and its future settles exactly as an
  interactive prompt's does. The one host decline known
  only after the row is out — a linked Slack thread the prompt could not be
  posted to — retires the row through that frame; its arrival chime is the
  accepted residual, since holding the row back until the post settles would
  hide the card, the chime and the Board's pending state for the post's whole
  duration on every linked prompt. This site is
  the `chat_done` layering: no feed row and no producer for the protected
  `system.approval` bus channel (which still has none); the same gate also
  posts this prompt's OS toast (see "OS toast"). A
  prompt already parked when a tab opens or reconnects reaches it through the
  snapshot and the transcript rehydration, not through this frame, and stays
  silent.

Every synthesized chime (`TURN_DONE_KIND` and all three `APPROVAL_KIND`
sites) is suppressed during reconnect catch-up replay, and
`shouldChimeOnTurnDone` also suppresses slot-less turn completions. A real feed
`notification` frame fires `MC_NOTIFICATION_EVENT` with its own `kind`, except
when the note is muted-channel (`silenced`) or `passive`; that sound is not
gated on catch-up, only the frame's live banner is (see Trigger below).

### Settings and resolution

Settings persist in `localStorage` under `mc-notification-sound`
(`{ enabled, volume, perCategory, customTones?, customFiles?, mutedUntil? }`). `presetForKind(kind, settings)` resolves
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

### Timed mute

The Sound card's **Mute sounds** select silences sound for 1 hour, 2 hours or
until the next 8:00 local time, and turns it back on by itself. It stores only
`mutedUntil` (epoch ms), and `isSoundMuted(settings)` compares it with the clock
at play time, so expiry needs no timer and no write; the panel re-renders when it
runs out so the select reads Off again. While it runs, three places play
nothing: the `MC_NOTIFICATION_EVENT` listener in `useNotificationSound`
(approvals and the dictation cue included, as with `enabled === false`), the
theme pack's two app-event listeners in `ThemeExperienceLayer` (its notification
and `message-received` sounds), and a crew avatar's state cue in
`useCrewAvatarState`. A theme's ambient loop and the sounds its own frame asks
for follow the theme's Sounds switch, not this mute. The feed, badges, banners
and dashboard OS toasts (already `silent`) are untouched, and a Test button
still plays: it is the user asking for a sound. A web push banner shown while
the dashboard is closed comes from the service worker, which cannot read this
browser setting, so its sound stays the OS's.

### Custom sounds

A user can add named sounds of their own (Settings > Notifications > Custom
sounds), so instances in one room can sound different. They live in the same
blob under `customTones` (`{ [name]: ToneStep[] }`, each step
`{ freq, start, dur, gain }`, the shape of the built-in table). A category
selects one as `custom:<name>`, so a custom name can never collide with a
built-in id. The built-in presets, their ids and their levels are unchanged.

`validateCustomTone` holds every sound to `CUSTOM_TONE_LIMITS`: 1-32 character
name of letters, digits, space, `-` or `_`, not a built-in, `none` or `default`
and not already used (case-insensitive); 1-16 steps; `freq` 20-20000 Hz; `dur`
0.02-2 s; `gain` above 0 and at most 1; every step starting at 0 or later and
ending within 5 s; at most 20 sounds. It runs when a sound is added AND inside
`loadSoundSettings`, because the stored blob can be hand-edited or restored from
`ui-prefs.json`: a sound that fails is dropped, and a category pointing at a
missing custom sound falls back as if unset. The host backup stores the blob
as an opaque string, like every other ui-pref, and never parses it: every
reader goes through `loadSoundSettings`, so the load-time check is the one
gate between a stored value and the speakers. Playback scales a custom sound by
its loudest overlap of step gains, so it never drives the output past full
scale. The form edits one row per tone (Pitch, Start, Length, Volume), starts
from a two-note example, and can play the draft ("Play") before it is saved;
the draft plays under a throwaway id and is never stored. Bound failures show
as plain hints, not error banners, each under the field it is about (a name
problem under Name, a tone bound under its row); the offending inputs get
`aria-invalid`, and "Save sound" stays off until the user edits. A save that
fails (add, delete, undo, or a sound, volume or category setting) shows through
`ErrorNotice` with a next step (free up this site's browser storage, then try
again), without the agent hand-off because the form holds an unsaved draft.
Deleting a sound clears every category set to it (`all` returns to `chime`) and
offers an Undo. Undo merges into the settings as they are at that moment: it
puts back the one sound and each category that used it unless the user has
since set that category to something else, so a sound added or a choice made
after the delete survives. Saving a sound under the deleted name (any case)
withdraws the Undo, and an Undo refused because the name was reused elsewhere
is withdrawn after its hint, so no notice says a listed sound was deleted.
Pickers label a custom sound
"<name> (yours)". Crew avatar sounds still offer only the built-ins.

#### Sound files

A user can also add a sound from an audio file (the "Use an audio file" card in the
same pane). The bytes stay in the browser: `hooks/customSoundFiles.ts` keeps
each file in IndexedDB (database `kirocrew-notification-sounds`, store `files`)
and playback reads it into a `blob:` URL that is revoked once the `<audio>`
element is released, or at the latest a few seconds past the 5 s limit, so a
platform pause that fires neither `ended` nor `error` cannot pin it. Nothing is
uploaded to or stored by the gateway. The settings blob holds only the record
that makes the sound pickable, under `customFiles`
(`{ [name]: { bytes, seconds, key } }`), and a category selects it as
`file:<name>`.

Bytes are stored under a key made for each write (`newSoundFileKey`: its
creation time, then a random id), never under the sound's name, and the record
carries that key; a record without a well-formed key is dropped on load. Two
adds of the same name, from two tabs or a retry, therefore write two entries,
and each one's clean-up can only delete its own bytes.

`readAudioFile` checks a picked file before it can be saved, against
`CUSTOM_FILE_LIMITS`: at most 256 KB (checked first, so an oversized file is
never read), then a decode by an
`OfflineAudioContext` (which opens no output device) that proves the file plays
and measures it exactly, at most 5 s. `validateCustomFile` holds the
record to the same limits on add, on Undo and on every load, so a hand-edited
or restored record past them is dropped like a tone sound. Tone sounds and
sound files share one name space (case-insensitive) and one limit of 20,
because both validators check against `allCustomSounds`.

Adding writes the bytes first and the record second, re-checking the record
against the settings as they are after the write: if another tab took the name
or the last slot meanwhile, this add deletes its own bytes and saves nothing.
Deleting a file forgets its record, with the same category clean-up and Undo as
a tone sound, and leaves its bytes in place for that Undo. Opening the pane
brings the two back in line (`pruneSoundFiles`): bytes no record points at are
deleted, reading the records again before each delete, and a record whose
bytes are not stored (settings restored from `ui-prefs.json` into another
browser) is dropped with its categories at that opening. Until then a
notification for a category set to it plays what that category would play
with its file choices unset (`missingFileFallback`), so restored settings never
silence a category. Bytes and records
younger than `ORPHAN_GRACE_MS` (10 minutes, read from the key) are exempt from
both, because an add in another tab writes its bytes before its record; a key
dated further than that in the future (a clock that ran ahead, a hand-edited
record) is not exempt, so it cannot shield a record forever. Collecting can
still remove the bytes another tab is holding for an Undo; that Undo first
checks the bytes are there, and when they are not it restores nothing and says
the file is gone. Two tabs can still race inside that check (one tab's Undo
passes it just before the other's collection runs); the record left without
bytes is then dropped at the next opening of the pane. Undo is off while it
runs, so a double click restores once. When IndexedDB cannot be read at all
nothing is concluded and every record is kept; a file sound that cannot be
read plays silence, and a connection the browser closes is reopened on the
next call.

The pane is three titled cards, each a named region. "Your sounds" lists every
saved sound with a small "File" or "Tones" tag and its Play and Delete
buttons. "Use an audio file" comes next, as the quicker way, then "Build from
tones" (the tone form). Both forms use the same words on purpose (Name, Play,
Save sound): the card title says which way the user is making a sound. In the
file card, Save sound stays off until a picked file has passed its checks; a
refused file's reason is shown under the picker in the danger colour, stays
until another file is picked, and is the only thing that marks the picker
`aria-invalid` (a limit or name problem is shown as a hint without blaming the
file). A successful save, from either form, says "Saved <name>." with a link,
"Choose where it plays in Per-category sounds", that opens that sub-page
through `SettingsSubNav`'s `select`, because a saved sound is heard only once a
category is set to it.

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

## Mouse haptics (client)

`website/src/hooks/useMouseHaptics.ts` sends each notification chime to the Kiro
Crew plugin for Logi Options+, which buzzes an MX Master 4. `useAppShell` (`shell/appShell.tsx`) mounts it <!-- wokeignore:rule=master -->
next to `useNotificationSound()`, and it listens for the same
`MC_NOTIFICATION_EVENT`. Options+ is the only program that talks to the mouse,
so the dashboard needs no device access and no macOS Input Monitoring
permission. Driving the mouse directly over WebHID would need that permission,
and on macOS it would also cover every process the desktop app starts.

- **What buzzes.** An event buzzes exactly when the Logitech mouse haptics switch
  is on, no timed mute (`isSoundMuted`) is running, and
  `presetForKind(kind, loadSoundSettings())` is not `'none'`, so the sound switch,
  the timed mute and the per-category Silent choices apply to the mouse too. The volume
  is not consulted, so volume 0 gives buzz-only alerts. Buzzes are debounced to
  one per 300 ms. Every open dashboard window hears each alert and stamps it
  with the wall-clock time it arrived. A window that can reach the plugin then
  takes a Web Locks lock just long enough to compare that stamp with the last
  buzz any window recorded under the `mc-mouse-haptics-last-buzz-at`
  localStorage key. Within 300 ms either way, it is the same alert and the
  window skips it; otherwise the window records its stamp and sends, provided
  the switch and that alert's sound are still on at that moment. No timer
  ever holds the lock, so a background window whose timers the browser
  throttles cannot delay another window's alert. A window still waiting out a
  failed probe never asks for the lock, so it cannot take an alert from a
  window that can send it.
- **Plugin contract.** The plugin listens on `http://127.0.0.1:41870`.
  `GET /v1/status` returns `{"plugin": "KiroCrew", "api": 1, "events": [...]}`.
  `POST /v1/events/{event}` raises `turn_done` (a `turn` chime), `needs_input`
  (`approval`) or `notification` (every other kind). It answers requests whose
  `Host` is the endpoint itself and whose `Origin`, when present, is an
  `http(s)` page on `localhost`, `127.0.0.1` or `[::1]`. A request from any
  other host or origin gets a 403.
  Each user picks the waveform for each event in Options+, under Haptic
  feedback. The defaults are completed, ringing and knock.
- **Opt-in and detection.** The switch is the opt-in and starts off, so a
  dashboard that never turned it on sends nothing to the plugin's port and
  shows no plugin status. Once it is on, the bridge sends an alert only after a
  status probe has identified the plugin. A failed probe or a failed alert parks
  it for five minutes, so without the plugin each dashboard window makes at most
  one request per five minutes, and only when alerts fire. Requests carry no
  credentials, no referrer and no body.
- **Settings.** Settings > Notifications > Sound has a Logitech mouse haptics
  switch, stored per device under the `mc-mouse-haptics` localStorage key
  (`'1'` is on; any other value, or none, is off). An open panel follows
  another window's change to the switch, and shows it off when another window
  clears localStorage. The switch is disabled while
  notification sound is off, and the row then shows why: "Turn on “Play sound on
  new notifications” to use mouse haptics." Under it, a status line probes the
  plugin with the same check the bridge uses and reports one of: connected, not
  found, unreachable (through `ErrorNotice`, with the Options+ install steps),
  or available only on a loopback page. A reply whose body fails to arrive
  counts as unreachable. An unreachable Settings probe journals
  its full loopback address and method, plus either the HTTP status and response
  error or the network failure class, then attaches that report to the agent
  hand-off. It probes only while the switch and the sound are on, and again when
  the window regains focus. A refused save of the switch (quota exhausted after
  reclaim, or storage blocked) leaves the switch unchanged and shows an
  `ErrorNotice` under it, journaled with `code` `storage_write_refused` and the
  key as `detail`, with the agent hand-off; a later save that lands, or Dismiss,
  clears it. Turning the switch on, any Settings status probe that finds the
  plugin, or the status line opening on a cached "connected" answer, fires
  `MC_MOUSE_HAPTICS_RECHECK_EVENT`, which ends the bridge's five-minute wait.
  So a plugin installed meanwhile, or one that recovers after a failed alert,
  buzzes on the next alert.
- **Loopback pages only.** The hook does nothing unless the page's hostname is
  `localhost`, `127.0.0.1` or `[::1]`. That covers the desktop app and a
  browser on an SSH-forwarded port. A Tailscale or LAN address would make the
  browser ask for local-network permission for a request the plugin refuses.
- **The plugin.** `packages/kirocrew-logi-plugin/` is a Logi Actions SDK
  plugin (C#, .NET 8) with its own xunit suite. No CI workflow runs that suite
  yet, so after a change run
  `dotnet test packages/kirocrew-logi-plugin/tests/KiroCrewPlugin.Tests.csproj`.
  The suite compiles the endpoint sources without `PluginApi.dll`. Building the
  plugin itself on a machine without Logi Plugin Service needs that file copied
  from the LogiPluginTool package into the plugin's `lib/` first. A Debug build
  also links its output into Logi Plugin Service for development; a Release
  build does not. `logiplugintool pack` turns a Release build into
  `KiroCrew.lplug4`. To install it, open Options+ at MX Master 4 → Haptic <!-- wokeignore:rule=master -->
  feedback → Install and uninstall plugins, then open the `.lplug4`.
  Opening the file installs it only while that page is open. The user-facing
  steps are in `src/kiro_crew/docs/dashboard.md`, "Mouse haptics".

`useMouseHaptics.test.ts` pins the switch, the sound-settings gating, the event
mapping (names inherited from `Object.prototype` fall back to `notification`),
how the status probe classes each answer, detection with its retry
window and the recheck, the request options, an alert dropped when the switch
or its sound goes off before the probe answers,
the debounce, one buzz across windows, and the loopback-only mount.
`NotificationsPanel.mouseHaptics.test.tsx` pins the label, the switch's
off-by-default start and persistence, following another window's change or
localStorage clear, the notice and journal entry a refused
save produces and their clearing, the reason the row shows and associates
with the disabled switch while sound is off, the connected,
negative-identification, unreachable (also when a reply's body fails to
arrive) and loopback-only status outcomes, the
unreachable hand-off context and install guidance, a recheck after every
status fetch that finds the plugin and on opening with a cached "connected"
answer, and that no probe goes out
while the switch or the sound is off or from a non-loopback page.

## OS toast (client)

`website/src/hooks/useNativeNotification.ts` is the **single poster** of an OS
toast for a feed note. It posts through `lib/nativeNotify.ts`
`postNativeNotification`, which constructs a page-context `Notification` in a
top-level window. In an embedded instance pane (the full dashboard inside the
Instances hub's cross-origin iframe, where the frame's own permission is
`denied` by design) it relays the note instead: it posts an `mc-native-notify`
envelope to `window.parent` at the exact loopback origin `document.referrer`
names — never `'*'`, and never from an `/embed/*` document. The hub
(`InstancesViewport`) accepts it only from a warm tunnel port, prefixes the title
with the instance's name, namespaces the tag per instance id, brings that
instance forward when the banner is clicked, posts only when its own permission
is `granted`, and never prompts. The pane keeps its own mute, away and `silent`
rules, so only a note it would have shown is relayed. It watches the unacked,
unsilenced notes in the Redux store, leaving out a quiet replacement, and when
one appears that it has not seen, posts one toast
carrying the newest such note's title and flattened body, tagged with the
gateway-stamped `tag` (else its `approval_id` / `job_id` / `task_id`, or
`kirocrew-notif`) so a burst about
one subject replaces rather than stacks. An `approval` frame reaches the OS
through the feed entry the socket's approval registry
(`website/src/hooks/websocket/approvals.ts`) dispatches for it; the socket layer
constructs no toast of its own. One event, one constructor, one tag: the OS
collapses only equal tags, so a second constructor with its own tag is two
banners for one approval.

The toast fires **only while the user is away from the window**:
`isWindowAway()` (`hooks/windowAway.ts`) is `document.hidden ||
!document.hasFocus()`, both axes because Page Visibility reports an occluded or
unfocused window as visible. While the window is visible and focused the in-app
banner and the bell badge already show the note, and the toast stays quiet; a
note that arrived while focused is not re-announced when focus later leaves.
The same predicate is the in-app banner's `windowFocused` (its complement) and
the chat-complete toast's away check, so a live note lands on exactly one of
the two surfaces. The gate sits inside the permission-granted branch: the
best-effort `requestPermission()` on an undecided permission runs regardless
of focus.

The opt-in "a background chat finished" toast (`hooks/chatCompleteNotify.ts`,
constructed by the socket's turn-completion owner
`website/src/hooks/websocket/turnCompletion.ts` on `chat_done`) is a separate, default-OFF
surface with its own `kirocrew-chat-done:<slot>` tag; it shares only the away
predicate. In an embedded pane it reads the hub's opt-in from the host model,
since the hub's Settings toggle cannot write the pane's storage
([turn-complete-chime](turn-complete-chime.md)).

An interactive chat's tool prompt (the chat runner's `permission` row, which has no feed note) is toasted by the socket's transcript owner `website/src/hooks/websocket/chatStream.ts` on the row's live arrival, behind the same gate as its chime (`shouldChimeOnPermissionRow`: not `resolved`, not a reconnect replay). It is on by default, posts only with the OS permission granted and while `isWindowAway()`, is `silent`, reads like the chat-complete toast (session title as the title, `Waiting for your approval: <tool>` as the body), and carries a `kirocrew-approval:<slot>:<request_id>` tag, so one pending prompt is one banner however many windows receive the row. Neither mute suppresses it, the session's own per-row mute or its creator's: a prompt is the session waiting on the user.

## Web Push (closed app)

Web Push is a second sink on the bus. `NotificationCoordinator.deliver` stamps
`note["tag"] = native_notification_tag(note)` (a keyed note's source and key,
else the approval, job or task id, else the note timestamp, hashed when its
JSON-serialized form is over 128 bytes), runs local delivery, then calls the
`web_push_fanout` hook (`DashboardState._fan_out_web_push`) for every note that
is not silenced, passive or a quiet replacement; a keyed note reaches it only
after its write commits (see "Keyed notes"). The fan-out schedules
`_send_web_push_all` on the running loop; it is
best effort and logs any failure, so a failed push never affects the note that
already landed.

- **Payload.** `_web_push_payload` builds one declarative payload
  (`web_push: 8030` plus `notification`) that iOS and Safari 18.4+ render with no
  service worker and that `sw.js`'s `push` handler reads on Chromium and
  Firefox. Every string field has a budget in serialized bytes so the encrypted
  message stays under 4096 bytes; a deep link over budget becomes `/`.
- **One banner.** The tab toast (`useNativeNotification`) and the push banner use
  the stamped `tag`, so a hidden tab plus a push shows one banner, and both carry
  the note's `url` in `data.url` for `notificationclick`. A keyed note asks for
  `renotify`, so a louder replacement alerts again on a closed device. The worker
  keeps that re-alert whenever it shows the OS banner, because a hidden tab cannot
  be trusted to play a sound. A
  tab not answering within the probe timeout counts as not connected: a busy tab
  can then sound a keyed note twice, and a frozen tab still gets its alert.
- **Service worker.** The `push` handler skips the banner only when a visible
  dashboard window answers its probe (`kc-push-probe` over a `MessageChannel`,
  answered by `website/src/lib/swConnectedProbe.ts` from `dashboard.connected`)
  with `connected: true`, since only a connected tab receives the note over the
  WebSocket. No answer within 500 ms, `false`, or no visible dashboard shows the
  banner, so a visible tab whose socket was refused never swallows a push.
  `notificationclick` focuses or opens only dashboard windows. A standalone app window (`/app-windows/...`), a popout (`/popout/...`)
  and an embed (`/embed/...`) count as neither: none renders the notification
  banner or hosts the dashboard's routes, so none is probed or focused. Pinned by
  `website/src/test/serviceWorkerNotificationClick.test.ts`.
- **Sender.** `notifications/web_push.py` signs VAPID ES256 and encrypts RFC 8291
  `aes128gcm` with `cryptography`. Its session resolves through
  `PublicAddressResolver`, which drops every address
  `link_unfurl.address_is_not_public` rejects. A 404 or 410 removes the
  subscription.
- **Stores.** The subscriptions (`push_store.py`) and the VAPID keypair
  (`vapid_keys.py`) live in the `web-push` directory of the data home, which is
  pre-created and masked in every sandbox and fenced from agents
  (`sandbox_leaves.py`, `sandbox_precreate.py`, `security/paths_lists.py`).
  A subscriptions file the load cannot use reads as empty, but a subscribe never
  saves over it unread: one leading UTF-8 byte-order mark reads normally, a file
  that is not usable JSON is moved to `subscriptions.json.corrupt-<utc>` first,
  and a read that fails leaves the file alone and the subscribe raises
  `PushStoreUnreadable`.
- **Routes.** Owner only: `GET /api/notifications/push/vapid-public-key`,
  `POST /api/notifications/push/subscribe` (HTTPS endpoint, vetted by
  `link_unfurl.vet_unfurl_url`) and `POST /api/notifications/push/unsubscribe`.
- **Client.** `usePushSubscription` and `NotificationsPushSection.tsx` drive the
  Settings toggle. `test/test_web_push.py`, `test/test_notification_push_routes.py`
  and `website/src/test/notificationSharedTag.test.ts` cover the contract.

## Bell sheet dismissal (client)

A press on the bell sheet's own background dismisses it, like a press outside. It dismisses at click, and only when the pointerdown, the pointerup and the click all land on background and the text selection is collapsed, so a drag or a text selection never closes it. A press on the sheet's own scrollbar, a card (`notif-material`), a row (`data-notif-row`), the detail panel (`data-nc-material`) or any control keeps it open (`isSheetBackgroundPress` in `shell/notifications/notificationSheet.tsx`). Every child composed into the sheet must be material or background by decision; the structural test in `App.notificationSheetBackgroundDismiss.test.tsx` enforces it.

## In-app banner (client)

`website/src/components/notifications/NotificationBanner.tsx`, mounted once with
the bell's sheet (`website/src/shell/notifications/notificationSheet.tsx`, which
the bell button in `shell/topbar/shellHeader.tsx` renders) and portalled beside it, shows a
macOS Notification Center-style card under the top bar for a **live**
notification. The card body is `NotificationCard.tsx`, the ONE rendering the
bell popover's mac rows and the banner both use (kind-tinted 26 px icon square,
one-line title, two-line body, relative time with the unread dot, hover-reveal
close, quiet capsule actions). The card IS a Liquid Glass pane
(`components/Glass.tsx`, the `panel` recipe, `CARD_RADIUS` 16 px) — the same
material as the composer dock — so it carries no tint, blur, border or shadow
classes of its own: the pane's layers are the material, `glass-shadow` the
rest shadow. The card does not know which surface it is on: the pane reads
correctly over the sheet's scrim and over page content alike, so there is no
elevation prop and both surfaces are one glass.
State is a tint step on the host, never an edge: `glass-accent` for the feed's
selected row, `glass-hover` while a pressable card is hovered, and
`glass-faded` (the tint thinned to 55 %) for a silenced row, a collapsed
stack's edges and the banner's deck shells — recession is a tint step, never
an `opacity` on a glass host, because opacity < 1 makes the host a backdrop
root and voids the pane's own blur. The banner's blank deck
shells, its "+N more in your inbox" pill and the feed's controls card, stack
edges and first-note-from-a-channel prompt (its own accent-tinted pane under
the row, since a pane has one radius) are the same primitive. Every pane also
carries `notif-material`, the `website/src/styles/liquid-glass.css` hook that solidifies it to the card
color where backdrop-filter is unsupported and the selector the sheet's
background-press verdict keys on. The
card's `body` prop replaces the two-line clamp: the feed passes the full
read-only approval render for every approval row, because the popover card
keeps one-click Approve/Reject and a clamped excerpt hides the tail of the
command they authorize. The banner, which offers only Review, keeps the
excerpt. A critical note is signalled only by its danger dot and the approval
icon tint, never an edge or a label. Nothing about the banner is persisted
server-side.

### Trigger

The banner listens to `MC_LIVE_NOTIFICATION_EVENT` (`hooks/notificationEvent.ts`),
which the socket fires for a `notification` frame received on a live
connection (the frame's arm in `useWebSocket`'s router) and for the feed note
the approval registry (`website/src/hooks/websocket/approvals.ts`) synthesizes
from an `approval` frame (the
note carries the owning `slot`, so `targetsCurrentView` skips it while that
chat is on screen and its inline permission card is visible; an approval with
no slot banners on every surface). It never reads the Redux list: the boot `fetchNotifications`
snapshot and reconnect refetches fill the store with history, and history is
never bannered. Both paths withhold the event during a reconnect catch-up
(`reconnectingRef`, held by `website/src/hooks/websocket/connection.ts`), the
same window that mutes the turn-done chime.

### Priorities

| Priority | Banner |
|---|---|
| `critical` | stays until clicked, dismissed, or acted on; the live region is `role="alert"` while one is pending |
| `default` | auto-hides after `BANNER_AUTO_HIDE_MS` (6 s). Every pending default card shares ONE timer, restarted by each default arrival and paused while the stack is hovered or holds focus. The pointer and keyboard are tracked as two separate holds and the clock resumes only when BOTH have let go. A card's removal destroys ownership without firing the release event, so the holds are re-read after every change to the deck and to how it is rendered (expanding it, or crossing the mobile breakpoint, unmounts the focusable "+N" pill and deck shells with no note leaving): FOCUS is owned by an element (held while a card still shown contains the active one; a leaving card stays mounted and focused through its exit animation and nothing re-reads the holds when it finally unmounts, so focus inside it is released as it starts to leave), the POINTER by the container (a removal does not move that boundary, so only a real pointer-leave — or an empty deck — releases it) |
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
active chat on a chat route, or whose `url` path is the current route. A
Bot's message (`system.member`) instead counts as on screen only when its
`slot` is the active chat on a chat route or the thread the Members page
registered in `lib/viewedThread`, read at arrival: its `url` is the whole
Members page, which shows one thread at a time. A stopped-chat alert
(`system.turn`) is on screen only when its `slot` is the active chat on a chat
route: its `url` is `/chat?sid=<dying slot>`, so the generic `url`-path branch
would strip the query and match the bare `/chat` route, suppressing the banner
whenever the operator is on ANY chat page — including a different one, the case
it exists for — so the channel short-circuits to not-on-screen there instead.
Opening
the popover, landing on the inbox page, or switching the preference off also
retires every pending card.

### Stack

Newest on top. Beyond the top card, up to `BANNER_DECK_DEPTH` (2) older cards
peek as a deck of BLANK shells (the card's glass only, no text, icon or time;
4/8 px offset, .98/.96 scale, the `glass-faded` tint step), so nothing prints through the
translucent top card. The shells are absolutely positioned over the top card's
box, so the top card is `position: relative` and its higher `zIndex` paints it
above them — unpositioned, it sat under the shells, which blurred it and took
its close click. Each shell and the "Show N more" pill on the top card's
corner are the same control (`Show N more notifications`) that expands to a
vertical list of at most `BANNER_EXPANDED_MAX` (4) cards plus a "+N more in your
inbox" line that goes to `/notifications` (through the navigation leave guard) —
the same place the popover's "Open inbox" goes, so "inbox" names one place. On the mobile breakpoint only the newest card renders,
full width, with its close visible at rest (no hover on touch).

On the mobile breakpoint the card also goes with a sideways or upward swipe
(`components/notifications/SwipeToDismiss.tsx`, wrapped around the card; inert on
desktop). The gesture is touch-only and calls the same `removeNotes` the close
control calls, so a swiped note waits unread in the bell exactly as a closed one
does, and the close stays visible for keyboard and screen-reader users. A touch
locks the sideways axis once it has travelled `SWIPE_DISMISS_SLOP` (10 px)
sideways, or the upward axis once it has travelled that far up, whichever comes
first; a touch that first drifts past `SWIPE_DISMISS_SCROLL_SLOP` (8 px) downward
is declined for good. The wrapper is `touch-action: none`, because under `pan-y`
the browser claims an upward drag as a scroll and cancels it (WebKit has no
`pan-up`); this covers the card's own box only, so a finger that starts anywhere
else still scrolls the page, and a `pointercancel` snaps the card back. The card
follows the finger 1:1 sideways in either direction, and upward but never below
its rest position; a release past `SWIPE_DISMISS_DISTANCE` (80 px) on the locked
axis dismisses, a shorter one snaps back
(`transition-transform`, none under `prefers-reduced-motion`), and the click a
release fires is swallowed for `SWIPE_CLICK_SWALLOW_MS` (350 ms, the drawer's
own ceiling) so a snapped-back swipe does not also open the note; a cancel arms
no swallow, and past the window a keyboard activation goes through, since a
touch that moved past the tap slop may fire no click at all.
`NotificationBanner.test.tsx` ("swipe to dismiss") pins the thresholds on both
axes, the snap-back, the axis lock, the downward decline, the cancel, the click
swallow, the mouse and desktop no-ops and the reduced-motion class.

### Motion

Enter: slide in from the right with a fade (~220 ms, ease-out; the deck offsets
move on the same curve). Exit, for auto-hide and
dismiss alike: the card shrinks about its top-right corner and travels to the
bell (`computeExitDelta` measures the vector from the card's own rect to
`bellRef`'s) while fading (~260 ms) — the relocation animates the same element
into its new home rather than swapping it out. The presence is
`mode="popLayout"`: a leaving card is taken out of flow the instant it is
dismissed or auto-hidden, so the card behind it moves into the vacated slot
straight away rather than after the exit finishes. That slide is its own
layout transition, 160 ms on an ease-out-expo curve (`[0.16, 1, 0.3, 1]`), short
and front-loaded because it is the one motion a user chases with a second
click: every card's close sits at the same offset from its top-right corner, so
the next close is under a stationary pointer within a few frames and repeated
clicks clear the stack. For the whole of its exit a leaving card takes no
pointer events (`pointerEvents: 'none'` in the exit target, `'auto'` in the
live one), so a click during the overlap reaches the card sliding in, never
the one flying out. Under `prefers-reduced-motion`
(`useReducedMotion`) enter and exit are plain fades (the exit still drops
pointer events) and neither the slot fill nor the deck/list switch does any
layout animation. Escape dismisses the topmost card; arrival never moves
focus.

### Setting

Settings › Notifications › Desktop alerts › "Show a banner while this window
is open", default ON, `localStorage` key `mc-notification-banner`
(`loadBannerEnabled` / `saveBannerEnabled`). A flip is announced same-window via
`MC_BANNER_SETTING_CHANGED_EVENT` and cross-tab via the DOM `storage` event, so
a mounted banner honours it immediately.

### System-notification permission surfaces

`hooks/useNotificationPermission.ts` exposes `Notification.permission` as state
(`unsupported | default | granted | denied`), re-read on window focus and after
its own `request()` settles. In an embedded instance pane that relays its toasts
to the hub it reports `unsupported`: the pane's own verdict is not the user's
switch (the hub's is), so both surfaces below unmount there. Two user-gesture
surfaces call `request()`:

- **Settings › Notifications › Desktop alerts › System notifications when this
  window is in the background** (`SystemNotificationsRow`): `granted` shows
  "Allowed" with a check and no button; `default` offers "Allow system
  notifications"; `denied` states in plain language that the browser blocked
  it and where to turn it back on. Absent entirely when `Notification` is
  undefined.
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
