import { useSyncExternalStore } from 'react'
import { loadChatConfig } from '../pages/chat/ChatSettings'

// Single source for the two "Optimize prompt" preferences, read the same way
// as useComposerSpellcheck: they stay live because the Settings rows dispatch
// `mc-config-changed` on save.
const sub = (cb: () => void) => { window.addEventListener('mc-config-changed', cb); return () => window.removeEventListener('mc-config-changed', cb) }
const get = () => loadChatConfig().optimizeBeforeSend
const getShown = () => loadChatConfig().showOptimizePrompt

export const useComposerOptimizeBeforeSend = () => useSyncExternalStore(sub, get)
export const useComposerShowOptimizePrompt = () => useSyncExternalStore(sub, getShown)
