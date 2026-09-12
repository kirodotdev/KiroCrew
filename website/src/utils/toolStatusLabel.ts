import { renderDerivedTitle } from './toolCallTitle'
import type { ToolAction } from './toolAction'
import { pickToolLabel } from './toolLabel'

/** The labels a live `tool` status carries — the same set an inline tool pill
 *  chooses between (see ToolCallLine's `toolLabel`). */
export type ToolStatusDetail = {
  kind?: string
  /** The agent-written purpose; '' when it supplied none. */
  text?: string
  /** The transport's title verbatim (the command, `@server/tool`, a stub). */
  toolName?: string
  /** The backend's own description of the call (R0.0), when the transport sent
   *  one; '' otherwise. Transport text, not localized, so it is stored as-is. */
  derivedTitle?: string
  /** The language-neutral action a template applied to, so the status line is
   *  rendered in the CURRENT locale at read time rather than cached in the
   *  locale that was active when the frame arrived. */
  derivedAction?: ToolAction
  derivedMore?: number
}
/**
 * Resolve a live session status to the label the user's `simplifiedToolNames`
 * preference asks for, so a session-list row agrees with the inline tool pill
 * instead of always showing the purpose.
 *
 * The websocket layer stores three forms on every `tool` status (see the
 * `tool_call` case in useWebSocket): `text` is the agent-written purpose — EMPTY
 * when the agent supplied none, because conflating the two upstream makes a
 * refinement unable to tell a real purpose from a stub title — `toolName` is the
 * raw tool title, and `derivedTitle` is the argument-derived title. The fallback
 * order is this function's job and mirrors the pill: simplified mode shows the
 * purpose, else the derived title, else the raw title; raw mode shows the raw
 * title whenever it says anything and the derived title only where the raw one
 * was a stub (`shell`, `Run Command`). Non-tool phases — `thinking`,
 * `streaming`, a server-supplied `chat_status` — carry a single label and pass
 * through unchanged, so a caller can route every status through this one
 * function.
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
  if (detail.kind === 'tool') {
    const raw = detail.toolName || ''
    const derived = detail.derivedAction
      ? renderDerivedTitle(detail.derivedAction, detail.derivedMore || 0)
      : detail.derivedTitle || ''
    // Same base-label rule as the pill and the approval bar (`pickToolLabel`):
    // raw mode keeps the verbatim title unless it is a stub the derivation
    // improves on; simplified mode shows the derived title, then the purpose
    // (`detail.text`) guarded against the active UI language. Falls back to
    // `text` for statuses that predate `toolName` (restored/legacy details)
    // rather than blanking the row.
    return (
      pickToolLabel({
        simplified: simplifiedToolNames,
        purpose: simplifiedToolNames ? detail.text : undefined,
        rawLabel: raw,
        derivedTitle: derived || undefined,
        uiLang,
      }) ||
      detail.text ||
      ''
    )
  }
  return detail.text || ''
}
