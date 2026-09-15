import { dispatchIsCommandShaped, markerDeclined } from '../app-sdk/protocol/recommendation'

/** Determines if a follow-up option click should send immediately (Quick Send mode). */
export function shouldQuickSend(quickSend: boolean | undefined, shiftKey: boolean, slotRunning: boolean, pickedSize: number): boolean {
  return !!(quickSend && !shiftKey && !slotRunning && pickedSize === 0)
}

/** Attempts quick-send; returns true if sent, false if caller should fall through to multi-select. */
export function tryQuickSend(option: string, quickSend: boolean | undefined, shiftKey: boolean, slotRunning: boolean, pickedSize: number, send: (text: string) => void): boolean {
  // Refused HERE because the host's onSelect owns this call, so the chip's own exemption
  // cannot reach it -- and quick-send would dispatch the prefix as the user's own words.
  if (markerDeclined(option)) return false
  // A label needs no marker to be dangerous: a BARE `/clear` chip is not marker-declined, so
  // the check above passed it and one click ran it. Same grammar, asked of the raw label.
  if (dispatchIsCommandShaped(option)) return false
  if (shouldQuickSend(quickSend, shiftKey, slotRunning, pickedSize)) {
    send(option)
    return true
  }
  return false
}
