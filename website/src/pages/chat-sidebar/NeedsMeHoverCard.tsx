/** The sidebar's needs-me hover card: hovering a session row that owes the
 *  person something opens a card beside it with a recap, every ask in full, and
 *  a quick reply. Approval asks carry Approve and Deny.
 *
 *  One layer for the whole list, mounted once by `ChatSidebar`: it listens on the
 *  sidebar root and finds the row under the pointer by its `data-slot-key`, so
 *  `SessionRow` carries nothing for it. Desktop only: the card opens for a mouse
 *  on a device that matches `(hover: hover) and (pointer: fine)`, never for touch.
 *
 *  Nothing here is a new decision path. Approve and Deny post the STRICT form of
 *  the session's own approve route (`POST /api/chat/slots/{slot}/approve` with
 *  `origin: 'native'`, `request_id` and `request_mid`), which settles exactly the
 *  row shown or answers 404/409, and the reply goes through `sendTurn`, the
 *  composer's own send. There is no approve-all and no standing grant. */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type MutableRefObject, type RefObject } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { useImeGuard } from '../../hooks/useImeGuard'
import { ArrowUp, Check, FileText, MessageCircleQuestionMark, Reply, ShieldCheck, X } from 'lucide-react'
import { store, useAppDispatch, useAppSelector } from '../../store'
import { capturePendingAskId, resolveApprovalRow } from '../../store/chatSlice'
import { resolveAskAfterSend } from '../../lib/resolveAskAfterSend'
import { api, ApiError } from '../../api/client'
import { OverlayPortal, useCloseWhenCrewHidden } from '../../components/overlayRoot'
import ErrorNotice from '../../components/ErrorNotice'
import StatusNotice from '../../components/StatusNotice'
import { Btn } from '../../components/ui'
import MarkdownRenderer from '../../components/MarkdownRenderer'
import { mintSendId, sendTurn } from '../../chat-core/transport/sendTurn'
import { adoptSendRaceRevision, baseRevisionFor, isSendRaceConflict, sendIdempotencyKey } from '../../api/sendRace'
import { baseRevisionForPress } from '../../hooks/sharedCrew/sendRevisionSeed'
import { ROW_META_CLS, ROW_STATUS_CLS } from '../../components/listShell'
import { alreadyResolvedDecision, decisionVerdict, mintResponseId } from '../../components/chat-input/sharedDecision'
import { toApiDecision } from '../../utils/approvalDecision'
import { sessionDisplayTitle } from '../../utils/sessionDisplayTitle'
import { isPeerRow, sessionRowIdentity } from './rowIdentity'
import type { ChatSlot } from '../../types'
import { crewApprovalDecidableHere } from '../../lib/crewMode'
import { replyOptions } from '../chat/command-center/model'
import { parseOptions } from '../../app-sdk/protocol'
import { approvalKey, collectAsks, lastAssistantText, pickRecap, slotNeedsMe, type CardQuestion, type NeedsMeAsk } from './needsMeCard'

/** The pointer must rest this long on a row before the card opens. */
export const OPEN_DELAY_MS = 350
/** Moving from one needs-me row to the next swaps the open card this fast. */
const SWAP_DELAY_MS = 60
/** Leaving the row (or the card) closes it after this grace, so the pointer can cross the gap. */
export const CLOSE_GRACE_MS = 180
const CARD_WIDTH = 360
const GAP = 8
export const FINE_POINTER_QUERY = '(hover: hover) and (pointer: fine)'

function finePointer(): boolean {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function' && window.matchMedia(FINE_POINTER_QUERY).matches
}

const NO_QUESTIONS: readonly CardQuestion[] = []

/** What the quick reply shows. A send in flight resolves to the one it ends in. */
type ReplyState = { kind: 'idle' | 'sending' | 'sent' | 'queued' | 'unconfirmed' } | { kind: 'error'; reason?: string }

// `refused`: a press the server refused as no longer pending (404). It is
// status with the refusal sentence, unlike `expired`, which is an ask that
// ended with no press.
type Outcome = 'pending' | 'approved' | 'denied' | 'expired' | 'refused' | 'elsewhere_approved' | 'elsewhere_denied' | 'error'

export default function NeedsMeHoverLayer({ rootRef }: { rootRef: RefObject<HTMLElement | null> }) {
  const [target, setTarget] = useState<{ key: string; row: HTMLElement; focusReply: boolean } | null>(null)
  // Unsent replies, per row: the card unmounts when its last ask settles or the
  // pointer leaves, and a reply the person was typing must come back with it.
  const drafts = useRef(new Map<string, string>())
  // A send in flight per row; a reply mounted mid-send waits on it to settle.
  const inFlight = useRef(new Map<string, Promise<ReplyState>>())
  const openTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const closeTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const cardRef = useRef<HTMLDivElement | null>(null)
  const targetRef = useRef(target)
  targetRef.current = target
  const draftRef = useRef('')

  const clearTimers = () => {
    if (openTimer.current) clearTimeout(openTimer.current)
    if (closeTimer.current) clearTimeout(closeTimer.current)
    openTimer.current = closeTimer.current = null
  }
  const close = useCallback((refocus = false) => {
    clearTimers()
    const was = targetRef.current
    setTarget(null)
    if (refocus && was) was.row.focus()
  }, [])
  const scheduleClose = useCallback(() => {
    if (openTimer.current) { clearTimeout(openTimer.current); openTimer.current = null }
    if (closeTimer.current) clearTimeout(closeTimer.current)
    closeTimer.current = setTimeout(() => {
      // A half-typed reply keeps the card while focus is inside it.
      if (draftRef.current.trim() && cardRef.current?.contains(document.activeElement)) return
      close()
    }, CLOSE_GRACE_MS)
  }, [close])

  useEffect(() => {
    const root = rootRef.current
    if (!root) return
    const rowAt = (el: EventTarget | null): { key: string; row: HTMLElement } | null => {
      // The ORIGIN-QUALIFIED identity, never the raw slot key: a peer crew's row can
      // carry a key a local slot also has, and must not open the local card.
      const row = (el as HTMLElement | null)?.closest?.('[data-session-row]') as HTMLElement | null
      const key = row?.getAttribute('data-session-row')
      return row && key ? { key, row } : null
    }
    const onOver = (e: PointerEvent) => {
      if (e.pointerType === 'touch' || !finePointer()) return
      const hit = rowAt(e.target)
      if (!hit) {
        if (openTimer.current) { clearTimeout(openTimer.current); openTimer.current = null }
        if (targetRef.current) scheduleClose()
        return
      }
      if (targetRef.current?.key === hit.key) { if (closeTimer.current) { clearTimeout(closeTimer.current); closeTimer.current = null } return }
      if (closeTimer.current) { clearTimeout(closeTimer.current); closeTimer.current = null }
      if (openTimer.current) clearTimeout(openTimer.current)
      openTimer.current = setTimeout(() => setTarget({ ...hit, focusReply: false }), targetRef.current ? SWAP_DELAY_MS : OPEN_DELAY_MS)
    }
    const onLeave = (e: PointerEvent) => {
      if (e.pointerType === 'touch') return
      // Leaving the sidebar straight onto the card: React has already run the card's
      // own pointer-enter for this move, so a close scheduled here would never be cancelled.
      if (e.relatedTarget instanceof Node && cardRef.current?.contains(e.relatedTarget)) return
      scheduleClose()
    }
    // A click opens the session: the card has done its job.
    const onDown = () => { if (targetRef.current || openTimer.current) close() }
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'ArrowRight' || e.altKey || e.metaKey || e.ctrlKey || e.shiftKey || !finePointer()) return
      const hit = rowAt(e.target)
      if (!hit || e.target !== hit.row) return
      e.preventDefault()
      clearTimers()
      setTarget({ ...hit, focusReply: true })
    }
    const onScroll = () => { if (targetRef.current && !cardRef.current?.contains(document.activeElement)) close() }
    root.addEventListener('pointerover', onOver)
    root.addEventListener('pointerleave', onLeave)
    root.addEventListener('pointerdown', onDown)
    root.addEventListener('keydown', onKey)
    root.addEventListener('scroll', onScroll, true)
    return () => {
      root.removeEventListener('pointerover', onOver)
      root.removeEventListener('pointerleave', onLeave)
      root.removeEventListener('pointerdown', onDown)
      root.removeEventListener('keydown', onKey)
      root.removeEventListener('scroll', onScroll, true)
      clearTimers()
    }
  }, [rootRef, close, scheduleClose])

  // Stand down when the local Crew is hidden, as every overlay layer does.
  useCloseWhenCrewHidden(!!target, close)
  useEffect(() => {
    if (!target) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape' && !cardRef.current?.contains(document.activeElement)) close() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [target, close])

  return (
    <OverlayPortal>
      {/* `wait`: the exiting card unmounts before the next mounts, so only one holds `cardRef`. */}
      <AnimatePresence mode="wait">
        {target && (
          <NeedsMeCard
            key={target.key}
            identity={target.key}
            drafts={drafts.current}
            inFlight={inFlight.current}
            row={target.row}
            focusReply={target.focusReply}
            cardRef={cardRef}
            draftRef={draftRef}
            // Resting on the card also cancels a swap armed by a row the pointer crossed on the way.
            onPointerEnter={clearTimers}
            onPointerLeave={scheduleClose}
            onClose={close}
          />
        )}
      </AnimatePresence>
    </OverlayPortal>
  )
}

function NeedsMeCard({ identity, drafts, inFlight, row, focusReply, cardRef, draftRef, onPointerEnter, onPointerLeave, onClose }: {
  identity: string
  drafts: Map<string, string>
  inFlight: Map<string, Promise<ReplyState>>
  row: HTMLElement
  focusReply: boolean
  cardRef: MutableRefObject<HTMLDivElement | null>
  draftRef: { current: string }
  onPointerEnter: () => void
  onPointerLeave: () => void
  onClose: (refocus?: boolean) => void
}) {
  const { t } = useTranslation()
  const dispatch = useAppDispatch()
  const queryClient = useQueryClient()
  // Local slots only: a peer row's identity names no local slot, so it gets no card.
  const slot = useAppSelector(s => s.dashboard.slots.find(x => !isPeerRow(x as { peer_id?: string }) && sessionRowIdentity(x) === identity)) as ChatSlot | undefined
  const slotKey = slot?.key ?? ''
  const questions = useAppSelector(s => (slotKey ? s.chat.pendingQuestions?.[slotKey]?.questions : undefined)) ?? NO_QUESTIONS
  const needsMe = !!slot && slotNeedsMe(slot, questions.length > 0)
  const options = useMemo(() => (slot ? replyOptions(slot) : []), [slot])

  const summary = useQuery({
    queryKey: ['session-summary', slotKey],
    queryFn: () => api.sessionSummary(slotKey),
    enabled: needsMe, staleTime: 30_000, retry: false,
  })
  const detail = useQuery({
    queryKey: ['needs-me-asks', slotKey, slot?.messages ?? 0, !!slot?.pending_approval],
    queryFn: () => api.chatSlotDetail(slotKey, 80),
    // Also for an options reply: the row only carries a short preview of it.
    enabled: needsMe && (!!slot?.pending_approval || options.length > 0), staleTime: 0, retry: false,
  })

  const [outcomes, setOutcomes] = useState<Record<string, Outcome>>({})
  const settled = useMemo(() => new Set(Object.keys(outcomes)), [outcomes])
  // The options list is drawn under the text, so the marker line is stripped as the chat bubble strips it.
  const reply = useMemo(() => options.length ? { text: parseOptions(lastAssistantText(detail.data?.messages ?? [])).text, options } : undefined, [options, detail.data])
  // The one crew rule: a Crew Mode worker's approval is decided on its conductor's card only.
  const decidable = crewApprovalDecidableHere(slot, 'sidebar_hover_card')
  const asks = useMemo(
    () => collectAsks(detail.data?.messages ?? [], questions, { settled, reply }),
    [detail.data, questions, settled, reply],
  )
  const recap = pickRecap(summary.data, reply ? undefined : slot?.last_message)
  const openApproval = decidable && asks.some(a => a.kind === 'approval' && !outcomes[approvalKey(a.requestId, a.requestMid)] && !a.resolved)

  const decide = useCallback((a: Extract<NeedsMeAsk, { kind: 'approval' }>, approve: boolean) => {
    if (!a.requestMid) return
    const rowTarget = { origin: 'native' as const, id: a.requestId, slot: slotKey, mid: a.requestMid }
    const k = approvalKey(a.requestId, a.requestMid)
    setOutcomes(o => ({ ...o, [k]: 'pending' }))
    const set = (v: Outcome) => setOutcomes(o => ({ ...o, [k]: v }))
    const decision = approve ? 'approved' : 'rejected_once'
    api.decideApproval(rowTarget, toApiDecision(decision), mintResponseId()).then(() => {
      set(approve ? 'approved' : 'denied')
      dispatch(resolveApprovalRow({ target: rowTarget, decision }))
    }).catch((err: unknown) => {
      const elsewhere = alreadyResolvedDecision(err)
      if (elsewhere) {
        set(decisionVerdict(elsewhere) === 'approved' ? 'elsewhere_approved' : 'elsewhere_denied')
        dispatch(resolveApprovalRow({ target: rowTarget, decision: decisionVerdict(elsewhere) }))
      } else if (err instanceof ApiError && err.status === 404) {
        set('refused')
        dispatch(resolveApprovalRow({ target: rowTarget, decision: 'stale' }))
      } else {
        set('error')
      }
    }).finally(() => { void queryClient.invalidateQueries({ queryKey: ['needs-me-asks', slotKey] }) })
  }, [slotKey, dispatch, queryClient])

  // Placement: beside the row, clamped to the viewport.
  const [pos, setPos] = useState<{ left: number; top: number; nub: number } | null>(null)
  useLayoutEffect(() => {
    const place = () => {
      const r = row.getBoundingClientRect()
      const h = cardRef.current?.offsetHeight ?? 0
      const left = Math.min(r.right + GAP, window.innerWidth - CARD_WIDTH - GAP)
      const top = Math.max(GAP, Math.min(r.top - GAP, window.innerHeight - h - GAP))
      setPos({ left, top, nub: Math.max(12, Math.min(h - 20, r.top + r.height / 2 - top - 5)) })
    }
    place()
    window.addEventListener('resize', place)
    // The card grows as its reads land (a long reply, a notice): keep it on screen.
    const ro = typeof ResizeObserver === 'function' && cardRef.current ? new ResizeObserver(place) : null
    if (ro && cardRef.current) ro.observe(cardRef.current)
    return () => { window.removeEventListener('resize', place); ro?.disconnect() }
  }, [row, cardRef, asks.length, recap?.text, outcomes, detail.isError, summary.isError])

  const replyRef = useRef<HTMLTextAreaElement | null>(null)
  const hasAsks = asks.length > 0
  useEffect(() => {
    if (focusReply && hasAsks) replyRef.current?.focus()
  }, [focusReply, hasAsks])

  // A draft keeps the card up after its last ask settles, so the text is not lost.
  const holdsDraft = !!drafts.get(identity)?.trim()
  if (!slot || (!needsMe && !holdsDraft) || (asks.length === 0 && !detail.isError && !holdsDraft)) return null
  const title = sessionDisplayTitle(slot)

  return (
    <motion.div
      ref={cardRef}
      role="dialog"
      aria-modal="false"
      aria-label={t('pages.chatSidebar.needsMeCard.card_label', { title })}
      data-testid="needs-me-card"
      className="fixed z-50 flex flex-col rounded-[10px] border border-border bg-card text-card-fg shadow-[var(--shadow-float,var(--shadow-lg))] max-h-[min(560px,calc(100vh-16px))]"
      style={{ width: CARD_WIDTH, left: pos?.left ?? -9999, top: pos?.top ?? 0 }}
      initial={{ opacity: 0, x: -4 }}
      animate={{ opacity: 1, x: 0 }}
      exit={{ opacity: 0, x: -4 }}
      transition={{ duration: 0.12 }}
      onPointerEnter={onPointerEnter}
      onPointerLeave={onPointerLeave}
      onKeyDown={e => { if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); onClose(true) } }}
      // Close only when focus lands on a real element elsewhere. A click on the card's
      // own text (no tabIndex) blurs to nothing, and must not throw away a half-typed reply.
      onBlur={e => { const to = e.relatedTarget as Node | null; if (to && !e.currentTarget.contains(to) && to !== row) onClose() }}
    >
      <span aria-hidden className="absolute -left-[6px] w-[10px] h-[10px] rotate-45 bg-card border-l border-b border-border" style={{ top: pos?.nub ?? 22 }} />
      <div className="px-3.5 pt-3 pb-2.5 border-b border-border shrink-0">
        <div className="text-[13px] leading-[18px] font-semibold text-text-strong truncate">{title}</div>
      </div>

      <section className="px-3.5 py-2.5 border-b border-border shrink-0" aria-labelledby="needs-me-recap">
        <div id="needs-me-recap" className={`${ROW_META_CLS} font-semibold uppercase tracking-[.06em] text-muted mb-1.5`}>{t('pages.chatSidebar.needsMeCard.recap')}</div>
        {/* No hand-off: the card's quick-reply draft sits below this notice. */}
        {summary.isError && <ErrorNotice variant="inline" message={t('pages.chatSidebar.needsMeCard.recap_failed')} className="mb-1" />}
        {recap
          ? <p className={`m-0 text-[12px] leading-[18px] line-clamp-3 ${recap.fromSummary ? 'text-text' : 'text-muted'}`} data-testid="needs-me-recap">{recap.text}</p>
          : <p className="m-0 text-[12px] leading-[18px] text-muted" data-testid="needs-me-recap">{t('pages.chatSidebar.needsMeCard.recap_none')}</p>}
      </section>

      <section className="px-3.5 py-2.5 border-b border-border min-h-0 flex flex-col" aria-labelledby="needs-me-asks">
        <div id="needs-me-asks" className={`${ROW_META_CLS} font-semibold uppercase tracking-[.06em] text-muted mb-1.5 shrink-0`}>{t('pages.chatSidebar.needsMeCard.needs_you')}</div>
        {openApproval && <p className={`m-0 mb-1.5 ${ROW_STATUS_CLS} text-muted shrink-0`} data-testid="needs-me-decision-hint">{t('pages.chatSidebar.needsMeCard.decision_hint')}</p>}
        {/* No hand-off: the card's quick-reply draft sits below this notice. */}
        {detail.isError && <ErrorNotice variant="inline" message={t('pages.chatSidebar.needsMeCard.asks_failed')} className="mb-1.5" />}
        <ul className="list-none m-0 p-0 flex flex-col gap-1.5 overflow-y-auto min-h-0" data-testid="needs-me-asks">
          {asks.map((a, i) => (
            <AskRow key={a.kind === 'approval' ? approvalKey(a.requestId, a.requestMid) : `${a.kind}-${i}`} ask={a}
              outcome={a.kind === 'approval' ? outcomes[approvalKey(a.requestId, a.requestMid)] : undefined}
              decidable={decidable} onDecide={decide} />
          ))}
        </ul>
      </section>

      <QuickReply slotKey={slotKey} title={title} replyRef={replyRef} draftRef={draftRef} drafts={drafts} inFlight={inFlight} identity={identity} openApproval={openApproval} />
    </motion.div>
  )
}

/** An agent-written ask body, drawn by the chat's own markdown renderer so a
 *  table, bold text or a link reads as it does in the session. `softBreaks`
 *  keeps a single line break, as the plain text this replaced did. */
function AskMarkdown({ text, className = '' }: { text: string; className?: string }) {
  return <div className={`text-text-strong break-words min-w-0 ${className}`} data-testid="needs-me-ask-body"><MarkdownRenderer content={text} softBreaks /></div>
}

const PILL_CLS = 'inline-flex items-center gap-1 shrink-0 rounded-full px-1.5 text-[10px] leading-[14px] font-semibold'

function KindPill({ kind, muted = false }: { kind: NeedsMeAsk['kind']; muted?: boolean }) {
  const { t } = useTranslation()
  if (kind === 'approval') return <span className={`${PILL_CLS} ${muted ? 'text-muted bg-bg' : 'text-warn-text bg-warn-subtle'}`}><ShieldCheck size={10} aria-hidden />{t('pages.chatSidebar.needsMeCard.kind_approval')}</span>
  if (kind === 'question') return <span className={`${PILL_CLS} text-info-text bg-info-subtle`}><MessageCircleQuestionMark size={10} aria-hidden />{t('pages.chatSidebar.needsMeCard.kind_question')}</span>
  if (kind === 'form') return <span className={`${PILL_CLS} text-info-text bg-info-subtle`}><FileText size={10} aria-hidden />{t('pages.chatSidebar.needsMeCard.kind_form')}</span>
  return <span className={`${PILL_CLS} text-accent bg-accent-subtle`}><Reply size={10} aria-hidden />{t('pages.chatSidebar.needsMeCard.kind_reply')}</span>
}

function AskRow({ ask, outcome, decidable, onDecide }: {
  ask: NeedsMeAsk
  decidable: boolean
  outcome: Outcome | undefined
  onDecide: (a: Extract<NeedsMeAsk, { kind: 'approval' }>, approve: boolean) => void
}) {
  const { t } = useTranslation()
  const box = 'rounded-lg bg-bg-hover px-2 py-[7px] text-[12px] leading-[16px]'
  if (ask.kind === 'approval') {
    const tool = ask.tool || t('pages.chatSidebar.needsMeCard.unknown_tool')
    const done = outcome && outcome !== 'pending' && outcome !== 'error' ? outcome
      : ask.resolved ? (ask.resolved === 'stale' ? 'expired' : ask.resolved.startsWith('approved') ? 'elsewhere_approved' : 'elsewhere_denied') : undefined
    return (
      <li className={box} data-testid="needs-me-ask" data-kind="approval">
        <div className="flex items-center gap-2 min-w-0"><KindPill kind="approval" muted={!!done} /><span className="font-medium text-text-strong truncate">{tool}</span></div>
        {ask.purpose && <div className="mt-1.5 text-text whitespace-pre-wrap break-words" data-testid="needs-me-purpose">{ask.purpose}</div>}
        <pre className={`mt-1.5 mb-0 px-2 py-1.5 rounded-md border border-border bg-bg font-mono ${ROW_STATUS_CLS} text-text-strong whitespace-pre-wrap break-all`} data-testid="needs-me-command">{ask.command || t('pages.chatSidebar.needsMeCard.no_arguments')}</pre>
        <div className="mt-1.5 flex flex-wrap items-center justify-end gap-1.5 min-h-6" aria-live="polite">
          {done === 'approved' && <span className="inline-flex items-center gap-1 text-[11px] font-semibold text-ok-text"><Check size={12} aria-hidden />{t('pages.chatSidebar.needsMeCard.approved')}</span>}
          {done === 'denied' && <span className="inline-flex items-center gap-1 text-[11px] font-semibold text-danger-text"><X size={12} aria-hidden />{t('pages.chatSidebar.needsMeCard.denied')}</span>}
          {done === 'expired' && <span className="text-[11px] font-semibold text-muted">{t('pages.chatSidebar.needsMeCard.expired')}</span>}
          {/* A gone approval is status: nothing the user did failed. */}
          {done === 'refused' && <StatusNotice tone="info" message={t('components.approvalCard.approval_no_longer_pending')} className="basis-full min-w-0" />}
          {done === 'elsewhere_approved' && <span className="text-[11px] font-semibold text-muted">{t('pages.chatSidebar.needsMeCard.approved_elsewhere')}</span>}
          {done === 'elsewhere_denied' && <span className="text-[11px] font-semibold text-muted">{t('pages.chatSidebar.needsMeCard.denied_elsewhere')}</span>}
          {!done && ask.requestMid && decidable && (
            <>
              {/* No hand-off: the card's quick-reply draft sits below this notice. */}
              {outcome === 'error' && <ErrorNotice variant="inline" message={t('pages.chatSidebar.needsMeCard.decision_failed')} className="basis-full min-w-0" />}
              <Btn danger type="button" disabled={outcome === 'pending'} className="px-2.5 py-0.5 text-[11px] font-semibold"
                aria-label={t('pages.chatSidebar.needsMeCard.deny_aria', { tool })}
                onClick={() => onDecide(ask, false)}>{t('pages.chatSidebar.needsMeCard.deny')}</Btn>
              <Btn type="button" disabled={outcome === 'pending'} className="px-2.5 py-0.5 text-[11px] font-semibold bg-ok text-ok-fg border-ok hover:bg-ok hover:brightness-110"
                aria-label={t('pages.chatSidebar.needsMeCard.approve_aria', { tool })}
                onClick={() => onDecide(ask, true)}><Check size={12} aria-hidden />{t('pages.chatSidebar.needsMeCard.approve')}</Btn>
            </>
          )}
          {!done && (!ask.requestMid || !decidable) && <span className="text-[11px] text-muted">{t(decidable ? 'pages.chatSidebar.needsMeCard.answer_in_session' : 'pages.chatSidebar.needsMeCard.decide_on_conductor')}</span>}
        </div>
      </li>
    )
  }
  if (ask.kind === 'question') {
    return (
      <li className={box} data-testid="needs-me-ask" data-kind="question">
        <KindPill kind="question" />
        {ask.questions.map((q, i) => (
          <div key={i} className="mt-1.5">
            <AskMarkdown text={q.question} />
            {q.options.length > 0 && (
              <ul className="mt-1 mb-0 pl-4 text-[11px] text-muted list-disc">
                {q.options.map((o, j) => <li key={j}>{o.label}{o.description ? ` · ${o.description}` : ''}</li>)}
              </ul>
            )}
          </div>
        ))}
        <div className="mt-1.5 text-[11px] text-muted">{t('pages.chatSidebar.needsMeCard.answer_in_session')}</div>
      </li>
    )
  }
  if (ask.kind === 'form') {
    return (
      <li className={box} data-testid="needs-me-ask" data-kind="form">
        <div className="flex items-center gap-2 min-w-0"><KindPill kind="form" />{ask.asker && <span className="font-medium text-text-strong truncate">{ask.asker}</span>}</div>
        {ask.message && <AskMarkdown text={ask.message} className="mt-1.5" />}
        {ask.fields.length > 0 && (
          <ul className="mt-1 mb-0 pl-4 text-[11px] text-muted list-disc">
            {ask.fields.map(f => (
              <li key={f.name}>
                <span className="text-text">{f.title || f.name}</span>
                {f.required ? ` (${t('pages.chatSidebar.needsMeCard.required')})` : ''}
                {f.description ? ` · ${f.description}` : ''}
                {f.options?.length ? ` · ${f.options.map(o => o.label).join(' / ')}` : ''}
              </li>
            ))}
          </ul>
        )}
        <div className="mt-1.5 text-[11px] text-muted">{t('pages.chatSidebar.needsMeCard.answer_in_session')}</div>
      </li>
    )
  }
  return (
    <li className={box} data-testid="needs-me-ask" data-kind="reply">
      <KindPill kind="reply" />
      {ask.text && <AskMarkdown text={ask.text} className="mt-1.5" />}
      {ask.options.length > 0 && (
        <ul className="mt-1 mb-0 pl-4 text-[11px] text-muted list-disc">
          {ask.options.map((o, j) => <li key={j}>{o}</li>)}
        </ul>
      )}
    </li>
  )
}

function QuickReply({ slotKey, title, replyRef, draftRef, drafts, inFlight, identity, openApproval }: {
  slotKey: string
  drafts: Map<string, string>
  inFlight: Map<string, Promise<ReplyState>>
  identity: string
  title: string
  replyRef: MutableRefObject<HTMLTextAreaElement | null>
  draftRef: { current: string }
  openApproval: boolean
}) {
  const { t } = useTranslation()
  const ime = useImeGuard()
  const dispatch = useAppDispatch()
  const [text, setTextState] = useState(() => drafts.get(identity) ?? '')
  const setText = (v: string) => { setTextState(v); if (v) drafts.set(identity, v); else drafts.delete(identity) }
  // A send outlives the card: reopening mid-send must not allow a second submit.
  const [state, setState] = useState<ReplyState>(() => (inFlight.has(identity) ? { kind: 'sending' } : { kind: 'idle' }))
  draftRef.current = text
  // Mounted mid-send (the card was closed and reopened): settle when that send does.
  useEffect(() => {
    const pending = inFlight.get(identity)
    if (!pending) return
    let live = true
    // The outcome travels with the promise, so a refusal or an unconfirmed
    // delivery shows here too, not only in the card that sent it.
    void pending.then(outcome => {
      if (!live) return
      setTextState(drafts.get(identity) ?? '')
      setState(outcome)
    })
    return () => { live = false }
  }, [inFlight, drafts, identity])
  const send = async () => {
    const message = text.trim()
    if (!message || state.kind === 'sending' || inFlight.has(identity)) return
    setState({ kind: 'sending' })
    let settle: (outcome: ReplyState) => void = () => {}
    inFlight.set(identity, new Promise<ReplyState>(r => { settle = r }))
    const outcome = await deliver(message)
    inFlight.delete(identity)
    settle(outcome)
    setState(outcome)
  }
  const deliver = async (message: string): Promise<ReplyState> => {
    // As the composer does: a reply answers the open question card, so capture it now.
    const askAtSend = capturePendingAskId(store.getState().chat.pendingQuestions, slotKey)
    // First submit wins, as in the composer: the revision is read at the press.
    const baseAtPress = baseRevisionFor(slotKey)
    const baseRevision = await baseRevisionForPress(slotKey, baseAtPress)
    const receipt = await sendTurn({ message, slot: slotKey, race: { baseRevision, idempotencyKey: sendIdempotencyKey(mintSendId()) } })
      .then(r => {
        // An accepted send clears its own text from the shared draft before a
        // remounted reply reads it.
        if ((r.status === 'dispatched' || r.status === 'queued' || r.status === 'unknown') && drafts.get(identity)?.trim() === message) drafts.delete(identity)
        return r
      })
    if (receipt.status === 'refused' && isSendRaceConflict(receipt.body)) {
      // Adopt the gateway's revision so the next send is current. No composer
      // notice: its retry would send the composer's draft, not this reply.
      adoptSendRaceRevision(slotKey, receipt.body)
      return { kind: 'error', reason: t('pages.chatSidebar.needsMeCard.send_race') }
    }
    if (receipt.status === 'refused' || receipt.status === 'transport-error') {
      return { kind: 'error', reason: receipt.status === 'refused' ? receipt.reason : undefined }
    }
    // No receipt in time: delivery is unknown, so the text stays for the person to check.
    if (receipt.status === 'response-late') return { kind: 'unconfirmed' }
    if (askAtSend) void resolveAskAfterSend(receipt.body, askAtSend, dispatch, slotKey)
    // Clear only what was sent: text typed while the send was in flight stays.
    // The shared draft was settled above, so it is cleared even when the card
    // closed before the receipt arrived.
    setTextState(prev => (prev.trim() === message ? '' : prev))
    return { kind: receipt.status === 'queued' ? 'queued' : 'sent' }
  }
  return (
    <div className="px-3.5 py-2.5 shrink-0">
      <div className="flex items-end gap-1.5 rounded-lg border border-border bg-bg pl-2.5 pr-1.5 py-1.5 focus-within:border-ring">
        <textarea
          ref={replyRef}
          rows={1}
          value={text}
          onChange={e => { setText(e.target.value); if (state.kind !== 'sending') setState({ kind: 'idle' }) }}
          {...ime.bindComposition()}
          onKeyDown={e => {
            if (e.key === 'Enter' && !e.shiftKey) { if (ime.claimEnter(e)) void send() }
          }}
          placeholder={t('pages.chatSidebar.needsMeCard.reply_placeholder')}
          aria-label={t('pages.chatSidebar.needsMeCard.reply_aria', { title })}
          aria-keyshortcuts="Enter Shift+Enter Escape"
          data-testid="needs-me-reply"
          className="flex-1 min-w-0 resize-none border-0 outline-none bg-transparent text-text-strong text-[12px] leading-[18px] py-0.5 max-h-[72px] placeholder:text-muted font-body"
        />
        <Btn primary type="button" onClick={() => void send()} disabled={!text.trim() || state.kind === 'sending'}
          aria-label={t('pages.chatSidebar.needsMeCard.send')} data-testid="needs-me-send"
          className="w-[26px] h-[26px] shrink-0 p-0 justify-center">
          <ArrowUp size={14} aria-hidden />
        </Btn>
      </div>
      <div className="mt-1.5 text-[10px] leading-[14px] text-muted" aria-live="polite">
        {state.kind === 'sent' && <span className="text-ok-text">{t('pages.chatSidebar.needsMeCard.sent')}</span>}
        {state.kind === 'queued' && <span className="text-ok-text">{t('pages.chatSidebar.needsMeCard.queued')}</span>}
        {state.kind === 'unconfirmed' && <span className="text-warn-text">{t('pages.chatSidebar.needsMeCard.unconfirmed')}</span>}
        {(state.kind === 'idle' || state.kind === 'sending') && (openApproval ? t('pages.chatSidebar.needsMeCard.reply_not_approval') : t('pages.chatSidebar.needsMeCard.reply_keys'))}
      </div>
      {state.kind === 'error' && (
        /* No hand-off: the unsent reply is still in the box above. */
        <ErrorNotice variant="inline" message={state.reason || t('pages.chatSidebar.needsMeCard.send_failed')} className="mt-1" />
      )}
    </div>
  )
}
