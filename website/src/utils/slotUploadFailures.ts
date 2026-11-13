/**
 * Attachment failures waiting for their chat to come on screen.
 *
 * An upload or capture that fails after the person switched chats belongs to the
 * chat it started in, not the one on screen. It is held here, outside the
 * transcript (no history rebuild can drop or reorder it), and the chat page moves
 * it into its upload banner when that chat is next shown. Module scope, so a
 * failure that lands after the page unmounted is still there when it remounts.
 *
 * An entry leaves when its banner takes it (`takeSlotUploadFailure`) or its chat
 * is closed (`forgetSlotUploadFailure`), and the table keeps only the newest
 * `MAX_HELD`, so a long-lived tab cannot grow it without bound.
 */

/** Chats with a failure waiting at once; the oldest is dropped past this. */
export const MAX_HELD = 50

// A `Map` iterates in insertion order, so the first key is the oldest entry.
const waiting = new Map<string, string>()

/** Called with the chat a failure was just held for. */
type HeldListener = (slot: string) => void
const listeners = new Set<HeldListener>()

/** Hold `message` for `slot`; a later failure for the same chat replaces it.
 *  A mounted page showing `slot` hears of it at once (`onSlotUploadFailureHeld`),
 *  so a failure held by an earlier page instance is not missed. */
export function holdSlotUploadFailure(slot: string, message: string): void {
  waiting.delete(slot)
  waiting.set(slot, message)
  while (waiting.size > MAX_HELD) waiting.delete(waiting.keys().next().value as string)
  for (const listener of [...listeners]) listener(slot)
}

/** Hear of each failure held from now on; returns the unsubscribe. */
export function onSlotUploadFailureHeld(listener: HeldListener): () => void {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

/** The failure held for `slot`, removed so it is shown once. */
export function takeSlotUploadFailure(slot: string | null | undefined): string {
  if (!slot) return ''
  const message = waiting.get(slot) ?? ''
  waiting.delete(slot)
  return message
}

/** Drop what is held for a chat that is gone. */
export function forgetSlotUploadFailure(slot: string): void {
  waiting.delete(slot)
}

export function clearSlotUploadFailures(): void {
  waiting.clear()
  listeners.clear()
}
