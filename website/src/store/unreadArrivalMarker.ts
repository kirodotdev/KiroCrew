import { safeRemoveItem, safeSetItem } from '../utils/safeStorage'
import { secureRandomId } from '../utils/secureId'

const UNREAD_ARRIVAL_KEY_PREFIX = 'mc-unread-arrival:'

/** Read the marker for an arrival absorbed by a newer shared watermark. */
export const readUnreadArrival = (slot: string): string | undefined => {
  try { return localStorage.getItem(UNREAD_ARRIVAL_KEY_PREFIX + slot) ?? undefined } catch { return undefined }
}

export const holdUnreadArrival = (slot: string): string | undefined => {
  const marker = secureRandomId()
  if (!safeSetItem(UNREAD_ARRIVAL_KEY_PREFIX + slot, marker)) return undefined
  return readUnreadArrival(slot) === marker ? marker : undefined
}

export const clearUnreadArrival = (slot: string): void =>
  safeRemoveItem(UNREAD_ARRIVAL_KEY_PREFIX + slot)
