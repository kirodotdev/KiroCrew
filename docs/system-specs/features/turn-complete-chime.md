# Turn-Complete Chime

When the agent finishes a turn and the user is not watching, the dashboard plays a notification sound.

## Policy

Chime only when attention is elsewhere. A turn completion (`chat_done` WS event) plays a sound when ALL of:

- the event carries a `slot`, and
- the client is not in reconnect catch-up replay (`reconnectingRef`, same suppression as `markSlotUnread`), and
- the user is not watching the reply land: the finishing slot is not the active slot, OR the tab is hidden (`document.hidden`), OR the window is unfocused (`!document.hasFocus()`).

A turn finishing in the active chat of a focused, visible window is silent — the user saw it stream.

The decision is the pure function `shouldChimeOnTurnDone()` in `website/src/hooks/notificationEvent.ts`, unit-tested in `website/src/test/notificationEvent.test.ts`.

## Wiring

Sound-only path. No Redux notification, no toast, no badge, no feed entry:

- `useWebSocket.ts` `chat_done` handler dispatches the window event `MC_NOTIFICATION_EVENT` with `kind: TURN_DONE_KIND` (`'turn'`) when the policy passes.
- `useNotificationSound.ts` (already mounted app-wide) resolves the preset via the `turn` category. `'turn'` is a member of `SOUND_CATEGORIES`; with no per-category override it falls back to the `all` default (chime). Its existing 300 ms rate-limit dedupes bursts (e.g. several background slots finishing together).
- Settings: Notifications panel row "Agent replies" — per-category preset override incl. Silent, same chrome as other categories. The main enable toggle and volume slider apply.

## Non-goals

- Native OS banner for turn completion (feed/banner behavior is owned by the notification bus; this feature is a sound cue only).
- Backend involvement: the `turn` kind is synthesized in the frontend and never appears in the notifications feed or `~/.kirocrew` state.
