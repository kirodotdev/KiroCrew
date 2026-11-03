/**
 * The pane of a dashboard session (PoC): an HTML page the conductor writes, in
 * place of a chat.
 *
 * The page is the OBJECT. It starts blank; ask for something and the conductor
 * writes it. Pick elements on it (Pick mode, V) or nothing (the whole page) and
 * ask (C) to talk to the conductor about them; Use mode (I) lets the page's own
 * controls work. The conductor's overlay artifact is the STATE: drawn on top,
 * pinned to page elements, never part of the page. Each reply's page changes are
 * one undo step (⌘Z).
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { MessageSquarePlus, MousePointer2, MousePointerClick, Redo2, Sparkles, Undo2 } from 'lucide-react'
import { api, ApiError } from '../../../../api/client'
import ApprovalCard from '../../../../components/ApprovalCard'
import { ConductorControls, SessionMessagesPanel } from '../ConductorSession'
import CrewApprovalCards from '../../../../components/crew/CrewApprovalCards'
import ErrorNotice from '../../../../components/ErrorNotice'
import { sendTurn } from '../../../../chat-core/transport/sendTurn'
import { useSandboxDoc } from '../../../../hooks/useSandboxDoc'
import { i18nT } from '../../../../i18n/t'
import { AGENT_PAGE_SANDBOX, readThemeVars } from '../../../../lib/widgetSrcdoc'
import { useAppDispatch, useAppSelector } from '../../../../store'
import { switchSlot } from '../../../../store/chatSlice'
import { selectSlotMessages } from '../../../../store/chat/selectors'
import { slotRowByKey } from '../../../../store/slotIndex'
import { toApiDecision } from '../../../../utils/approvalDecision'
import ConversationBar, { cycleDirection, stepId, type ConversationItem } from '../ConversationBar'
import DocAnswer from '../DocAnswer'
import { ANSWER_STYLE, type ThreadView } from '../docConductor'
import { OVERLAY_CLASS_STYLE, OVERLAY_KEYFRAMES, overlaySlug, parseOverlay, type OverlayItem } from '../docOverlay'
import { deriveThreads } from '../docThreads'
import { nudgeApart, placeAround } from '../overlayPlace'
import { specToCss } from '../styleSpec'
import {
  applyPageChanges, labelFor, pageChangesFromMessages, parseDashboard, redoPage, serializeDashboard, undoPage, withPage,
  type Dashboard, type PageAsk,
} from './dashboardModel'
import { buildDashboardTurn } from './dashboardTurn'
import { buildPageDocument, isFrameMessage, type FrameRect } from './pageFrame'

const SAVE_DEBOUNCE_MS = 500
const OVERLAY_POLL_MS = 3000

const SCOPE = '[data-dashboard-root]'
const PANE_CSS = [
  specToCss(ANSWER_STYLE, SCOPE),
  specToCss(OVERLAY_CLASS_STYLE, SCOPE),
  '@keyframes cm-ask-pulse{0%,100%{opacity:1}50%{opacity:.35}}',
  OVERLAY_KEYFRAMES,
  `@media (prefers-reduced-motion: reduce){${SCOPE} [data-da="dot"],${SCOPE} .ov-status::before{animation:none!important}}`,
].join('\n')

type Mode = 'pick' | 'use'
interface Picked { target: string; label: string }

function conflictBody(err: unknown): { content: string; version: string } | null {
  if (!(err instanceof ApiError) || err.status !== 409) return null
  try {
    const parsed = JSON.parse(err.body) as { content?: unknown; version?: unknown }
    return typeof parsed.content === 'string' && typeof parsed.version === 'string' ? { content: parsed.content, version: parsed.version } : null
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

/** An overlay anchor as a target: `el:`/`css:` as written, a bare word as a data-id. */
const anchorTarget = (anchor: string) => (/^(el|css):/.test(anchor) ? anchor : `el:${anchor}`)

const newAskId = () => Math.random().toString(36).slice(2, 10)

export default function DashboardPane({ docId, slotKey, title, approvalMode }: { docId: string; slotKey: string; title?: string; approvalMode?: string }) {
  const dispatch = useAppDispatch()
  const [sessionOpen, setSessionOpen] = useState(false)
  const [load, setLoad] = useState<'loading' | 'ready' | { failed: string }>('loading')
  const [dash, setDash] = useState<Dashboard>(() => parseDashboard(''))
  const [saveError, setSaveError] = useState('')
  const [sendError, setSendError] = useState('')
  const [conflict, setConflict] = useState<{ content: string; version: string } | null>(null)
  const dashRef = useRef(dash)
  dashRef.current = dash
  const versionRef = useRef<string | null>(null)
  const dirtyRef = useRef(false)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const savingRef = useRef<Promise<void> | null>(null)

  const save = useCallback(async () => {
    if (timerRef.current) { clearTimeout(timerRef.current); timerRef.current = null }
    if (savingRef.current) await savingRef.current
    if (!dirtyRef.current) return
    const sent = serializeDashboard(dashRef.current)
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

  const change = useCallback((next: Dashboard) => {
    dashRef.current = next
    setDash(next)
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
      const d = parseDashboard(doc.content)
      dashRef.current = d
      setDash(d)
      setLoad('ready')
    }, err => { if (!cancelled) setLoad({ failed: err instanceof Error ? err.message : String(err) }) })
    return () => {
      cancelled = true
      if (dirtyRef.current) void save()
    }
  }, [docId, save])

  // ---- the page, in its frame ---------------------------------------------------

  const frame = useRef<HTMLIFrameElement>(null)
  const document_ = useMemo(() => (dash.html.trim() ? buildPageDocument(dash.html, readThemeVars()) : null), [dash.html])
  const { url, failed: mintFailed, retry: retryMint } = useSandboxDoc(document_)
  const [mode, setMode] = useState<Mode>('pick')
  const [hover, setHover] = useState<{ target: string; rect: FrameRect; label: string } | null>(null)
  const [rects, setRects] = useState<Record<string, FrameRect | null>>({})
  const [picked, setPicked] = useState<Picked[]>([])
  const [asking, setAsking] = useState(false)
  const [askText, setAskText] = useState('')
  const toFrame = useCallback((m: unknown) => { frame.current?.contentWindow?.postMessage(m, '*') }, [])

  // ---- the conductor ---------------------------------------------------------------

  const messages = useAppSelector(s => selectSlotMessages(s, slotKey))
  const slots = useAppSelector(s => s.dashboard.slots)
  const running = !!slotRowByKey(slots, slotKey)?.running
  const threads = useMemo(() => deriveThreads(messages), [messages])
  const [readCounts, setReadCounts] = useLocalRecord<Record<string, number>>(`docwork.read.${docId}`, {})
  const views = useMemo(() => {
    const out: Record<string, ThreadView> = {}
    for (const [id, t] of Object.entries(threads)) {
      const replies = t.messages.filter(m => m.who === 'conductor').length
      out[id] = { messages: t.messages, pending: t.awaiting && running, unread: replies > (readCounts[id] ?? 0) }
    }
    return out
  }, [threads, running, readCounts])

  const sendFor = (ask: PageAsk, request: string, followUp: boolean) => {
    setSendError('')
    void save()
    const message = buildDashboardTurn({ id: ask.id, request, html: dashRef.current.html, targets: ask.targets, followUp, title, docId })
    void sendTurn({ message, slot: slotKey }).then(r => {
      if (r.status === 'refused') setSendError(r.reason || i18nT('pages.chatPage.send_failed'))
      else if (r.status === 'transport-error') setSendError(i18nT('pages.chatPage.send_failed'))
    })
  }
  const ask = (text: string) => {
    const request = text.trim()
    if (!request) return
    const targets = picked.map(p => p.target)
    const label = picked.length
      ? `${labelFor(picked[0].label, picked[0].target.replace(/^el:/, ''))}${picked.length > 1 ? ` (+${picked.length - 1})` : ''}`
      : labelFor(request, 'Whole page')
    const entry: PageAsk = { id: newAskId(), targets, label }
    change({ ...dashRef.current, asks: [...dashRef.current.asks, entry] })
    sendFor(entry, request, false)
    setPicked([])
    setAsking(false)
    setAskText('')
  }
  const followUp = (id: string, text: string) => {
    const entry = dashRef.current.asks.find(a => a.id === id)
    if (entry) sendFor(entry, text, true)
  }
  const openSession = (key: string) => { void dispatch(switchSlot({ key, announceOnMissing: true, gesture: true })) }
  const markRead = (id: string) => {
    const replies = threads[id]?.messages.filter(m => m.who === 'conductor').length ?? 0
    if ((readCounts[id] ?? 0) !== replies) setReadCounts({ ...readCounts, [id]: replies })
  }

  // Each finished reply's page changes land once, as one undo step.
  const [applied, setApplied] = useLocalRecord<{ keys: string[] }>(`docwork.applied.${docId}`, { keys: [] })
  useEffect(() => {
    if (running || load !== 'ready') return
    const done = new Set(applied.keys)
    const fresh = pageChangesFromMessages(messages).filter(r => !done.has(r.key))
    if (!fresh.length) return
    let next = dashRef.current
    for (const r of fresh) next = withPage(next, applyPageChanges(next.html, r.changes))
    if (next !== dashRef.current) change(next)
    setApplied({ keys: [...applied.keys, ...fresh.map(r => r.key)] })
  }, [messages, running, load, applied, setApplied, change])

  // The conductor's overlay: the state drawn on top of the page.
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

  // ---- what the frame is asked to track, and what it reports -------------------------

  const tracked = useMemo(() => {
    const set = new Set<string>()
    for (const p of picked) set.add(p.target)
    for (const a of dash.asks) if (a.targets[0]) set.add(a.targets[0])
    for (const o of overlay) if (o.anchor) set.add(anchorTarget(o.anchor))
    return [...set]
  }, [picked, dash.asks, overlay])
  const trackedRef = useRef(tracked)
  trackedRef.current = tracked
  const modeRef = useRef(mode)
  modeRef.current = mode
  useEffect(() => { toFrame({ dw: 'track', targets: tracked }) }, [tracked, toFrame, url])
  useEffect(() => { toFrame({ dw: 'mode', mode }); if (mode === 'use') setHover(null) }, [mode, toFrame, url])

  // Undo and redo step through the page's versions.
  const undo = useCallback(() => { const d = dashRef.current; if (d.past.length) change(undoPage(d)) }, [change])
  const redo = useCallback(() => { const d = dashRef.current; if (d.future.length) change(redoPage(d)) }, [change])

  // ---- conversations: the bar, and ⌃⌥← / ⌃⌥→ ------------------------------------------

  const [activeId, setActiveId] = useState<string | null>(null)
  const [focusSignals, setFocusSignals] = useState<Record<string, number>>({})
  const focusConversation = useCallback((id: string | null) => {
    setActiveId(id)
    if (id) setFocusSignals(s => ({ ...s, [id]: (s[id] ?? 0) + 1 }))
  }, [])
  const conversations = useMemo<ConversationItem[]>(() => dash.asks.map(a => {
    const v = views[a.id]
    return { id: a.id, label: a.label || 'Whole page', state: !v || v.pending ? 'pending' : v.unread ? 'unread' : 'read' }
  }), [dash.asks, views])
  const conversationIds = useRef<string[]>([])
  conversationIds.current = conversations.map(c => c.id)
  const activeIdRef = useRef(activeId)
  activeIdRef.current = activeId

  // One key handler for the pane and for keys pressed inside the page.
  const onKey = useCallback((k: { key: string; meta: boolean; ctrl: boolean; alt: boolean; shift: boolean }) => {
    const dir = cycleDirection({ key: k.key, ctrlKey: k.ctrl, altKey: k.alt, metaKey: k.meta, shiftKey: k.shift })
    if (dir) { focusConversation(stepId(conversationIds.current, activeIdRef.current, dir)); return true }
    if ((k.meta || k.ctrl) && k.key.toLowerCase() === 'z') { if (k.shift) redo(); else undo(); return true }
    if (k.meta || k.ctrl || k.alt) return false
    const key = k.key.toLowerCase()
    if (key === 'escape') { setAsking(false); setPicked([]); return true }
    if (key === 'v') { setMode('pick'); return true }
    if (key === 'i') { setMode('use'); return true }
    if (key === 'c' || key === 't') { setAsking(true); return true }
    return false
  }, [focusConversation, undo, redo])

  useEffect(() => {
    const onMessage = (e: MessageEvent) => {
      if (!frame.current || e.source !== frame.current.contentWindow || !isFrameMessage(e.data)) return
      const m = e.data
      if (m.dw === 'ready') { toFrame({ dw: 'mode', mode: modeRef.current }); toFrame({ dw: 'track', targets: trackedRef.current }) }
      else if (m.dw === 'hover') setHover(m.target && m.rect ? { target: m.target, rect: m.rect, label: m.label } : null)
      else if (m.dw === 'rects') setRects(m.rects)
      else if (m.dw === 'pick') {
        if (!m.target) { setPicked([]); setAsking(false); return }
        const entry = { target: m.target, label: m.label }
        setPicked(prev => m.shift
          ? prev.some(p => p.target === m.target) ? prev.filter(p => p.target !== m.target) : [...prev, entry]
          : [entry])
      } else if (m.dw === 'key') onKey(m)
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [toFrame, onKey])

  // Overlay links open sessions; the markup is the conductor's, so delegate.
  const overlayLayer = useRef<HTMLDivElement>(null)
  const openRef = useRef(openSession)
  openRef.current = openSession
  useEffect(() => {
    const el = overlayLayer.current
    if (!el) return
    const onClick = (e: MouseEvent) => {
      const key = (e.target as HTMLElement).closest('[data-open-session]')?.getAttribute('data-open-session')
      if (key) { e.preventDefault(); openRef.current(key) }
    }
    el.addEventListener('click', onClick)
    return () => el.removeEventListener('click', onClick)
  }, [load])
  useLayoutEffect(() => { nudgeApart(overlayLayer.current, '[data-testid="dashboard-overlay-item"][data-place]') })

  const askBox = useRef<HTMLTextAreaElement>(null)
  useLayoutEffect(() => { if (asking) askBox.current?.focus() }, [asking])

  const approvals = messages.filter(m => m.role === 'permission' && !m.meta?.resolved && m.meta?.approval_id)
  const empty = load === 'ready' && !dash.html.trim()
  const pinned = overlay.map(item => ({ item, rect: item.anchor ? rects[anchorTarget(item.anchor)] ?? null : null }))
  const tray = pinned.filter(p => !p.rect)
  const firstPick = picked.map(p => rects[p.target]).find(Boolean) ?? null
  const pageAsks = dash.asks.filter(a => !a.targets.length || !rects[a.targets[0]])

  const askForm = (placeholder: string, big = false) => (
    <div className={big ? 'w-full' : 'w-[340px] max-w-[90vw] rounded-xl border border-border bg-bg-elevated p-2.5 shadow-xl'} data-testid="dashboard-ask">
      <textarea ref={askBox} rows={big ? 3 : 2} value={askText} data-testid="dashboard-ask-input" aria-label="Ask the conductor"
        placeholder={placeholder}
        className="w-full resize-none rounded-lg border border-border bg-bg px-2.5 py-2 text-sm text-text outline-none focus:border-accent"
        onChange={e => setAskText(e.target.value)}
        onKeyDown={e => {
          e.stopPropagation()
          if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ask(askText) }
          if (e.key === 'Escape') { setAsking(false); setAskText('') }
        }} />
      <div className="mt-1.5 flex items-center justify-between text-[11px] text-muted">
        <span>{picked.length ? `About ${picked.length === 1 ? 'this element' : `${picked.length} elements`}` : 'About the whole page'}</span>
        <span>Enter to send · Esc to close</span>
      </div>
    </div>
  )

  const toolBtn = 'flex h-9 w-9 items-center justify-center rounded-lg text-muted hover:bg-bg-hover hover:text-text data-[on=true]:bg-accent-subtle data-[on=true]:text-accent disabled:opacity-40'

  return (
    <div className="relative flex-1 min-h-0 overflow-hidden outline-none" data-testid="dashboard-pane" data-dashboard-root="" tabIndex={-1} role="application" aria-label="Dashboard"
      onKeyDown={e => { if (!(e.target as HTMLElement).closest('textarea, input, [contenteditable="true"]') && onKey({ key: e.key, meta: e.metaKey, ctrl: e.ctrlKey, alt: e.altKey, shift: e.shiftKey })) e.preventDefault() }}>
      <style>{PANE_CSS}</style>
      {typeof load === 'object' && (
        <div className="p-6"><ErrorNotice title={i18nT('pages.chat.dashboardPane.load_failed')} message={load.failed} /></div>
      )}

      {/* The object: the conductor's page. */}
      {url && !empty && (
        <iframe ref={frame} title={title || 'Dashboard'} src={url} sandbox={AGENT_PAGE_SANDBOX} data-testid="dashboard-frame"
          className="absolute inset-0 h-full w-full border-0" style={{ background: 'var(--bg)' }} />
      )}
      {mintFailed && !url && (
        <div className="p-6">
          <ErrorNotice title={i18nT('pages.chat.dashboardPane.frame_failed')} message="" footer={(
            <button type="button" className="mt-2 rounded-md border border-border bg-bg-elevated px-3 py-1 text-sm hover:bg-bg-hover" onClick={retryMint}>Try again</button>
          )} />
        </div>
      )}

      {/* A blank dashboard: a placeholder with the ask box as the only thing on it. */}
      {empty && (
        <div className="absolute inset-0 flex items-center justify-center p-6" data-testid="dashboard-empty"
          style={{ backgroundImage: 'radial-gradient(circle, color-mix(in srgb, var(--text) 12%, transparent) 1px, transparent 1.2px)', backgroundSize: '24px 24px' }}>
          <div className="w-full max-w-[520px] rounded-2xl border border-dashed border-border bg-bg-elevated p-6 text-text shadow-sm">
            <div className="mb-1 flex items-center gap-2 text-base font-semibold"><Sparkles size={16} className="text-accent" /> A blank dashboard</div>
            <p className="mb-4 text-sm text-muted">Describe what this page should be, and the conductor builds it. Then pick any part of it to talk about that part.</p>
            {askForm('A board of my open PRs with CI status, grouped by repo…', true)}
          </div>
        </div>
      )}

      {/* Picking: the hovered element and the picked ones, outlined over the page. */}
      <div className="pointer-events-none absolute inset-0" style={{ zIndex: 5 }}>
        {mode === 'pick' && hover && !picked.some(p => p.target === hover.target) && (
          <div className="absolute" data-testid="dashboard-hover" style={{ left: hover.rect.x - 2, top: hover.rect.y - 2, width: hover.rect.w + 4, height: hover.rect.h + 4, border: '1.5px dashed var(--accent)', borderRadius: 6 }}>
            <span className="absolute -top-5 left-0 max-w-[260px] truncate rounded bg-accent px-1.5 text-[11px] leading-[18px] text-[var(--bg)]">{hover.target.startsWith('el:') ? hover.target.slice(3) : hover.label || 'element'}</span>
          </div>
        )}
        {picked.map(p => {
          const r = rects[p.target]
          return r ? <div key={p.target} data-testid="dashboard-picked" className="absolute" style={{ left: r.x - 2, top: r.y - 2, width: r.w + 4, height: r.h + 4, border: '2px solid var(--accent)', borderRadius: 6, background: 'color-mix(in srgb, var(--accent) 8%, transparent)' }} /> : null
        })}
      </div>

      {/* The state: the conductor's overlay, pinned to page elements. */}
      <div ref={overlayLayer} className="pointer-events-none absolute inset-0" data-testid="dashboard-overlay" style={{ zIndex: 6 }}>
        {pinned.filter(p => p.rect).map(({ item, rect }) => (
          <div key={item.key} data-testid="dashboard-overlay-item" data-place={item.place} className="pointer-events-auto absolute"
            style={{ ...placeAround(item.place, rect!), maxWidth: 300, fontSize: 12.5, lineHeight: 1.45, color: 'var(--text)' }}
            // Sanitized by parseOverlay: scripts, handlers, forms and loads are gone.
            dangerouslySetInnerHTML={{ __html: item.html }} />
        ))}
        {tray.length > 0 && (
          <div data-testid="dashboard-overlay-tray" className="pointer-events-auto absolute right-4 top-16 flex w-[260px] flex-col gap-1.5">
            {tray.map(({ item }) => <div key={item.key} data-testid="dashboard-overlay-item" dangerouslySetInnerHTML={{ __html: item.html }} />)}
          </div>
        )}
      </div>

      {/* Answers: a pin on the element asked about; whole-page asks stack top-left. */}
      <div className="pointer-events-none absolute inset-0" style={{ zIndex: 7 }}>
        {dash.asks.map(a => {
          const r = a.targets[0] ? rects[a.targets[0]] : null
          const at = pageAsks.indexOf(a)
          const style = r ? { left: r.x + r.w - 14, top: r.y + r.h - 12 } : { left: 16, top: 64 + at * 38 }
          return (
            <div key={a.id} className="pointer-events-auto absolute" style={style}>
              <DocAnswer compact thread={views[a.id]} onOpen={() => markRead(a.id)} onSend={text => followUp(a.id, text)} onOpenSession={openSession}
                active={activeId === a.id} focusSignal={focusSignals[a.id] ?? 0} onActivate={() => setActiveId(a.id)} />
            </div>
          )
        })}
      </div>

      {/* The ask box: under the first picked element, or above the dock for the whole page. */}
      {asking && !empty && (
        firstPick
          ? <div className="absolute" style={{ left: Math.max(12, firstPick.x), top: firstPick.y + firstPick.h + 10, zIndex: 9 }}>{askForm('Ask the conductor about this…')}</div>
          : <div className="absolute bottom-20 left-1/2 -translate-x-1/2" style={{ zIndex: 9 }}>{askForm('Ask the conductor about the whole page…')}</div>
      )}

      {/* The floating header: the conversations, and notices. */}
      <div className="pointer-events-none absolute left-4 right-4 top-3 flex flex-col items-center gap-2" style={{ zIndex: 20 }}>
        <ConversationBar items={conversations} activeId={activeId} onSelect={focusConversation} />
        <div className="pointer-events-auto w-full max-w-[520px]">
          {conflict && (
            <ErrorNotice
              title={i18nT('pages.chat.docPane.conflict_title')}
              message={i18nT('pages.chat.docPane.conflict_message')}
              footer={(
                <div className="mt-2 flex gap-2">
                  <button type="button" className="rounded-md border border-border bg-bg-elevated px-3.5 py-1.5 text-sm hover:bg-bg-hover"
                    onClick={() => { versionRef.current = conflict.version; setConflict(null); dirtyRef.current = true; void save() }}>
                    {i18nT('pages.chat.docPane.keep_mine')}
                  </button>
                  <button type="button" className="rounded-md border border-border bg-bg-elevated px-3.5 py-1.5 text-sm hover:bg-bg-hover"
                    onClick={() => { versionRef.current = conflict.version; dirtyRef.current = false; const d = parseDashboard(conflict.content); dashRef.current = d; setDash(d); setConflict(null) }}>
                    {i18nT('pages.chat.docPane.take_theirs')}
                  </button>
                </div>
              )}
            />
          )}
          <ErrorNotice title={i18nT('pages.chat.docPane.save_failed')} message={saveError} />
          <ErrorNotice title={i18nT('pages.chat.docPane.send_failed')} message={sendError} />
        </div>
      </div>

      {/* The tool dock. */}
      {!empty && load === 'ready' && (
        <div className="absolute bottom-5 left-1/2 flex -translate-x-1/2 items-center gap-0.5 rounded-xl border border-border bg-bg-elevated p-1 shadow-lg" style={{ zIndex: 25 }} data-testid="dashboard-dock">
          <button type="button" className={toolBtn} data-on={mode === 'pick'} aria-label="Pick elements" title="Pick elements (V)" aria-keyshortcuts="V" onClick={() => setMode('pick')}><MousePointer2 size={18} /></button>
          <button type="button" className={toolBtn} data-on={mode === 'use'} aria-label="Use the page" title="Use the page (I)" aria-keyshortcuts="I" onClick={() => setMode('use')}><MousePointerClick size={18} /></button>
          <button type="button" className={toolBtn} data-on={asking} aria-label="Ask the conductor" title={picked.length ? 'Ask about the picked elements (C)' : 'Ask about the whole page (C)'} aria-keyshortcuts="C" onClick={() => setAsking(a => !a)} data-testid="dashboard-ask-tool"><MessageSquarePlus size={18} /></button>
          <span className="mx-1 h-5 w-px bg-border" />
          <button type="button" className={toolBtn} aria-label="Undo" title="Undo the last page change (⌘Z)" disabled={!dash.past.length} onClick={undo}><Undo2 size={18} /></button>
          <button type="button" className={toolBtn} aria-label="Redo" title="Redo (⇧⌘Z)" disabled={!dash.future.length} onClick={redo}><Redo2 size={18} /></button>
        </div>
      )}

      {sessionOpen && <SessionMessagesPanel slotKey={slotKey} onClose={() => setSessionOpen(false)} />}
      <ConductorControls slotKey={slotKey} approvalMode={approvalMode} sessionOpen={sessionOpen} onToggleSession={() => setSessionOpen(o => !o)}
        className="absolute bottom-5 left-5 rounded-xl border border-border bg-bg-elevated p-1 shadow-lg" style={{ zIndex: 25 }} testid="dashboard-approval-mode" />
      {approvals.length > 0 && (
        <div className="absolute bottom-20 right-5 flex w-[380px] max-w-[90vw] flex-col gap-2" style={{ zIndex: 30 }} data-testid="dashboard-approvals">
          {approvals.map(m => {
            const id = String(m.meta?.approval_id)
            return (
              <ApprovalCard key={id} title={m.content} toolInput={String(m.meta?.tool_input ?? '')} showButtons showTrust={false}
                onApprove={decision => api.resolveApproval(id, toApiDecision(decision))} />
            )
          })}
        </div>
      )}
      {/* The workers' tool approvals are decided only on their conductor's page: this one. */}
      <div className="absolute bottom-20 left-5 w-[400px] max-w-[90vw]" style={{ zIndex: 30 }} data-testid="dashboard-worker-approvals">
        <CrewApprovalCards root={slotKey} />
      </div>
    </div>
  )
}
