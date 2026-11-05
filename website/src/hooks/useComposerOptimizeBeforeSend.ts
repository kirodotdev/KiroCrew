import { useSyncExternalStore } from 'react'
import { loadChatConfig } from '../pages/chat/ChatSettings'

// Single source for the Optimize Before Sending preference, read the same way
// as useComposerSpellcheck: it stays live because the Settings row dispatches
// `mc-config-changed` on save.
const sub = (cb: () => void) => { window.addEventListener('mc-config-changed', cb); return () => window.removeEventListener('mc-config-changed', cb) }
const get = () => loadChatConfig().optimizeBeforeSend

export const useComposerOptimizeBeforeSend = () => useSyncExternalStore(sub, get)
