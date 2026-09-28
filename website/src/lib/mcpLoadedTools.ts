import type { ChatMessage } from '../types'

/** Per-tool loaded state for the session MCP view. */
export type McpToolStatus = 'active' | 'deferred' | 'disabled'

/**
 * Derive the set of MCP tools loaded ("activated") so far in a session, as
 * `"<server_name>::<tool_name>"` ids.
 *
 * When kiro-cli's Tool Search is on, MCP tool specs are DEFERRED (the model
 * sees a compact list) and the model calls the `tool_search` tool to load
 * specific tools on demand. Each `tool_search` result is a JSON string
 * `{"tools":[{"tool_name","server_name",...}]}` stored on the tool message's
 * `meta.output`. Scanning those results reconstructs which tools have been
 * loaded — no backend endpoint required, since the chat store already holds
 * every tool message's output.
 *
 * This is an OBSERVED approximation, not kiro-cli ground truth: a page reload
 * or `session/load` starts the set empty until re-observed, and context
 * compaction can silently evict a loaded spec with no signal (so the set can
 * over-report). Present it as "loaded this session", never as absolute truth.
 */
export function deriveLoadedMcpTools(messages: ChatMessage[]): Set<string> {
  const loaded = new Set<string>()
  for (const m of messages) {
    if (m.role !== 'tool') continue
    const out = m.meta?.output
    // Cheap prefilter before the JSON.parse — tool_search output always
    // enumerates server_name; skip the parse for every other tool's output.
    if (typeof out !== 'string' || !out.includes('server_name')) continue
    try {
      const parsed = JSON.parse(out) as {
        tools?: Array<{ tool_name?: unknown; server_name?: unknown }>
      }
      if (!Array.isArray(parsed?.tools)) continue
      for (const t of parsed.tools) {
        if (typeof t?.tool_name === 'string' && typeof t?.server_name === 'string') {
          loaded.add(`${t.server_name}::${t.tool_name}`)
        }
      }
    } catch {
      // Not a tool_search result (unparseable / different shape) — ignore.
    }
  }
  return loaded
}

/**
 * Classify a single tool for the per-tool status dot.
 * - `disabled`: the tool is in the server's `disabledTools` (never sent).
 * - `active`:   Tool Search is off (every spec sent each turn), OR the tool has
 *               been loaded this session.
 * - `deferred`: Tool Search is on and the tool has not been loaded yet.
 */
export function mcpToolStatus(
  server: string,
  tool: string,
  opts: { loaded: Set<string>; disabledTools?: string[]; toolSearchOn: boolean },
): McpToolStatus {
  if (opts.disabledTools?.includes(tool)) return 'disabled'
  if (!opts.toolSearchOn) return 'active'
  return opts.loaded.has(`${server}::${tool}`) ? 'active' : 'deferred'
}
