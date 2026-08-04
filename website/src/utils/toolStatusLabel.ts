import { pickToolLabel } from './toolLabel'

/** The pair of labels a live `tool` status carries — the same pair an inline
 *  tool pill chooses between (see ToolCallLine's `toolLabel`). */
export type ToolStatusDetail = { kind?: string; text?: string; toolName?: string }
/**
 * Resolve a live session status to the label the user's `simplifiedToolNames`
 * preference asks for, so a session-list row agrees with the inline tool pill
 * instead of always showing the purpose.
 *
 * The websocket layer stores both forms on every `tool` status (see the
 * `tool_call` case in useWebSocket): `text` is the agent-written purpose
 * (already falling back to the tool title when the agent supplied none) and
 * `toolName` is the raw tool title. Non-tool phases — `thinking`, `streaming`,
 * a server-supplied `chat_status` — carry a single label and pass through
 * unchanged, so a caller can route every status through this one function.
 *
 * Returns `''` when there is nothing to show; the caller owns the fallback copy
 * (which is localized, and therefore not this module's business).
 */
export function toolStatusLabel(
  detail: ToolStatusDetail | undefined,
  simplifiedToolNames: boolean,
  uiLang = '',
): string {
  if (!detail) return ''
  // Raw mode: prefer the tool title, but fall back to `text` for statuses that
  // predate `toolName` (restored/legacy details) rather than blanking the row.
  if (detail.kind === 'tool' && !simplifiedToolNames) return detail.toolName || detail.text || ''
  // Simplified mode on a tool phase: `text` is the agent purpose. Guard it
  // against the active UI language (see pickToolLabel) so a purpose written in
  // another language falls back to the language-neutral tool title.
  if (detail.kind === 'tool') {
    return pickToolLabel({
      simplified: true,
      purpose: detail.text,
      rawLabel: detail.toolName || detail.text || '',
      uiLang,
    })
  }
  return detail.text || ''
}
