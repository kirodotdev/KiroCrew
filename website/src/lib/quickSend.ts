import { dispatchIsCommandShaped } from '../app-sdk/protocol/dispatchShape'

/** Determines if a follow-up option click should send immediately (Quick Send mode). */
export function shouldQuickSend(quickSend: boolean | undefined, shiftKey: boolean, slotRunning: boolean, pickedSize: number): boolean {
  return !!(quickSend && !shiftKey && !slotRunning && pickedSize === 0)
}

/**
 * Attempts quick-send; returns true if sent, false if caller should fall through to multi-select.
 *
 * A command-shaped label falls through too, so one click picks it into the draft instead of running it.
 */
export function tryQuickSend(option: string, quickSend: boolean | undefined, shiftKey: boolean, slotRunning: boolean, pickedSize: number, send: (text: string) => void): boolean {
  if (shouldQuickSend(quickSend, shiftKey, slotRunning, pickedSize) && !dispatchIsCommandShaped(option)) {
    send(option)
    return true
  }
  return false
}
