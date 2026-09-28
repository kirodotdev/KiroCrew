import { useSyncExternalStore } from 'react'
import { loadChatConfig, type SendMode } from '../pages/chat/ChatSettings'

// Single source for the composer send-key preference (Settings -> Chat ->
// Composer -> Send shortcut). Every composer the user types into reads it here,
// so one setting governs them all instead of each host remembering to thread a
// prop; a host that omits the prop gets the stored mode, not a hardcoded
// 'enter'. It stays live because the Settings row dispatches
// `mc-config-changed` on save.
const sub = (cb: () => void) => { window.addEventListener('mc-config-changed', cb); return () => window.removeEventListener('mc-config-changed', cb) }
const get = (): SendMode => loadChatConfig().sendOnEnter

export const useComposerSendMode = () => useSyncExternalStore(sub, get)
