/**
 * Same-window change signal for unsent work on a slot.
 *
 * A `storage` event never reaches the window that wrote the key, so a composer in this
 * window that claims or drops work, or writes or clears a side draft, says so here. A
 * reader of `slotUnsentWorkSource` subscribes so its answer cannot outlive the work.
 */
const listeners = new Set<() => void>()

export function onSlotWorkChange(listener: () => void): () => void {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

export function notifySlotWorkChange(): void {
  for (const listener of [...listeners]) listener()
}
