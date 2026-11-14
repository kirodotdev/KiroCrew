import { useSyncExternalStore } from 'react'
import { loadChatConfig, type CodeWrap } from '../pages/chat/ChatSettings'

// The chat config's `codeWrap`, read live: `saveChatConfig` announces every
// write with `mc-config-changed`, so one code block's Wrap press reaches every
// code block on screen, and the stored value seeds the next one rendered.
const sub = (cb: () => void) => { window.addEventListener('mc-config-changed', cb); return () => window.removeEventListener('mc-config-changed', cb) }
const getCodeWrap = (): CodeWrap => loadChatConfig().codeWrap

export const useCodeWrap = () => useSyncExternalStore(sub, getCodeWrap)
