import { useSyncExternalStore } from 'react'
import { loadChatConfig } from '../pages/chat/ChatSettings'

// Single source for the composer-spellcheck preference. Every composer the user
// types into (main chat and the side panel) reads it here so one toggle governs
// them all, and it stays live because the Settings row dispatches
// `mc-config-changed` on save. When false the composer input carries
// `spellCheck={false}` and the browser draws no red misspelled-word underlines.
const sub = (cb: () => void) => { window.addEventListener('mc-config-changed', cb); return () => window.removeEventListener('mc-config-changed', cb) }
const get = () => loadChatConfig().spellcheck

export const useComposerSpellcheck = () => useSyncExternalStore(sub, get)
