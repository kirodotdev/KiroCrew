/**
 * CrewmateStepsLine — a run of a crewmate's steps (tool calls, thinking) as ONE
 * chat line instead of a list of rows (components/chat/crewmateBubbles folds
 * the run into a `CREWMATE_STEPS_ROLE` row).
 *
 * Live (the step the crewmate is on now): the tool line's own spinning ring,
 * the current step's title and the step count; before the turn's first step,
 * the same ring and "Thinking" as a plain line. It stands in for the pane's working footer, which
 * the pane hides while this line is the newest row, so the chat never shows two
 * "working" indicators. An earlier run of the running turn (the crewmate has
 * spoken since): a quiet "Worked through N steps", the summary the ordinary
 * transcript's own turn fold uses. Either way the line is a disclosure button;
 * opened, it draws the ordinary rows it folded, each with its own live state.
 * All lines fold away when the turn ends.
 */
import { useId, useState, type ReactNode } from 'react'
import { ChevronRight, LoaderCircle } from 'lucide-react'
import { useAppSelector } from '../../store'
import { useSimplifiedToolNames } from '../../hooks/useSimplifiedToolNames'
import { useLanguage } from '../../i18n/LanguageProvider'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import { i18nT } from '../../i18n/t'
import { toolStatusLabel } from '../../utils/toolStatusLabel'
import { Btn } from '../../components/ui'
import type { CrewmateSteps } from '../../components/chat/crewmateBubbles'
import type { ChatMessage } from '../../types'

/** The newest step's own words when the slot has no live tool status: the
 *  agent's purpose, else the tool title's first line. */
function stepTitle(steps: ChatMessage[]): string {
  const last = steps[steps.length - 1]
  if (!last || last.role !== 'tool') return ''
  const call = [...steps].reverse().find(m => m.role === 'tool' && m.content.startsWith('🔧'))
  if (!call) return ''
  const purpose = typeof call.meta?.purpose === 'string' ? call.meta.purpose.trim() : ''
  return purpose || call.content.replace(/^🔧\s*/, '').split('\n')[0].trim()
}

export default function CrewmateStepsLine({ steps, slot, disclosure, onDisclosureChange, children }: {
  steps: CrewmateSteps
  slot?: string
  /** Open state held above the row (the pane's disclosure map), which outlives
   *  the remount every transcript update causes. Absent: local state. */
  disclosure?: boolean
  onDisclosureChange?: (expanded: boolean) => void
  /** The folded rows, drawn only while open. */
  children: () => ReactNode
}) {
  useLanguageGeneration()
  const [local, setLocal] = useState(false)
  const expanded = disclosure ?? local
  const onToggle = () => (onDisclosureChange ? onDisclosureChange(!expanded) : setLocal(!expanded))
  const panelId = useId()
  const simplified = useSimplifiedToolNames()
  const uiLang = useLanguage().resolved
  const detail = useAppSelector(s => (slot ? s.chat.slotStatusDetail?.[slot] : undefined))
  const liveTitle = steps.live
    ? ((detail?.kind === 'tool' ? toolStatusLabel(detail, simplified, uiLang) : '') ||
      stepTitle(steps.steps) ||
      i18nT('pages.chat.chatFooter.thinking'))
    : ''
  // The same ring a running tool row spins, so the line and its rows read as
  // one family; still under reduced motion.
  const mark = steps.live && (
    <LoaderCircle
      size={12}
      data-testid="crewmate-steps-mark"
      aria-hidden="true"
      className="shrink-0 text-accent animate-spin motion-reduce:animate-none"
    />
  )
  // Narrow screens wrap the whole title rather than cut it: a tooltip is out of
  // reach on touch, and the current step is the line's whole point.
  const title = <span className="min-w-0 break-words sm:truncate text-text" title={liveTitle}>{liveTitle}</span>
  // The footer's working announcement, which stands down while this line is
  // live: a listener hears the same status it would have.
  const status = steps.live && <span role="status" className="sr-only">{i18nT('pages.chat.chatFooter.thinking')}</span>
  const lineClass = '-ml-2 flex max-w-full min-w-0 items-center gap-2 rounded-md px-2 py-0.5 text-left text-[13px] text-muted'
  // A turn that has not taken a step yet: nothing to open, so a plain status
  // line with the same mark and words rather than a button that opens nothing.
  if (steps.steps.length === 0) {
    return (
      <div data-testid="crewmate-steps" data-live={steps.live ? 'true' : undefined} className="mt-1.5 min-w-0">
        <div className={lineClass}><span aria-hidden="true" className="size-3 shrink-0" />{mark}{title}</div>
        {status}
      </div>
    )
  }
  return (
    <div data-testid="crewmate-steps" data-live={steps.live ? 'true' : undefined} className="mt-1.5 min-w-0">
      {/* The shared Btn, flattened to a quiet text line: no border, no fill. */}
      <Btn
        type="button"
        aria-expanded={expanded}
        aria-controls={expanded ? panelId : undefined}
        onClick={onToggle}
        className={`${lineClass} border-transparent bg-transparent hover:border-transparent hover:bg-transparent hover:text-text`}
      >
        <ChevronRight size={12} aria-hidden="true" className={`shrink-0 transition-transform duration-150 motion-reduce:transition-none ${expanded ? 'rotate-90' : ''}`} />
        {mark}
        {steps.live ? (
          <>
            {/* Open, the running row below names the current step, so the
                heading keeps only the count. */}
            {(!expanded || steps.count === 0) && title}
            {steps.count > 0 && (
              <span className="shrink-0 text-muted">{i18nT('pages.chat.crewmateSteps.step', { count: steps.count })}</span>
            )}
          </>
        ) : (
          <span className="truncate">{i18nT('pages.chat.turnBlock.worked_through_step', { count: steps.count })}</span>
        )}
      </Btn>
      {status}
      {expanded && (
        // Indented past the chevron, so the rows read as this line's children.
        <div id={panelId} data-testid="crewmate-steps-list" className="min-w-0 pl-5">
          {children()}
        </div>
      )}
    </div>
  )
}
