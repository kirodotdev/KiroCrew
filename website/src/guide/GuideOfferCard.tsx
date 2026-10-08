/**
 * A guide's offer, drawn in the conversation at the point the agent offered it,
 * and its one-line result once it ends.
 *
 * Built on the change card's shell (same border, header, button row and
 * one-line result) so the two read as one kind of object in the chat. The
 * row that places it is the gateway's (a `card` transcript row, see
 * `cards/ConversationCard`); the guide's LIVE state is the guide store's, read
 * through GuideContext, so the offer turns into its result line in place.
 * Nothing moves until Start; the floating GuideLayer then walks the user
 * through the steps.
 *
 * When the store no longer holds the guide (pruned, or the gateway restarted),
 * the row's own recorded status is what is drawn, without actions.
 */
import { useEffect, useId, useMemo, useRef, useState } from 'react'
import { motion, useReducedMotion } from 'framer-motion'
import { CheckCircle2, Compass, RotateCcw, X } from 'lucide-react'
import { Btn } from '../components/ui'
import ErrorNotice from '../components/ErrorNotice'
import { i18nT } from '../i18n/t'
import { GUIDE_TERMINAL_STATUSES, type Guide, type GuideStatus } from '../api/guide'
import { useGuide } from './GuideContext'
import { resolveGuideActions } from './guideActions'
import { useCardCorners } from '../cards/cardCorners'
import GuideAgentNote from './GuideAgentNote'

/** How a guide's end reads, by terminal status; shared with GuideLayer's finish chip. */
export const FINISHED_KEYS: Record<string, string> = {
  completed: 'components.guideLayer.finished_completed',
  cancelled: 'components.guideLayer.finished_cancelled',
  expired: 'components.guideLayer.finished_expired',
}

/** The end's catalog key; a guide whose save went through without it says so. */
export function finishedKeyFor(status: string, reason?: string | null, fallback = 'components.guideLayer.finished_expired'): string {
  if (status === 'cancelled' && reason === 'saved_without_guide') return 'components.guideLayer.finished_saved_without_guide'
  return FINISHED_KEYS[status] ?? fallback
}

interface GuideOfferCardProps {
  /** The guide this conversation row offered. */
  guideId: string
  /** The slot whose conversation draws the row; only that slot's guide is live here. */
  slotKey: string | null
  /** The status the row last recorded, drawn when the store no longer has it. */
  recordedStatus?: string | null
  /** The end reason the row recorded, for the same fallback. */
  recordedReason?: string | null
}

/**
 * Every guide card drawn in a conversation, in mount order, so an ended
 * guide's line can tell whether a newer guide card follows it: a cancelled or
 * expired line with a newer guide after it is noise (one question asked again
 * and again leaves a wall of "Guide cancelled" rows on a phone), so only the
 * last one is drawn.
 */
const guideCards = new Set<HTMLElement>()
const cardListeners = new Set<() => void>()
const cardsChanged = () => { for (const l of cardListeners) l() }

/** A ref for this card's element, and whether a guide card follows it in the document. */
export function useLaterGuideCard(): [(el: HTMLElement | null) => void, boolean] {
  const [el, setEl] = useState<HTMLElement | null>(null)
  const [later, setLater] = useState(false)
  useEffect(() => {
    if (!el) return
    guideCards.add(el)
    const read = () => setLater(Array.from(guideCards).some(o => o !== el && o.isConnected
      && !!(el.compareDocumentPosition(o) & Node.DOCUMENT_POSITION_FOLLOWING)))
    cardListeners.add(read)
    cardsChanged()
    return () => {
      guideCards.delete(el)
      cardListeners.delete(read)
      cardsChanged()
    }
  }, [el])
  return [setEl, later]
}

/** One line for a guide that is running elsewhere or has ended. */
function GuideLine({ status, guide, reason, lineRef, headingId, onReplay, busy }: {
  status: string
  guide: Guide | null
  /** The end reason when the live guide is gone (the row's record). */
  reason?: string | null
  lineRef?: React.Ref<HTMLElement>
  headingId: string
  /** Walk this guide again; absent when it cannot be replayed. */
  onReplay?: () => void
  busy?: boolean
}) {
  const corners = useCardCorners()
  // The change was made even when the guide could not confirm it: not a failure mark.
  const saved = status === 'cancelled' && (guide?.reason ?? reason) === 'saved_without_guide'
  const completed = status === 'completed' || saved
  const live = !GUIDE_TERMINAL_STATUSES.has(status as GuideStatus)
  const Icon = completed ? CheckCircle2 : live ? Compass : X
  const text = live
    ? i18nT('components.guideLayer.in_progress')
    : i18nT(finishedKeyFor(status, guide?.reason ?? reason))
  return (
    <div
      aria-labelledby={headingId}
      data-testid="guide-result-row"
      data-guide-status={status}
      className={`${corners} border border-border bg-card text-card-fg px-3 py-1.5`}
    >
      <div
        ref={lineRef as React.Ref<HTMLDivElement>}
        tabIndex={-1}
        data-testid="guide-result"
        className="flex min-h-11 flex-wrap items-center gap-x-2 gap-y-1 rounded-md focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <Icon size={16} className={`shrink-0 ${completed ? 'text-ok' : 'text-muted'}`} aria-hidden="true" />
        <span id={headingId} role="status" aria-live="polite" className={`min-w-0 flex-1 basis-48 break-words text-[13px] ${completed ? 'text-text' : 'text-muted'}`}>
          {text}
        </span>
        {onReplay && (
          <Btn className="min-h-11 border-transparent hover:border-transparent text-muted hover:text-text" onClick={onReplay} disabled={busy} data-testid="guide-replay">
            <RotateCcw size={14} aria-hidden="true" />
            {i18nT('components.guideLayer.show_again')}
          </Btn>
        )}
      </div>
      {guide?.actions.map((action, index) => {
        const name = action.result?.name
        return action.id === 'crewmate.create' && typeof name === 'string'
          ? <p key={index} className="m-0 mb-1 text-[13px] text-text" data-testid="guide-result-crewmate">{i18nT('components.meetCrewmatesFlow.step4_title', { name })}</p>
          : null
      })}
    </div>
  )
}

export default function GuideOfferCard({ guideId, slotKey, recordedStatus = null, recordedReason = null }: GuideOfferCardProps) {
  const ctx = useGuide()
  const reduce = useReducedMotion()
  const corners = useCardCorners()
  const headingId = useId()
  const rootRef = useRef<HTMLElement>(null)
  const lineRef = useRef<HTMLElement>(null)
  const acted = useRef(false)
  const [cardRef, newerCardFollows] = useLaterGuideCard()

  const live = (slotKey && ctx?.guides.find(g => g.guide_id === guideId && g.slot_key === slotKey)) || null
  const offered = live?.status === 'offered' ? live : null
  const resolved = useMemo(() => (offered ? resolveGuideActions(offered.actions) : null), [offered])
  // A refused guide is the floating layer's to explain; the chat offers only
  // a guide the page can finish.
  const showOffer = !!offered && !!resolved?.ok
  // Not live in the store: the row's own record says how it ended. Before the
  // first read of the store an unfinished record cannot be told from a live
  // offer, so nothing is drawn until the read answers.
  const status = live ? live.status : recordedStatus
  const showLine = !showOffer && !!status && (!!live || GUIDE_TERMINAL_STATUSES.has(status as GuideStatus) || !!ctx?.guidesLoaded)
  // A recorded offer the store has forgotten never finished: it is drawn as expired.
  const lineStatus = !live && status && !GUIDE_TERMINAL_STATUSES.has(status as GuideStatus) ? 'expired' : status

  // A completed show-me guide can be walked again; one that saved a change
  // (a committed step) cannot, so it is never offered twice.
  const replayable = !!live && live.status === 'completed' && (() => {
    const r = resolveGuideActions(live.actions)
    return r.ok && r.actions.every(a => a.steps.every(st => st.complete.kind !== 'committed'))
  })()

  // The collapse to a result line moves focus to it, when the user was here.
  const wasLine = useRef(showLine)
  useEffect(() => {
    if (showLine && !wasLine.current) {
      const inside = !!rootRef.current && rootRef.current.contains(document.activeElement)
      if (acted.current || inside) lineRef.current?.focus()
    }
    wasLine.current = showLine
  }, [showLine])

  if (!ctx) return null

  if (!showOffer) {
    if (!showLine || !lineStatus) return null
    const endReason = live?.reason ?? recordedReason
    // A guide replaced by a newer offer leaves nothing: the new one is right below.
    if (lineStatus === 'cancelled' && endReason === 'superseded') return null
    // A cancelled or expired guide with a newer guide card after it: the newer
    // one says where things stand, so this one leaves nothing visible (it
    // stays registered, so the order is still known).
    if ((lineStatus === 'cancelled' && endReason !== 'saved_without_guide') || lineStatus === 'expired') {
      if (newerCardFollows) return <span ref={cardRef} hidden data-testid="guide-line-collapsed" />
    }
    // One the person cancelled leaves one muted line, not a card: several of
    // them in a row must not wall off the conversation.
    if (lineStatus === 'cancelled' && endReason !== 'saved_without_guide') {
      return (
        <div ref={cardRef} className="flex flex-col" data-testid="guide-offer-cards">
        <p
          className="m-0 px-1 text-[12px] leading-5 text-muted"
          role="status"
          data-testid="guide-result-row"
          data-guide-status={lineStatus}
        >
          <span ref={lineRef} tabIndex={-1} data-testid="guide-result" className="focus:outline-none focus-visible:ring-2 focus-visible:ring-ring rounded-sm">
            {i18nT(finishedKeyFor(lineStatus, endReason))}
          </span>
        </p>
        </div>
      )
    }
    // The result line REPLACES the card: a different element, so the offer's
    // shell (header, hint, buttons) leaves the DOM rather than wrapping it.
    return (
      <div ref={cardRef} className="flex flex-col gap-2" data-testid="guide-offer-cards">
        <GuideLine
          status={lineStatus}
          guide={live}
          reason={recordedReason}
          lineRef={lineRef}
          headingId={headingId}
          busy={ctx.busy}
          onReplay={replayable ? () => { acted.current = true; ctx.replay(live!) } : undefined}
        />
        {/* A failed Show again is said where it was pressed; nothing else is on screen. */}
        {/* No hand-off: the result line sits in the chat beside the composer, whose unsent draft the hand-off navigation would discard. */}
        {replayable && <ErrorNotice variant="inline" testId="guide-line-error" message={acted.current ? ctx.error : ''} />}
      </div>
    )
  }

  const action = resolved && resolved.ok ? resolved.actions[offered!.action_index] ?? resolved.actions[0] : null
  return (
    <div ref={cardRef} className="flex flex-col gap-2" data-testid="guide-offer-cards">
      <motion.section
        ref={rootRef}
        // Size only: a scroll moves the card inside the virtual list, and
        // animating that position change would make it jitter.
        layout={reduce ? false : 'size'}
        transition={reduce ? { duration: 0 } : { duration: 0.2 }}
        aria-labelledby={headingId}
        data-testid="guide-offer-card"
        data-guide-status="offered"
        className={`@container ${corners} border border-border bg-card text-card-fg`}
      >
        <div className="px-4 py-3">
          {/* One row: the offer on the left, its two buttons on the right;
              a narrow card wraps the buttons under the text. */}
          <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
            <div className="min-w-[12rem] flex-1">
              <div className="flex items-center gap-2 text-[12px] leading-4 font-medium text-muted">
                <Compass size={16} className="shrink-0" aria-hidden="true" />
                <span className="min-w-0 break-words">{i18nT('components.guideLayer.region_label')}</span>
              </div>
              {action && <h3 id={headingId} className="m-0 mt-1 text-[14px] font-semibold leading-5 text-text break-words">{i18nT(action.titleKey, action.titleVars)}</h3>}
              <GuideAgentNote className="mt-1.5" text={offered!.intro} testId="guide-offer-intro" slotKey={slotKey} />
            </div>
            <div className="ml-auto flex shrink-0 items-center gap-2">
              <Btn
                className="min-h-11 border-transparent hover:border-transparent text-muted hover:text-text"
                onClick={() => { acted.current = true; ctx.cancel(offered!) }}
                disabled={ctx.busy}
                data-testid="guide-dismiss"
              >
                {i18nT('components.guideLayer.dismiss')}
              </Btn>
              <Btn primary className="min-h-11 px-4" onClick={() => { acted.current = true; ctx.start(offered!) }} disabled={ctx.busy} data-testid="guide-start">
                {i18nT('components.guideLayer.start')}
              </Btn>
            </div>
          </div>
          {/* No hand-off: the card sits beside a chat draft the hand-off navigation would discard. */}
          <ErrorNotice className="mt-2" variant="inline" testId="guide-offer-error" message={acted.current ? ctx.error : ''} />
        </div>
      </motion.section>
    </div>
  )
}
