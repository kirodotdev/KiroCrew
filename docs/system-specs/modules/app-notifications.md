# App notification producers

## Overview

Installed apps declare notification channels in `app.json` and publish through `POST /api/notifications/push` with an app token. `dashboard.handlers.notifications_push.api_push_notification` resolves the producer from the verified token rather than the request body, requires a manifest-declared channel, and uses the state-owned rate limiter. `NotificationBus.push` enriches the payload and calls `DashboardState._deliver_note`, which redacts, applies channel settings, appends the note, broadcasts it, and queues persistence, then schedules the notification bridge's chat-transport fanout without awaiting it.

### Reaching the endpoint from an entryPoint backend

A `backend.entryPoint` app runs as a separate loopback process, so it must learn the gateway's own address before it can push. The gateway injects that at spawn time as two generic environment variables (see `docs/app-kit/api-reference.md` -> Backend Environment Variables): `KIROCREW_GATEWAY_ORIGIN`, the gateway's `http://127.0.0.1:<bound port>`, and `KIROCREW_GATEWAY_ORIGIN_PROOF` (`HMAC-SHA256(app_secret, origin)`, injected only when the app has a `.app_secret` and the origin is set). The origin is set ONLY from the port the gateway ACTUALLY bound (its exported `KIROCREW_BOUND_PORT`, numeric and in `1..65535`), never the app's own `PORT`, an inherited `KIROCREW_PORT`, a config value, a default, or a request-derived value. Without that bound-port evidence both variables are omitted, so a backend that needs a callback base fails closed (stays dormant) rather than pushing to a guessed address. When the origin is present the backend recomputes the proof with its owner-only `0600` `.app_secret` to confirm the origin is one this gateway minted, then pushes to `POST {KIROCREW_GATEWAY_ORIGIN}/api/notifications/push`, authenticating with its app secret. In-gateway route apps (`backend.routes`) have no separate process and push in-process (see `ops-mission-control` `notify_out`), so they need neither variable.

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

`DashboardState._deliver_note` redacts the note, applies settings, appends it to the in-memory log, broadcasts it, and queues `_persist_notification`. On a running event loop, delivery appends and rewrite mutations share `_notification_io_executor`, a single-worker executor; `DashboardState._rewrite_notifications_async` awaits rewrites for delete, acknowledgement, unacknowledgement, clear, and acknowledge-all paths. Submission order is load-bearing: a rewrite queued after an append cannot be overtaken, preventing deleted rows from reappearing. Snapshot copies prevent later loop-side mutations from changing the data being serialized. `test_dashboard.py::test_deliver_note_offloads_persist_on_running_loop` and `::test_ack_persists_durably_before_return` cover these guarantees; synchronous callers persist inline. The bridge leg is scheduled after this local work completes and is never awaited, so a chat transport cannot delay or reorder dashboard delivery.

## Testing

`test/test_notifications_push.py` covers app-token authorization, manifest channel enforcement, bounded/chunked bodies, rate-limit and refund semantics, falsy valid fields, signing-payload coverage, lazy registration, and sink/persistence failure paths. `test/test_dashboard.py` covers persistence, load-time redaction, ordered executor persistence, and durable rewrite behavior. `test/test_notification_settings.py` covers settings persistence, protected channels, sink application, badge behavior, and settings APIs.

## Channel lifecycle

Channels register lazily on the first push to each declared channel. App lifecycle routes call `NotificationBus.unregister_app_channels(app_name)` while holding the app lifecycle lock; disabling or uninstalling an app removes its registered `<app>.*` channels, and a later enabled push registers them again. `test_notifications_push.py::TestUnregisterAppChannels` pins boundary-safe removal and preservation of system channels. `RESERVED_APP_NAMES` rejects `system` during manifest validation, and `_resolve_app_channels` rejects it again, preventing app channels from shadowing `system.*`.

## Per-channel settings

`notifications.settings.ChannelSettings` is state-owned, writes atomically, and loads an invalid settings file as empty defaults. `ChannelSettings.apply` runs in `DashboardState._deliver_note` before append and broadcast, so disk and clients receive the same user view while `NotificationBus` remains policy-free.

`deliver_to` is an egress authorization, so `notification_settings.json` is fenced from the agent on both gates rather than one: `security._CREW_SECRET_LEAVES` refuses the file tools, and `sandbox._CREW_HIDDEN_LEAVES` bind-masks it for every sandboxed process, because a spawned command's `open()` never reaches the tool gate. Both the leaf and the writer's staging directory are materialised before a namespace spawn, since `mount(2)` cannot mask an absent path and an install that has never saved a setting has neither. The write stages in that masked directory rather than beside the target: a temp in the data-home root carries the real routing bytes at a name the leaf mask does not cover.

- A muted non-protected channel remains in history but receives `silenced: true` and passive priority. `test_apply_mute_forces_passive_and_silenced` and `test_muted_channel_excluded_from_badge` pin the visibility and badge invariant.
- A priority override replaces the effective producer or channel priority.
- `system.approval` is protected. `ChannelSettings.update` rejects muting or lowering it, and `ChannelSettings.apply` enforces the same floor for hand-edited settings; `test_protected_channel_cannot_be_muted_or_lowered` and `test_apply_ignores_noncritical_override_on_protected_channel` cover both boundaries.

Dashboard-user settings routes expose the union of registered channels and stored settings through `api_notification_channels`; `api_notification_channel_settings` accepts mute and priority updates, clears an override for `priority: null`, and broadcasts `notification_channel_settings`.

## Notification bridge (chat-transport egress)

`notifications.bridge.BridgeDispatcher` is the bus's second sink: it routes matching notes to the user's connected chat transports as owner DMs. `DashboardState._deliver_note` calls the local sink first and synchronously, then hands the note to `_schedule_bridge_after_persist`. Dashboard delivery therefore never awaits a transport, and the bridge reads the note the local sink has already redacted and settled — so routing keys off the **effective** priority, meaning a user override decides what routes and a muted channel reads `passive`.

The bridge leg waits on **durability**, not only on the local sink. The persist is fire-and-forget (`run_in_executor`) and the app and agent push handlers turn its failure into a `500` the producer retries, so egressing before the write lands would let a failed-then-retried push deliver the same chat message twice. A cancelled, raising or falsy persist withholds the bridge leg and logs it; the dashboard keeps the note either way, which is what makes the trade acceptable — a duplicate dashboard row is recoverable and a duplicate DM is not. The note is snapshotted before that await, because acknowledgement and the TTL sweep mutate the stored row in place. `NotificationCoordinator.deliver` RETURNS that delivery's durability handle -- the persist future on a running loop, the inline write's boolean off it -- and `_deliver_note` passes it straight to the bridge. Returned rather than left on the state because two off-loop producers run concurrently, so a shared field can hand one note the other's verdict, bridging a failed write or withholding a good one. `state.last_notification_persist` is still set for the push handlers that read it.

The inline path is the one with no recovery at all -- no future to await and no `500` to make the producer retry -- so a falsy inline write withholds the leg rather than being read as success, which is what `persist-before-you-publish` requires.

Because each leg is a task, shutdown can land between `schedule` and the send. `GatewayOrchestrator._shutdown` therefore awaits `BridgeDispatcher.drain(timeout=2.0)` while the transports are still open, before the closes it queues into its cleanup tasks. Bounded, and a timeout is not an error: the note is already durable and on the dashboard before the bridge is ever scheduled, so the worst outcome of a slow leg is one chat DM the owner reads on the dashboard instead, and a shutdown that waited longer than that for a secondary surface would be the worse trade. A raising drain is logged and does not stop the shutdown. The test asserts the ORDER against the transport close rather than only that the drain was called, since a drain running after the close is the defect.

A task is not the only thing a drain has to see. Two producers accept a delivery before any task exists, and a drain reading `_tasks` alone returned immediately in both windows: an off-loop producer queues `_spawn` on the gateway loop with `call_soon_threadsafe`, and a persist-gated delivery does not call `schedule` until its durable write lands. Both therefore `reserve()` on the dispatcher before arming the thing that will later create the task, and `release()` only after `schedule` has entered it — one counter, because `drain` asks a single question and a second counter is a second thing a later producer can forget to join. A reservation is retired on the paths that decide NOT to deliver too (a closed loop, a failed persist), since an obligation that never retires turns a lost note into a shutdown that waits out its whole timeout. `test/test_notification_bridge_drain_window.py` pins both windows, both orderings, and that a drain with nothing owed still returns at once.

`host_session_key` defaults to the shared `HOST_SESSION_KEY` sentinel rather than a readable string, because the value selects a governance profile instead of labelling one: `_infer_surface` maps `"_host"` to the `host` surface, while a bare `"dashboard"` matches no prefix and falls through to `slack` — which would judge "may the host send to Slack" under a Slack surface profile and skip an operator's `surface:host` denial. A test asserts the default both equals the constant and infers `host`.

The dispatcher takes a sink resolver and a settings reader and holds no reference to the bus, so it has no path that can publish a note. `test_notification_bridge.py::LoopSafetyTests` pins that by parsing the module and asserting its AST names neither bus class, the dashboard's bus attribute, nor a `push`/`notify` call; a further test plants a real publish and asserts the same scan catches it.

### Routing rule

Two optional `ChannelSettings` fields per source channel:

| Field | Type | Notes |
|-------|------|-------|
| `deliver_to` | list of transport ids | Validated against the KNOWN set (`slack`, `discord`, `telegram`, `webex`, `wecom`), never the connected set, so a transport that is down keeps its persisted route. A list or tuple specifically, not anything iterable: a mapping yields its KEYS, so `{"slack": false}` would otherwise arm Slack off a value that says not to. Absent or empty means no bridging. |
| `deliver_min_priority` | `critical`, `default`, or `all` | Floor for what routes. `critical` is written as the default when a route is armed without one. `all` includes passive notes, and is the only floor that routes a muted channel. |

`ChannelSettings.update` validates both before taking its lock, so a rejected rule reaches neither memory nor disk, and `api_notification_channel_settings` maps a `ChannelSettingsError` to `400`. `deliver_to: []` disarms the route and `deliver_to: null` clears it; both drop the floor with it, so a disarmed entry keeps no floor for an absent route. `ChannelSettings.apply` neither reads nor writes these fields: `apply` mutates the note every sink sees, and routing is one sink's decision about a note it must not change. `api_notification_channels` also returns `bridge_transports`, one row per routable id carrying two separate facts: `connected` (the transport itself is up) and `bridgeable` (a bridge sink exists for it in this build, from `state.bridge_sink_implemented`). They are separate because one flag would have to lie about the other — a connected transport with no sink yet routes to an audited skip, and a picker shown only `connected` would offer a row that can never deliver. Validation still uses the known set, so a transport that is merely down keeps its saved route.

**Both routing fields are owner-only, enforced where the row is produced rather than at the transport layer.** They decide whether the owner's notifications leave the machine as chat DMs, which is the owner's call and not an installed app's, so `api_notification_channel_settings` refuses an app-token caller that names either with an audited `403` (`delivery_routing_owner_only`), and `api_notification_channels` withholds them from an app-token read. `muted` and `priority` are presentation state on a channel with no route, and keep their existing access there, so an app already managing mute state keeps working. On a channel whose route is ARMED they are not presentation state: `ChannelSettings.apply` rewrites the note's priority and `priority_clears_floor` ranks that effective value against the rule's floor, deliberately, so muting a channel also silences its DMs. An app PUT of `priority: "passive"` or `muted: true` therefore reaches the floor comparison and suppresses a DM the owner armed without ever naming a routing field. `ChannelSettings.update` refuses that pair from an app-token caller **only while the stored route is armed** (same audited `403` code), which closes that path and leaves the old contract intact everywhere it was the whole story. The refusal lives in the writer method rather than in the handler because it has to be atomic with the write it guards: a handler-side check reads the stored route lock-free and then awaits a separate `to_thread` write, so an owner PUT arming the route commits in between and the app's mute merges onto an armed entry. Under `update`'s lock the decision reads the same entry the write merges onto. `ChannelSettingsAuthError` carries it back, and it subclasses `ChannelSettingsError` so a caller handling only the base still refuses the write rather than letting it through.

**Arming a route also discards the display overrides it inherits.** The refusal above fires only while a route is ALREADY armed, so writing the pair first and letting the owner arm afterwards reaches the same end without ever tripping it: `apply()` forces a muted armed channel to `passive`, and the retained value then suppresses every DM on the route until the owner notices. So on the unarmed-to-armed transition `update` drops `muted` and `priority` before the entry is persisted. Only the values the request does not itself speak for, so an owner arming and muting in one PUT gets the mute they asked for; and only on the transition, so an already-armed channel keeps the owner's settled preferences. Shutdown has the matching ordering property: the gateway cancels the notification PRODUCERS before draining the bridge, because `cancel_all` emits terminal announcements whose bridge legs schedule when they are emitted, and a drain that runs first cannot flush them.

The refusal fires on a request that NAMES a routing field, so it is not the whole contract: a PUT that names neither a routing field nor the display pair is a supported flow that skips it, and both the reply and the `notification_channel_settings` WS frame carry the WHOLE stored row. Two further withholdings close that. The PUT's own reply drops the keys for an app-token caller, which is response shaping and not a mutation -- the owner's route must survive an app's request. Since the display pair is refused on an armed channel, that reply shaping is now defence in depth behind the `403` rather than the only thing standing between an app and the route. The WS frame is stripped per client at `websocket_hub._serialize_for_client` via `ws_event_scope.channel_settings_for_app`, because that frame is attributable by channel prefix and so reaches an app scoped to its own channel whenever ANY writer changes a setting on it, including an owner PUT the handler-side withholding cannot see. Owner dashboards are exempted at that same chokepoint, so the Settings page still renders its armed route; the paired tests assert both directions, since a strip that applied to every client would be a blanket break rather than a fence.

The check cannot be delegated to `app_token_path_allowed`, because that exclusion is declarative and defeatable: it grants only `/api/notifications/push` and its comment states app tokens must not reach `/api/notifications`, but its final clause still honours the app's own `permissions.api`, and `_api_pattern_matches` treats a bare `/api/notifications` prefix as covering every child path. An owner installing an app that declares that prefix is an ordinary supported flow, so the route IS reachable and the handler is what stands. The guarded names live in one place, `notifications.settings.DELIVERY_SETTING_KEYS`, which every carrier derives its check from -- the PUT's refusal, the PUT's reply, the channels GET, and the WS strip, the last importing the set rather than copying it -- so a third routing field added later is refused and withheld without any of them being edited. A test asserts that import by IDENTITY, because a copied set compares equal today and diverges silently on the day a key is added, which is the failure the single declaration exists to prevent.

### Dispatch pipeline

Per routed transport, each leg independent of the others:

1. **Governance**, on a worker thread: `vet_and_audit("capabilities.messaging", "")` then `vet_and_audit("channels", <transport>)`, both `fail_closed=True`, for the INTERSECTION of every subject that could own the note. Any denial denies.
   * The **host surface** always, via `HOST_SESSION_KEY`, so an operator's host-level denial binds every bridged delivery.
   * The **producing app**, as `app=<name>` from the bus's server-set `source` (`app:<name>`, which a request body cannot override), so an app whose own profile denies messaging cannot egress on a permissive surface.
   * The **session the note claims**, when it names one -- a cron, a hook, an agent turn. `meta` merges flat and `_RESERVED_NOTE_KEYS` covers none of `session_key`, `slot` or `caller`, so gateway code and an app-token request body write those through the same door and the note keeps no record of which did. The claim is therefore ADDED, never substituted, and that asymmetry is what makes consulting it safe: a real producer's profile is honored, while a forged claim can at worst deny the forger's own note. It can never lift the host or app denial.

     Two producers could not name themselves, and in both the omission was total rather than partial. `send_notification` publishes the server-fixed `source="system"` on `system.agent`, and `_task_notify` (`slack/gateway.py`) publishes `system.taskrunner` carrying only `{"task_id": ...}` -- a task id names a task, not a session -- so in each case the ONLY subject was the host surface. An agent whose profile denies `channels/slack` was refused by `send_message` on that transport and then egressed to the same Slack DM through the bridge: one producer, one transport, one policy, two answers. `api_notification_agent_push` now sets `meta={"session_key": ...}` from the `X-Session-Key` it already reads for the missing-slot refusal, so the refusal and the attribution judge the same string; `_task_notify` now sets it from the originating `session_key` it already threads from `start_background` and already uses to pick the channel ladder, so the delivery target and the attribution judge the same string. Those values are server-set on those paths specifically because neither passes a request body's `meta` into the payload; each is still only ADDED to the subjects, because the bridge cannot tell a server-set claim from a body-set one and added-only is what keeps a forged claim unable to widen anything. A producer with no session key adds no subject, which is the pre-existing host-only behaviour and stays correct for a genuinely host-produced note.

     That is the rule, not a list of exceptions: a note carrying a session's own OUTPUT names that session, and a host lifecycle notice ABOUT a session does not. The cron delivery paths (`_deliver_script_result`, and the completion notes in `_note_complete`) carry a cron's output and already name `cron:<job.id>` as the SEL subject for the same note, so they are the same shape and remain unnamed for governance -- host-only, which is main's pre-existing behaviour for them and no worse than before this bridge existed, but inconsistent with the rule above. Extending the rule there changes which deliveries an operator's cron profile can withhold across the whole cron surface, so it is tracked separately rather than folded in here. Heartbeat and nudge-expiry notices resolve no originating session at all and are correctly host-only.

     `slot` is QUALIFIED to `dashboard:<slot>` before it is used, because it is the only one of the three that is structurally a fragment rather than a session key: the frontend stores a bare slot id (`chat-1`), `_infer_source` recognises no prefix in it, and it falls through to the `slack` default. Both directions of that are wrong, and the second is the one a user notices: an operator's `surface:dashboard` denial is not consulted, AND a `surface:slack` denial is applied to a note a dashboard slot produced -- which, because the claim can only narrow, withholds a delivery that should have gone out. A value already carrying a prefix passes through unchanged, so a producer that wrote a full key here does not become `dashboard:dashboard:chat-1`. `session_key` and `caller` are full keys already, so whatever surface they infer is theirs by the same function the rest of the gateway uses.

   Each subject carries its OWN app bind, and the pairing is the mechanism rather than a detail. `resolve_active_scope` returns the FIRST bound profile in a fixed precedence and the app bind outranks the surface bind (`governance_profiles.py`, `if app:` ahead of the surface lookup), so ONE call can only ever answer for one profile. Carrying `app=` on the host lookup would therefore have a permissive app profile answer "may the host send", silently skipping the operator's `surface:host` denial; omitting it everywhere would have a surface profile answer for the app. So the app is bound on its own lookup alone, and the host and claimed-session lookups pass `app=""`. Without that split the loop is not an intersection at all, only N reads of the same profile.

   This is pinned by tests that resolve REAL bound profiles rather than patching `vet_and_audit`, because an argument-shape assertion cannot see what the resolver does with the arguments: a permissive `app` profile against a denying `surface:host` profile must withhold, a denying `app` profile against a permissive host must withhold, and both permitting must deliver. The third case is what stops a guard that merely refuses everything from satisfying the first two.

   SEL attribution uses `source` for the same trust reason. The app's own lookup keeps the host session key, because SEL records the session rather than the app and the bridge does run as the host while deciding about the app's note.
2. **Sink resolution**: `DashboardState._bridge_sink_for` per delivery, not cached at boot. `None` means configured but unable to receive, which is an audited skip rather than an error.
3. **Egress budget**: a per-transport token bucket (`BridgeRateLimiter`, 20 per 5 minutes, burst 5) so an `all`-floor route cannot turn a producer loop into a chat flood. Both halves of that are measured rather than asserted in prose: `allow` reads `time.monotonic` directly, so `RateLimiterTests` replaces the bridge module's `time` with a hand-advanced clock and drives the real method -- one token per `BRIDGE_WINDOW_SECS / BRIDGE_TOKENS_PER_WINDOW` seconds, and a ten-window idle still releasing only `BRIDGE_BURST`. The cap rather than the rate is what bounds the worst case, since a producer silent for an hour would otherwise post its whole backlog at once. The ingress limiter does not cover any of this: it is per app, while this protects one chat surface from every producer at once. Charged AFTER the sink resolves and after governance, because the budget caps what is DELIVERED: a skipped or denied leg sends nothing, so charging it would let a transport that is merely disconnected drain the burst and throttle the first real delivery after it reconnects.
4. **Redaction and render**: `render_bridge_text` produces one neutral plain-text form (priority marker, title, body, internal deep link, channel) and the dispatcher redacts credentials and exfiltration URLs before handing it to a sink. That repeats the local sink's redaction deliberately, so the guarantee holds for the bridge standing alone rather than only for the pair; a redactor that cannot run withholds the content instead of passing it through.
5. **Send exactly once**, then **SEL audit** through `log_api_access` with `source="notification-bridge"`, `operation="notification_bridge.<transport>"`, and the note's `source` as caller. The dispatcher does not retry a failed send: a send that fails after the platform accepted the message is indistinguishable from one that never landed, so a retry can post the same notification twice, and the note is already on the dashboard -- a dropped leg costs the user a surface while a double post costs them a wrong one. Retry a transport can make safely belongs in that transport's own policy, where it knows which failures are idempotent (the Slack sink retries DM *resolution* there, which is).

`asyncio.gather(..., return_exceptions=True)` keeps one leg's failure from suppressing another's delivery, and `schedule` never raises into the producer.

`schedule` returns a task only when it created one on the CALLER's own loop, so `None` means "nothing here to await" rather than "not delivered". An off-loop producer is handed to the gateway loop through `DashboardState.serving_loop` and `loop.call_soon_threadsafe`, which also keeps the task set mutated only from the loop thread. That path is not hypothetical: `code_review_sage/backend/routes.py` publishes its run-finished notice as `asyncio.to_thread(state.notify, ...)`, deliberately off-loop because the delivery sink writes to disk, and in that thread `get_running_loop` raises although the gateway loop is alive. Only an unreachable loop -- a CLI, a boot-time note, a synchronous test -- is a real skip, and the dashboard still holds the note there.

### Transport sinks

`slack.notification_sink.SlackBridgeSink` is phase B1's only sink. Slack is deliberately absent from `DashboardState.channel_transports` (it keeps `slack_client` for the rich streaming mirror), so the sink is built from that client plus `owner_id`, resolves the DM through `slack.retry.open_dm_with_retry`, and renders through `render_for_slack` — the same redaction boundary every other Slack egress uses. `slack_sink_for` returns `None` while `slack_socket_connected` is false. Discord, Telegram, Webex and WeCom resolve to `None` until phase B2 adds their sinks over the shared `MessagingTransport` registry; a route to one of them is still validated, persisted and audited as a skip, so nothing about the rule changes then.

`security_posture._REDACTION_SINKS` carries the bridge as a registered output boundary, which `test_security_posture.py::TestOmissionDetection` requires of every module that calls a redactor: a new egress path cannot be added without someone deciding whether it is a sink or a non-egress caller.

`test/test_notification_bridge.py` covers rule normalization, floors, rendering, governance, redaction, retry, throttling, isolation and the loop invariant; `test/test_notification_bridge_wiring.py` covers the composite-egress seam, sink resolution, the routing API, and one end-to-end critical note reaching a Slack DM.

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
component that emits sound. The feed toast's page-context `Notification`
constructor (`website/src/hooks/useNativeNotification.ts`, see "OS toast"
below) passes `silent: true`, so the OS toast never adds its own system chime
on top of the WebAudio tone. A browser that
ignores `silent` degrades to the prior double-sound behavior and no worse.

### Sound events

Two sound kinds are synthesized by the websocket layer. `TURN_DONE_KIND`
(`'turn'`, on `chat_done`) is sound-only: it never appears in the feed (no Redux
entry, no toast, no badge). `APPROVAL_KIND` (`'approval'`, on an `approval`
frame) is synthesized for sound, but the same approval frame *separately* adds an
approval notification to the feed — so approval both chimes and shows a feed
entry, and the two are independent (the feed entry is also what carries the
approval to the OS toast). Both chimes are suppressed during reconnect
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

## OS toast (client)

`website/src/hooks/useNativeNotification.ts` is the **single constructor** of a
page-context `Notification` for a feed note. It watches the count of unacked,
unsilenced notes in the Redux store and, when the count grows, posts one toast
carrying the newest note's title and flattened body, tagged with its
`approval_id` / `job_id` / `task_id` (or `kirocrew-notif`) so a burst about
one subject replaces rather than stacks. An `approval` frame reaches the OS
through the feed entry `useWebSocket` dispatches for it; the socket layer
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
constructed in `useWebSocket` on `chat_done`) is a separate, default-OFF
surface with its own `kirocrew-chat-done:<slot>` tag; it shares only the away
predicate.

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
connection and for the feed note it synthesizes from an `approval` frame (the
note carries the owning `slot`, so `targetsCurrentView` skips it while that
chat is on screen and its inline permission card is visible; an approval with
no slot banners on every surface). It never reads the Redux list: the boot `fetchNotifications`
snapshot and reconnect refetches fill the store with history, and history is
never bannered. `useWebSocket` withholds the event during a reconnect catch-up
(`reconnectingRef`) for both frames, the same window that mutes the turn-done
chime.

### Priorities

| Priority | Banner |
|---|---|
| `critical` | stays until clicked, dismissed, or acted on; the live region is `role="alert"` while one is pending |
| `default` | auto-hides after `BANNER_AUTO_HIDE_MS` (6 s). Every pending default card shares ONE timer, restarted by each default arrival and paused while the stack is hovered or holds focus. The pointer and keyboard are tracked as two separate holds and the clock resumes only when BOTH have let go. A card's removal destroys ownership without firing the release event, so the holds are re-read after every change to the deck: FOCUS is owned by an element (held while the stack still contains the active one, released when its holder unmounts), the POINTER by the container (a removal does not move that boundary, so only a real pointer-leave — or an empty deck — releases it) |
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
