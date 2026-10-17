/**
 * A finished change card, collapsed to one line in the conversation: icon,
 * summary, actions.
 *
 * Details expands what changed; Undo replays the gateway's undo plan and is
 * named for what it actually does (Disconnect, Delete secret). An undone or
 * cancelled card reads muted and says so. The line takes focus when the card
 * collapses into it, and its summary is announced. It has no close control:
 * it is the conversation's record of the change, where the agent proposed it.
 */
import { forwardRef, useId, useState } from 'react'
import { CheckCircle2, AlertTriangle, RotateCcw, X, ChevronDown, LoaderCircle } from 'lucide-react'
import { Btn } from '../components/ui'
import ErrorNotice from '../components/ErrorNotice'
import { i18nT } from '../i18n/t'
import type { Card } from '../api/cards'
import { cardChanges, cardTitle, undoLabelFor, type CardKindSpec } from './cardRegistry'
import { problemText, wantsRefresh, type CardProblem } from './refusals'
import { CardChanges, changeLabel } from './CardChanges'
import { isOneShot, oneShotWhen } from './oneShot'
import { fmtCardCron } from './scheduleText'

interface CardResultLineProps {
  card: Card
  spec: CardKindSpec | null
  headingId: string
  undoing: boolean
  statusText: string
  problem: CardProblem | null
  onUndo: () => void
  onRecover: () => void
  onClearError: () => void
}

export function resultSummary(card: Card): string {
  const title = cardTitle(card)
  switch (card.status) {
    case 'undone': return i18nT('components.changeCards.result_undone', { title })
    case 'cancelled': return i18nT('components.changeCards.result_cancelled', { title })
    case 'partial': return card.result?.summary || i18nT('components.changeCards.result_partial', { title })
    // The gateway's summary is a fixed English "Done: <title>", and its title is
    // an imperative ("Create schedule …"), so "Done: Create …" read as an order.
    // The dashboard words the outcome per kind from the card's own data.
    default: return appliedSummary(card, title)
  }
}

/** What an applied card now IS, by kind: "Reminder set: …", "Schedule created: …". */
function appliedSummary(card: Card, title: string): string {
  const name = typeof card.params.name === 'string' && card.params.name.trim() ? card.params.name.trim() : null
  if (card.kind === 'schedule.create' && name) {
    if (isOneShot(card)) {
      const when = oneShotWhen(card, { capitalize: false })
      if (when) return i18nT('components.changeCards.result_reminder_set', { name, when })
    } else if (typeof card.params.cron_expr === 'string') {
      return i18nT('components.changeCards.result_schedule_created', { name, schedule: fmtCardCron(card.params.cron_expr) })
    }
  }
  if (card.kind === 'crewmate.create' && name) return i18nT('components.changeCards.result_crewmate_created', { name })
  if (card.kind === 'setting.change') {
    const row = cardChanges(card)[0]
    const label = row ? changeLabel(row) : ''
    if (label) return i18nT('components.changeCards.result_setting_changed', { label })
  }
  return i18nT('components.changeCards.result_applied', { title })
}

const CardResultLine = forwardRef<HTMLDivElement, CardResultLineProps>(function CardResultLine(
  { card, spec, headingId, undoing, statusText, problem, onUndo, onRecover, onClearError },
  ref,
) {
  const [open, setOpen] = useState(false)
  const detailsId = useId()
  const settled = card.status === 'undone' || card.status === 'cancelled'
  const undoable = (card.status === 'applied' || card.status === 'partial') && !!card.plan.undo?.length
  const Icon = card.status === 'partial' ? AlertTriangle : settled ? (card.status === 'undone' ? RotateCcw : X) : CheckCircle2
  const tone = card.status === 'partial' ? 'text-warn' : settled ? 'text-muted' : 'text-ok'

  return (
    <div className="px-4 py-1.5" data-testid="change-card-result-body">
      <div
        ref={ref}
        tabIndex={-1}
        data-testid="change-card-result"
        className="flex min-h-11 flex-wrap items-center gap-x-2 gap-y-1 rounded-md focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <Icon size={16} className={`shrink-0 ${tone}`} aria-hidden="true" />
        <span id={headingId} role="status" aria-live="polite" className={`min-w-0 flex-1 basis-48 break-words text-[13px] ${settled ? 'text-muted' : 'text-text'}`}>
          {resultSummary(card)}
        </span>
        <span className="ml-auto inline-flex flex-wrap items-center gap-1">
          <Btn
            className="min-h-11 border-transparent hover:border-transparent"
            aria-expanded={open}
            aria-controls={detailsId}
            onClick={() => setOpen(o => !o)}
            data-testid="change-card-details"
          >
            {i18nT('components.changeCards.details')}
            <ChevronDown size={14} aria-hidden="true" className={`transition-transform ${open ? 'rotate-180' : ''}`} />
          </Btn>
          {undoable && spec && (
            <Btn className="min-h-11 border-transparent hover:border-transparent" onClick={onUndo} disabled={undoing} data-testid="change-card-undo">
              {undoing && <LoaderCircle size={14} className="animate-spin" aria-hidden="true" />}
              {undoLabelFor(card)}
            </Btn>
          )}
        </span>
      </div>
      <p role="status" aria-live="polite" className="m-0 text-[12px] text-muted empty:hidden">
        {statusText}
      </p>
      {!undoable && (card.status === 'applied' || card.status === 'partial') && card.undo_unavailable_reason && (
        <p className="m-0 mb-1 text-[12px] text-muted" data-testid="change-card-undo-unavailable">
          {card.undo_unavailable_reason === 'overwrites_existing'
            ? i18nT('components.changeCards.undo_unavailable_overwrites')
            : i18nT('components.changeCards.undo_unavailable')}
        </p>
      )}
      {/* No hand-off: nothing unsaved here, but the line sits beside a chat draft. */}
      <ErrorNotice variant="inline" className="mb-1" testId="change-card-undo-error" message={problem ? problemText(problem) : ''} onDismiss={onClearError} />
      {problem && wantsRefresh(problem.code) && (
        <Btn className="mb-1 min-h-11" onClick={onRecover} data-testid="change-card-refresh">
          {i18nT('components.changeCards.refresh_preview')}
        </Btn>
      )}
      <div id={detailsId} hidden={!open} className="pb-2 [&>[data-card-row]+[data-card-row]]:border-t [&>[data-card-row]+[data-card-row]]:border-border">
        {open && <CardChanges changes={cardChanges(card)} />}
        {open && card.result?.details && <p className="m-0 pt-2 text-[12px] text-muted whitespace-pre-wrap break-words">{card.result.details}</p>}
      </div>
    </div>
  )
})

export default CardResultLine
