import { useSyncExternalStore } from 'react'
import { loadChatConfig } from '../pages/chat/ChatSettings'

// The composer's "Require a second Escape to stop the turn" preference, read the
// same way as useComposerSpellcheck so the controls that name the stop key stay
// live: the Settings row dispatches `mc-config-changed` on save.
const sub = (cb: () => void) => { window.addEventListener('mc-config-changed', cb); return () => window.removeEventListener('mc-config-changed', cb) }
const get = () => loadChatConfig().doubleEscapeToStop

export const useComposerDoubleEscapeToStop = () => useSyncExternalStore(sub, get)
