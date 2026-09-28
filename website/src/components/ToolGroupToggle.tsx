import type { ReactNode } from 'react'
import { ChevronRight } from 'lucide-react'

import { i18nT } from '../i18n/t'

/**
 * Ring colour of the pill. `attention` is a pending approval, `resolved` a
 * decision just recorded; everything else — a settled group, a running one,
 * TurnBlock's tool fold — is `default`.
 */
export type ToolGroupToggleTone = 'default' | 'attention' | 'resolved'

interface ToolGroupToggleProps {
  expanded: boolean
  onToggle: () => void
  /** What the pill says: an inline icon plus the count/state text. */
  label: ReactNode
  /**
   * Plain text for a verb-led accessible name — `Expand 2 tool calls` /
   * `Collapse 2 tool calls`. Pass it when the visible label is STATELESS (the
   * group header always reads "N tool calls"), so the name carries the action
   * the label does not. Omit it when the visible label already says what the
   * click does ("Hide tool calls"): the name is then the visible text itself,
   * which is what a speech-input user reads aloud (WCAG 2.5.3 label-in-name),
   * and `aria-expanded` carries the state.
   */
  labelText?: string
  /**
   * Leading indicator. Defaults to the disclosure chevron, which rotates to
   * point down while expanded; a host with a live state (pending approval,
   * running tools) passes its own dot instead.
   */
  indicator?: ReactNode
  tone?: ToolGroupToggleTone
}

const TONE_RING: Record<ToolGroupToggleTone, string> = {
  default: 'ring-border hover:ring-border-strong',
  attention: 'ring-warn hover:ring-warn/80',
  resolved: 'ring-ok/60 hover:ring-ok/80',
}

/**
 * The ONE affordance for "this row hides N tool calls" across the chat hosts.
 *
 * `CollapsibleToolGroup` (ChatPage + the app-sdk `ChatMessageList`) renders its
 * header through it, and `TurnBlock` renders its fold toggle through it, so a
 * reader who sees both in one session sees one pill, not a filled bar in one
 * place and a bare text row in the other (#9699: the UX lane's blind read of
 * #9689 could not tell which of the two was "the real one"). Keeping the
 * classes in a single place is what makes that hold: a restyle lands on both
 * hosts or on neither.
 *
 * The button owns the a11y contract too — `aria-expanded` on every mount, and
 * an accessible name that is either the visible label or a verb-led form of it
 * (see `labelText`) — so a host cannot reuse the look and drop the semantics.
 */
export default function ToolGroupToggle({ expanded, onToggle, label, labelText, indicator, tone = 'default' }: ToolGroupToggleProps) {
  return (
    <button
      type="button"
      data-testid="tool-group-toggle"
      className={`flex items-center gap-2 px-4 py-2 rounded-md text-[13px] leading-5 font-mono text-muted bg-card ring-1 ring-inset forced-colors:border cursor-pointer transition-all w-full text-left ${TONE_RING[tone]} hover:text-text`}
      onClick={onToggle}
      aria-expanded={expanded}
      aria-label={labelText === undefined ? undefined : `${expanded ? i18nT('pages.chat.collapsibleToolGroup.collapse') : i18nT('pages.chat.collapsibleToolGroup.expand')} ${labelText}`}
    >
      {indicator ?? <ChevronRight size={14} aria-hidden="true" className={`flex-shrink-0 transition-transform duration-150 ${expanded ? 'rotate-90' : ''}`} />}
      <span>{label}</span>
    </button>
  )
}
