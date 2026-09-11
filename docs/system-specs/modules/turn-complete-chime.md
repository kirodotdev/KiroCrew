# Turn-Complete Chime

When the dashboard receives an eligible `chat_done` WebSocket event, it dispatches a frontend turn-sound event. `useNotificationSound()` turns that event into audio only when the current sound settings and browser audio state permit playback.

## Policy

`notificationEvent.ts:shouldChimeOnTurnDone()` accepts a slot-bearing event while `reconnecting` is false. `notificationEvent.test.ts` pins both exclusions: slot-less events and events received while the reconnect catch-up flag is set. These gates are load-bearing because they keep malformed events and reconnect replay from creating extra sound requests.

The policy does not inspect the active slot, document focus, or tab visibility. It also does not inspect a completion outcome: every slot-bearing `chat_done` event that passes the reconnect gate requests the turn sound, including a terminal event emitted by a stop or cancellation path.

## Wiring

- The backend emits `chat_done` with a `slot` from terminal chat paths, including `chat_runner.py`, `chat_orchestrator.py`, and `chat_handlers.py`.
- `useWebSocket.ts` passes the event slot and `reconnectingRef.current` to `shouldChimeOnTurnDone()`. When it returns true, the handler calls `notificationEvent.ts:dispatchMcNotification(TURN_DONE_KIND)`.
- `dispatchMcNotification()` emits `MC_NOTIFICATION_EVENT` with the frontend-only `turn` kind and contains listener failures so a sound listener cannot interrupt WebSocket handling.
- `App.tsx` mounts `useNotificationSound()` application-wide. Its listener resolves the `turn` category through `useNotificationSound.ts:presetForKind()`: an explicit `turn` preset wins, otherwise the `all` preset supplies the fallback. `notificationEvent.test.ts` and `useNotificationSound.test.ts` pin the category and fallback behavior.
- `useNotificationSound()` suppresses playback when sounds are disabled, volume is silent, or the selected preset is Silent. It also coalesces closely spaced audible notification events; `useNotificationSound.test.ts` pins that cooldown behavior. This prevents completion bursts from stacking audio.
- `ThemeExperienceLayer.tsx` also observes `MC_NOTIFICATION_EVENT`. An enabled, consented theme with a `notification` audio trigger can play its manifest sound for the same turn event.
- `NotificationsPanel.tsx` exposes the localized **Agent replies** row for the `turn` category, including preset overrides and Silent. Its main sound toggle and volume control apply to this category.

The chime branch does not add a notification-feed record, and creates no native OS notification of its own. The surrounding `chat_done` handler still performs ordinary completion work, including marking a background slot unread; that badge behavior is separate from the sound event.

## Sibling: the opt-in background-completion toast

A native OS toast for the same `chat_done` event lives beside the chime in the handler, on its own gate, and is **not** part of the chime policy above. It exists because the chime cannot say WHICH session finished, which is what a user tracking several background threads needs.

- `chatCompleteNotify.ts:shouldNotifyOnChatComplete()` is a separate predicate. It repeats the chime's two suppressions (slot-less events, reconnect catch-up replay) and adds three of its own: the user opted in, the browser reports `Notification.permission === 'granted'`, and the user is away — `document.hidden || !document.hasFocus()`. Reusing `shouldChimeOnTurnDone()` here would be wrong: that predicate deliberately ignores focus and visibility.
- The opt-in is **default OFF**, persisted per device in `localStorage` under `mc-notify-chat-complete` (`'1'` on, anything else off), and exposed as the **Desktop alerts** row in `NotificationsPanel.tsx`.
- `chatCompleteNotify.ts:saveChatCompleteNotify()` calls `Notification.requestPermission()` when the toggle is switched on while the permission is still `default`. Without this the feature is a silent no-op: `useNativeNotification.ts` is the only other place that asks, and only when an unacked feed notification arrives.
- `useWebSocket.ts` constructs the toast when the predicate passes: title is the finishing slot's `title` from `dashboard.slots` (falling back to the slot key), body is `hooks.useWebSocket.response_ready`, and `tag` is `kirocrew-chat-done:<slot>` so concurrent completions coalesce per session rather than overwriting one another. Construction is wrapped in `try`/`catch` for the same reason as the approval toast — page-context `Notification` throws on Android Chrome.
- `chatCompleteNotify.test.ts` pins the default-off state, the away gate, the permission request, and the title/body/tag the handler emits.

## Non-goals

- A dedicated backend notification kind or persisted turn-notification record. The backend supplies `chat_done`; `notificationEvent.ts` synthesizes `TURN_DONE_KIND` in the frontend, and `useNotificationSound.ts` documents it as sound-only rather than a feed kind.
- Focus or visibility gating **of the chime**. The chime policy intentionally requests the sound event for active and background slots alike; users control audible playback through sound settings, while the listener's cooldown limits bursts. The sibling toast above gates on focus and visibility, which is why it is a separate predicate rather than a parameter on this one.
- A feed record, toast, or badge for the opt-in native notification. It is an OS-level toast only; the unread badge already covers the in-app surface.
