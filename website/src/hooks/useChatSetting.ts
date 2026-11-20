import { useSyncExternalStore } from 'react'
import { loadChatConfig, type ChatConfig } from '../pages/chat/ChatSettings'

// The one live read of a Settings -> Chat value. Every chat surface draws the
// same composer and the same message rows (the Sessions chat, a Bot's chat, a
// split-view pane, a pop-out crew window, an embedded chat), so the row or
// composer reads the setting itself: a host that never threads it as a prop
// still follows the user's choice. It stays live because every chat-config
// save dispatches `mc-config-changed`, and a save in another tab fires
// `storage` here. Every ChatConfig value is a primitive, so the snapshot
// compares by value and an event for another key re-renders nothing.
const EVENTS = ['mc-config-changed', 'storage'] as const
const sub = (cb: () => void) => {
  for (const e of EVENTS) window.addEventListener(e, cb)
  return () => { for (const e of EVENTS) window.removeEventListener(e, cb) }
}

export function useChatSetting<K extends keyof ChatConfig>(key: K): ChatConfig[K] {
  return useSyncExternalStore(sub, () => loadChatConfig()[key])
}
