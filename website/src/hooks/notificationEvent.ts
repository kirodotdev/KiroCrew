/** Shared contract between useSSE.ts (dispatcher) and useNotificationSound.ts (listener). */
export const MC_NOTIFICATION_EVENT = 'mc-notification' as const
export const MC_SOUND_SETTINGS_CHANGED_EVENT = 'mc-notification-sound-changed' as const

export interface McNotificationDetail {
  kind?: string
}

/**
 * Sound kind for agent turn completion. Synthesized by the websocket layer on
 * `chat_done` — it never appears in the notification feed (no Redux entry, no
 * toast, no badge); it exists only so useNotificationSound can key a per-category
 * sound for "the agent finished replying".
 */
export const TURN_DONE_KIND = 'turn' as const

/**
 * Whether a finished turn warrants a chime. Principle: never chime at a user
 * who is already watching the reply land. Chime only when attention is
 * elsewhere — the turn finished in a background chat, the tab is hidden, or
 * the window is unfocused. Reconnect catch-up replays never chime (mirrors the
 * markSlotUnread suppression on the same event).
 */
export function shouldChimeOnTurnDone(opts: {
  slot: string | undefined | null
  activeSlot: string | null
  reconnecting: boolean
  hidden: boolean
  focused: boolean
}): boolean {
  if (!opts.slot || opts.reconnecting) return false
  if (opts.slot !== opts.activeSlot) return true
  return opts.hidden || !opts.focused
}
