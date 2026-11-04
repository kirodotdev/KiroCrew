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
import { ArrowUp, Check, FileText, MessageCircleQuestionMark, Reply, ShieldCheck, X } from 'lucide-react'
import { useAppDispatch, useAppSelector } from '../../store'
import { resolveByApprovalId } from '../../store/chatSlice'
import { api, ApiError } from '../../api/client'
import { OverlayPortal } from '../../components/overlayRoot'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn } from '../../components/ui'
import { sendTurn } from '../../chat-core/transport/sendTurn'
import { alreadyResolvedDecision, decisionVerdict, mintResponseId } from '../../components/chat-input/sharedDecision'
import { sessionDisplayTitle } from '../../utils/sessionDisplayTitle'
import type { ChatSlot } from '../../types'
import { approvalKey, collectAsks, pickRecap, slotNeedsMe, type CardQuestion, type NeedsMeAsk } from './needsMeCard'

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

type Outcome = 'pending' | 'approved' | 'denied' | 'expired' | 'elsewhere' | 'error'

export default function NeedsMeHoverLayer({ rootRef }: { rootRef: RefObject<HTMLElement | null> }) {
  const [target, setTarget] = useState<{ key: string; row: HTMLElement; focusReply: boolean } | null>(null)
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
      const row = (el as HTMLElement | null)?.closest?.('[data-session-row]') as HTMLElement | null
      const key = row?.closest('[data-slot-key]')?.getAttribute('data-slot-key')
      return row && key ? { key, row } : null
    }
    const onOver = (e: PointerEvent) => {
      if (e.pointerType === 'touch' || !finePointer()) return
      const hit = rowAt(e.target)
      if (!hit) { if (targetRef.current) scheduleClose(); return }
      if (targetRef.current?.key === hit.key) { if (closeTimer.current) { clearTimeout(closeTimer.current); closeTimer.current = null } return }
      if (closeTimer.current) { clearTimeout(closeTimer.current); closeTimer.current = null }
      if (openTimer.current) clearTimeout(openTimer.current)
      openTimer.current = setTimeout(() => setTarget({ ...hit, focusReply: false }), targetRef.current ? SWAP_DELAY_MS : OPEN_DELAY_MS)
    }
    const onLeave = (e: PointerEvent) => {
      if (e.pointerType === 'touch') return
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

  useEffect(() => {
    if (!target) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape' && !cardRef.current?.contains(document.activeElement)) close() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [target, close])

  return (
    <OverlayPortal>
      <AnimatePresence>
        {target && (
          <NeedsMeCard
            key={target.key}
            slotKey={target.key}
            row={target.row}
            focusReply={target.focusReply}
            cardRef={cardRef}
            draftRef={draftRef}
            onPointerEnter={() => { if (closeTimer.current) { clearTimeout(closeTimer.current); closeTimer.current = null } }}
            onPointerLeave={scheduleClose}
            onClose={close}
          />
        )}
      </AnimatePresence>
    </OverlayPortal>
  )
}

function NeedsMeCard({ slotKey, row, focusReply, cardRef, draftRef, onPointerEnter, onPointerLeave, onClose }: {
  slotKey: string
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
  const slot = useAppSelector(s => s.dashboard.slots.find(x => x.key === slotKey)) as ChatSlot | undefined
  const questions = useAppSelector(s => s.chat.pendingQuestions?.[slotKey]?.questions) ?? NO_QUESTIONS
  const needsMe = !!slot && slotNeedsMe(slot, questions.length > 0)

  const summary = useQuery({
    queryKey: ['session-summary', slotKey],
    queryFn: () => api.sessionSummary(slotKey),
    enabled: needsMe, staleTime: 30_000, retry: false,
  })
  const detail = useQuery({
    queryKey: ['needs-me-asks', slotKey, slot?.messages ?? 0, !!slot?.pending_approval],
    queryFn: () => api.chatSlotDetail(slotKey, 80),
    enabled: needsMe && !!slot?.pending_approval, staleTime: 0, retry: false,
  })

  const [outcomes, setOutcomes] = useState<Record<string, Outcome>>({})
  const settled = useMemo(() => new Set(Object.keys(outcomes)), [outcomes])
  const lastReply = slot && !slot.running && slot.has_options ? (slot.last_message ?? '') : ''
  const asks = useMemo(
    () => collectAsks(detail.data?.messages ?? [], questions, { settled, lastReply }),
    [detail.data, questions, settled, lastReply],
  )
  const recap = pickRecap(summary.data, lastReply ? undefined : slot?.last_message)
  const openApproval = asks.some(a => a.kind === 'approval' && !outcomes[approvalKey(a.requestId, a.requestMid)] && !a.resolved)

  const decide = useCallback((a: Extract<NeedsMeAsk, { kind: 'approval' }>, approve: boolean) => {
    if (!a.requestMid) return
    const k = approvalKey(a.requestId, a.requestMid)
    setOutcomes(o => ({ ...o, [k]: 'pending' }))
    const set = (v: Outcome) => setOutcomes(o => ({ ...o, [k]: v }))
    const decision = approve ? 'approved' : 'rejected'
    api.approveChatSlot(slotKey, decision, {
      request_id: a.requestId, request_mid: a.requestMid, origin: 'native', response_id: mintResponseId(),
    }).then(() => {
      set(approve ? 'approved' : 'denied')
      dispatch(resolveByApprovalId({ id: a.requestId, slot: slotKey, decision, mid: a.requestMid }))
    }).catch((err: unknown) => {
      const elsewhere = alreadyResolvedDecision(err)
      if (elsewhere) {
        set('elsewhere')
        dispatch(resolveByApprovalId({ id: a.requestId, slot: slotKey, decision: decisionVerdict(elsewhere), mid: a.requestMid }))
      } else if (err instanceof ApiError && err.status === 404) {
        set('expired')
        dispatch(resolveByApprovalId({ id: a.requestId, slot: slotKey, decision: 'stale', mid: a.requestMid }))
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
    return () => window.removeEventListener('resize', place)
  }, [row, cardRef, asks.length, recap?.text, outcomes])

  const replyRef = useRef<HTMLTextAreaElement | null>(null)
  const hasAsks = asks.length > 0
  useEffect(() => {
    if (focusReply && hasAsks) replyRef.current?.focus()
  }, [focusReply, hasAsks])

  if (!slot || !needsMe || asks.length === 0) return null
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
      onBlur={e => { if (!e.currentTarget.contains(e.relatedTarget as Node | null) && e.relatedTarget !== row) onClose() }}
    >
      <span aria-hidden className="absolute -left-[6px] w-[10px] h-[10px] rotate-45 bg-card border-l border-b border-border" style={{ top: pos?.nub ?? 22 }} />
      <div className="px-3.5 pt-3 pb-2.5 border-b border-border shrink-0">
        <div className="text-[13px] leading-[18px] font-semibold text-text-strong truncate">{title}</div>
      </div>

      <section className="px-3.5 py-2.5 border-b border-border shrink-0" aria-labelledby="needs-me-recap">
        <div id="needs-me-recap" className="text-[10px] leading-[12px] font-semibold uppercase tracking-[.06em] text-muted mb-1.5">{t('pages.chatSidebar.needsMeCard.recap')}</div>
        {recap
          ? <p className={`m-0 text-[12px] leading-[18px] line-clamp-3 ${recap.fromSummary ? 'text-text' : 'text-muted'}`} data-testid="needs-me-recap">{recap.text}</p>
          : <p className="m-0 text-[12px] leading-[18px] text-muted" data-testid="needs-me-recap">{t('pages.chatSidebar.needsMeCard.recap_none')}</p>}
      </section>

      <section className="px-3.5 py-2.5 border-b border-border min-h-0 flex flex-col" aria-labelledby="needs-me-asks">
        <div id="needs-me-asks" className="text-[10px] leading-[12px] font-semibold uppercase tracking-[.06em] text-muted mb-1.5 shrink-0">{t('pages.chatSidebar.needsMeCard.needs_you')}</div>
        <ul className="list-none m-0 p-0 flex flex-col gap-1.5 overflow-y-auto min-h-0" data-testid="needs-me-asks">
          {asks.map((a, i) => (
            <AskRow key={a.kind === 'approval' ? approvalKey(a.requestId, a.requestMid) : `${a.kind}-${i}`} ask={a}
              outcome={a.kind === 'approval' ? outcomes[approvalKey(a.requestId, a.requestMid)] : undefined}
              onDecide={decide} />
          ))}
        </ul>
      </section>

      <QuickReply slotKey={slotKey} title={title} replyRef={replyRef} draftRef={draftRef} openApproval={openApproval} />
    </motion.div>
  )
}

const PILL_CLS = 'inline-flex items-center gap-1 shrink-0 rounded-full px-1.5 text-[10px] leading-[14px] font-semibold'

function KindPill({ kind }: { kind: NeedsMeAsk['kind'] }) {
  const { t } = useTranslation()
  if (kind === 'approval') return <span className={`${PILL_CLS} text-warn bg-warn-subtle`}><ShieldCheck size={10} aria-hidden />{t('pages.chatSidebar.needsMeCard.kind_approval')}</span>
  if (kind === 'question') return <span className={`${PILL_CLS} text-info bg-info-subtle`}><MessageCircleQuestionMark size={10} aria-hidden />{t('pages.chatSidebar.needsMeCard.kind_question')}</span>
  if (kind === 'form') return <span className={`${PILL_CLS} text-info bg-info-subtle`}><FileText size={10} aria-hidden />{t('pages.chatSidebar.needsMeCard.kind_form')}</span>
  return <span className={`${PILL_CLS} text-accent bg-accent-subtle`}><Reply size={10} aria-hidden />{t('pages.chatSidebar.needsMeCard.kind_reply')}</span>
}

function AskRow({ ask, outcome, onDecide }: {
  ask: NeedsMeAsk
  outcome: Outcome | undefined
  onDecide: (a: Extract<NeedsMeAsk, { kind: 'approval' }>, approve: boolean) => void
}) {
  const { t } = useTranslation()
  const box = 'rounded-lg bg-bg-hover px-2 py-[7px] text-[12px] leading-[16px]'
  if (ask.kind === 'approval') {
    const done = outcome && outcome !== 'pending' && outcome !== 'error' ? outcome : ask.resolved ? 'elsewhere' : undefined
    return (
      <li className={box} data-testid="needs-me-ask" data-kind="approval">
        <div className="flex items-center gap-2 min-w-0"><KindPill kind="approval" /><span className="font-medium text-text-strong truncate">{ask.tool}</span></div>
        <pre className="mt-1.5 mb-0 px-2 py-1.5 rounded-md border border-border bg-bg font-mono text-[11px] leading-[16px] text-text-strong whitespace-pre-wrap break-all" data-testid="needs-me-command">{ask.command || t('pages.chatSidebar.needsMeCard.no_arguments')}</pre>
        <div className="mt-1.5 flex items-center justify-end gap-1.5 min-h-6" aria-live="polite">
          {done === 'approved' && <span className="inline-flex items-center gap-1 text-[11px] font-semibold text-ok"><Check size={12} aria-hidden />{t('pages.chatSidebar.needsMeCard.approved')}</span>}
          {done === 'denied' && <span className="inline-flex items-center gap-1 text-[11px] font-semibold text-danger"><X size={12} aria-hidden />{t('pages.chatSidebar.needsMeCard.denied')}</span>}
          {done === 'expired' && <span className="text-[11px] font-semibold text-muted">{t('pages.chatSidebar.needsMeCard.expired')}</span>}
          {done === 'elsewhere' && <span className="text-[11px] font-semibold text-muted">{t('pages.chatSidebar.needsMeCard.answered_elsewhere')}</span>}
          {!done && ask.requestMid && (
            <>
              {/* No hand-off: the card's quick-reply draft sits below this notice. */}
              {outcome === 'error' && <ErrorNotice variant="inline" message={t('pages.chatSidebar.needsMeCard.decision_failed')} className="mr-auto min-w-0" />}
              <Btn danger type="button" disabled={outcome === 'pending'} className="px-2.5 py-0.5 text-[11px] font-semibold"
                aria-label={t('pages.chatSidebar.needsMeCard.deny_aria', { tool: ask.tool })}
                onClick={() => onDecide(ask, false)}>{t('pages.chatSidebar.needsMeCard.deny')}</Btn>
              <Btn type="button" disabled={outcome === 'pending'} className="px-2.5 py-0.5 text-[11px] font-semibold bg-ok text-ok-fg border-ok hover:bg-ok hover:brightness-110"
                aria-label={t('pages.chatSidebar.needsMeCard.approve_aria', { tool: ask.tool })}
                onClick={() => onDecide(ask, true)}><Check size={12} aria-hidden />{t('pages.chatSidebar.needsMeCard.approve')}</Btn>
            </>
          )}
          {!done && !ask.requestMid && <span className="text-[11px] text-muted">{t('pages.chatSidebar.needsMeCard.answer_in_session')}</span>}
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
            <div className="text-text-strong whitespace-pre-wrap break-words">{q.question}</div>
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
        {ask.message && <div className="mt-1.5 text-text-strong whitespace-pre-wrap break-words">{ask.message}</div>}
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
      <div className="mt-1.5 text-text-strong whitespace-pre-wrap break-words">{ask.text}</div>
    </li>
  )
}

function QuickReply({ slotKey, title, replyRef, draftRef, openApproval }: {
  slotKey: string
  title: string
  replyRef: MutableRefObject<HTMLTextAreaElement | null>
  draftRef: { current: string }
  openApproval: boolean
}) {
  const { t } = useTranslation()
  const [text, setText] = useState('')
  const [state, setState] = useState<{ kind: 'idle' | 'sending' | 'sent' | 'queued' } | { kind: 'error'; reason?: string }>({ kind: 'idle' })
  draftRef.current = text
  const send = async () => {
    const message = text.trim()
    if (!message || state.kind === 'sending') return
    setState({ kind: 'sending' })
    const receipt = await sendTurn({ message, slot: slotKey })
    if (receipt.status === 'refused' || receipt.status === 'transport-error') {
      setState({ kind: 'error', reason: receipt.status === 'refused' ? receipt.reason : undefined })
      return
    }
    setText('')
    setState({ kind: receipt.status === 'queued' ? 'queued' : 'sent' })
  }
  return (
    <div className="px-3.5 py-2.5 shrink-0">
      <div className="flex items-end gap-1.5 rounded-lg border border-border bg-bg pl-2.5 pr-1.5 py-1.5 focus-within:border-ring">
        <textarea
          ref={replyRef}
          rows={1}
          value={text}
          onChange={e => { setText(e.target.value); if (state.kind !== 'sending') setState({ kind: 'idle' }) }}
          onKeyDown={e => {
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void send() }
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
        {state.kind === 'sent' && <span className="text-ok">{t('pages.chatSidebar.needsMeCard.sent')}</span>}
        {state.kind === 'queued' && <span className="text-ok">{t('pages.chatSidebar.needsMeCard.queued')}</span>}
        {(state.kind === 'idle' || state.kind === 'sending') && (openApproval ? t('pages.chatSidebar.needsMeCard.reply_not_approval') : t('pages.chatSidebar.needsMeCard.reply_keys'))}
      </div>
      {state.kind === 'error' && (
        /* No hand-off: the unsent reply is still in the box above. */
        <ErrorNotice variant="inline" message={state.reason || t('pages.chatSidebar.needsMeCard.send_failed')} className="mt-1" />
      )}
    </div>
  )
}
