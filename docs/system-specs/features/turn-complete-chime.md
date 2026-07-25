# Turn-Complete Chime

When the agent finishes a turn, the dashboard plays a notification sound.

## Policy

Every real turn completion chimes — active chat or background, focused window or not. A turn completion (`chat_done` WS event) plays a sound when:

- the event carries a `slot` (slot-less events have no real turn behind them), and
- the client is not in reconnect catch-up replay (`reconnectingRef`, same suppression as `markSlotUnread`) — stale completions replayed on reconnect must not chime-storm; the unread badges already cover them.

Users who want silence set the "Agent replies" category to Silent (or use the main enable toggle).

The decision is the pure function `shouldChimeOnTurnDone()` in `website/src/hooks/notificationEvent.ts`, unit-tested in `website/src/test/notificationEvent.test.ts`.

## Wiring

Sound-only path. No Redux notification, no toast, no badge, no feed entry:

- `useWebSocket.ts` `chat_done` handler dispatches the window event `MC_NOTIFICATION_EVENT` with `kind: TURN_DONE_KIND` (`'turn'`) when the policy passes.
- `useNotificationSound.ts` (already mounted app-wide) resolves the preset via the `turn` category. `'turn'` is a member of `SOUND_CATEGORIES`; with no per-category override it falls back to the `all` default (chime). Its existing 300 ms rate-limit dedupes bursts (e.g. several sessions finishing together).
- Settings: Notifications panel row "Agent replies" — per-category preset override incl. Silent, same chrome as other categories. The main enable toggle and volume slider apply.

## Non-goals

- Native OS banner for turn completion (feed/banner behavior is owned by the notification bus; this feature is a sound cue only).
- Backend involvement: the `turn` kind is synthesized in the frontend and never appears in the notifications feed or `~/.kirocrew` state.
- Attention gating (hidden tab / unfocused window / background-slot heuristics): the chime is an unconditional audible cue for any finished session; per-user silence lives in the sound settings, not in focus heuristics.
