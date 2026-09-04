import type { NavIntent } from './popoutController'
import { safeSetSessionItem } from './safeStorage'

/**
 * Shared composer-prefill + navigation-intent helpers.
 *
 * `PREFILL_STORAGE_KEY` is the sessionStorage channel ChatPage's slot-restore
 * effect honors: `{ slotKey, prompt, ts }` seeds the composer when the slot
 * becomes active (30s TTL). It's exported from here so non-page modules (the
 * popout nav-intent applier below) can write it without importing a page
 * component. ChatPage re-exports it for its existing importers.
 */
export const PREFILL_STORAGE_KEY = 'kirocrew_prefill'

/** Seed the composer prefill for a slot. Returns whether sessionStorage accepted it. */
export function writePrefill(slotKey: string, prompt: string): boolean {
  return safeSetSessionItem(PREFILL_STORAGE_KEY, JSON.stringify({ slotKey, prompt, ts: Date.now() }))
}

/**
 * The chat session a dashboard path names, or `''` when it names none.
 *
 * `/chat?sid=<slotKey>` is the dashboard's session deep link — the System page's
 * session rows, Telemetry's conversation links and the app SDK all build it — and
 * `?slot=` is the legacy spelling ChatPage still reads. A `/chat/<slug>` prefix is
 * decorative (ChatPage writes it from the session title), so it is accepted too.
 *
 * Used to tell a session deep link apart from an ordinary route, because the two
 * cannot be honoured the same way: `navigate()` alone only selects a session while
 * ChatPage is MOUNTING, so a link followed while /chat is already open lands on
 * the page with the previous session still selected.
 */
export function chatDeepLinkSlot(path: string): string {
  let url: URL
  try {
    // A relative path needs some base to parse against; only the path and query
    // are ever read back off it.
    url = new URL(path, 'http://dashboard.invalid')
  } catch {
    return ''
  }
  const onChat = url.pathname === '/chat' || url.pathname.startsWith('/chat/')
  if (!onChat) return ''
  return url.searchParams.get('sid') || url.searchParams.get('slot') || ''
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
