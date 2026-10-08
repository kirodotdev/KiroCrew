/**
 * One change card, from proposal to its one-line result.
 *
 * The card is one element through its whole life, drawn in the conversation
 * where the agent proposed it (`ConversationCard`): pending and applying render
 * the full card, a finished card collapses (animated, in place) into the result
 * line. What changes and its risk are the gateway's; the agent's reason is a
 * separate, plain-text block. Apply and undo replay the card's own plan against
 * the existing routes; the status shown afterwards is the gateway's record.
 */
import { useCallback, useEffect, useId, useRef, useState } from 'react'
import { motion, useReducedMotion } from 'framer-motion'
import { useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, LoaderCircle } from 'lucide-react'
import { Btn } from '../components/ui'
import ErrorNotice from '../components/ErrorNotice'
import { i18nT } from '../i18n/t'
import { fmtRelative, toDate } from '../i18n/format'
import { cardsApi, CARD_FINISHED_STATUSES, type Card } from '../api/cards'
import { CardChanges } from './CardChanges'
import { cardChanges, cardTitle, FALLBACK_ICON, specFor } from './cardRegistry'
import { oneShotRunAt } from './oneShot'
import { CardRow, UserInputs } from './kindBodies'
import CardResultLine from './CardResultLine'
import { runCardPlan, userFields } from './runPlan'
import { problemFrom, problemText, wantsRefresh, type CardProblem } from './refusals'
import { useCardCorners } from './cardCorners'
import { MEMBERS_ROSTER_QUERY_KEY } from '../api/membersQuery'

/** How long an edited field waits before it is re-previewed. */
export const PREVIEW_DEBOUNCE_MS = 400
/** How often a waiting card re-reads the clock for its countdown and expiry. */
export const CLOCK_TICK_MS = 15_000

export function isCardExpired(card: Card, now = Date.now()): boolean {
  if (card.status === 'expired') return true
  if (card.status !== 'pending') return false
  const at = toDate(card.expires_at)
  return !!at && at.getTime() <= now
}

function safeHttpUrl(value: string): string {
  try {
    const url = new URL(value)
    return url.protocol === 'https:' || url.protocol === 'http:' ? url.toString() : ''
  } catch {
    return ''
  }
}


interface ChangeCardProps {
  card: Card
  store: (c: Card | null) => void
  refresh: () => void
  remove: (id: string) => void
}

export default function ChangeCard({ card, store, refresh, remove }: ChangeCardProps) {
  const spec = specFor(card.kind)
  const reduce = useReducedMotion()
  const corners = useCardCorners()
  const queryClient = useQueryClient()
  const headingId = useId()
  const rootRef = useRef<HTMLElement>(null)
  const lineRef = useRef<HTMLDivElement>(null)
  const [running, setRunning] = useState<null | 'apply' | 'undo'>(null)
  const [problem, setProblem] = useState<CardProblem | null>(null)
  const [ackRevision, setAckRevision] = useState<number | null>(null)
  const [previewing, setPreviewing] = useState(false)
  const [inputs, setInputs] = useState<Record<string, string>>({})
  const [signinUrl, setSigninUrl] = useState<string | null>(null)
  const acted = useRef(false)

  // The card re-reads the clock while it is waiting on the person, so a
  // proposal that expires, or a reminder whose time comes, says so in place
  // instead of only failing when the button is pressed.
  const [now, setNow] = useState(() => Date.now())
  const runAt = oneShotRunAt(card)
  const waiting = card.status === 'pending' || card.status === 'failed'
  useEffect(() => {
    if (!waiting || (runAt === null && !card.expires_at)) return
    const tick = () => setNow(Date.now())
    tick()
    // The countdown ticks; the moment the time comes is scheduled exactly, so
    // the button never outlives it by a tick. A hidden tab's timers are
    // throttled, so coming back re-reads the clock at once.
    const id = window.setInterval(tick, CLOCK_TICK_MS)
    const at = runAt ?? toDate(card.expires_at)?.getTime() ?? null
    const delay = at === null ? -1 : at - Date.now()
    const edge = delay >= 0 && delay < 2 ** 31 - 1 ? window.setTimeout(tick, delay + 50) : undefined
    document.addEventListener('visibilitychange', tick)
    return () => {
      window.clearInterval(id)
      if (edge !== undefined) window.clearTimeout(edge)
      document.removeEventListener('visibilitychange', tick)
    }
  }, [waiting, runAt, card.expires_at])
  const timePassed = waiting && runAt !== null && runAt <= now
  // When the time passes under the person's focus on the button, focus moves
  // to the control that is still there instead of falling to the page.
  const footerRef = useRef<HTMLDivElement>(null)
  const applyFocused = useRef(false)
  useEffect(() => {
    if (!timePassed || !applyFocused.current) return
    applyFocused.current = false
    const active = document.activeElement
    if (active === null || active === document.body) {
      footerRef.current?.querySelector<HTMLButtonElement>('button')?.focus()
    }
  }, [timePassed])

  const fields = userFields(card)
  const finished = CARD_FINISHED_STATUSES.has(card.status) && card.status !== 'failed' && card.status !== 'expired'
  const expired = isCardExpired(card, now)
  // An apply this tab is not running that stopped between two writes (a reload,
  // a dropped connection) or while waiting on an approval poll: it continues
  // from the step the gateway expects.
  const stranded = card.status === 'applying' && running === null && !!card.resume
    && (card.resume.step > 0 || !!card.progress?.waiting)
  const busy = running !== null || (card.status === 'applying' && !stranded) || previewing
  const widen = card.risk === 'widen'
  const acknowledged = !widen || ackRevision === card.revision
  const canApply = !!spec && !expired && !timePassed && !busy && acknowledged
    && (card.status === 'pending' || card.status === 'failed' || stranded)
    && fields.every(f => (inputs[f] ?? '').trim().length > 0)

  const preview = useCallback((params: Record<string, unknown>) => {
    setPreviewing(true)
    setProblem(null)
    return cardsApi.preview(card, params)
      .then(store)
      .catch(e => setProblem(problemFrom(e)))
      .finally(() => setPreviewing(false))
  }, [card, store])

  // The collapse to a result line moves focus to it, when the user was here.
  const wasFinished = useRef(finished)
  useEffect(() => {
    if (finished && !wasFinished.current) {
      const inside = !!rootRef.current && rootRef.current.contains(document.activeElement)
      if (acted.current || inside) lineRef.current?.focus()
    }
    wasFinished.current = finished
  }, [finished])

  /** The gateway's current record of this card, folded into the cache. */
  const reload = useCallback(async () => {
    const list = await cardsApi.pending(card.slot_key)
    list.forEach(store)
    return list.find(c => c.id === card.id) ?? null
  }, [card.slot_key, card.id, store])

  const run = useCallback(async (op: 'apply' | 'undo') => {
    if (running) return
    // The clock is re-read at the press itself: a reminder whose time has just
    // passed would only be refused by the gateway.
    if (op === 'apply' && runAt !== null && runAt <= Date.now()) {
      setNow(Date.now())
      return
    }
    acted.current = true
    setRunning(op)
    setProblem(null)
    // The typed values leave the card's state as the plan starts; only this
    // run's closure holds them until its steps are sent.
    const typed = op === 'apply' ? inputs : {}
    if (op === 'apply') setInputs({})
    try {
      const outcome = await runCardPlan(card, op, {
        inputs: typed,
        resume: op === 'apply' ? (stranded ? card.resume : undefined) : card.undo_resume,
        reload,
        onRepeat: r => {
          const url = (r as { oauth_url?: unknown } | null)?.oauth_url
          setSigninUrl(typeof url === 'string' ? safeHttpUrl(url) || null : null)
        },
      })
      if (!outcome.ok) setProblem(problemFrom(outcome.error))
      else if (outcome.card) store(outcome.card)
    } catch (error) {
      // A step's own refusal comes back as a PlanOutcome; anything else the plan
      // throws (a failed re-read of the card between polls) lands here, so the
      // press still ends on an ErrorNotice rather than silently.
      setProblem(problemFrom(error))
    } finally {
      setSigninUrl(null)
      setRunning(null)
      refresh()
      // A crewmate card adds or changes a roster row: the sidebar's count and
      // the Crewmates list read the roster, so it is re-read now rather than
      // whenever the next chat event happens to refetch it.
      if (card.kind.startsWith('crewmate.')) void queryClient.invalidateQueries({ queryKey: MEMBERS_ROSTER_QUERY_KEY })
    }
  }, [running, runAt, stranded, card, inputs, reload, store, refresh, queryClient])

  const cancel = useCallback(() => {
    acted.current = true
    setProblem(null)
    setInputs({})
    cardsApi.cancel(card).then(store).catch(e => setProblem(problemFrom(e)))
  }, [card, store])

  const dismiss = useCallback(() => {
    cardsApi.dismiss(card).then(() => remove(card.id)).catch(e => setProblem(problemFrom(e)))
  }, [card, remove])

  // A refusal a fresh read resolves: a pending card is previewed again at its
  // current revision; a finished one is simply re-read.
  const recover = useCallback(() => {
    setProblem(null)
    if (card.status === 'pending' || card.status === 'failed') void preview(card.params)
    else void reload().catch(e => setProblem(problemFrom(e)))
  }, [card, preview, reload])

  const shownProblem: CardProblem | null = problem
    ?? (card.error && (card.status === 'failed' || card.status === 'partial' || card.status === 'applied') ? { code: card.error.code, message: card.error.message } : null)

  const progress = card.progress
  const statusText = progress?.waiting ? i18nT('components.changeCards.status_waiting_signin')
    : running === 'undo' || progress?.op === 'undo' ? i18nT('components.changeCards.status_undoing')
    : running === 'apply' || card.status === 'applying'
      ? (progress && progress.total > 1
        ? i18nT('components.changeCards.status_applying_step', { done: Math.min(progress.done + 1, progress.total), total: progress.total })
        : i18nT('components.changeCards.status_applying'))
    : previewing ? i18nT('components.changeCards.status_previewing')
    : timePassed && !expired ? i18nT('components.changeCards.time_passed') : ''

  const Icon = spec?.icon ?? FALLBACK_ICON
  const expiresAt = toDate(card.expires_at)
  const title = cardTitle(card)

  return (
    <motion.section
      ref={rootRef}
      // Size only: a scroll moves the card inside the virtual list, and
      // animating that position change would make it jitter.
      layout={reduce ? false : 'size'}
      transition={reduce ? { duration: 0 } : { duration: 0.2 }}
      aria-labelledby={headingId}
      data-testid="change-card"
      data-card-status={card.status}
      className={`@container ${corners} border border-border bg-card text-card-fg`}
    >
      {finished ? (
        <CardResultLine
          ref={lineRef}
          card={card}
          spec={spec}
          headingId={headingId}
          undoing={running === 'undo'}
          statusText={running === 'undo' ? statusText : ''}
          problem={problem ?? (card.error ? { code: card.error.code, message: card.error.message } : null)}
          onUndo={() => void run('undo')}
          onRecover={recover}
          onClearError={() => setProblem(null)}
        />
      ) : (
        // One left edge: the body and the footer share the same 16px inset, and
        // nothing inside adds horizontal padding of its own. The footer's
        // divider runs the card's full width.
        <div className="flex flex-col">
          <div className="px-4 pt-4 pb-3" data-testid="change-card-body">
            <div className="flex items-center gap-2 text-[12px] leading-4 font-medium text-muted" data-testid="change-card-kind">
              <Icon size={16} className="shrink-0" aria-hidden="true" />
              <span className="min-w-0 break-words">{spec?.label() ?? card.kind}</span>
            </div>
            <div className="mt-1 flex flex-wrap items-baseline gap-x-4 gap-y-0.5">
              <h3 id={headingId} className="m-0 min-w-0 flex-1 basis-[14rem] text-[14px] font-semibold leading-5 text-text break-words">{title}</h3>
              {runAt !== null && !timePassed && !expired && card.status === 'pending' ? (
                <span className="text-[12px] leading-5 text-muted @min-[480px]:ml-auto" data-testid="change-card-due">
                  {i18nT('components.changeCards.due', { when: fmtRelative(runAt, { now, style: 'long' }) })}
                </span>
              ) : expiresAt && !expired && !timePassed && card.status === 'pending' && (
                <span className="text-[12px] leading-5 text-muted @min-[480px]:ml-auto" data-testid="change-card-expires">
                  {i18nT('components.changeCards.expires', { when: fmtRelative(expiresAt, { now }) })}
                </span>
              )}
            </div>

            {(card.risk === 'widen' || card.risk === 'code_exec') && (
              <div className="mt-3 rounded-md border border-warn/30 bg-warn-subtle px-3 py-2 text-[13px] leading-5 text-text" data-testid="change-card-risk">
                <div className="flex items-start gap-2">
                  <AlertTriangle size={14} className="mt-[3px] shrink-0 text-warn" aria-hidden="true" />
                  <span>{card.risk === 'widen' ? i18nT('components.changeCards.risk_widen') : i18nT('components.changeCards.risk_code_exec')}</span>
                </div>
                {widen && (
                  <label className="mt-1 flex min-h-11 items-center gap-2 cursor-pointer">
                    <input
                      type="checkbox"
                      aria-label={i18nT('components.changeCards.risk_widen_ack')}
                      data-testid="change-card-widen-ack"
                      checked={ackRevision === card.revision}
                      disabled={expired || busy}
                      onChange={e => setAckRevision(e.target.checked ? card.revision : null)}
                      className="h-5 w-5 shrink-0 accent-[var(--accent)]"
                    />
                    {i18nT('components.changeCards.risk_widen_ack')}
                  </label>
                )}
              </div>
            )}

            <div className="mt-2 [&>[data-card-row]+[data-card-row]]:border-t [&>[data-card-row]+[data-card-row]]:border-border" data-testid="change-card-rows">
              <CardChanges changes={cardChanges(card)} />
              {card.scope && (
                <CardRow label={i18nT('components.changeCards.scope_label')}>
                  {/* A complete name list already says how many; the count is
                      shown only when some affected members go unnamed. */}
                  {card.scope.members.length === card.scope.count && card.scope.count > 0
                    ? <span className="break-words">{card.scope.members.join(', ')}</span>
                    : <>
                        <span>{i18nT('components.changeCards.scope_count', { count: card.scope.count })}</span>
                        {card.scope.members.length > 0 && <span className="block text-muted break-words">{card.scope.members.join(', ')}</span>}
                      </>}
                </CardRow>
              )}
              {spec && <spec.Body card={card} disabled={busy || expired} />}
              <UserInputs fields={fields} values={inputs} disabled={busy || expired} onChange={(f, v) => setInputs(prev => ({ ...prev, [f]: v }))} />
            </div>

            {card.reason && (
              <div className="mt-2" data-testid="change-card-reason">
                <div className="text-[12px] leading-4 text-muted">{i18nT('components.changeCards.reason_label')}</div>
                <p className="m-0 mt-1 whitespace-pre-wrap break-words text-[13px] leading-5 text-text">{card.reason}</p>
              </div>
            )}

            {/* No hand-off: a typed secret may be unsaved here. */}
            <ErrorNotice
              className="mt-3"
              variant="inline"
              testId="change-card-error"
              message={shownProblem ? problemText(shownProblem) : ''}
            />
            {shownProblem && wantsRefresh(shownProblem.code) && (
              <Btn className="mt-2 min-h-11" onClick={recover} disabled={busy} data-testid="change-card-refresh">
                {i18nT('components.changeCards.refresh_preview')}
              </Btn>
            )}

            {expired && <p className="mt-3 mb-0 text-[12px] text-muted" data-testid="change-card-expired">{i18nT('components.changeCards.expired')}</p>}

            <p role="status" aria-live="polite" className="m-0 mt-2 text-[12px] text-muted empty:hidden" data-testid="change-card-status" data-time-passed={timePassed && !expired ? 'true' : undefined}>
              {statusText}
            </p>
            {signinUrl && (
              <a href={signinUrl} target="_blank" rel="noopener noreferrer" className="inline-flex min-h-11 items-center text-[13px] text-accent">
                {i18nT('components.changeCards.open_signin')}
              </a>
            )}
          </div>

          <div ref={footerRef} className="flex flex-wrap items-center justify-end gap-2 border-t border-border px-4 py-3" data-testid="change-card-footer">
            {card.status === 'pending' && !expired && (
              <Btn
                className="min-h-11 border-transparent hover:border-transparent text-muted hover:text-text"
                onClick={cancel}
                disabled={busy}
                data-testid="change-card-cancel"
              >
                {i18nT('components.changeCards.cancel')}
              </Btn>
            )}
            {(card.status === 'failed' || expired) && (
              <Btn
                className="min-h-11 border-transparent hover:border-transparent text-muted hover:text-text"
                onClick={dismiss}
                disabled={running !== null}
                data-testid="change-card-dismiss"
              >
                {i18nT('components.changeCards.dismiss')}
              </Btn>
            )}
            {!timePassed && <Btn
              onFocus={() => { applyFocused.current = true }}
              onBlur={e => { if (e.relatedTarget) applyFocused.current = false }}
              primary className="min-h-11 px-4" onClick={() => void run('apply')} disabled={!canApply} data-testid="change-card-apply">
              {running === 'apply' && <LoaderCircle size={14} className="animate-spin" aria-hidden="true" />}
              {stranded ? i18nT('components.changeCards.continue_apply')
                : card.status === 'failed' ? i18nT('components.changeCards.retry')
                : spec?.primary(card) ?? i18nT('components.changeCards.apply_fallback')}
            </Btn>}
          </div>
        </div>
      )}
    </motion.section>
  )
}
