/** Live MCP App render payloads (`chat.mcpApps`): session-scoped keys, the
 *  per-slot retention bound, and eviction with the slot. Payloads are never
 *  persisted; see mcp-apps.md. */
import type { PayloadAction } from '@reduxjs/toolkit'
import type { McpAppRenderPayload } from '../../lib/mcpAppSrcdoc'
import type { ChatState } from './state'
import { isUnsafeKey } from './wire'

/** Composite key for `state.mcpApps`: `<session>\u001F<tool_call_id>`. The
 *  session scope prevents cross-slot render collisions and makes per-slot
 *  eviction a prefix scan (the payloads carry multi-MB app HTML, so they must
 *  not outlive their slot). \u001F (unit separator) cannot appear in either
 *  component. */
export const MCP_APP_KEY_SEP = '\u001F'

export const mcpAppKey = (sessionKey: string, toolCallId: string): string =>
  `${sessionKey}${MCP_APP_KEY_SEP}${toolCallId}`

/** Max MCP App render payloads retained per slot (each carries multi-MB HTML);
 *  oldest are evicted past this bound. */
const MCP_APPS_PER_SLOT_MAX = 24

/** Drop every MCP App render payload belonging to `sessionKey` (slot deleted
 *  or its conversation cleared — the tool rows the apps hang off are gone). */
export const evictMcpApps = (state: { mcpApps: Record<string, McpAppRenderPayload> }, sessionKey: string): void => {
  const prefix = `${sessionKey}${MCP_APP_KEY_SEP}`
  // `?? {}` for the same reason the teardown enumerations in slotResidue.ts
  // carry it: a preloaded state need not define every per-slot map, and
  // teardown is now reachable from three writers rather than one.
  for (const k of Object.keys(state.mcpApps ?? {})) {
    if (k.startsWith(prefix)) delete state.mcpApps[k]
  }
}

export const mcpAppReducers = {
  /** Store an MCP App (SEP-1865) render payload, keyed by BOTH its session
   *  and tool_call_id (see mcpAppKey): the session scope means an ACP
   *  tool-call-id reuse across slots can never cross-render another
   *  session's app (or its live callback capability), and per-slot eviction
   *  (payloads are multi-MB) is a simple prefix scan. */
  sseMcpAppRender(state: ChatState, action: PayloadAction<McpAppRenderPayload>) {
    const p = action.payload
    if (!p?.tool_call_id || isUnsafeKey(p.tool_call_id)) return
    if (!p.session_key || isUnsafeKey(p.session_key)) return
    state.mcpApps[mcpAppKey(p.session_key, p.tool_call_id)] = p
    // Bound per-slot retention: payloads carry multi-MB app HTML, so a
    // long-lived slot that renders many apps must not grow unbounded. Keys
    // enumerate in insertion order, so the oldest slot entries are dropped
    // first once the cap is exceeded.
    const prefix = `${p.session_key}${MCP_APP_KEY_SEP}`
    const slotKeys = Object.keys(state.mcpApps).filter((k) => k.startsWith(prefix))
    for (let i = 0; i < slotKeys.length - MCP_APPS_PER_SLOT_MAX; i++) {
      delete state.mcpApps[slotKeys[i]]
    }
  },
}
