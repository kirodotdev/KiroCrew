/** Shared contract between useSSE.ts (dispatcher) and useNotificationSound.ts (listener). */
export const MC_NOTIFICATION_EVENT = 'mc-notification' as const
export const MC_SOUND_SETTINGS_CHANGED_EVENT = 'mc-notification-sound-changed' as const

export interface McNotificationDetail {
  kind?: string
}

/**
 * Fire the `mc-notification` DOM event for a sound `kind`. Centralizes the
 * dispatch + defensive try/catch that three websocket call sites (feed, approval,
 * turn-done) previously duplicated inline, so the `CustomEvent` wiring and its
 * error handling live in one place. Swallows listener errors (a broken listener
 * must not break WS handling) and logs once here.
 */
export function dispatchMcNotification(kind: string): void {
  try {
    const detail: McNotificationDetail = { kind }
    window.dispatchEvent(new CustomEvent(MC_NOTIFICATION_EVENT, { detail }))
  } catch (err) {
    // Last-resort surface for a broken notification listener; no app logger on this DOM-event path.
    // eslint-disable-next-line no-console
    console.warn('mc-notification listener error', err)
  }
}

/**
 * Sound kind for agent turn completion. Synthesized by the websocket layer on
 * `chat_done` — it never appears in the notification feed (no Redux entry, no
 * toast, no badge); it exists only so useNotificationSound can key a per-category
 * sound for "the agent finished replying".
 */
export const TURN_DONE_KIND = 'turn' as const

/**
 * Sound kind for tool-approval prompts. Synthesized by the websocket layer on
 * `approval` frames — the agent is blocked waiting for a user decision. Uses
 * a distinct preset from turn-complete so the user can distinguish "needs my
 * action" from "finished, no action needed" without looking.
 */
export const APPROVAL_KIND = 'approval' as const

/**
 * Whether a finished turn warrants a chime. Policy: every real turn
 * completion chimes — active chat or background, focused or not — so the
 * user always gets an audible cue when any session finishes. Two
 * suppressions remain: slot-less events (no real turn behind them) and
 * reconnect catch-up replays (mirrors the markSlotUnread suppression;
 * stale completions replayed on reconnect must not chime-storm — the
 * unread badges already cover them).
 */
export function shouldChimeOnTurnDone(opts: {
  slot: string | undefined | null
  reconnecting: boolean
}): boolean {
  return !!opts.slot && !opts.reconnecting
}
