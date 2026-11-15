import { createContext, useContext, useSyncExternalStore } from 'react'
import { loadChatConfig } from '../pages/chat/ChatSettings'

// Single source for the "Tool calls start expanded" and "Reasoning starts
// expanded" chat settings. ToolCallLine and CollapsibleToolGroup read the
// first, ThinkingBlock the second, as the initial disclosure of a row; they
// stay live because the Settings row dispatches `mc-config-changed`.
const sub = (cb: () => void) => { window.addEventListener('mc-config-changed', cb); return () => window.removeEventListener('mc-config-changed', cb) }
const getTools = () => loadChatConfig().toolCallsStartExpanded
const getReasoning = () => loadChatConfig().reasoningStartsExpanded

/** True inside a ChatPanel an app mounts with `expandSteps`: that panel's
 *  transcript opens its reasoning and tool rows as if both settings and "Show
 *  Thinking Inline" were on, without writing the user's saved chat config. */
export const ExpandStepsContext = createContext(false)

export const useToolCallsStartExpanded = () => {
  const forced = useContext(ExpandStepsContext)
  return useSyncExternalStore(sub, getTools) || forced
}

export const useReasoningStartsExpanded = () => {
  const forced = useContext(ExpandStepsContext)
  return useSyncExternalStore(sub, getReasoning) || forced
}
