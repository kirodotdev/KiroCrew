/**
 * The running-turn clock's one-click offer of the turn-done toast
 * (`chatCompleteNotify.ts`): when it may show, and its "don't offer again".
 *
 * Kept per device in localStorage, beside the toast's own opt-in, because the
 * toast is a per-device browser capability.
 */
import { useSyncExternalStore } from 'react'
import { isEmbeddedPane } from '../lib/embedded'
import { relayTargetOrigin } from '../lib/nativeNotify'
import { safeGetItem, safeSetItem } from '../utils/safeStorage'
import { loadChatCompleteMinSeconds } from './chatCompleteNotify'

/** localStorage key: `'1'` once the person declined the offer. */
export const CHAT_COMPLETE_OFFER_DISMISSED_KEY = 'mc-notify-chat-complete-offer-dismissed'

/** The shortest running turn, in seconds, that earns the offer: past a minute
 *  the person is likely to look away. A longer minimum-turn-length setting wins,
 *  because the toast would not fire for a turn shorter than that anyway. */
export const CHAT_COMPLETE_OFFER_MIN_SECONDS = 60

export function chatCompleteOfferSeconds(): number {
  return Math.max(CHAT_COMPLETE_OFFER_MIN_SECONDS, loadChatCompleteMinSeconds())
}

/** Whether the toast can be delivered once it is on. An embedded pane with
 *  a relay target defers permission to its parent; a standalone dashboard
 *  still requires its own Notification API and a non-denied permission. */
export function canOfferChatCompleteNotify(): boolean {
  if (isEmbeddedPane()) return relayTargetOrigin() !== null
  return typeof Notification !== 'undefined' && Notification.permission !== 'denied'
}

const listeners = new Set<() => void>()

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  // A dismissal in another window of this browser reaches this one too.
  const onStorage = (e: StorageEvent) => {
    if (e.key === CHAT_COMPLETE_OFFER_DISMISSED_KEY || e.key === null) listener()
  }
  window.addEventListener('storage', onStorage)
  return () => {
    listeners.delete(listener)
    window.removeEventListener('storage', onStorage)
  }
}

export function loadChatCompleteOfferDismissed(): boolean {
  return safeGetItem(CHAT_COMPLETE_OFFER_DISMISSED_KEY) === '1'
}

export function dismissChatCompleteOffer(): void {
  safeSetItem(CHAT_COMPLETE_OFFER_DISMISSED_KEY, '1')
  listeners.forEach(listener => listener())
}

export function useChatCompleteOfferDismissed(): boolean {
  return useSyncExternalStore(subscribe, loadChatCompleteOfferDismissed)
}
