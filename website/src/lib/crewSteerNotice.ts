import { useSyncExternalStore } from 'react'

/**
 * A person's steer into a Crew Mode worker that was DELIVERED but not recorded on
 * the ledger (the send receipt's `ledger_error`, edge review R2 F6).
 *
 * The send path's receipts carry `ledger_event` / `ledger_error` for a steer; a
 * plain send carries neither. The transport notes the reason here per slot, and the
 * worker's composer shows one muted line under it until the next recorded steer.
 * In memory only, per tab.
 */
const unrecorded = new Map<string, string>()
const listeners = new Set<() => void>()

function emit() {
  for (const listener of listeners) listener()
}

/** Read one send receipt body for *slot*: a `ledger_error` sets the slot's note,
 *  a recorded (or still-owed) steer clears it, anything else leaves it alone. */
export function noteCrewSteerReceipt(slot: string, body: unknown): void {
  if (!slot || !body || typeof body !== 'object') return
  const receipt = body as { ledger_error?: unknown; ledger_event?: unknown; ledger_pending?: unknown }
  const reason = receipt.ledger_error
  if (typeof reason === 'string' && reason) {
    if (unrecorded.get(slot) === reason) return
    unrecorded.set(slot, reason)
    emit()
    return
  }
  if ((typeof receipt.ledger_event === 'string' && receipt.ledger_event) || receipt.ledger_pending === true) {
    if (unrecorded.delete(slot)) emit()
  }
}

/** Forget *slot*'s note (the person dismissed it). */
export function clearCrewSteerNotice(slot: string): void {
  if (unrecorded.delete(slot)) emit()
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

/** The reason *slot*'s last steer was not recorded, or `''`. */
export function useCrewSteerNotice(slot: string): string {
  return useSyncExternalStore(subscribe, () => unrecorded.get(slot) ?? '', () => '')
}
