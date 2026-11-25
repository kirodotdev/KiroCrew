import { useSyncExternalStore } from 'react'
import { loadChatConfig, saveChatConfig, type CodeWrap } from '../pages/chat/ChatSettings'

// The chat config's `codeWrap`, read live: `saveChatConfig` announces every
// write with `mc-config-changed`, so one code block's Wrap press reaches every
// code block on screen, and the stored value seeds the next one rendered.
// A press the browser storage refuses still applies for this page: it is held
// here, in memory, and read over the stored value until a reload.
let unsaved: CodeWrap | null = null
const listeners = new Set<() => void>()
const sub = (cb: () => void) => {
  listeners.add(cb)
  window.addEventListener('mc-config-changed', cb)
  return () => { listeners.delete(cb); window.removeEventListener('mc-config-changed', cb) }
}
const getCodeWrap = (): CodeWrap => unsaved ?? loadChatConfig().codeWrap

export const useCodeWrap = () => useSyncExternalStore(sub, getCodeWrap)

/** Sets the shared choice. Returns false when the storage refused it: the
 *  choice still applies on this page, and is lost on a reload. */
export function setCodeWrap(next: CodeWrap): boolean {
  const saved = saveChatConfig({ ...loadChatConfig(), codeWrap: next })
  unsaved = saved ? null : next
  // Always: the save's own event fired while the old in-memory choice still
  // stood, so a subscriber that read it then has not seen this one yet.
  listeners.forEach((cb) => cb())
  return saved
}

/** Drops the in-memory choice, so one test's refused save does not reach the next. */
export function __resetCodeWrapForTests(): void { unsaved = null }
