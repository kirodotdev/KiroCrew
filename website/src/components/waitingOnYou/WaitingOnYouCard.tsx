/** One pinned ask: what the agent needs from the person, and the controls
 *  that answer or dismiss it. A decision card offers the agent's options; an
 *  action card offers Done with an optional note. Once the ask is gone the same
 *  card turns into its notice, so the person sees the card they were reading
 *  change rather than one card leave and another arrive. */
import { useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { motion } from 'framer-motion'
import { Check, Hand, Send, X } from 'lucide-react'
import { Btn, IconButton } from '../ui'
import ErrorNotice from '../ErrorNotice'
import MarkdownRenderer from '../MarkdownRenderer'
import { fmtRelative } from '../../i18n/format'
import { useViewerIsNotOwner } from '../../hooks/sharedCrew/useSharedCrew'
import type { WaitingOnYouAsk } from '../../types'
import type { WaitingAskState, WaitingChoice } from './useWaitingOnYouActions'

/** A detail this long, or on more than one line, is clamped with a toggle. */
const DETAIL_CLAMP_CHARS = 120

const CARD_CLS = 'rounded-lg border bg-card px-3 py-2.5 text-[13px] text-text transition-colors'

export function WaitingHeader({ agent, ts, trailing }: { agent?: string; ts?: number; trailing?: ReactNode }) {
  const { t } = useTranslation()
  return (
    <div className="flex items-center gap-1.5 min-w-0 text-[11px] leading-4">
      <Hand size={12} className="shrink-0 text-aim" aria-hidden />
      <span className="font-semibold uppercase tracking-[.06em] text-aim shrink-0">{t('components.waitingOnYou.header')}</span>
      {agent && <span className="text-muted truncate min-w-0" translate="no">· {agent}</span>}
      <span className="flex-1" />
      {ts ? <span className="text-muted shrink-0" data-testid="waiting-on-you-age">{t('components.waitingOnYou.pinned_ago', { time: fmtRelative(ts * 1000) })}</span> : null}
      {trailing}
    </div>
  )
}

/** An ask that is gone, kept on screen because the person still has something
 *  to see or do about it: a note they had typed, or why their answer did not go. */
export interface WaitingClosedView {
  testId: string
  message: string
  /** The unsent note, shown so the person can still copy it. */
  note?: string
  actions?: ReactNode
}

export default function WaitingOnYouCard({ ask, agent, state, answerQueued, note, onNoteChange, onAnswer, onDismiss, closed }: {
  ask: WaitingOnYouAsk
  agent?: string
  state: WaitingAskState | undefined
  /** This ask already has an answer waiting in the slot queue. */
  answerQueued?: boolean
  /** The action card's note, held outside this component so it survives a remount. */
  note: string
  onNoteChange: (text: string) => void
  onAnswer: (choice: WaitingChoice) => void
  onDismiss: () => void
  /** Set once the ask is gone: the card shows this notice in place of its controls. */
  closed?: WaitingClosedView
}) {
  const { t } = useTranslation()
  const [expanded, setExpanded] = useState(false)
  const [selectedLabel, setSelectedLabel] = useState('')
  // A collaborator who is not the session owner sees the ask with no controls:
  // the routes refuse anyone else, so a button would only fail.
  const readOnly = useViewerIsNotOwner().notOwner
  const busy = !!state?.busy
  const controlsDisabled = busy || !!answerQueued
  const isAction = ask.options.length === 0
  const clampable = ask.detail.length > DETAIL_CLAMP_CHARS || ask.detail.includes('\n')
  // The length rule only guesses: on a wide card a long detail can fit in two
  // lines, and a toggle that reveals nothing reads as broken. Once laid out,
  // the clamp itself says whether it hides anything.
  const detailRef = useRef<HTMLDivElement>(null)
  const [clampHides, setClampHides] = useState(true)
  useLayoutEffect(() => {
    const el = detailRef.current
    if (!el || !clampable || expanded) return
    const measure = () => { if (el.clientHeight > 0) setClampHides(el.scrollHeight > el.clientHeight + 1) }
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(measure)
    observer.observe(el)
    if (el.firstElementChild) observer.observe(el.firstElementChild)
    return () => observer.disconnect()
  }, [clampable, expanded, ask.detail])
  // Only a change of state fades the body in; a card mounted in either state shows at once.
  const [mountedClosed] = useState(!!closed)
  const bodyFades = mountedClosed !== !!closed
  const showClampToggle = clampable && (expanded || clampHides)
  const trailing = !closed && !readOnly && (
      <IconButton variant="danger" aria-label={t('components.waitingOnYou.dismiss_aria')} disabled={controlsDisabled} onClick={onDismiss} data-testid="waiting-on-you-dismiss">
        <X size={13} aria-hidden />
      </IconButton>
    )
  return (
    <div className={`${CARD_CLS} ${closed ? 'border-border' : 'border-[color-mix(in_srgb,var(--aim)_40%,var(--border))]'}`}
      data-testid={closed ? closed.testId : 'waiting-on-you-card'} data-ask-key={ask.key} data-kind={isAction ? 'action' : 'decision'}
      role={closed ? 'status' : undefined}>
      <WaitingHeader agent={agent} ts={closed ? undefined : ask.ts} trailing={trailing} />
      <div className={`mt-1 font-medium break-words ${closed ? 'text-muted line-through' : 'text-text-strong'}`}
        data-testid={closed ? undefined : 'waiting-on-you-title'}>{ask.title}</div>
      <motion.div key={closed ? 'closed' : 'live'} initial={bodyFades ? { opacity: 0 } : false} animate={{ opacity: 1 }} transition={{ duration: 0.14 }}>
        {closed ? (
          <>
            <p className="mt-1 mb-0 text-[12px] text-text">{closed.message}</p>
            {closed.note && (
              <p className="mt-1.5 mb-0 px-2.5 py-1.5 rounded-md bg-bg-elevated border border-border text-[12px] text-text whitespace-pre-wrap break-words select-text"
                data-testid="waiting-on-you-closed-note">{closed.note}</p>
            )}
            {closed.actions && <div className="mt-2 flex flex-wrap gap-1.5">{closed.actions}</div>}
          </>
        ) : (
          <>
            {ask.detail && (
              <div className="mt-1 text-text">
                <div ref={detailRef} className={clampable && !expanded ? 'line-clamp-2' : ''} data-testid="waiting-on-you-detail">
                  <MarkdownRenderer content={ask.detail} softBreaks />
                </div>
                {showClampToggle && (
                  <button type="button" className="mt-0.5 p-0 bg-transparent border-none cursor-pointer text-[12px] text-muted hover:text-text"
                    aria-expanded={expanded} onClick={() => setExpanded(v => !v)}>
                    {t(expanded ? 'components.waitingOnYou.show_less' : 'components.waitingOnYou.show_more')}
                  </button>
                )}
              </div>
            )}
            {answerQueued && (
              <p className="mt-1.5 mb-0 text-[12px] text-muted" role="status" data-testid="waiting-on-you-answer-queued">
                {t('components.waitingOnYou.answer_queued')}
              </p>
            )}
            {readOnly ? (
              <>
                {ask.options.length > 0 && (
                  <ul className="mt-1.5 mb-0 pl-4 text-[12px] text-muted list-disc">
                    {ask.options.map(o => <li key={o.label} className="[overflow-wrap:anywhere]">{o.label}</li>)}
                  </ul>
                )}
                <p className="mt-1.5 mb-0 text-[12px] text-muted" data-testid="waiting-on-you-read-only">{t('components.waitingOnYou.read_only')}</p>
              </>
            ) : isAction ? (
              <div className="mt-2 flex items-end gap-2">
                <textarea
                  value={note}
                  onChange={e => onNoteChange(e.target.value)}
                  disabled={controlsDisabled}
                  rows={1}
                  aria-label={t('components.waitingOnYou.note_label')}
                  placeholder={t('components.waitingOnYou.note_placeholder')}
                  data-testid="waiting-on-you-note"
                  className="flex-1 min-w-0 resize-y bg-bg-elevated border border-border rounded-md px-2.5 py-1.5 text-[13px] text-text font-body outline-hidden focus-ring"
                />
                <Btn type="button" primary busy={busy} disabled={controlsDisabled} onClick={() => onAnswer({ text: note.trim() })} data-testid="waiting-on-you-done">
                  <Check size={13} aria-hidden />{t('components.waitingOnYou.done')}
                </Btn>
              </div>
            ) : (
              <>
                <div className="mt-2 flex flex-col gap-1.5" role="group" aria-label={ask.title} data-testid="waiting-on-you-options">
                  {ask.options.map(o => {
                    const selected = selectedLabel === o.label
                    return (
                      <button
                        key={o.label}
                        type="button"
                        disabled={controlsDisabled}
                        aria-pressed={selected}
                        title={o.description || undefined}
                        className={`flex items-start text-left px-3 py-2 rounded-lg text-[13px] cursor-pointer transition-all border disabled:opacity-50 disabled:cursor-not-allowed ${
                          selected
                            ? 'border-aim text-text bg-aim-subtle'
                            : 'border-border text-muted hover:text-text hover:border-aim bg-bg'
                        }`}
                        onClick={() => setSelectedLabel(current => current === o.label ? '' : o.label)}
                        data-testid="waiting-on-you-option"
                      >
                        <span className="min-w-0 [overflow-wrap:anywhere]">
                          <span className="font-medium">{o.label}</span>
                          {o.description && <span className="block text-[11px] text-muted font-normal">{o.description}</span>}
                        </span>
                      </button>
                    )
                  })}
                </div>
                <div className="mt-2 flex justify-end">
                  <Btn type="button" primary busy={busy} disabled={controlsDisabled || !selectedLabel}
                    onClick={() => onAnswer({ label: selectedLabel })} data-testid="waiting-on-you-send">
                    <Send size={13} aria-hidden />{t('components.chatInput.send')}
                  </Btn>
                </div>
              </>
            )}
            {/* No hand-off: the note above is unsent, and the card's own buttons retry. */}
            <ErrorNotice className="mt-2" variant="inline" testId="waiting-on-you-error"
              message={state?.errorKey ? [t(state.errorKey), state.errorDetail].filter(Boolean).join(' ') : null} />
          </>
        )}
      </motion.div>
    </div>
  )
}
