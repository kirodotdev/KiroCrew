import { useSyncExternalStore } from 'react'
import { loadChatConfig } from '../pages/chat/ChatSettings'

// Single source for the "Tool calls start expanded" chat setting (#18254).
// ToolCallLine and CollapsibleToolGroup read it as the initial disclosure of a
// row; it stays live because the Settings row dispatches `mc-config-changed`.
const sub = (cb: () => void) => { window.addEventListener('mc-config-changed', cb); return () => window.removeEventListener('mc-config-changed', cb) }
const get = () => loadChatConfig().toolCallsStartExpanded

export const useToolCallsStartExpanded = () => useSyncExternalStore(sub, get)
