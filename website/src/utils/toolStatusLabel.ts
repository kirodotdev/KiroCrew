import { i18nT } from '../i18n/t'
import { renderDerivedTitle } from './toolCallTitle'
import type { ToolAction } from './toolAction'
import { pickToolLabel } from './toolLabel'

/** The parts a live `tool` status carries — the same set an inline tool pill
 *  chooses between (see ToolCallLine's `toolLabel`). None of them is a display
 *  label on its own: `toolStatusLabel` is the one place that resolves them into
 *  the string a row paints, so a `tool` phase deliberately has no `label`. */
export type ToolPhaseDetail = {
  kind: 'tool'
  /** The agent-written purpose; '' (or absent) when it supplied none. */
  purpose?: string
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
/** A non-tool phase. The fixed phases (`thinking`, `streaming`, `idle`) carry
 *  no copy of their own: `kind` is the language-neutral marker and the label is
 *  a catalog key resolved at render time, so a UI language switch re-renders
 *  the row instead of freezing the phrase that was active at dispatch. A
 *  server-supplied `chat_status` also arrives as `kind: 'thinking'` and is the
 *  one non-tool phase with a `label`, painted verbatim. */
export type PhaseDetail = {
  kind: 'thinking' | 'streaming' | 'idle'
  label?: string
}
/** A slot's live status, split by `kind` so the agent-written `purpose` of a
 *  tool call and the server-supplied `label` of a status line are distinct
 *  fields the type checker tells apart, rather than one field whose meaning
 *  would depend on `kind`. */
export type ToolStatusDetail = ToolPhaseDetail | PhaseDetail
/**
 * Resolve a live session status to the label the user's `simplifiedToolNames`
 * preference asks for, so a session-list row agrees with the inline tool pill
 * instead of always showing the purpose.
 *
 * The websocket layer stores three forms on every `tool` status (see the
 * `tool_call` case in useWebSocket): `purpose` is the agent-written purpose —
 * EMPTY when the agent supplied none, because conflating the two upstream makes
 * a refinement unable to tell a real purpose from a stub title — `toolName` is
 * the raw tool title, and `derivedTitle` is the argument-derived title. The
 * fallback order is this function's job and mirrors the pill: simplified mode
 * shows the purpose, else the derived title, else the raw title; raw mode shows
 * the raw title whenever it says anything and the derived title only where the
 * raw one was a stub (`shell`, `Run Command`). Non-tool phases resolve from
 * `kind`: a server-supplied `chat_status` passes its `label` through unchanged,
 * `thinking` and `streaming` render their catalog copy in the current locale,
 * so a caller can route every status through this one function.
 *
 * Returns `''` when there is nothing to show (an `idle` phase, a tool call with
 * no title and no purpose); the caller owns the fallback copy.
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
    // guarded against the active UI language. Falls back to the purpose for
    // statuses that predate `toolName` (restored/legacy details) rather than
    // blanking the row.
    return (
      pickToolLabel({
        simplified: simplifiedToolNames,
        purpose: simplifiedToolNames ? detail.purpose : undefined,
        rawLabel: raw,
        derivedTitle: derived || undefined,
        uiLang,
      }) ||
      detail.purpose ||
      ''
    )
  }
  if (detail.label) return detail.label
  switch (detail.kind) {
    case 'thinking':
      return i18nT('pages.chatSidebar.thinking')
    case 'streaming':
      return i18nT('pages.chatSidebar.streaming')
    default:
      return ''
  }
}
