/** Shared contract between useSSE.ts (dispatcher) and useNotificationSound.ts (listener). */
export const MC_NOTIFICATION_EVENT = 'mc-notification' as const
export const MC_SOUND_SETTINGS_CHANGED_EVENT = 'mc-notification-sound-changed' as const
export const MC_OS_SETTINGS_CHANGED_EVENT = 'mc-notification-os-changed' as const

export interface McNotificationDetail {
  kind?: string
  /** Human title of the underlying notification (bus note title, or a
   * synthesized label for `turn` / `approval` events). Consumed by the OS
   * notification surface — the sound surface only reads `kind`. */
  title?: string
  /** Human body of the underlying notification. */
  body?: string
  /** Dashboard-internal deep link of the underlying note (`Notification.url`,
   * validated server-side as a path). Banner click target. */
  url?: string
  /** Owning chat slot, when the event belongs to a session. Lets the OS
   * surface name the session in the banner and open it on click. */
  slot?: string
  /** Stable collapse tag: a retrigger of the same underlying event replaces
   * the previous banner instead of stacking a duplicate. */
  tag?: string
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
