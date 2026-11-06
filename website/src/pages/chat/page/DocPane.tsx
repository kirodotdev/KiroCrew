/**
 * The pane of a doc session: a markdown document you write in, in place of a chat.
 * The doc slot runs the conductor agent, and the document is the only way to talk
 * to it (`docConductor`): highlight, ask, and read the reply in a popover; workers
 * it starts show on the sections they own.
 *
 * The text lives in one file per doc (`/api/docs/<id>`), saved a moment after each
 * edit. Every save names the version it was read at, so an edit made to the file
 * elsewhere is reported here instead of overwritten.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import type { EditorView } from '@codemirror/view'
import { api, ApiError } from '../../../api/client'
import ApprovalCard from '../../../components/ApprovalCard'
import { SessionMessagesPanel } from './ConductorSession'
import MainThreadDock from './MainThreadDock'
import CrewApprovalCards from '../../../components/crew/CrewApprovalCards'
import ErrorNotice from '../../../components/ErrorNotice'
import { sendTurn } from '../../../chat-core/transport/sendTurn'
import { i18nT } from '../../../i18n/t'
import { useAppSelector } from '../../../store'
import { selectSlotMessages } from '../../../store/chatSlice'
import { slotRowByKey } from '../../../store/slotIndex'
import { toApiDecision } from '../../../utils/approvalDecision'
import DocEditor from './DocEditor'
import DocHeader from './DocHeader'
import { joinDoc, randomDocIcon, randomGradient, splitDoc, type DocMeta } from './docMeta'
import { useSessionRefs } from './SessionMention'
import { DEFAULT_GOAL_EVERY } from './goalLoop'
import { GOAL_INTERVALS } from './board/boardModel'
import type { EmbedTheme } from './DocHtmlFrame'
import { useOptionalTheme } from '../../../hooks/useTheme'
import { readThemeVars } from '../../../lib/widgetSrcdoc'
import ConversationBar, { cycleDirection, shortLabel, stepId, type ConversationItem } from './ConversationBar'
import { changeForWrite, markerContent, parseMarkers, threadQuote, type ConductorHost, type DocData } from './docConductor'
import DocThreadPanel from './DocThreadPanel'
import { rowQuoteLabel } from './docAskRows'
import { OVERLAY_KEYFRAMES, findAnchor, overlaySlug, parseOverlay, type OverlayItem } from './docOverlay'
import { MAIN_THREAD, buildAskTurn, deriveThreads, docTextForCopy, goalCheckPending } from './docThreads'

/** Quiet time after the last edit before it is written. */
const SAVE_DEBOUNCE_MS = 600
/** How long the conversation bar stays after the last ⌃⌥ step. */
const BAR_LINGER_MS = 2500
/** How often the open doc reads the conductor's overlay. */
const OVERLAY_POLL_MS = 3000

type Load = { state: 'loading' } | { state: 'ready' } | { state: 'failed'; message: string }

/** The file as the server holds it, from a 409's body. */
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

/** A small per-doc record in localStorage (PoC): read counts, applied actions. */
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

/** `onOpenSession` opens a session the page links to; the page switches slots, never a pane. */
export default function DocPane({ docId, slotKey, title, approvalMode, onOpenSession }: { docId: string; slotKey: string; title?: string; approvalMode?: string; onOpenSession: (key: string) => void }) {
  const [load, setLoad] = useState<Load>({ state: 'loading' })
  // The text the editor is seeded with, and a counter that remounts it when the
  // text is replaced from outside (load, "use the other version").
  const [seed, setSeed] = useState({ text: '', generation: 0 })
  const [saveError, setSaveError] = useState('')
  const [sendError, setSendError] = useState('')
  const [conflict, setConflict] = useState<{ content: string; version: string } | null>(null)
  // The text and version a save sends: refs, so the debounced write and the
  // unmount flush read the latest values without re-arming on every keystroke.
  const contentRef = useRef('')
  const versionRef = useRef<string | null>(null)
  const dirtyRef = useRef(false)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const savingRef = useRef<Promise<void> | null>(null)
  const viewRef = useRef<EditorView | null>(null)

  const save = useCallback(async () => {
    if (timerRef.current) { clearTimeout(timerRef.current); timerRef.current = null }
    // One write at a time: a second would carry the version the first replaces.
    if (savingRef.current) await savingRef.current
    if (!dirtyRef.current) return
    const sent = contentRef.current
    dirtyRef.current = false
    const run = (async () => {
      try {
        // The version the text was read at ("" for a doc with no file yet), so a
        // file that moved meanwhile is refused rather than overwritten.
        const saved = await api.saveDoc(docId, sent, versionRef.current)
        versionRef.current = saved.version
        setSaveError('')
      } catch (err) {
        const current = conflictBody(err)
        if (current) {
          // Keep the person's text in the editor and in the dirty state, so
          // nothing they typed is lost; they choose what wins.
          setConflict(current)
        } else {
          setSaveError(err instanceof Error ? err.message : String(err))
        }
        dirtyRef.current = true
      }
    })()
    savingRef.current = run
    await run
    savingRef.current = null
  }, [docId])

  // The highlights in document order, for the conversation bar. Kept as state
  // only when the set or a label changes, so typing does not re-render the pane.
  const [asks, setAsks] = useState<{ id: string; label: string }[]>([])
  const trackAsks = useCallback((text: string) => {
    const next = parseMarkers(text).filter(m => m.kind === 'ask').map(m => ({ id: m.id, label: shortLabel(markerContent(text, m)) }))
    setAsks(prev => (JSON.stringify(prev) === JSON.stringify(next) ? prev : next))
  }, [])

  // The page header (icon, cover) lives in the file's front matter: the editor is
  // given only the body, and every save writes the two back together.
  const [meta, setMeta] = useState<DocMeta>({})
  const headRef = useRef<{ meta: DocMeta; other: string[] }>({ meta: {}, other: [] })
  const bodyRef = useRef('')
  const adopt = useCallback((file: string) => {
    const doc = splitDoc(file)
    headRef.current = { meta: doc.meta, other: doc.other }
    bodyRef.current = doc.body
    setMeta(doc.meta)
    return doc.body
  }, [])

  const markDirty = useCallback(() => {
    contentRef.current = joinDoc({ ...headRef.current, body: bodyRef.current })
    dirtyRef.current = true
    if (timerRef.current) clearTimeout(timerRef.current)
    timerRef.current = setTimeout(() => { void save() }, SAVE_DEBOUNCE_MS)
  }, [save])

  const edit = useCallback((next: string) => {
    trackAsks(next)
    bodyRef.current = next
    markDirty()
  }, [markDirty, trackAsks])

  const changeMeta = useCallback((next: DocMeta) => {
    headRef.current = { ...headRef.current, meta: next }
    setMeta(next)
    markDirty()
  }, [markDirty])

  useEffect(() => {
    let cancelled = false
    setLoad({ state: 'loading' })
    setConflict(null)
    setSaveError('')
    api.getDoc(docId).then(doc => {
      if (cancelled) return
      contentRef.current = doc.content
      versionRef.current = doc.version
      dirtyRef.current = false
      const body = adopt(doc.content)
      setSeed(s => ({ text: body, generation: s.generation + 1 }))
      trackAsks(body)
      // A doc never saved yet gets a random document icon and cover, as a new Notion
      // page does. Saved straight away, so the file exists and a reload keeps them.
      if (!doc.version && !headRef.current.meta.icon && !headRef.current.meta.cover) {
        const fresh = { ...headRef.current.meta, icon: randomDocIcon(), cover: randomGradient() }
        headRef.current = { ...headRef.current, meta: fresh }
        setMeta(fresh)
        markDirty()
      }
      setLoad({ state: 'ready' })
    }, err => {
      if (!cancelled) setLoad({ state: 'failed', message: err instanceof Error ? err.message : String(err) })
    })
    return () => {
      cancelled = true
      // Leaving the doc (another session, or closing it) writes what is pending.
      if (dirtyRef.current) void save()
    }
  }, [docId, save, trackAsks, adopt, markDirty])

  const takeTheirs = useCallback(() => {
    if (!conflict) return
    contentRef.current = conflict.content
    versionRef.current = conflict.version
    dirtyRef.current = false
    const body = adopt(conflict.content)
    setSeed(s => ({ text: body, generation: s.generation + 1 }))
    trackAsks(body)
    setConflict(null)
  }, [conflict, trackAsks, adopt])
  const keepMine = useCallback(() => {
    if (!conflict) return
    versionRef.current = conflict.version
    setConflict(null)
    dirtyRef.current = true
    void save()
  }, [conflict, save])

  // ---- the conductor ----------------------------------------------------------

  const messages = useAppSelector(s => selectSlotMessages(s, slotKey))
  const [sessionOpen, setSessionOpen] = useState(false)
  const slots = useAppSelector(s => s.dashboard.slots)
  const running = !!slotRowByKey(slots, slotKey)?.running
  const sessionRefs = useSessionRefs(slots)
  // HTML blocks draw with the page's theme, read again on every theme change
  // (themeVersion covers an edited custom theme under the same name).
  const themeCtx = useOptionalTheme()
  const mode = themeCtx?.theme ?? 'light'
  // eslint-disable-next-line react-hooks/exhaustive-deps -- the vars are read fresh when any of these change
  const embedTheme = useMemo<EmbedTheme>(() => ({ mode, vars: readThemeVars() }), [mode, themeCtx?.colorTheme, themeCtx?.themeVersion])
  const threads = useMemo(() => deriveThreads(messages), [messages])
  const [readCounts, setReadCounts] = useLocalRecord<Record<string, number>>(`docwork.read.${docId}`, {})
  // A row ask's label (its row), kept from when it was sent: its markers wrap the
  // whole table, so the highlight alone would only name the table.
  const [rowLabels, setRowLabels] = useLocalRecord<Record<string, string>>(`docwork.rowLabels.${docId}`, {})

  // The page's current conversation. `focus` is 0 when a click opened it (it is
  // already in view), and a fresh number when the bar or its shortcut moved there.
  const [active, setActive] = useState<{ id: string; focus: number } | null>(null)
  const focusCounter = useRef(0)
  const focusConversation = useCallback((id: string | null) => {
    if (id === null) {
      // None: every answer folds away and the caret goes back to the doc.
      setActive(null)
      viewRef.current?.focus()
      return
    }
    focusCounter.current += 1
    setActive({ id, focus: focusCounter.current })
  }, [])

  const data = useMemo<DocData>(() => {
    const views: DocData['threads'] = {}
    for (const [id, t] of Object.entries(threads)) {
      const replies = t.messages.filter(m => m.who === 'conductor').length
      // A goal's pin also shows the check every goal is waiting on.
      const waiting = t.awaiting || (id.startsWith('goal-') && goalCheckPending(threads))
      views[id] = { messages: t.messages, pending: waiting && running, unread: replies > (readCounts[id] ?? 0), topic: rowLabels[id] }
    }
    return { threads: views, workers: {}, active, goalsPending: goalCheckPending(threads) && running }
  }, [threads, running, readCounts, active, rowLabels])

  const conversations = useMemo<ConversationItem[]>(() => asks.map(a => {
    const t = data.threads[a.id]
    return { id: a.id, label: rowLabels[a.id] ? shortLabel(rowLabels[a.id]) : a.label, state: !t || t.pending ? 'pending' : t.unread ? 'unread' : 'read' }
  }), [asks, data.threads, rowLabels])

  // ⌃⌥← / ⌃⌥→ step through the conversations from anywhere on the page. Capture
  // phase, so the editor never sees the keys.
  const conversationIds = useRef<string[]>([])
  conversationIds.current = conversations.map(c => c.id)
  const activeId = active?.id ?? null
  const activeIdRef = useRef(activeId)
  activeIdRef.current = activeId
  // The bar is out of sight until the shortcut is used: it appears with the step
  // and fades a moment after the last one, unless the pointer is resting on it.
  const [barShown, setBarShown] = useState(false)
  const reduceMotion = useReducedMotion()
  const barTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const barHovered = useRef(false)
  const hideBarSoon = useCallback(() => {
    if (barTimer.current) clearTimeout(barTimer.current)
    barTimer.current = setTimeout(() => { if (!barHovered.current) setBarShown(false) }, BAR_LINGER_MS)
  }, [])
  useEffect(() => () => { if (barTimer.current) clearTimeout(barTimer.current) }, [])
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const dir = cycleDirection(e)
      if (!dir || !conversationIds.current.length) return
      e.preventDefault()
      e.stopPropagation()
      focusConversation(stepId(conversationIds.current, activeIdRef.current, dir))
      setBarShown(true)
      hideBarSoon()
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [focusConversation, hideBarSoon])

  // The conductor's status overlay: an artifact it publishes and updates. Read
  // often while the doc is open (PoC); a missing one is simply no overlay yet.
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

  // What the conductor was last sent, so an ask carries the title and a copy of the
  // doc only when they changed. Edits never start a turn: a change rides on the next
  // ask. Empty after a reload, so the first ask then sends both.
  const lastSent = useRef<{ title: string; copy: string } | null>(null)

  // The conductor layer calls back through a ref, so the editor never rebuilds.
  const hostRef = useRef<ConductorHost>({ send: () => {}, openSession: () => {}, markRead: () => {} })
  hostRef.current = {
    // Threads open in the side panel, as a Slack thread does beside its channel.
    threadsInSidePanel: true,
    activate: id => setActive(a => (a?.id === id ? a : { id, focus: 0 })),
    send: (id, request, quote, doc, followUp) => {
      setSendError('')
      const rowLabel = followUp ? null : rowQuoteLabel(quote)
      if (rowLabel) setRowLabels({ ...rowLabels, [id]: rowLabel })
      // A new ask opens its thread beside the doc, so the answer is seen arriving.
      if (!followUp && id !== MAIN_THREAD) setActive({ id, focus: 0 })
      // The markers just went in: write them now, not after the debounce.
      void save()
      const name = title || ''
      // Changed is judged without the ask markers (a new highlight is no change),
      // but a copy that goes out carries them, so it matches the doc exactly.
      const text = docTextForCopy(doc)
      const message = buildAskTurn({
        id, request, quote, doc,
        title: lastSent.current?.title === name ? null : name,
        copy: lastSent.current?.copy === text ? null : doc,
      })
      void sendTurn({ message, slot: slotKey }).then(r => {
        if (r.status === 'refused') setSendError(r.reason || i18nT('pages.chatPage.send_failed'))
        else if (r.status === 'transport-error') setSendError(i18nT('pages.chatPage.send_failed'))
        // Counted as seen only once it went out, so a failed send resends both.
        else lastSent.current = { title: name, copy: text }
      })
    },
    openSession: onOpenSession,
    markRead: id => {
      const replies = threads[id]?.messages.filter(m => m.who === 'conductor').length ?? 0
      if ((readCounts[id] ?? 0) !== replies) setReadCounts({ ...readCounts, [id]: replies })
    },
  }

  // The thread in the side panel is being read: its replies count as seen as they arrive.
  const shownReplies = activeId ? (threads[activeId]?.messages.filter(m => m.who === 'conductor').length ?? 0) : 0
  useEffect(() => {
    if (activeId) hostRef.current.markRead(activeId)
  }, [activeId, shownReplies])

  // Add what the conductor's finished replies asked to write, once each. A write
  // whose text is already in the doc is skipped, so a reload or a second browser
  // never adds it twice.
  const [applied, setApplied] = useLocalRecord<{ keys: string[] }>(`docwork.applied.${docId}`, { keys: [] })
  useEffect(() => {
    const view = viewRef.current
    if (running || load.state !== 'ready' || !view) return
    const done = new Set(applied.keys)
    const fresh: string[] = []
    for (const t of Object.values(threads)) {
      for (const { key, actions } of t.actions) {
        if (done.has(key)) continue
        for (const w of actions.writes) {
          const change = changeForWrite(view.state.doc.toString(), w, findAnchor)
          if (change) view.dispatch({ changes: change })
        }
        fresh.push(key)
      }
    }
    if (fresh.length) setApplied({ keys: [...applied.keys, ...fresh] })
  }, [threads, running, load.state, applied, setApplied, seed.generation])

  // ---- heading goals: the gateway arms the one loop on the conductor from the saved file ----
  const goalEvery = GOAL_INTERVALS.includes(Number(meta['goal-every'])) ? Number(meta['goal-every']) : DEFAULT_GOAL_EVERY
  const setGoalEvery = useCallback((m: number) => changeMeta({ ...headRef.current.meta, 'goal-every': String(m) }), [changeMeta])
  // Each goal's finished checks, for the bars on its heading's chip.
  const goalChecks = useMemo(() => Object.fromEntries(
    Object.entries(threads).filter(([k, t]) => k.startsWith('goal-') && t.checks?.length).map(([k, t]) => [k.slice(5), t.checks!]),
  ), [threads])

  // The conductor's tool approvals: there is no chat to answer them in.
  const approvals = messages.filter(m => m.role === 'permission' && !m.meta?.resolved && m.meta?.approval_id)

  return (
    // A still frame around the scrolling page, so what floats on it (the
    // approval mode) stays put while the doc scrolls.
    <div className="relative flex min-h-0 flex-1 flex-col">
    {/* The conductor's approval mode, the same control as the chat composer's,
        at the bottom left as on the board: Trust here carries to the workers it
        starts, so they stop asking too. */}
    {sessionOpen && <SessionMessagesPanel slotKey={slotKey} onClose={() => setSessionOpen(false)} />}
    <MainThreadDock slotKey={slotKey} approvalMode={approvalMode} sessionOpen={sessionOpen} onToggleSession={() => setSessionOpen(o => !o)}
      thread={data.threads[MAIN_THREAD]} running={running} placeholder="Message about the whole doc…" controlsTestid="doc-approval-mode"
      onSend={text => {
        const view = viewRef.current
        if (view) hostRef.current.send(MAIN_THREAD, text, '', view.state.doc.toString(), !!threads[MAIN_THREAD])
      }}
      className="z-[25]" />
    <div className="flex min-h-0 flex-1">
    <div className="flex-1 min-h-0 overflow-y-auto relative" data-testid="doc-pane">
      {/* The overlay's animations; the board mounts the same copy. */}
      <style>{OVERLAY_KEYFRAMES}</style>
      {/* The conversations bar, floating and taking no room in the doc: only
          while ⌃⌥← / ⌃⌥→ steps through the conversations. */}
      <div className="pointer-events-none sticky top-0 z-20 h-0 overflow-visible">
        {/* Below the session's title row (about 45px), which overlays the top of
            this pane: higher up, it sat behind it with only its edge showing. */}
        <AnimatePresence>
          {barShown && (
            <motion.div key="bar" className="flex justify-center px-4 pt-[56px]" data-testid="doc-conversation-bar"
              initial={reduceMotion ? false : { opacity: 0, y: -6 }} animate={{ opacity: 1, y: 0 }} exit={reduceMotion ? { opacity: 0, transition: { duration: 0 } } : { opacity: 0, y: -6 }}
              transition={{ duration: 0.16 }}
              onMouseEnter={() => { barHovered.current = true }}
              onMouseLeave={() => { barHovered.current = false; hideBarSoon() }}>
              <ConversationBar items={conversations} activeId={activeId} onSelect={focusConversation} />
            </motion.div>
          )}
        </AnimatePresence>
      </div>
      {load.state === 'ready' && <DocHeader meta={meta} onChange={changeMeta} />}
      <div style={{ margin: '0 auto', width: '100%', maxWidth: 'var(--mc-content-width)', boxSizing: 'border-box', padding: load.state === 'ready' ? '4px 20px 0' : '56px 20px 0' }}>
        {load.state === 'failed' && (
          <ErrorNotice title={i18nT('pages.chat.docPane.load_failed')} message={load.message} />
        )}
        {conflict && (
          <ErrorNotice
            title={i18nT('pages.chat.docPane.conflict_title')}
            message={i18nT('pages.chat.docPane.conflict_message')}
            className="mb-3"
            footer={(
              <div className="flex gap-2 mt-2">
                <button type="button" className="text-xs py-1.5 px-3.5 rounded-md border border-border bg-bg-elevated hover:bg-bg-hover" onClick={keepMine}>
                  {i18nT('pages.chat.docPane.keep_mine')}
                </button>
                <button type="button" className="text-xs py-1.5 px-3.5 rounded-md border border-border bg-bg-elevated hover:bg-bg-hover" onClick={takeTheirs}>
                  {i18nT('pages.chat.docPane.take_theirs')}
                </button>
              </div>
            )}
          />
        )}
        <ErrorNotice title={i18nT('pages.chat.docPane.save_failed')} message={saveError} className="mb-3" />
        <ErrorNotice title={i18nT('pages.chat.docPane.send_failed')} message={sendError} className="mb-3" />
        {load.state === 'ready' && (
          <DocEditor
            key={seed.generation}
            initial={seed.text}
            onChange={edit}
            placeholderText={i18nT('pages.chat.docPane.empty_hint')}
            host={hostRef}
            data={data}
            overlay={overlay}
            viewRef={viewRef}
            sessions={sessionRefs}
            selfKey={slotKey}
            embedTheme={embedTheme}
            goalEvery={goalEvery}
            goalChecks={goalChecks}
            onGoalEvery={setGoalEvery}
          />
        )}
      </div>
      {approvals.length > 0 && (
        <div className="fixed bottom-safe-offset-6 right-safe-offset-6 z-30 w-[380px] max-w-[90vw] flex flex-col gap-2" data-testid="doc-approvals">
          {approvals.map(m => {
            const id = String(m.meta?.approval_id)
            return (
              <ApprovalCard
                key={id}
                title={m.content}
                toolInput={String(m.meta?.tool_input ?? '')}
                showButtons
                showTrust={false}
                onApprove={decision => api.resolveApproval(id, toApiDecision(decision))}
              />
            )
          })}
        </div>
      )}
    </div>
    {activeId && (
      <DocThreadPanel
        key={activeId}
        thread={data.threads[activeId]}
        about={viewRef.current ? threadQuote(viewRef.current.state, activeId) : ''}
        isGoal={activeId.startsWith('goal-')}
        title={title}
        focusSignal={active?.focus ?? 0}
        onSend={text => {
          const view = viewRef.current
          if (view) hostRef.current.send(activeId, text, threadQuote(view.state, activeId), view.state.doc.toString(), true)
        }}
        onClose={() => focusConversation(null)}
        onOpenSession={key => hostRef.current.openSession(key)}
      />
    )}
    </div>
    {/* The workers' tool approvals: Crew Mode decides those only on their
        conductor's page, and this doc is that page. Stacked above the approval
        mode, which holds the corner. */}
    <div className="absolute bottom-[150px] left-5 z-30 w-[400px] max-w-[90vw]" data-testid="doc-worker-approvals">
      <CrewApprovalCards root={slotKey} />
    </div>
    </div>
  )
}
