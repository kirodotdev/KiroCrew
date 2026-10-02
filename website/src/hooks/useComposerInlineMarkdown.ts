import { useSyncExternalStore } from 'react'
import { loadChatConfig } from '../pages/chat/ChatSettings'

// Single source for the composer's live-markdown-styling preference, read the
// same way as useComposerSpellcheck: every composer reads it here, and it stays
// live because the Settings row dispatches `mc-config-changed` on save.
const sub = (cb: () => void) => { window.addEventListener('mc-config-changed', cb); return () => window.removeEventListener('mc-config-changed', cb) }
const get = () => loadChatConfig().inlineMarkdown

export const useComposerInlineMarkdown = () => useSyncExternalStore(sub, get)
