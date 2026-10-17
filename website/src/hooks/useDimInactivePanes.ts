import { useSyncExternalStore } from 'react'
import { loadChatConfig } from '../pages/chat/ChatSettings'

// Single source for the split-view dimming preference. PaneDim reads it here
// so one toggle governs every split pane, and it stays live because the
// Settings row dispatches `mc-config-changed` on save.
const sub = (cb: () => void) => { window.addEventListener('mc-config-changed', cb); return () => window.removeEventListener('mc-config-changed', cb) }
const get = () => loadChatConfig().dimInactivePanes

export const useDimInactivePanes = () => useSyncExternalStore(sub, get)
