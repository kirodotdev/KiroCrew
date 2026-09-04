/**
 * Reader for the reserved tool-purpose argument.
 *
 * kiro-cli injects a `__tool_use_purpose` property ("A brief explanation why
 * you are making this tool use") into EVERY tool schema it exposes — built-ins
 * and MCP tools alike — so a tool call's arguments carry the agent's own
 * one-line reason for the call. The dashboard surfaces that line as the tool
 * pill label, the pending-approval preview, and the pet's approval bubble.
 *
 * Nothing validates the key, though: it is a synthetic parameter the model
 * fills in from prose, and models paraphrase its name. Real transcripts carry
 * `__purpose`, `__thinking_purpose` and `__woohoo_purpose` alongside the
 * declared `__tool_use_purpose` and its camelCased echo. Matching a fixed list
 * of literals therefore drops the purpose for whole sessions at a time, and the
 * unrecognized key instead leaks into the arguments view as if it were a real
 * parameter.
 *
 * So the match is by SHAPE, mirroring `_dispatch.is_tool_purpose_key()` /
 * `extract_tool_purpose()` on the backend.
 */

/** The spellings kiro-cli itself emits: the declared snake_case property and
 *  the camelCased echo some calls come back with. Preferred over a shape match
 *  so the reading is stable when a call carries more than one candidate. */
export const TOOL_PURPOSE_KEYS = ['__tool_use_purpose', '__toolUsePurpose'] as const

/**
 * True when `key` names the reserved tool-purpose argument.
 *
 * The `__` prefix is load-bearing: only dunder names are reserved, so a tool's
 * own functional argument that happens to be called `purpose` stays out of the
 * match.
 */
export function isToolPurposeKey(key: string): boolean {
  if (!key.startsWith('__')) return false
  return key.toLowerCase().replace(/[^a-z0-9]/g, '').endsWith('purpose')
}

/**
 * The purpose line carried by a tool call's parsed arguments, or `''`.
 *
 * Canonical spellings win; other shape-matching keys are then scanned in sorted
 * order so the choice is deterministic regardless of key order on the wire.
 */
export function purposeFromToolArgs(args: unknown): string {
  if (!args || typeof args !== 'object' || Array.isArray(args)) return ''
  const obj = args as Record<string, unknown>
  for (const key of TOOL_PURPOSE_KEYS) {
    const value = obj[key]
    if (typeof value === 'string' && value.trim()) return value.trim()
  }
  for (const key of Object.keys(obj).filter(isToolPurposeKey).sort()) {
    const value = obj[key]
    if (typeof value === 'string' && value.trim()) return value.trim()
  }
  return ''
}
