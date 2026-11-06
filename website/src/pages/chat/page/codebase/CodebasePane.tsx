/**
 * The pane of a codebase session (PoC): a 3D model of a work tree in place of a chat.
 *
 * The session file (a doc's file) holds where the model points and the asks; the
 * model itself is the scan, re-read every few seconds so edits show up as they land.
 * The slot runs the conductor agent, and pointing at the model is the only way to
 * talk to it: pick buildings or districts, press C, and ask. Its answers pin to what
 * was picked, and its status overlay pins to files and folders.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Boxes, Crosshair, FolderOpen, GitBranch, Network, RefreshCw, Sparkles, X } from 'lucide-react'
import { api, ApiError } from '../../../../api/client'
import ApprovalCard from '../../../../components/ApprovalCard'
import { ConductorControls, SessionMessagesPanel } from '../ConductorSession'
import ProjectPicker from '../../../../components/ProjectPicker'
import CrewApprovalCards from '../../../../components/crew/CrewApprovalCards'
import ErrorNotice from '../../../../components/ErrorNotice'
import { sendTurn } from '../../../../chat-core/transport/sendTurn'
import { fmtNumber } from '../../../../i18n/format'
import { i18nT } from '../../../../i18n/t'
import { useAppSelector } from '../../../../store'
import { selectSlotMessages } from '../../../../store/chatSlice'
import { slotRowByKey } from '../../../../store/slotIndex'
import { toApiDecision } from '../../../../utils/approvalDecision'
import DocAnswer from '../DocAnswer'
import { ANSWER_STYLE, type ThreadView } from '../docConductor'
import { OVERLAY_CLASS_STYLE, OVERLAY_KEYFRAMES, overlaySlug, parseOverlay, type OverlayItem } from '../docOverlay'
import { deriveThreads } from '../docThreads'
import { specToCss } from '../styleSpec'
import { newId } from '../board/boardModel'
import CodebaseScene, { type SceneApi } from './CodebaseScene'
import {
  decodeScan, langColor, nodeLabel, parseCodebaseDoc, serializeCodebaseDoc,
  type CodeModel, type CodebaseDoc,
} from './codebaseModel'
import { buildCodebaseTurn, codebaseChangesFromMessages } from './codebaseTurn'

const SCAN_POLL_MS = 6000
const OVERLAY_POLL_MS = 3000

const SCOPE = '[data-codebase-root]'
const PANE_CSS = [
  specToCss(ANSWER_STYLE, SCOPE),
  specToCss(OVERLAY_CLASS_STYLE, SCOPE),
  '@keyframes cm-ask-pulse{0%,100%{opacity:1}50%{opacity:.35}}',
  OVERLAY_KEYFRAMES,
  `@media (prefers-reduced-motion: reduce){${SCOPE} [data-da="dot"],${SCOPE} .ov-status::before{animation:none!important}}`,
].join('\n')

const isTyping = (t: EventTarget | null) => t instanceof HTMLElement && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))

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

/** Paths that are new, or whose line count moved, between two scans. */
export function changedPaths(before: CodeModel | null, after: CodeModel): Set<string> {
  const out = new Set<string>()
  if (!before || before.root !== after.root) return out
  for (const f of after.files) {
    const old = before.byPath.get(f.path)
    if (!old || old.lines !== f.lines) out.add(f.path)
  }
  return out
}

function Segmented<T extends string>({ value, options, onChange, testid }: { value: T; options: { id: T; label: string; icon?: React.ReactNode }[]; onChange: (v: T) => void; testid: string }) {
  return (
    <div className="flex rounded-lg border border-border bg-bg p-0.5" data-testid={testid}>
      {options.map(o => (
        <button key={o.id} type="button" onClick={() => onChange(o.id)} aria-pressed={value === o.id}
          className={`flex items-center gap-1 rounded-md px-2 py-1 text-xs ${value === o.id ? 'bg-bg-elevated text-text shadow-sm' : 'text-muted hover:text-text'}`}>
          {o.icon}{o.label}
        </button>
      ))}
    </div>
  )
}

/** `onOpenSession` opens a session the model links to; the page switches slots, never a pane. */
export default function CodebasePane({ docId, slotKey, title, approvalMode, project, onOpenSession }: { docId: string; slotKey: string; title?: string; approvalMode?: string; project?: string; onOpenSession: (key: string) => void }) {
  const [sessionOpen, setSessionOpen] = useState(false)
  const [load, setLoad] = useState<'loading' | 'ready' | { failed: string }>('loading')
  const [doc, setDoc] = useState<CodebaseDoc>(() => parseCodebaseDoc(''))
  const docRef = useRef(doc)
  docRef.current = doc
  const versionRef = useRef<string | null>(null)
  const [saveError, setSaveError] = useState('')
  const [sendError, setSendError] = useState('')

  const save = useCallback(async (next: CodebaseDoc) => {
    try {
      const saved = await api.saveDoc(docId, serializeCodebaseDoc(next), versionRef.current)
      versionRef.current = saved.version
      setSaveError('')
    } catch (err) {
      // This file is only settings and asks: the newest write wins.
      if (err instanceof ApiError && err.status === 409) {
        try {
          const current = JSON.parse(err.body) as { version?: string }
          versionRef.current = current.version ?? null
          const again = await api.saveDoc(docId, serializeCodebaseDoc(next), versionRef.current)
          versionRef.current = again.version
          return
        } catch { /* fall through to the notice */ }
      }
      setSaveError(err instanceof Error ? err.message : String(err))
    }
  }, [docId])
  const update = useCallback((patch: Partial<CodebaseDoc>) => {
    const next = { ...docRef.current, ...patch }
    docRef.current = next
    setDoc(next)
    void save(next)
  }, [save])

  useEffect(() => {
    let cancelled = false
    api.getDoc(docId).then(file => {
      if (cancelled) return
      versionRef.current = file.version
      const d = parseCodebaseDoc(file.content)
      docRef.current = d
      setDoc(d)
      setLoad('ready')
      // A new session starts on its project.
      if (!d.root && project) update({ root: project })
    }, err => { if (!cancelled) setLoad({ failed: err instanceof Error ? err.message : String(err) }) })
    return () => { cancelled = true }
    // `update` and `project` only seed a brand-new session.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [docId])

  // ---- the scan ---------------------------------------------------------------------------
  const [model, setModel] = useState<CodeModel | null>(null)
  const modelRef = useRef<CodeModel | null>(null)
  const [changed, setChanged] = useState<Set<string>>(new Set())
  const [scanError, setScanError] = useState('')
  const [scanning, setScanning] = useState(false)
  const scanBusy = useRef(false)
  const scanAgain = useRef(false)
  const scannedRoot = useRef('')
  const rescan = useCallback(async (force = false) => {
    const root = docRef.current.root
    if (!root) return
    // One scan at a time; one asked for meanwhile (a new root, a turn ending) runs next.
    if (scanBusy.current) { scanAgain.current = true; return }
    scanBusy.current = true
    setScanning(true)
    try {
      const prev = modelRef.current
      const r = await api.codebaseScan(root, !force && prev && scannedRoot.current === root ? prev.sig : undefined)
      // The root moved while this was in flight: its result is for a tree no longer drawn.
      if (r.unchanged || docRef.current.root !== root) return
      const next = decodeScan(r)
      setChanged(changedPaths(prev, next))
      modelRef.current = next
      scannedRoot.current = root
      setModel(next)
      setScanError('')
    } catch (err) {
      let message = err instanceof Error ? err.message : String(err)
      if (err instanceof ApiError) { try { message = (JSON.parse(err.body) as { error?: string }).error ?? message } catch { /* keep */ } }
      if (docRef.current.root === root) setScanError(message)
    } finally {
      scanBusy.current = false
      setScanning(false)
      if (scanAgain.current) { scanAgain.current = false; void rescan(scannedRoot.current !== docRef.current.root) }
    }
  }, [])
  useEffect(() => {
    if (load !== 'ready' || !doc.root) return
    void rescan(true)
    const timer = setInterval(() => { if (document.visibilityState === 'visible') void rescan() }, SCAN_POLL_MS)
    return () => clearInterval(timer)
  }, [load, doc.root, rescan])

  // ---- the conductor ------------------------------------------------------------------------
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
  const markRead = (id: string) => {
    const replies = threads[id]?.messages.filter(m => m.who === 'conductor').length ?? 0
    if ((readCounts[id] ?? 0) !== replies) setReadCounts({ ...readCounts, [id]: replies })
  }
  const openSession = onOpenSession

  const send = (id: string, request: string, nodeIds: string[], followUp: boolean) => {
    if (!modelRef.current) return
    setSendError('')
    const message = buildCodebaseTurn({ id, request, model: modelRef.current, selected: nodeIds, followUp, title, docId })
    void sendTurn({ message, slot: slotKey }).then(r => {
      if (r.status === 'refused') setSendError(r.reason || i18nT('pages.chatPage.send_failed'))
      else if (r.status === 'transport-error') setSendError(i18nT('pages.chatPage.send_failed'))
    })
  }

  // A finished reply's new root (a worktree it made) and camera focus, applied once.
  const sceneApi = useRef<SceneApi | null>(null)
  const [applied, setApplied] = useLocalRecord<{ keys: string[] }>(`docwork.applied.${docId}`, { keys: [] })
  useEffect(() => {
    if (running || load !== 'ready') return
    const done = new Set(applied.keys)
    const fresh = codebaseChangesFromMessages(messages).filter(c => !done.has(c.key))
    if (!fresh.length) return
    for (const c of fresh) {
      if (c.root && c.root !== docRef.current.root) update({ root: c.root })
      if (c.focus?.length) { const ids = c.focus; setTimeout(() => sceneApi.current?.focus(ids), 300) }
    }
    setApplied({ keys: [...applied.keys, ...fresh.map(c => c.key)] })
    // A turn ending is when workers' edits usually land: look again now.
    void rescan()
  }, [messages, running, load, applied, setApplied, update, rescan])

  // The conductor's overlay, read while the pane is open.
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

  // ---- picking and asking ----------------------------------------------------------------
  const [selected, setSelected] = useState<string[]>([])
  const [asking, setAsking] = useState(false)
  const [draft, setDraft] = useState('')
  const askInput = useRef<HTMLInputElement>(null)
  const onPick = useCallback((id: string | null, additive: boolean) => {
    setAsking(false)
    if (!id) { if (!additive) setSelected([]); return }
    setSelected(cur => additive ? (cur.includes(id) ? cur.filter(x => x !== id) : [...cur, id]) : [id])
  }, [])
  const submitAsk = () => {
    const text = draft.trim()
    if (!text || !selected.length) return
    const id = newId('a')
    update({ asks: [...docRef.current.asks, { id, nodeIds: selected }] })
    send(id, text, selected, false)
    setDraft('')
    setAsking(false)
    setSelected([])
  }
  useEffect(() => { if (asking) askInput.current?.focus() }, [asking])
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (isTyping(e.target) || e.metaKey || e.ctrlKey || e.altKey) return
      if (e.key === 'c' || e.key === 'C') { if (selected.length) { e.preventDefault(); setAsking(true) } }
      else if (e.key === 'f' || e.key === 'F') { e.preventDefault(); if (selected.length) sceneApi.current?.focus(selected); else sceneApi.current?.fit() }
      else if (e.key === 'Escape') { setAsking(false); setSelected([]) }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [selected])

  const approvals = messages.filter(m => m.role === 'permission' && !m.meta?.resolved && m.meta?.approval_id)
  const anchored = overlay.filter(o => o.anchor && /^(file|dir):/.test(o.anchor))
  const tray = overlay.filter(o => !o.anchor || !/^(file|dir):/.test(o.anchor))
  const legend = useMemo(() => {
    if (!model) return []
    const counts = new Map<string, number>()
    for (const f of model.files) counts.set(f.lang, (counts.get(f.lang) ?? 0) + 1)
    return [...counts].sort((a, b) => b[1] - a[1]).slice(0, 8)
  }, [model])
  const totalLines = model?.dirs.get('.')?.lines ?? 0
  const [rootDraft, setRootDraft] = useState('')
  // The path box: for a new session with no project, when the root cannot be read
  // (a session's default project need not be a git checkout), or on request.
  const [pickingRoot, setPickingRoot] = useState(false)
  const [browsing, setBrowsing] = useState(false)
  const browseRef = useRef<HTMLButtonElement>(null)
  const rootFailed = !!scanError && !model
  const showRootForm = load === 'ready' && (!doc.root || pickingRoot || rootFailed)
  const openRootForm = () => { setRootDraft(doc.root); setPickingRoot(true) }
  const chooseRoot = (path: string) => {
    setPickingRoot(false)
    setScanError('')
    update({ root: path })
    // The same path again is a retry: the root did not change, so look now.
    void rescan(true)
  }
  useEffect(() => { if (rootFailed && !rootDraft) setRootDraft(doc.root) }, [rootFailed, rootDraft, doc.root])
  const repoName = (model?.root || doc.root).split('/').filter(Boolean).pop() ?? ''

  return (
    <div className="relative flex-1 min-h-0 overflow-hidden" data-testid="codebase-pane" data-codebase-root="">
      <style>{PANE_CSS}</style>
      {sessionOpen && <SessionMessagesPanel slotKey={slotKey} onClose={() => setSessionOpen(false)} />}
      {typeof load === 'object' && (
        <div className="p-6"><ErrorNotice title={i18nT('pages.chat.codebasePane.load_failed')} message={load.failed} /></div>
      )}
      {showRootForm && (
        <div className="absolute inset-0 flex items-center justify-center p-6" style={{ zIndex: 35 }}>
          <form className="w-full max-w-[480px] rounded-xl border border-border bg-bg-elevated p-5 shadow-lg" data-testid="codebase-root-form"
            onSubmit={e => { e.preventDefault(); if (rootDraft.trim()) chooseRoot(rootDraft.trim()) }}>
            <div className="mb-1 flex items-center gap-2 text-sm font-medium text-text"><Boxes size={16} /> Which codebase?</div>
            <p className="mb-3 text-xs text-muted">The absolute path of a git checkout. Refactors happen in a worktree, never in it.</p>
            {rootFailed && !pickingRoot && (
              <p className="mb-3 rounded-md border border-border bg-bg px-2.5 py-2 text-xs text-text" data-testid="codebase-root-error">
                Could not read <code className="break-all">{doc.root}</code>: {scanError}
              </p>
            )}
            <div className="flex gap-2">
              <input value={rootDraft} onChange={e => setRootDraft(e.target.value)} placeholder="/Users/you/code/project" autoFocus
                className="min-w-0 flex-1 rounded-md border border-border bg-bg px-2.5 py-1.5 text-sm text-text outline-none focus:border-accent" aria-label="Codebase path" />
              <button type="button" ref={browseRef} onClick={() => setBrowsing(true)} data-testid="codebase-root-browse"
                className="flex shrink-0 items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-sm text-text hover:bg-bg-hover">
                <FolderOpen size={14} /> Browse…
              </button>
              <button type="submit" className="rounded-md bg-accent px-3 py-1.5 text-sm text-accent-fg">Open</button>
              {model && (
                <button type="button" onClick={() => setPickingRoot(false)} className="rounded-md border border-border px-3 py-1.5 text-sm text-muted hover:text-text">Cancel</button>
              )}
            </div>
            {browsing && (
              <ProjectPicker open onOpenChange={o => { if (!o) setBrowsing(false) }} anchorRef={browseRef}
                startPath={rootDraft || doc.root}
                onSelect={path => { setBrowsing(false); setRootDraft(path); chooseRoot(path) }} />
            )}
          </form>
        </div>
      )}
      {model && (
        <CodebaseScene model={model} lens={doc.lens} colorBy={doc.colorBy} selected={selected} onPick={onPick} changed={changed} apiRef={sceneApi}>
          {anchored.map(item => (
            <div key={`o${item.key}`} data-node={item.anchor!} data-place={item.place === 'margin' ? 'top' : item.place} data-scene-ui="" data-testid="codebase-overlay-item"
              onPointerDown={e => e.stopPropagation()}
              onClick={e => { const k = (e.target as HTMLElement).closest('[data-open-session]')?.getAttribute('data-open-session'); if (k) openSession(k) }}
              dangerouslySetInnerHTML={{ __html: item.html }} />
          ))}
          {doc.asks.filter(a => views[a.id] && a.nodeIds.length).map(a => (
            <div key={a.id} data-node={a.nodeIds[0]} data-place="right" data-scene-ui="" data-testid="codebase-answer" onPointerDown={e => e.stopPropagation()}>
              <DocAnswer compact thread={views[a.id]} onOpen={() => markRead(a.id)} onSend={text => send(a.id, text, a.nodeIds, true)} onOpenSession={openSession} />
            </div>
          ))}
        </CodebaseScene>
      )}

      {/* The header: what is drawn, and how. */}
      {load === 'ready' && doc.root && (
        <div className="pointer-events-none absolute left-4 right-4 top-4 flex items-start gap-2" style={{ zIndex: 20 }}>
          <div className="pointer-events-auto flex flex-wrap items-center gap-2 rounded-xl border border-border bg-bg-elevated p-1.5 shadow-lg" data-testid="codebase-toolbar">
            <Segmented testid="codebase-lens" value={doc.lens} onChange={lens => update({ lens })}
              options={[{ id: 'city', label: 'City', icon: <Boxes size={13} /> }, { id: 'graph', label: 'Graph', icon: <Network size={13} /> }]} />
            <Segmented testid="codebase-color" value={doc.colorBy} onChange={colorBy => update({ colorBy })}
              options={[{ id: 'language', label: 'Language' }, { id: 'recency', label: 'Recently changed' }]} />
            <button type="button" onClick={openRootForm} title={`${doc.root} · click to open another codebase`} data-testid="codebase-branch"
              className="flex items-center gap-1 rounded-md px-1.5 py-1 text-xs text-muted hover:bg-bg-hover hover:text-text">
              <GitBranch size={13} /> {repoName}{model?.branch ? ` · ${model.branch}` : ''}{model ? ` · ${fmtNumber(model.files.length)} files · ${fmtNumber(totalLines)} lines` : ''}
            </button>
            <button type="button" title="Rescan" aria-label="Rescan" onClick={() => void rescan(true)} className="rounded-md p-1.5 text-muted hover:bg-bg-hover hover:text-text">
              <RefreshCw size={14} className={scanning ? 'animate-spin' : ''} />
            </button>
            <button type="button" title="Fit (F)" aria-label="Fit" onClick={() => sceneApi.current?.fit()} className="rounded-md p-1.5 text-muted hover:bg-bg-hover hover:text-text">
              <Crosshair size={14} />
            </button>
          </div>
          <div className="flex-1" />
          <ConductorControls slotKey={slotKey} approvalMode={approvalMode} sessionOpen={sessionOpen} onToggleSession={() => setSessionOpen(o => !o)}
            className="pointer-events-auto rounded-xl border border-border bg-bg-elevated p-1 shadow-lg" testid="codebase-approval-mode" />
        </div>
      )}

      {/* Notices and the overlay items with no node. */}
      <div className="pointer-events-none absolute right-4 top-20 flex w-[300px] flex-col gap-2" style={{ zIndex: 20 }}>
        <div className="pointer-events-auto flex flex-col gap-2">
          <ErrorNotice title={i18nT('pages.chat.codebasePane.scan_failed')} message={rootFailed ? '' : scanError} />
          <ErrorNotice title={i18nT('pages.chat.docPane.save_failed')} message={saveError} />
          <ErrorNotice title={i18nT('pages.chat.docPane.send_failed')} message={sendError} />
          {tray.map(item => (
            <div key={`t${item.key}`} data-testid="codebase-overlay-tray-item"
              onClick={e => { const k = (e.target as HTMLElement).closest('[data-open-session]')?.getAttribute('data-open-session'); if (k) openSession(k) }}
              dangerouslySetInnerHTML={{ __html: item.html }} />
          ))}
        </div>
      </div>

      {/* The legend. */}
      {model && (
        <div className="pointer-events-none absolute bottom-5 left-5 rounded-xl border border-border bg-bg-elevated px-3 py-2 text-[11px] text-muted shadow-lg" style={{ zIndex: 15 }} data-testid="codebase-legend">
          {doc.colorBy === 'language' ? (
            <div className="flex max-w-[360px] flex-wrap gap-x-3 gap-y-1">
              {legend.map(([lang, n]) => (
                <span key={lang} className="flex items-center gap-1"><span className="inline-block h-2.5 w-2.5 rounded-sm" style={{ background: langColor(lang) }} />{lang} {n}</span>
              ))}
            </div>
          ) : (
            <div className="flex items-center gap-2">older <span className="inline-block h-2 w-28 rounded" style={{ background: 'linear-gradient(90deg,#6b7a99,#e8b24a 85%,#f0553c)' }} /> newest</div>
          )}
          <div className="mt-1">{doc.lens === 'city' ? 'Height and footprint: lines of code' : 'Size: lines · links: imports between folders'} · click to pick, C to ask</div>
        </div>
      )}

      {/* What is picked, and the ask. */}
      {selected.length > 0 && (
        <div className="absolute bottom-5 left-1/2 w-[min(560px,90vw)] -translate-x-1/2 rounded-xl border border-border bg-bg-elevated p-2 shadow-xl" style={{ zIndex: 25 }} data-testid="codebase-pick-bar">
          {!asking ? (
            <div className="flex items-center gap-2">
              <span className="min-w-0 flex-1 truncate px-1 text-xs text-text" title={selected.join('\n')}>
                {selected.slice(0, 3).map(nodeLabel).join(', ')}{selected.length > 3 ? ` +${selected.length - 3}` : ''}
              </span>
              <button type="button" data-testid="codebase-ask-button" onClick={() => setAsking(true)} title="Ask conductor (C)" aria-keyshortcuts="C"
                className="flex items-center gap-1.5 rounded-lg bg-accent px-2.5 py-1.5 text-xs font-medium text-accent-fg">
                <Sparkles size={14} /> Ask conductor <kbd className="ml-0.5 rounded border border-current/40 px-1 text-[10px] opacity-80">C</kbd>
              </button>
              <button type="button" aria-label="Clear" title="Clear (Esc)" onClick={() => setSelected([])} className="rounded-md p-1.5 text-muted hover:bg-bg-hover hover:text-text"><X size={14} /></button>
            </div>
          ) : (
            <form className="flex items-center gap-2" onSubmit={e => { e.preventDefault(); submitAsk() }}>
              <Sparkles size={14} className="ml-1 shrink-0 text-accent" />
              <input ref={askInput} value={draft} onChange={e => setDraft(e.target.value)} data-testid="codebase-ask-input"
                onKeyDown={e => { if (e.key === 'Escape') { e.stopPropagation(); setAsking(false) } }}
                placeholder={`Ask about ${selected.slice(0, 2).map(nodeLabel).join(', ')}${selected.length > 2 ? '…' : ''}`}
                className="min-w-0 flex-1 bg-transparent px-1 py-1 text-sm text-text outline-none" aria-label="Ask the conductor" />
              <button type="submit" disabled={!draft.trim()} className="rounded-lg bg-accent px-2.5 py-1.5 text-xs font-medium text-accent-fg disabled:opacity-50">Send</button>
            </form>
          )}
        </div>
      )}

      {approvals.length > 0 && (
        <div className="absolute bottom-24 right-5 w-[380px] max-w-[90vw] flex flex-col gap-2" style={{ zIndex: 30 }} data-testid="codebase-approvals">
          {approvals.map(m => {
            const id = String(m.meta?.approval_id)
            return (
              <ApprovalCard key={id} title={m.content} toolInput={String(m.meta?.tool_input ?? '')} showButtons showTrust={false}
                onApprove={decision => api.resolveApproval(id, toApiDecision(decision))} />
            )
          })}
        </div>
      )}
      {/* The workers' tool approvals: Crew Mode decides those only on their conductor's page. */}
      <div className="absolute bottom-24 left-5 w-[400px] max-w-[90vw]" style={{ zIndex: 30 }} data-testid="codebase-worker-approvals">
        <CrewApprovalCards root={slotKey} />
      </div>
    </div>
  )
}
