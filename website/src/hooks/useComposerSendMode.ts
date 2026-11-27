import { useSyncExternalStore } from 'react'
import { loadChatConfig, type SendMode } from '../pages/chat/ChatSettings'
import { useIsMobile } from './useIsMobile'
import { useIsTouchDevice } from './useIsTouchDevice'
import { isTouchFirst } from '../lib/touchLayoutForm'

// Single source for the composer send-key preference (Settings -> Chat ->
// Composer -> Send shortcut). Every composer the user types into reads it here,
// so one setting governs them all instead of each host remembering to thread a
// prop; a host that omits the prop gets the stored mode, not a hardcoded
// 'enter'. It stays live because the Settings row dispatches
// `mc-config-changed` on save.
const sub = (cb: () => void) => { window.addEventListener('mc-config-changed', cb); return () => window.removeEventListener('mc-config-changed', cb) }
const get = (): SendMode => loadChatConfig().sendOnEnter

/**
 * True on a phone: the phone layout form (`useIsMobile`) on a touch device. A
 * phone's on-screen keyboard has no Shift+Return, so if Return sends there is
 * no way to start a new line, and one slip sends a half-written message to the
 * agent. The composers and reply boxes that read this hook therefore treat
 * Return as a new line on a phone, and their Send button sends. A phone whose
 * browser draws the page desktop-wide counts too, by its screen. A narrow
 * desktop window and a tablet in desktop mode (often with a hardware keyboard)
 * keep the stored choice.
 */
export function useReturnInsertsNewline(): boolean {
  const narrow = useIsMobile()
  const touch = useIsTouchDevice()
  return (narrow && touch) || touchFirstPhoneScreen()
}

// The phone screen behind a desktop-width page. A phone browser can lay a page
// out wider than its screen and name no phone in its user agent (an enterprise
// in-app browser, a desktop-site mode), so the page is drawn desktop and its
// width says nothing. The screen itself still does: a touch-first device whose
// short side is under 600 CSS px is a phone. 600 is the smallest tablet short
// side (Android's sw600dp tablet line; an iPad mini is 744), while the largest
// phones are about 440, so a tablet in landscape keeps the stored choice.
const PHONE_SCREEN_SHORT_SIDE_LIMIT = 600

function touchFirstPhoneScreen(): boolean {
  if (typeof screen === 'undefined' || !isTouchFirst()) return false
  const short = Math.min(screen.width, screen.height)
  return short > 0 && short < PHONE_SCREEN_SHORT_SIDE_LIMIT
}

/** The send key a composer applies: the stored choice, or a new-line Return on a phone. */
export function useComposerSendMode(): SendMode {
  const stored = useSyncExternalStore(sub, get)
  return useReturnInsertsNewline() ? 'ctrl-enter' : stored
}
