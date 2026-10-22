import { useSyncExternalStore } from 'react'
import { loadChatConfig, type SendMode } from '../pages/chat/ChatSettings'
import { useIsMobile } from './useIsMobile'
import { useIsTouchDevice } from './useIsTouchDevice'

// Single source for the composer send-key preference (Settings -> Chat ->
// Composer -> Send shortcut). Every composer the user types into reads it here,
// so one setting governs them all instead of each host remembering to thread a
// prop; a host that omits the prop gets the stored mode, not a hardcoded
// 'enter'. It stays live because the Settings row dispatches
// `mc-config-changed` on save.
const sub = (cb: () => void) => { window.addEventListener('mc-config-changed', cb); return () => window.removeEventListener('mc-config-changed', cb) }
const get = (): SendMode => loadChatConfig().sendOnEnter

/**
 * True on a phone: a narrow viewport on a touch device. A phone's on-screen
 * keyboard has no Shift+Return, so if Return sends there is no way to start a
 * new line, and one slip sends a half-written message to the agent. The
 * composers and reply boxes that read this hook therefore treat Return as a
 * new line on a phone, and their Send button sends. A narrow desktop window and
 * a wide touch screen (a tablet, often with a hardware keyboard) keep the
 * stored choice.
 */
export function useReturnInsertsNewline(): boolean {
  const narrow = useIsMobile()
  const touch = useIsTouchDevice()
  return narrow && touch
}

/** The send key a composer applies: the stored choice, or a new-line Return on a phone. */
export function useComposerSendMode(): SendMode {
  const stored = useSyncExternalStore(sub, get)
  return useReturnInsertsNewline() ? 'ctrl-enter' : stored
}
