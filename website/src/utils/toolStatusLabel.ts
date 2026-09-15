import { pickToolLabel } from './toolLabel'

/** The pair of labels a live `tool` status carries — the same pair an inline
 *  tool pill chooses between (see ToolCallLine's `toolLabel`). */
export type ToolStatusDetail = { kind?: string; label?: string; purpose?: string }
/**
 * Resolve a live session status to the label the user's `simplifiedToolNames`
 * preference asks for, so a session-list row agrees with the inline tool pill
 * instead of always showing the purpose.
 *
 * The websocket layer stores both forms on every `tool` status (see the
 * `tool_call` case in useWebSocket): `purpose` is the agent-written purpose —
 * EMPTY when the agent supplied none, because conflating the two upstream makes
 * a refinement unable to tell a real purpose from a stub title — and `label` is
 * the raw tool title. Falling back from an absent purpose to the title is
 * therefore this function's job. Non-tool phases — `thinking`, `streaming`, a
 * server-supplied `chat_status` — carry a single label and pass through
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
  // Raw mode prefers the tool title, but the purpose is still a safe fallback
  // when a malformed or legacy frame omitted its label.
  if (detail.kind === 'tool' && !simplifiedToolNames) return detail.label || detail.purpose || ''
  // Simplified mode on a tool phase: `purpose` is the agent purpose. Guard it
  // against the active UI language (see pickToolLabel) so a purpose written in
  // another language falls back to the language-neutral tool title.
  if (detail.kind === 'tool') {
    return pickToolLabel({
      simplified: true,
      purpose: detail.purpose,
      rawLabel: detail.label || '',
      uiLang,
    })
  }
  return detail.label || ''
}
