/**
 * The pane of a board session: a FigJam-style whiteboard in place of a chat (PoC).
 *
 * The board is a doc whose file holds the board's JSON (`boardModel`), saved a
 * moment after each change with the version it was read at, like a doc. The slot
 * runs the conductor agent, and the board is the only way to talk to it: select
 * shapes and ask. Its answers hang under the shapes, what it adds lands on the
 * board when its turn ends, and its status overlay is pinned to shapes.
 */
import { ShieldCheck, X } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, ApiError } from '../../../../api/client'
import ApprovalCard from '../../../../components/ApprovalCard'
import { ConductorControls, SessionMessagesPanel } from '../ConductorSession'
import CrewApprovalCards from '../../../../components/crew/CrewApprovalCards'
import ErrorNotice from '../../../../components/ErrorNotice'
import { sendTurn } from '../../../../chat-core/transport/sendTurn'
import { i18nT } from '../../../../i18n/t'
import { useAppDispatch, useAppSelector } from '../../../../store'
import { switchSlot } from '../../../../store/chatSlice'
import { selectSlotMessages } from '../../../../store/chat/selectors'
import { slotRowByKey } from '../../../../store/slotIndex'
import { toApiDecision } from '../../../../utils/approvalDecision'
import { ANSWER_STYLE, type ThreadView } from '../docConductor'
import { OVERLAY_CLASS_STYLE, OVERLAY_KEYFRAMES, overlaySlug, parseOverlay, type OverlayItem } from '../docOverlay'
import { deriveThreads, latestGoalReport } from '../docThreads'
import { syncGoalLoop } from '../goalLoop'
import { specToCss } from '../styleSpec'
import { MENTION_STYLE } from '../docMentions'
import { MENU_STYLE } from '../docSlash'
import { useSessionRefs } from '../SessionMention'
import BoardCanvas from './BoardCanvas'
import { boardGoalEvery, boardHasGoals, parseBoard, serializeBoard, type Board } from './boardModel'
import { applyReplyChecked, boardChangesFromMessages, buildBoardTurn } from './boardTurn'

const SAVE_DEBOUNCE_MS = 500
const OVERLAY_POLL_MS = 3000

/** The answer and overlay styles the doc editor's theme carries, for this surface. */
const SCOPE = '[data-board-root]'
const BOARD_CSS = [
  specToCss(ANSWER_STYLE, SCOPE),
  specToCss(OVERLAY_CLASS_STYLE, SCOPE),
  // Session links: the doc's chips and "@" picker, outside an editor.
  specToCss(MENTION_STYLE, SCOPE),
  specToCss(MENU_STYLE, SCOPE),
  '@keyframes cm-ask-pulse{0%,100%{opacity:1}50%{opacity:.35}}',
  OVERLAY_KEYFRAMES,
  `@media (prefers-reduced-motion: reduce){${SCOPE} [data-da="dot"],${SCOPE} .ov-status::before{animation:none!important}}`,
].join('\n')

function conflictBody(err: unknown): { content: string; version: string } | null {
  if (!(err instanceof ApiError) || err.status !== 409) return null
  try {
    const parsed = JSON.parse(err.body) as { content?: unknown; version?: unknown }
    return typeof parsed.content === 'string' && typeof parsed.version === 'string'
      ? { content: parsed.content, version: parsed.version }
      : null
  } catch {
    return null
  }
}

function useLocalRecord<T>(key: string, empty: T): [T, (next: T) => void] {
  const [value, setValue] = useState<T>(() => {
    try { return { ...empty, ...JSON.parse(localStorage.getItem(key) ?? 'null') } } catch { return empty }
  })
  const write = useCallback((next: T) => {
    setValue(next)
    try { localStorage.setItem(key, JSON.stringify(next)) } catch { /* private mode: in memory only */ }
  }, [key])
  return [value, write]
}

export default function BoardPane({ docId, slotKey, title, approvalMode }: { docId: string; slotKey: string; title?: string; approvalMode?: string }) {
  const dispatch = useAppDispatch()
  const [sessionOpen, setSessionOpen] = useState(false)
  const [load, setLoad] = useState<'loading' | 'ready' | { failed: string }>('loading')
  const [board, setBoard] = useState<Board>(() => parseBoard(''))
  const [saveError, setSaveError] = useState('')
  const [sendError, setSendError] = useState('')
  // What the conductor tried to change in a part it may not, until the person dismisses it.
  const [blockedNote, setBlockedNote] = useState<string[]>([])
  const [conflict, setConflict] = useState<{ content: string; version: string } | null>(null)
  const boardRef = useRef(board)
  boardRef.current = board
  const versionRef = useRef<string | null>(null)
  const dirtyRef = useRef(false)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const savingRef = useRef<Promise<void> | null>(null)

  const save = useCallback(async () => {
    if (timerRef.current) { clearTimeout(timerRef.current); timerRef.current = null }
    if (savingRef.current) await savingRef.current
    if (!dirtyRef.current) return
    const sent = serializeBoard(boardRef.current)
    dirtyRef.current = false
    const run = (async () => {
      try {
        const saved = await api.saveDoc(docId, sent, versionRef.current)
        versionRef.current = saved.version
        setSaveError('')
      } catch (err) {
        const current = conflictBody(err)
        if (current) setConflict(current)
        else setSaveError(err instanceof Error ? err.message : String(err))
        dirtyRef.current = true
      }
    })()
    savingRef.current = run
    await run
    savingRef.current = null
  }, [docId])

  const change = useCallback((next: Board) => {
    boardRef.current = next
    setBoard(next)
    dirtyRef.current = true
    if (timerRef.current) clearTimeout(timerRef.current)
    timerRef.current = setTimeout(() => { void save() }, SAVE_DEBOUNCE_MS)
  }, [save])

  useEffect(() => {
    let cancelled = false
    api.getDoc(docId).then(doc => {
      if (cancelled) return
      versionRef.current = doc.version
      dirtyRef.current = false
      const b = parseBoard(doc.content)
      boardRef.current = b
      setBoard(b)
      setLoad('ready')
    }, err => { if (!cancelled) setLoad({ failed: err instanceof Error ? err.message : String(err) }) })
    return () => {
      cancelled = true
      if (dirtyRef.current) void save()
    }
  }, [docId, save])

  // ---- the conductor ----------------------------------------------------------

  const messages = useAppSelector(s => selectSlotMessages(s, slotKey))
  const slots = useAppSelector(s => s.dashboard.slots)
  const sessionRefs = useSessionRefs(slots)
  const running = !!slotRowByKey(slots, slotKey)?.running
  const threads = useMemo(() => deriveThreads(messages), [messages])
  // Each section goal's latest report, by section id: a check's reply is split per goal.
  const goalReport = useCallback((sectionId: string) => latestGoalReport(threads, sectionId), [threads])

  // Section goals: one loop on the conductor, at the board's one rate. Synced when
  // the board gains its first goal, loses its last one, or its rate changes; a board
  // with no goals asks nothing of the loop until it gains one.
  const hasGoals = boardHasGoals(board)
  const goalEvery = boardGoalEvery(board)
  const [goalError, setGoalError] = useState('')
  const goalSynced = useRef<string | null>(null)
  useEffect(() => {
    if (load !== 'ready') return
    const want = `${hasGoals}:${goalEvery}`
    if (goalSynced.current === want || (goalSynced.current === null && !hasGoals)) return
    goalSynced.current = want
    syncGoalLoop({ slotKey, kind: 'board', docId, title, every: goalEvery, active: hasGoals })
      .then(() => setGoalError(''), err => {
        goalSynced.current = null
        setGoalError(err instanceof Error ? err.message : String(err))
      })
  }, [load, hasGoals, goalEvery, slotKey, docId, title])
  const [readCounts, setReadCounts] = useLocalRecord<Record<string, number>>(`docwork.read.${docId}`, {})
  const views = useMemo(() => {
    const out: Record<string, ThreadView> = {}
    for (const [id, t] of Object.entries(threads)) {
      const replies = t.messages.filter(m => m.who === 'conductor').length
      out[id] = { messages: t.messages, pending: t.awaiting && running, unread: replies > (readCounts[id] ?? 0) }
    }
    return out
  }, [threads, running, readCounts])

  const send = (id: string, request: string, b: Board, followUp: boolean) => {
    setSendError('')
    void save()
    const ask = b.asks.find(a => a.id === id)
    const message = buildBoardTurn({ id, request, board: b, selected: ask?.shapeIds ?? [], followUp, title, docId })
    void sendTurn({ message, slot: slotKey }).then(r => {
      if (r.status === 'refused') setSendError(r.reason || i18nT('pages.chatPage.send_failed'))
      else if (r.status === 'transport-error') setSendError(i18nT('pages.chatPage.send_failed'))
    })
  }
  const openSession = (key: string) => { void dispatch(switchSlot({ key, announceOnMissing: true, gesture: true })) }
  const markRead = (id: string) => {
    const replies = threads[id]?.messages.filter(m => m.who === 'conductor').length ?? 0
    if ((readCounts[id] ?? 0) !== replies) setReadCounts({ ...readCounts, [id]: replies })
  }

  // What the conductor's finished replies change on the board: each reply's
  // edits and additions land together as one undo step, once.
  const canvasApi = useRef<{ commit: (next: Board) => void } | null>(null)
  const [applied, setApplied] = useLocalRecord<{ keys: string[] }>(`docwork.applied.${docId}`, { keys: [] })
  useEffect(() => {
    if (running || load !== 'ready') return
    const done = new Set(applied.keys)
    const fresh = boardChangesFromMessages(messages).filter(r => !done.has(r.key))
    if (!fresh.length) return
    const blocked: string[] = []
    for (const r of fresh) {
      const { board: next, refused } = applyReplyChecked(boardRef.current, r)
      blocked.push(...refused)
      if (JSON.stringify(next) === JSON.stringify(boardRef.current)) continue
      if (canvasApi.current) canvasApi.current.commit(next)
      else change(next)
      boardRef.current = next
    }
    if (blocked.length) setBlockedNote([...new Set(blocked)])
    setApplied({ keys: [...applied.keys, ...fresh.map(r => r.key)] })
  }, [messages, running, load, applied, setApplied, change])

  // The conductor's overlay, read while the board is open.
  const [overlay, setOverlay] = useState<OverlayItem[]>([])
  const overlayVersion = useRef<number | null>(null)
  useEffect(() => {
    let stopped = false
    const read = () => {
      api.artifact(overlaySlug(docId)).then((a: { version?: number; content?: string }) => {
        if (stopped || a.version === overlayVersion.current) return
        overlayVersion.current = a.version ?? null
        setOverlay(parseOverlay(a.content ?? ''))
      }, () => { /* no overlay published yet */ })
    }
    read()
    const timer = setInterval(read, OVERLAY_POLL_MS)
    return () => { stopped = true; clearInterval(timer) }
  }, [docId, running])

  const approvals = messages.filter(m => m.role === 'permission' && !m.meta?.resolved && m.meta?.approval_id)

  return (
    <div className="relative flex-1 min-h-0" data-testid="board-pane" data-board-root="">
      <style>{BOARD_CSS}</style>
      {typeof load === 'object' && (
        <div className="p-6"><ErrorNotice title={i18nT('pages.chat.boardPane.load_failed')} message={load.failed} /></div>
      )}
      {load === 'ready' && (
        <BoardCanvas
          board={board}
          onChange={change}
          overlay={overlay}
          threads={views}
          onAsk={(id, text, b) => send(id, text, b, false)}
          onFollowUp={(id, text) => send(id, text, boardRef.current, true)}
          onOpenSession={openSession}
          onReadAsk={markRead}
          apiRef={canvasApi}
          sessions={sessionRefs}
          selfKey={slotKey}
          goalReport={goalReport}
        />
      )}
      <div className="pointer-events-none absolute left-4 right-4 top-4 flex flex-col items-center gap-2" style={{ zIndex: 20 }}>
        <div className="pointer-events-auto w-full max-w-[520px]">
          {/* No hand-off: the goals are already saved on the board; the next change retries the loop. */}
          <ErrorNotice title="Goal checks could not be scheduled" message={goalError} />
          {conflict && (
            <ErrorNotice
              title={i18nT('pages.chat.docPane.conflict_title')}
              message={i18nT('pages.chat.docPane.conflict_message')}
              footer={(
                <div className="flex gap-2 mt-2">
                  <button type="button" className="text-xs py-1.5 px-3.5 rounded-md border border-border bg-bg-elevated hover:bg-bg-hover"
                    onClick={() => { versionRef.current = conflict.version; setConflict(null); dirtyRef.current = true; void save() }}>
                    {i18nT('pages.chat.docPane.keep_mine')}
                  </button>
                  <button type="button" className="text-xs py-1.5 px-3.5 rounded-md border border-border bg-bg-elevated hover:bg-bg-hover"
                    onClick={() => { versionRef.current = conflict.version; dirtyRef.current = false; const b = parseBoard(conflict.content); boardRef.current = b; setBoard(b); setConflict(null) }}>
                    {i18nT('pages.chat.docPane.take_theirs')}
                  </button>
                </div>
              )}
            />
          )}
          <ErrorNotice title={i18nT('pages.chat.docPane.save_failed')} message={saveError} />
          <ErrorNotice title={i18nT('pages.chat.docPane.send_failed')} message={sendError} />
          {blockedNote.length > 0 && (
            <div data-testid="board-blocked-note" role="status" className="flex items-start gap-2 rounded-xl border border-border bg-bg-elevated px-3 py-2 text-[13px] text-text shadow-lg">
              <ShieldCheck size={16} className="mt-0.5 shrink-0 text-accent" aria-hidden />
              <span className="min-w-0 flex-1">
                Kept the conductor out of protected parts of the board. It tried to change: {blockedNote.slice(0, 4).join(', ')}{blockedNote.length > 4 ? ` and ${blockedNote.length - 4} more` : ''}.
              </span>
              <button type="button" aria-label="Dismiss" onClick={() => setBlockedNote([])} className="rounded p-0.5 text-muted hover:bg-bg-hover"><X size={14} /></button>
            </div>
          )}
        </div>
      </div>
      {/* The conductor's approval mode, the same control as the chat composer's:
          Trust here carries to the workers it starts, so they stop asking too. */}
      {sessionOpen && <SessionMessagesPanel slotKey={slotKey} onClose={() => setSessionOpen(false)} />}
      <ConductorControls slotKey={slotKey} approvalMode={approvalMode} sessionOpen={sessionOpen} onToggleSession={() => setSessionOpen(o => !o)}
        className="absolute bottom-5 left-5 rounded-xl border border-border bg-bg-elevated p-1 shadow-lg" style={{ zIndex: 25 }} testid="board-approval-mode" />
      {approvals.length > 0 && (
        <div className="absolute bottom-20 right-5 w-[380px] max-w-[90vw] flex flex-col gap-2" style={{ zIndex: 30 }} data-testid="board-approvals">
          {approvals.map(m => {
            const id = String(m.meta?.approval_id)
            return (
              <ApprovalCard key={id} title={m.content} toolInput={String(m.meta?.tool_input ?? '')} showButtons showTrust={false}
                onApprove={decision => api.resolveApproval(id, toApiDecision(decision))} />
            )
          })}
        </div>
      )}
      {/* The workers' tool approvals: Crew Mode decides those only on their
          conductor's page, and this board is that page. */}
      <div className="absolute bottom-20 left-5 w-[400px] max-w-[90vw]" style={{ zIndex: 30 }} data-testid="board-worker-approvals">
        <CrewApprovalCards root={slotKey} />
      </div>
    </div>
  )
}
