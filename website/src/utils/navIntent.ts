import type { NavIntent } from './popoutController'
import { safeSetSessionItem } from './safeStorage'

/**
 * Shared composer-prefill + navigation-intent helpers.
 *
 * `PREFILL_STORAGE_KEY` is the sessionStorage channel ChatPage's slot-restore
 * effect honors: `{ slotKey, prompt, ts }` seeds the composer when the slot
 * becomes active (30s TTL). It historically lived in ChatPage; it's exported
 * from here so non-page modules (the popout nav-intent applier below) can
 * write it without importing a page component. ChatPage re-exports it for its
 * existing importers.
 */
export const PREFILL_STORAGE_KEY = 'kirocrew_prefill'

/** Seed the composer prefill for a slot (consumed by ChatPage when that slot activates). */
export function writePrefill(slotKey: string, prompt: string): void {
  safeSetSessionItem(PREFILL_STORAGE_KEY, JSON.stringify({ slotKey, prompt, ts: Date.now() }))
}

/**
 * Perform a navigation intent forwarded from a popout window, in THIS main
 * dashboard window. Order matters: the prefill must be in sessionStorage
 * before the slot switch + route change so ChatPage's slot-restore effect
 * finds it when the target slot activates.
 */
export function applyNavIntentInMain(
  intent: NavIntent,
  deps: { navigate: (path: string) => void; switchSlot: (slotKey: string) => void },
): void {
  if (intent.prefill) writePrefill(intent.prefill.slotKey, intent.prefill.prompt)
  if (intent.slotKey) deps.switchSlot(intent.slotKey)
  deps.navigate(intent.path)
  // Best-effort raise — a channel-delivered intent has no user activation, so
  // browsers may veto this; the opener-focus on the popout side is the
  // reliable path and this is just the assist for the claimed-but-not-opener
  // main.
  try { window.focus() } catch { /* vetoed — non-fatal */ }
}
