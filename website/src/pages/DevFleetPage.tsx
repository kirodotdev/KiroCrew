/**
 * Dev Fleet — worktree management page ported to KiroCrew SPA.
 * Manages git worktrees, pod instances, syncing, pruning, and rebasing.
 */
import { useState, useRef, useCallback, useEffect, type CSSProperties, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Card, CardTitle, Btn, Checkbox, StatCard, EmptyState, ContentSkeleton, PageHeader, SearchInput, Badge, Select } from '../components/ui'
import InfoTip from '../components/InfoTip'
import Modal from '../components/Modal'
import Clickable from '../components/Clickable'
import { useAppDispatch } from '../store'
import { addNotification } from '../store/notificationsSlice'
import {
  Server, RefreshCw, Play, Square, ExternalLink, ChevronRight, Trash2,
  LoaderCircle, Check,
  Ellipsis, RotateCw, FileText, GitCommit, Rocket,
} from 'lucide-react'
import * as api from './devFleetApi'

/* ─── Notification helper (replaces useNotify) ─── */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
let _dispatch: any = null

type Toast = { id: number; msg: string; type: 'success' | 'error' | 'info' }
const _toastListeners = new Set<(t: Toast) => void>()
let _toastSeq = 1

function notify(msg: string, opts?: { type?: 'success' | 'error' | 'info' }) {
  const t: Toast = { id: _toastSeq++, msg, type: opts?.type || 'info' }
  _toastListeners.forEach((fn) => fn(t))
  if (!_dispatch) return
  _dispatch(addNotification({
    ts: String(Date.now()),
    title: msg,
    body: '',
    kind: opts?.type === 'error' ? 'error' : opts?.type === 'success' ? 'success' : 'info',
  }))
}

/* ─── Constants ─── */
const POLL_MS = 12000
const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms))

/* ─── Sync phase stepper model (marker protocol) ─── */
// Rough progress mapping for the 5 backend sync steps (fetch/merge/pip/npm ci/
// build), weighted by typical duration. Shown as a single coarse percentage --
// per-step labels were dropped (they implied more precision than we have).
const SYNC_STEP_CUM = [0, 5, 8, 25, 55, 100] as const
const SYNC_TOTAL_STEPS = 5
const STEP_MARKER_RE = /^::step::(\d+)::(.+)$/

function syncPhaseFromLines(lines: string[], prev: number): number {
  let p = prev
  for (const l of lines) {
    const m = STEP_MARKER_RE.exec(l)
    if (m) {
      const idx = parseInt(m[1], 10)
      p = Math.max(p, idx)
    }
  }
  return p
}

function filterStepMarkers(lines: string[]): string[] {
  return lines.filter((l) => !STEP_MARKER_RE.test(l))
}

function syncPercent(phase: number, phaseAtMs: number | undefined): number {
  const p = Math.min(Math.max(phase, 0), SYNC_TOTAL_STEPS)
  const base = SYNC_STEP_CUM[p]
  if (p >= SYNC_TOTAL_STEPS) return 100
  const next = SYNC_STEP_CUM[p + 1]
  const creep = phaseAtMs ? Math.min(next - base - 2, Math.floor((Date.now() - phaseAtMs) / 4000)) : 0
  return Math.min(96, base + Math.max(0, creep))
}

function fmtElapsed(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000))
  return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0')
}

function relTime(epoch: number | null | undefined): string {
  if (!epoch) return ''
  const s = Math.max(0, Math.floor(Date.now() / 1000 - epoch))
  if (s < 60) return 'just now'
  const m = Math.floor(s / 60); if (m < 60) return m + 'm ago'
  const h = Math.floor(m / 60); if (h < 24) return h + 'h ago'
  const d = Math.floor(h / 24); if (d < 30) return d + 'd ago'
  return Math.floor(d / 30) + 'mo ago'
}

function iconLabel(icon: ReactNode, label: string) {
  return <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5 } as CSSProperties}>{icon}{label}</span>
}

/* ─── Sub-components ─── */
interface MenuItemDef { label: string; icon?: ReactNode; onClick: () => void; disabled?: boolean; danger?: boolean; title?: string }
// Row-actions dropdown geometry. The menu is portaled to <body> so a row's
// <Card overflow> can't clip it (issue #146); these drive fixed positioning.
const MENU_GAP = 6        // gap between trigger and menu (was `calc(100% + 6px)`)
const MENU_MARGIN = 8     // min gap from the viewport edge
const MENU_ITEM_H = 32    // estimated per-item height for the flip decision
const MENU_PAD = 8        // container vertical padding (4px top + 4px bottom)
function MenuBtn({ items }: { items: (MenuItemDef | null)[] }) {
  const [open, setOpen] = useState(false)
  // Trigger rect captured on open; drives the portaled menu's fixed position.
  const [rect, setRect] = useState<DOMRect | null>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const visible = items.filter(Boolean) as MenuItemDef[]

  useEffect(() => {
    if (!open) return
    // The menu is portaled to <body>, so it is no longer a DOM descendant of
    // the trigger — the outside-click guard must exclude BOTH the trigger and
    // the menu (a plain trigger.contains() check would close on every menu
    // click). Escape closes and returns focus to the trigger.
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node
      if (!triggerRef.current?.contains(t) && !menuRef.current?.contains(t)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') { setOpen(false); triggerRef.current?.focus() } }
    // position:fixed desyncs from any scrolling ancestor — close on scroll
    // (capture phase catches nested scrollers) and on resize.
    const onScrollOrResize = () => setOpen(false)
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    window.addEventListener('scroll', onScrollOrResize, true)
    window.addEventListener('resize', onScrollOrResize)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
      window.removeEventListener('scroll', onScrollOrResize, true)
      window.removeEventListener('resize', onScrollOrResize)
    }
  }, [open])

  const toggle = () => {
    if (!open && triggerRef.current) setRect(triggerRef.current.getBoundingClientRect())
    setOpen((o) => !o)
  }

  // Right-align the menu's right edge to the trigger (as before), clamped so it
  // never sits flush against the viewport edge. Open downward by default; flip
  // up when there isn't room below for the estimated height and there's more
  // room above. Either `top` or `bottom` is set (never both) + maxHeight so the
  // menu is always clamped inside the viewport.
  const estH = visible.length * MENU_ITEM_H + MENU_PAD
  const spaceBelow = rect ? window.innerHeight - rect.bottom - MENU_GAP : 0
  const spaceAbove = rect ? rect.top - MENU_GAP : 0
  const openUp = !!rect && spaceBelow < estH + MENU_MARGIN && spaceAbove > spaceBelow
  const avail = Math.max(80, (openUp ? spaceAbove : spaceBelow) - MENU_MARGIN)
  const posStyle: CSSProperties = rect
    ? {
        position: 'fixed',
        right: Math.max(MENU_MARGIN, window.innerWidth - rect.right),
        ...(openUp
          ? { bottom: window.innerHeight - rect.top + MENU_GAP }
          : { top: rect.bottom + MENU_GAP }),
        maxHeight: avail,
      }
    : { position: 'fixed' }

  return (
    <span style={{ display: 'inline-flex' } as CSSProperties}>
      <Btn ref={triggerRef} onClick={toggle} title="More actions" aria-label="More actions" aria-haspopup="menu" aria-expanded={open}>
        <Ellipsis size={15} className="lucide-inline" />
      </Btn>
      {open && rect && createPortal(
        <div
          ref={menuRef}
          role="menu"
          aria-label="More actions"
          data-placement={openUp ? 'up' : 'down'}
          style={{ ...posStyle, zIndex: 4000, overflowY: 'auto', background: 'var(--card, #16161a)', border: '1px solid var(--border)', borderRadius: 10, padding: 4, minWidth: 168, boxShadow: '0 8px 24px rgba(0,0,0,0.45)' } as CSSProperties}
        >
          {visible.map((item, i) => (
            <Clickable
              key={'mi' + i}
              onClick={() => { setOpen(false); item.onClick() }}
              disabled={!!item.disabled}
              style={{ display: 'flex', alignItems: 'center', gap: 8, width: '100%', textAlign: 'left' as const, background: 'none', border: 'none', borderRadius: 7, padding: '7px 10px', fontSize: 12, color: item.danger ? 'var(--danger)' : 'var(--text)', cursor: item.disabled ? 'default' : 'pointer', opacity: item.disabled ? 0.5 : 1 } as CSSProperties}
            >
              {item.icon || null}{item.label}
            </Clickable>
          ))}
        </div>,
        document.body,
      )}
    </span>
  )
}

interface ConfirmBtnProps { title: string; desc: string; confirmLabel?: string; onConfirm: () => void; btn?: Record<string, unknown>; children: ReactNode }
function ConfirmBtn({ title, desc, confirmLabel, onConfirm, btn, children }: ConfirmBtnProps) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLSpanElement>(null)
  return (
    <span ref={ref} style={{ position: 'relative', display: 'inline-flex' } as CSSProperties}>
      <Btn {...(btn || {})} onClick={() => setOpen(!open)}>{children}</Btn>
      {open && (
        <div style={{ position: 'absolute', top: 'calc(100% + 6px)', right: 0, zIndex: 1200, background: 'var(--card, #16161a)', border: '1px solid var(--border)', borderRadius: 10, padding: '10px 12px', width: 264, boxShadow: '0 8px 24px rgba(0,0,0,0.45)', textAlign: 'left' as const } as CSSProperties}>
          <div style={{ fontSize: 12.5, fontWeight: 600, marginBottom: 4 }}>{title}</div>
          <div style={{ fontSize: 11.5, color: 'var(--muted)', lineHeight: 1.5, marginBottom: 9 }}>{desc}</div>
          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' } as CSSProperties}>
            <Btn onClick={() => setOpen(false)}>Cancel</Btn>
            <Btn primary onClick={() => { setOpen(false); onConfirm() }}>{confirmLabel || 'Start'}</Btn>
          </div>
        </div>
      )}
    </span>
  )
}

/* ─── Types ─── */
interface IssueRef { number: number; url?: string | null }
interface TicketRef { id: string; url?: string | null }
interface PrInfo { number?: number; state?: string; url?: string; isDraft?: boolean; title?: string }
interface Worktree {
  name: string; branch?: string; is_main?: boolean; running?: boolean
  has_dist?: boolean; dirty?: boolean; port?: number; health?: number; behind?: number
  last_updated_at?: number
  pr?: PrInfo | null; shipped?: boolean
  issues?: IssueRef[]; tickets?: TicketRef[]; summary?: string | null
  own_commits?: number; real_dirty?: boolean; is_live?: boolean; legacy?: boolean
  path?: string
}
interface FleetData { worktrees: Worktree[]; error?: string; sync_run_id?: string; build_pending?: boolean; gateway_service_active?: boolean }
interface SyncRun { rid: string; status: 'running' | 'done' | 'error'; phase: number; phaseAt?: number; lines: string[]; startedAt: number; exit?: number | null; last?: string }
interface RebaseResult { kind: 'ok' | 'conflict' | 'error'; text: string }

/* ─── Detail Panel (expanded row) ─── */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function DetailPanel({ w, d, busy, onRemove, onLoadLogs, logs, logsLoading }: { w: Worktree; d: any; busy: Record<string, boolean>; onRemove: () => void; onLoadLogs: () => void; logs?: string; logsLoading?: boolean }) {
  const mono: CSSProperties = { fontFamily: 'ui-monospace, SF Mono, Menlo, monospace', fontSize: 11.5 }
  const mutedSm: CSSProperties = { fontSize: 11, color: 'var(--muted)', lineHeight: 1.6 }
  const [logsOpen, setLogsOpen] = useState(false)
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      <div style={mutedSm}>Branch: <span style={{ ...mono, color: 'var(--text)' }}>{d.branch || '?'}</span></div>
      {d.pr ? (
        <div style={mutedSm}>
          PR: <a href={d.pr.url || '#'} target="_blank" rel="noopener noreferrer" title={d.pr.title || undefined} style={{ color: 'var(--accent)' }}>
            #{d.pr.number || '?'}{d.pr.title ? ' \u2014 ' + d.pr.title : ''}
          </a>{' '}
          <Badge variant={d.pr.state === 'MERGED' ? 'aim' : d.pr.state === 'OPEN' ? 'ok' : 'warn'}>
            {(d.pr.state || '').toLowerCase()}
          </Badge>
        </div>
      ) : null}
      {d.summary ? (
        <div style={mutedSm}>Purpose: <span style={{ color: 'var(--text)' }}>{d.summary}</span></div>
      ) : null}
      {d.issues?.length > 0 ? (
        <div style={mutedSm}>
          Issues:{' '}
          {d.issues.map((it: IssueRef, i: number) => (
            it.url
              ? <a key={i} href={it.url} target="_blank" rel="noopener noreferrer" style={{ color: 'var(--accent)', marginRight: 8 }}>#{it.number}</a>
              : <span key={i} style={{ color: 'var(--text)', marginRight: 8 }}>#{it.number}</span>
          ))}
        </div>
      ) : null}
      {d.tickets?.length > 0 ? (
        <div style={mutedSm}>
          Tickets:{' '}
          {d.tickets.map((t: TicketRef, i: number) => (
            t.url
              ? <a key={i} href={t.url} target="_blank" rel="noopener noreferrer" style={{ color: 'var(--accent)', marginRight: 8 }}>{t.id}</a>
              : <span key={i} style={{ color: 'var(--text)', marginRight: 8 }}>{t.id}</span>
          ))}
        </div>
      ) : null}
      {d.design_docs?.length > 0 ? (
        <div style={mutedSm}>
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}><FileText size={11} className="lucide-inline" /> Design docs:</span>
          <ul style={{ margin: '2px 0 0 16px', padding: 0, listStyle: 'none' }}>
            {d.design_docs.map((doc: string, i: number) => <li key={i} style={mono}>{doc}</li>)}
          </ul>
        </div>
      ) : null}
      {d.commits?.length > 0 ? (
        <div style={mutedSm}>
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}><GitCommit size={11} className="lucide-inline" /> Commits:</span>
          <ul style={{ margin: '2px 0 0 16px', padding: 0, listStyle: 'none' }}>
            {d.commits.map((c: { hash: string; subject: string; when: string }, i: number) => (
              <li key={i} style={{ ...mono, display: 'flex', gap: 8 }}>
                <span style={{ color: 'var(--accent)', flexShrink: 0 }}>{c.hash}</span>
                <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{c.subject}</span>
                <span style={{ color: 'var(--muted)', flexShrink: 0 }}>{c.when}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {d.disk_mb != null ? <div style={mutedSm}>Disk: {d.disk_mb} MB</div> : null}
      {d.pod_running ? (
        <div style={mutedSm}>
          Pod: running on :{d.pod_port || '?'}
        </div>
      ) : null}
      <div style={{ display: 'flex', gap: 8, marginTop: 4 }}>
        {d.pod_running ? (
          <Btn onClick={() => { if (!logsOpen) { setLogsOpen(true); onLoadLogs() } else setLogsOpen(false) }} disabled={!!logsLoading}>
            {iconLabel(<FileText size={12} className="lucide-inline" />, logsLoading ? 'Loading\u2026' : logsOpen ? 'Hide logs' : 'Load pod logs')}
          </Btn>
        ) : null}
        {!w.is_main ? (
          <Btn danger onClick={onRemove} disabled={!!busy[w.name + ':remove']}>
            {iconLabel(<Trash2 size={13} className="lucide-inline" />, 'Remove')}
          </Btn>
        ) : null}
      </div>
      {logsOpen && logs ? (
        <pre style={{ margin: '4px 0 0', padding: '8px 10px', maxHeight: 200, overflow: 'auto', fontSize: 11, lineHeight: 1.45, background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>{logs}</pre>
      ) : null}
    </div>
  )
}

/* ═══════════ Main component ═══════════ */
function ToastHost() {
  const [toasts, setToasts] = useState<Toast[]>([])
  useEffect(() => {
    const on = (t: Toast) => {
      setToasts((ts) => [...ts, t])
      window.setTimeout(() => setToasts((ts) => ts.filter((x) => x.id !== t.id)), t.type === 'error' ? 7000 : 4000)
    }
    _toastListeners.add(on)
    return () => { _toastListeners.delete(on) }
  }, [])
  if (!toasts.length) return null
  return (
    <div role="status" aria-live="polite" style={{ position: 'fixed', top: 14, left: '50%', transform: 'translateX(-50%)', zIndex: 9997, display: 'flex', flexDirection: 'column', gap: 6, alignItems: 'center', pointerEvents: 'none' } as CSSProperties}>
      {toasts.map((t) => (
        <div key={t.id} style={{ background: 'var(--card)', color: 'var(--card-fg)', border: '1px solid ' + (t.type === 'error' ? 'var(--danger)' : t.type === 'success' ? 'var(--ok)' : 'var(--border)'), borderRadius: 8, padding: '7px 14px', fontSize: 12.5, boxShadow: '0 4px 14px rgba(0,0,0,0.25)', maxWidth: 520 } as CSSProperties}>
          {t.msg}
        </div>
      ))}
    </div>
  )
}

export default function DevFleetPage() {
  const dispatch = useAppDispatch()
  _dispatch = dispatch
  const queryClient = useQueryClient()

  /* ─── react-query: fleet data ─── */
  const { data: fleet, isLoading: loading, error: fleetError } = useQuery<FleetData>({
    queryKey: ['dev-fleet', 'fleet'],
    queryFn: () => api.get<FleetData>('/fleet'),
    refetchInterval: POLL_MS,
  })

  /* ─── react-query: disk data ─── */
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const { data: disk } = useQuery<any>({
    queryKey: ['dev-fleet', 'disk'],
    queryFn: () => api.get('/disk'),
    refetchInterval: 30000,
  })

  const invalidateFleet = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ['dev-fleet', 'fleet'] })
  }, [queryClient])
  const invalidateAll = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ['dev-fleet'] })
  }, [queryClient])

  const [busy, setBusy] = useState<Record<string, boolean>>({})
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const [detail, setDetail] = useState<Record<string, any>>({})
  const [detailLoading, setDetailLoading] = useState<Record<string, boolean>>({})
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const [prov, setProv] = useState<Record<string, any>>({})
  const [rebaseResult, setRebaseResult] = useState<Record<string, RebaseResult>>({})
  const [podLogs, setPodLogs] = useState<Record<string, string>>({})
  const [podLogsLoading, setPodLogsLoading] = useState<Record<string, boolean>>({})
  const rebaseTimersRef = useRef<Record<string, ReturnType<typeof setTimeout>>>({})
  const [q, setQ] = useState('')
  const [sortBy, setSortBy] = useState('status')
  const [showLegacy, setShowLegacy] = useState(false)
  const [syncRun, setSyncRun] = useState<SyncRun | null>(null)
  const [syncLogOpen, setSyncLogOpen] = useState(false)
  const syncAttachedRef = useRef(false)
  // Poll-loop lifecycle: loops exit when the component unmounts or a run is
  // explicitly dismissed — otherwise navigation would leak up-to-900-request
  // closures, and dismissing the stepper would be undone by the next tick.
  const pollAliveRef = useRef(true)
  const cancelledRunsRef = useRef<Set<string>>(new Set())
  useEffect(() => { pollAliveRef.current = true; return () => { pollAliveRef.current = false } }, [])
  function dismissSync(rid?: string) {
    if (rid) cancelledRunsRef.current.add(rid)
    setSyncRun(null); setSyncLogOpen(false)
  }
  const [confirmReq, setConfirmReq] = useState<{ title: string; desc: ReactNode; confirmLabel?: string; danger?: boolean; width?: number; resolve: (v: boolean) => void } | null>(null)
  const [restarting, setRestarting] = useState(false)
  const [pruneDialog, setPruneDialog] = useState<{ candidates: { name: string; code?: string }[]; kept: { name: string; code?: string }[]; scanned: number } | null>(null)
  const [pruneSelected, setPruneSelected] = useState<Set<string>>(new Set())
  const [pruneProgress, setPruneProgress] = useState<{ total: number; done: number; current: string | null; results: { name: string; ok?: boolean; error?: string }[] } | null>(null)
  const askConfirm = (title: string, desc: ReactNode, opts?: { confirmLabel?: string; danger?: boolean; width?: number }) => new Promise<boolean>((resolve) => setConfirmReq({ title, desc, ...(opts || {}), resolve }))
  const settleConfirm = (val: boolean) => setConfirmReq((c) => { if (c) c.resolve(val); return null })

  const setFlag = (k: string, v: boolean) => setBusy((b) => ({ ...b, [k]: v }))

  function showRebaseResult(name: string, res: RebaseResult) {
    setRebaseResult((m) => ({ ...m, [name]: res }))
    clearTimeout(rebaseTimersRef.current[name])
    rebaseTimersRef.current[name] = setTimeout(() => setRebaseResult((m) => { const n = { ...m }; delete n[name]; return n }), res.kind === 'ok' ? 15000 : 60000)
  }
  function dismissRebaseResult(name: string) { clearTimeout(rebaseTimersRef.current[name]); setRebaseResult((m) => { const n = { ...m }; delete n[name]; return n }) }

  /* ─── Sync reattach on page load (v0.6.0) ─── */
  useEffect(() => {
    if (!fleet?.sync_run_id || syncAttachedRef.current) return
    syncAttachedRef.current = true
    const rid = fleet.sync_run_id
    api.get<{ status?: string; output?: string[]; started?: number; step?: number }>('/run?id=' + rid)
      .then((run) => {
        if (run?.status === 'running') {
          const t0 = run.started ? run.started * 1000 : Date.now()
          setSyncRun({ rid, status: 'running', phase: typeof run.step === 'number' ? run.step : syncPhaseFromLines(run.output || [], 0), lines: run.output || [], startedAt: t0 })
          pollSyncRun(rid, t0)
        }
      })
      .catch(() => { /* run endpoint unreachable — nothing to reattach */ })
  }, [fleet?.sync_run_id]) // eslint-disable-line react-hooks/exhaustive-deps

  /* ─── Tick for elapsed counter ─── */
  const [, setTick] = useState(0)
  useEffect(() => {
    if (!syncRun || syncRun.status !== 'running') return
    const t = setInterval(() => setTick((n) => n + 1), 1000)
    return () => clearInterval(t)
  }, [syncRun?.status]) // eslint-disable-line react-hooks/exhaustive-deps

  async function pollSyncRun(rid: string, startedAt: number) {
    let phase = 0
    let phaseAt = Date.now()
    for (let i = 0; i < 900; i++) {
      await sleep(2000)
      if (!pollAliveRef.current || cancelledRunsRef.current.has(rid)) return
      let run: { status?: string; output?: string[]; exit_code?: number; started?: number; step?: number } | null = null
      let gone = false
      try { run = await api.get('/run?id=' + rid) } catch (e) {
        // 404 = the gateway restarted and dropped the run registry — the run
        // is unrecoverable; freezing the bar forever was a real user trap.
        if ((e as { status?: number })?.status === 404) gone = true
        else continue
      }
      if (gone || !run) {
        if (gone) {
          setSyncRun({ rid, status: 'error', phase: 0, lines: [], startedAt, last: 'gateway restarted mid-sync — run lost; check git state and re-run Pull+Build' })
          setFlag('__syncmain', false)
          notify('Sync run lost (gateway restarted mid-sync). Re-run Pull+Build.', { type: 'error' })
          return
        }
        continue
      }
      const out = run.output || []
      const t0 = run.started ? run.started * 1000 : startedAt
      const prevPhase = phase
      // Prefer the server-tracked step (survives the 60-line output window a
      // chatty build floods) and fall back to marker lines still in view.
      phase = typeof run.step === 'number' ? Math.max(phase, run.step) : syncPhaseFromLines(out, phase)
      if (phase !== prevPhase) phaseAt = Date.now()
      const last = [...out].reverse().find((l) => l?.trim() && !STEP_MARKER_RE.test(l)) || ''
      if (run.status === 'done' || run.status === 'timeout') {
        const okRun = run.exit_code === 0
        setSyncRun({ rid, status: okRun ? 'done' : 'error', phase: okRun ? SYNC_TOTAL_STEPS : phase, lines: out, startedAt: t0, exit: run.exit_code, last })
        setFlag('__syncmain', false)
        if (okRun) notify('Synced \u2014 restart gateway to apply the new build.', { type: 'success' })
        else notify('Pull+Build failed (exit ' + run.exit_code + '): ' + last, { type: 'error' })
        invalidateFleet()
        return
      }
      setSyncRun({ rid, status: 'running', phase, phaseAt, lines: out, startedAt: t0, last })
    }
    setSyncRun((s) => (s && s.rid === rid ? { ...s, status: 'error', last: 'timed out after 30 min' } : s))
    setFlag('__syncmain', false)
  }

  async function toggleExpand(name: string) {
    const open = !expanded[name]; setExpanded((e) => ({ ...e, [name]: open }))
    if (open && !detail[name] && !detailLoading[name]) {
      setDetailLoading((d) => ({ ...d, [name]: true }))
      try { const dd = await api.get('/worktree?name=' + encodeURIComponent(name)); setDetail((d) => ({ ...d, [name]: dd })) }
      catch (e: unknown) { setDetail((d) => ({ ...d, [name]: { error: (e as Error)?.message || String(e) } })) }
      finally { setDetailLoading((d) => ({ ...d, [name]: false })) }
    }
  }

  async function act(name: string, kind: string) {
    const flag = name + ':' + kind; setFlag(flag, true)
    try {
      if (kind === 'open') {
        // Open synchronously while browser user-activation is still valid,
        // then point the window at the pod URL once the token arrives.
        // Sever opener immediately — pod frontend is worktree code under
        // test and must not be able to navigate the live dashboard tab.
        const w = window.open('about:blank', '_blank')
        if (w) w.opener = null
        const r = await api.post<{ ok?: boolean; url?: string; error?: string }>('/pod/token', { name })
        if (r?.ok && r.url) { if (w) w.location.href = r.url; else window.open(r.url, '_blank', 'noopener') }
        else { w?.close(); notify(r?.error || 'Token mint failed', { type: 'error' }) }
      }
      else if (kind === 'up') { notify('Starting pod for ' + name + '\u2026 (can take ~1 min)', { type: 'info' }); const r = await api.post<{ ok?: boolean; error?: string }>('/pod/up', { name }); notify(r?.ok ? 'Pod up: ' + name : (r?.error || 'Pod start failed'), { type: r?.ok ? 'success' : 'error' }); invalidateFleet() }
      else if (kind === 'down') { const r = await api.post<{ ok?: boolean; error?: string }>('/pod/down', { name }); notify(r?.ok ? 'Stopped ' + name : (r?.error || 'Failed'), { type: r?.ok ? 'success' : 'error' }); invalidateFleet() }
      else if (kind === 'restart') { const r = await api.post<{ ok?: boolean; error?: string }>('/pod/restart', { name }); notify(r?.ok ? 'Restarted ' + name : (r?.error || 'Failed'), { type: r?.ok ? 'success' : 'error' }); invalidateFleet() }
    } catch (e: unknown) { notify((e as Error)?.message || String(e), { type: 'error' }) }
    finally { setFlag(flag, false) }
  }

  async function provision(name: string) {
    setProv((p) => ({ ...p, [name]: { status: 'starting', last: 'starting\u2026' } }))
    try {
      const r = await api.post<{ ok?: boolean; run_id?: string }>('/pod/provision', { name })
      if (!r?.ok || !r.run_id) { notify('Provision failed', { type: 'error' }); setProv((p) => ({ ...p, [name]: null })); return }
      const rid = r.run_id
      for (let i = 0; i < 900; i++) {
        await sleep(2000)
        if (!pollAliveRef.current) return
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        let run: any = null; try { run = await api.get('/run?id=' + rid) } catch { continue }
        if (!run) continue; setProv((p) => ({ ...p, [name]: { status: run.status, last: (run.output || []).slice(-1)[0] || '' } }))
        if (run.status === 'done') { notify(run.exit_code === 0 ? 'Provisioned' : 'Provision failed (exit ' + run.exit_code + ')', { type: run.exit_code === 0 ? 'success' : 'error' }); setProv((p) => ({ ...p, [name]: null })); invalidateFleet(); return }
        if (run.status !== 'running') { notify(run.status === 'timeout' ? 'Provision timed out' : 'Provision failed (' + run.status + ')', { type: 'error' }); setProv((p) => ({ ...p, [name]: null })); invalidateFleet(); return }
      }
      // Poll budget exhausted (e.g. run id lost across a gateway restart):
      // clear the chip so the retry control reappears, and refresh state.
      notify('Provision polling timed out \u2014 check pod logs', { type: 'error' })
      setProv((p) => ({ ...p, [name]: null }))
      invalidateFleet()
    } catch (e: unknown) { notify((e as Error)?.message || String(e), { type: 'error' }); setProv((p) => ({ ...p, [name]: null })) }
  }

  async function removeWorktree(name: string, d: Worktree) {
    if (d?.is_main) { notify('Cannot remove the main worktree', { type: 'error' }); return }
    const shipped = !!d?.shipped; const empty = d && d.own_commits === 0 && d.real_dirty === false
    const desc = shipped ? 'PR merged \u2014 safe to remove. Runs `git worktree remove`. Cannot be undone.' : empty ? 'Empty worktree. Cannot be undone.' : 'Has unmerged work \u2014 removing DELETES permanently.'
    const ok = await askConfirm('Remove "' + name + '"?', desc, { confirmLabel: shipped || empty ? 'Remove' : 'Delete anyway', danger: true })
    if (!ok) return
    setFlag(name + ':remove', true)
    try { const r = await api.post<{ ok?: boolean; error?: string }>('/worktree/remove', { name, force: !shipped && !empty }); if (r?.ok) { notify('Removed ' + name, { type: 'success' }); invalidateAll() } else notify(r?.error || 'Failed', { type: 'error' }) }
    catch (e: unknown) { notify((e as Error)?.message || String(e), { type: 'error' }) }
    finally { setFlag(name + ':remove', false) }
  }

  async function syncMain() {
    setFlag('__syncmain', true)
    try {
      const r = await api.post<{ ok?: boolean; run_id?: string; error?: string }>('/sync', {})
      if (!r?.ok || !r.run_id) { notify(r?.error || 'Pull+Build failed to start', { type: 'error' }); setFlag('__syncmain', false); return }
      setSyncRun({ rid: r.run_id, status: 'running', phase: 0, lines: [], startedAt: Date.now() })
      pollSyncRun(r.run_id, Date.now())
    } catch (e: unknown) { notify((e as Error)?.message || String(e), { type: 'error' }); setFlag('__syncmain', false) }
  }

  async function rebaseWorktree(name: string) {
    const ok = await askConfirm('Rebase "' + name + '"?', 'Fetches latest main and replays. Refused if dirty; on conflict rebase is aborted.', { confirmLabel: 'Rebase' })
    if (!ok) return; setFlag(name + ':rebase', true)
    try {
      const r = await api.post<{ ok?: boolean; head?: string; ahead?: number; behind?: number; conflict?: boolean; error?: string }>('/rebase', { name })
      if (r?.ok) { const txt = 'Rebased (HEAD ' + (r.head || '?').slice(0, 7) + ')'; showRebaseResult(name, { kind: 'ok', text: txt }); notify(txt, { type: 'success' }) }
      else if (r?.conflict) { showRebaseResult(name, { kind: 'conflict', text: 'Conflicts \u2014 aborted' }); notify('Rebase conflicts', { type: 'error' }) }
      else { showRebaseResult(name, { kind: 'error', text: r?.error || 'failed' }); notify(r?.error || 'Rebase failed', { type: 'error' }) }
      invalidateFleet()
    } catch (e: unknown) { notify((e as Error)?.message || String(e), { type: 'error' }) }
    finally { setFlag(name + ':rebase', false) }
  }

  async function pruneShipped() {
    setFlag('__prune', true)
    try {
      const r = await api.get<{ ok?: boolean; candidates?: { name: string; code?: string }[]; kept?: { name: string; code?: string }[]; scanned?: number; error?: string }>('/prune-candidates')
      if (!r || r.ok === false) { notify(r?.error || 'Prune preview failed', { type: 'error' }); return }
      const cands = r.candidates || []
      const kept = r.kept || []
      if (!cands.length && !kept.length) { notify('Nothing to prune', { type: 'info' }); return }
      setPruneSelected(new Set(cands.map((c: { name: string }) => c.name)))
      setPruneDialog({ candidates: cands, kept, scanned: r.scanned || 0 })
    } catch (e: unknown) { notify((e as Error)?.message || String(e), { type: 'error' }) }
    finally { setFlag('__prune', false) }
  }

  async function pruneExecute(names: string[]) {
    if (!names.length) { notify('Nothing selected', { type: 'info' }); return }
    setPruneDialog(null)
    setPruneProgress({ total: names.length, done: 0, current: null, results: [] })
    try {
      await api.post('/prune-run', { names })
      for (let i = 0; i < 400; i++) {
        await sleep(1500)
        if (!pollAliveRef.current) return
        let st: { running?: boolean; done?: number; current?: string | null; results?: { name: string; ok?: boolean; error?: string }[] } | null = null
        try { st = await api.get('/prune-status') } catch { continue }
        if (!st) continue
        setPruneProgress({ total: names.length, done: st.done || 0, current: st.current || null, results: st.results || [] })
        if (!st.running) {
          const removed = (st.results || []).filter((r) => r.ok).length
          const failed = (st.results || []).filter((r) => !r.ok).length
          notify(removed > 0 ? `Pruned ${removed} worktree(s)` + (failed > 0 ? ` (${failed} failed)` : '') : `Prune: ${failed} failed`, { type: removed > 0 ? 'success' : 'error' })
          invalidateAll()
          setTimeout(() => setPruneProgress(null), 5000)
          return
        }
      }
      setPruneProgress(null)  // poll budget exhausted without completion
    } catch (e: unknown) {
      notify((e as Error)?.message || String(e), { type: 'error' })
      setPruneProgress(null)
    }
  }

  async function restartGateway() {
    const ok = await askConfirm('Restart gateway?', 'Applies the last Pull+Build. The dashboard will briefly disconnect.', { confirmLabel: 'Restart' })
    if (!ok) return
    setRestarting(true)
    try {
      const r = await api.post<{ ok?: boolean; error?: string }>('/restart-gateway', {})
      if (!r?.ok) { notify(r?.error || 'Restart failed', { type: 'error' }); setRestarting(false); return }
      const ctrl = new AbortController()
      const deadline = Date.now() + 60000
      await sleep(3000)
      while (Date.now() < deadline) {
        try {
          await fetch('/', { signal: AbortSignal.timeout(3000) })
          window.location.reload()
          return
        } catch { /* still restarting */ }
        await sleep(2000)
      }
      ctrl.abort()
      setRestarting(false)
      notify('Gateway still restarting after 60s \u2014 reload manually', { type: 'error' })
    } catch (e: unknown) { notify((e as Error)?.message || String(e), { type: 'error' }); setRestarting(false) }
  }

  async function makeLive(w: Worktree) {
    // Only the already-live row is blocked. Main is a valid target when it is
    // NOT live (after a cutover to a feature worktree, this is the way back).
    if (w.is_live) return
    if (!w.path) { notify('Cannot resolve worktree path for ' + w.name, { type: 'error' }); return }
    const ok = await askConfirm('Make "' + w.name + '" live?',
      'Swaps the code behind the live dashboard to this worktree (same port, same data). The gateway restarts and this page reconnects automatically. Refused unless the worktree is provisioned and built.',
      { confirmLabel: 'Make live' })
    if (!ok) return
    setFlag(w.name + ':makelive', true)
    try {
      const r = await api.post<{ ok?: boolean; error?: string }>('/make-live', { path: w.path })
      if (!r?.ok) { notify(r?.error || 'Make live failed', { type: 'error' }); setFlag(w.name + ':makelive', false); return }
      // Gateway is restarting into the new worktree — reuse the restart overlay,
      // poll until it answers again, then reload into the freshly-live code.
      setRestarting(true)
      await sleep(3000)
      const deadline = Date.now() + 60000
      while (Date.now() < deadline) {
        try { await fetch('/', { signal: AbortSignal.timeout(3000) }); window.location.reload(); return } catch { /* still restarting */ }
        await sleep(2000)
      }
      setRestarting(false); setFlag(w.name + ':makelive', false)
      notify('Gateway still restarting after 60s \u2014 reload manually', { type: 'error' })
    } catch (e: unknown) { notify((e as Error)?.message || String(e), { type: 'error' }); setRestarting(false); setFlag(w.name + ':makelive', false) }
  }

  async function loadPodLogs(name: string) {
    setPodLogsLoading((l) => ({ ...l, [name]: true }))
    try {
      const r = await api.get<{ ok?: boolean; logs?: string; error?: string }>('/pod/logs?name=' + encodeURIComponent(name) + '&n=100')
      if (r?.ok) setPodLogs((l) => ({ ...l, [name]: r.logs || '(empty)' }))
      else notify(r?.error || 'Failed to load logs', { type: 'error' })
    } catch (e: unknown) { notify((e as Error)?.message || String(e), { type: 'error' }) }
    finally { setPodLogsLoading((l) => ({ ...l, [name]: false })) }
  }

  /* ─── Render ─── */
  const wts = fleet?.worktrees || []
  const running = wts.filter((w) => w.running).length
  const needsProv = wts.filter((w) => !w.is_main && !w.has_dist).length
  const error = fleetError ? (fleetError as Error).message : fleet?.error || null
  const isDiscoveryError = !fleetError && !!fleet?.error
  const ql = q.trim().toLowerCase()
  const matchesRow = (w: Worktree) => !ql || (w.name + ' ' + (w.branch || '')).toLowerCase().includes(ql)
  const statusRank = (w: Worktree) => (w.is_main ? 0 : w.running ? 1 : (!w.has_dist ? 3 : 2))
  const mainRows = wts.filter((w) => w.is_main)
  const legacyAll = wts.filter((w) => !w.is_main && w.legacy)
  const others = wts.filter((w) => !w.is_main && matchesRow(w) && (showLegacy || !w.legacy))
  others.sort((a, b) => sortBy === 'name'
    ? a.name.localeCompare(b.name)
    : sortBy === 'recent'
      ? ((b.last_updated_at || 0) - (a.last_updated_at || 0)) || a.name.localeCompare(b.name)
      : sortBy === 'behind'
        ? ((b.behind || 0) - (a.behind || 0)) || a.name.localeCompare(b.name)
        : (statusRank(a) - statusRank(b)) || a.name.localeCompare(b.name))
  const visible = [...mainRows, ...others]

  const reviewState = (w: Worktree) => {
    if (!w.pr) return null
    const s = String(w.pr?.state || '').toUpperCase()
    if (s === 'MERGED') return { word: 'merged', variant: 'aim' as const }
    if (s === 'DRAFT' || w.pr?.isDraft) return { word: 'draft', variant: 'warn' as const }
    if (s === 'OPEN') return { word: 'open', variant: 'ok' as const }
    if (s === 'CLOSED') return { word: 'closed', variant: 'err' as const }
    return { word: '\u2026', variant: 'warn' as const }
  }

  function stateDot(w: Worktree) {
    let variant: 'ok' | 'err' | 'warn' | 'aim' | 'muted', label: string, title: string
    if (w.is_main) { variant = 'aim'; label = 'main'; title = 'The primary checkout this fleet is discovered from' }
    else if (w.running) {
      // 200 = open; 401/403 = serving but auth-gated — all mean the pod is up
      // (matches pod/runtime.py health() contract; anonymous probes get 403).
      const healthy = !!w.health && ((w.health >= 200 && w.health < 400) || w.health === 401 || w.health === 403)
      variant = healthy ? 'ok' : 'err'
      label = healthy ? 'pod up' : 'pod sick'
      title = healthy ? 'QA pod is running — click Open to use it' : 'QA pod is running but failing its health check'
    }
    else if (!w.has_dist) { variant = 'muted'; label = 'not built'; title = 'No venv/UI build yet — Provision builds this worktree so a pod can run' }
    else { variant = 'muted'; label = 'ready'; title = 'Built and ready — spin up a pod from the row menu to QA this branch' }
    return <Badge variant={variant} className="text-[10.5px] px-1.5 py-0" title={title}>{label}</Badge>
  }

  function rowButtons(w: Worktree): ReactNode[] {
    if (w.is_main) {
      const out: ReactNode[] = [
        <ConfirmBtn key="sync" title="Pull + Build main" desc="Pulls main and rebuilds (~6 min). Does NOT restart." confirmLabel="Start" onConfirm={() => syncMain()} btn={{ disabled: !!busy['__syncmain'] || syncRun?.status === 'running' }}>
          {iconLabel(<RefreshCw size={13} className="lucide-inline" />, busy['__syncmain'] || syncRun?.status === 'running' ? 'Building\u2026' : 'Pull+Build')}
        </ConfirmBtn>,
      ]
      if (fleet?.gateway_service_active) {
        out.push(
          <Btn key="restart" onClick={() => restartGateway()} aria-label="Restart gateway">
            {iconLabel(<RotateCw size={13} className="lucide-inline" />, 'Restart')}
          </Btn>
        )
        // After a cutover to a feature worktree, main is dormant (is_live=false)
        // and this inline control is the only way back to running main live.
        // Consistent with makeLive()'s guard: shown iff the row is NOT live.
        if (!w.is_live) {
          out.push(
            <Btn key="makelive" onClick={() => makeLive(w)} disabled={!!busy[w.name + ':makelive']} title="Repoint the live gateway back at main (restarts the gateway)">
              {iconLabel(<Rocket size={13} className="lucide-inline" />, 'Make live')}
            </Btn>
          )
        }
      }
      if (fleet?.build_pending) {
        out.push(<Badge key="bp" variant="warn">{'build pending \u2014 restart gateway to apply (kirocrew restart)'}</Badge>)
      }
      return out
    }
    const out: ReactNode[] = []
    if (!w.has_dist) {
      const pr = prov[w.name]
      out.push(pr
        ? <span key="p" style={{ fontSize: 11, color: 'var(--warn)', display: 'inline-flex', alignItems: 'center', gap: 4 } as CSSProperties}><LoaderCircle size={12} className="lucide-inline" />{pr.last || 'provisioning\u2026'}</span>
        : <Btn key="prov" onClick={() => provision(w.name)}>Provision</Btn>)
    } else if (w.running) {
      out.push(<Btn key="open" onClick={() => act(w.name, 'open')}>{iconLabel(<ExternalLink size={13} className="lucide-inline" />, 'Open')}</Btn>)
    }
    const podBusy = busy[w.name + ':up'] || busy[w.name + ':down'] || busy[w.name + ':restart']
    if (podBusy) out.push(<span key="podbusy" style={{ display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 11, color: 'var(--muted)' } as CSSProperties}><LoaderCircle size={12} className="lucide-inline" /> pod{"\u2026"}</span>)
    out.push(<MenuBtn key="menu" items={[
      w.has_dist && !w.running ? { label: 'Spin up pod', icon: <Play size={13} className="lucide-inline" />, onClick: () => act(w.name, 'up') } : null,
      w.running ? { label: 'Restart pod', icon: <RefreshCw size={13} className="lucide-inline" />, onClick: () => act(w.name, 'restart') } : null,
      { label: 'Rebase onto main', icon: <RefreshCw size={13} className="lucide-inline" />, onClick: () => rebaseWorktree(w.name), disabled: !!busy[w.name + ':rebase'] },
      !w.is_live ? { label: 'Make live', icon: <Rocket size={13} className="lucide-inline" />, onClick: () => makeLive(w), disabled: !!busy[w.name + ':makelive'], title: 'Repoint the live gateway at this worktree (restarts the gateway)' } : null,
      w.running ? { label: 'Stop pod', icon: <Square size={13} className="lucide-inline" />, onClick: () => act(w.name, 'down'), danger: true } : null,
    ]} />)
    const rr = rebaseResult[w.name]
    if (rr) out.push(<Clickable key="rr" aria-label="Dismiss" onClick={() => dismissRebaseResult(w.name)} style={{ fontSize: 11, color: rr.kind === 'ok' ? 'var(--ok)' : 'var(--danger)', cursor: 'pointer', maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', background: 'none', border: 'none', padding: 0 } as CSSProperties}>{rr.text}</Clickable>)
    return out
  }

  /* ─── Phase stepper (inline at main row) ─── */
  function renderSyncStepper() {
    if (!syncRun) return null
    const mono: CSSProperties = { fontFamily: 'ui-monospace, monospace', fontVariantNumeric: 'tabular-nums', fontSize: 11, color: 'var(--muted)' }
    if (syncRun.status === 'running') {
      const pct = syncPercent(syncRun.phase, syncRun.phaseAt)
      return (
        <div style={{ gridColumn: '4 / -1', display: 'flex', alignItems: 'center', gap: 10, minWidth: 0 } as CSSProperties}>
          <LoaderCircle size={12} className="lucide-inline" style={{ color: 'var(--accent)', flexShrink: 0 } as CSSProperties} />
          <span style={{ fontSize: 11, fontWeight: 600, flexShrink: 0 }}>Syncing</span>
          <span role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100} aria-label="Sync progress" style={{ flex: 1, height: 4, borderRadius: 2, background: 'var(--border)', overflow: 'hidden', minWidth: 60 } as CSSProperties}>
            <span style={{ display: 'block', height: '100%', width: pct + '%', background: 'var(--accent)', borderRadius: 2, transition: 'width 0.6s ease' } as CSSProperties} />
          </span>
          <span style={{ ...mono, flexShrink: 0 } as CSSProperties}>{'~' + pct + '%'}</span>
          <span style={mono}>{fmtElapsed(Date.now() - syncRun.startedAt)}</span>
          <Clickable aria-label="Toggle log" onClick={() => setSyncLogOpen((o) => !o)} style={{ background: 'none', border: 'none', color: 'var(--muted)', cursor: 'pointer', fontSize: 11, padding: 2 } as CSSProperties}>{syncLogOpen ? 'log \u25B4' : 'log \u25BE'}</Clickable>
          <Clickable aria-label="Dismiss sync status" onClick={() => dismissSync(syncRun?.rid)} style={{ background: 'none', border: 'none', color: 'var(--muted)', cursor: 'pointer', fontSize: 14, padding: 2 } as CSSProperties}>&times;</Clickable>
        </div>
      )
    }
    if (syncRun.status === 'done') {
      return (
        <div style={{ gridColumn: '4 / -1', display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 } as CSSProperties}>
          <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--ok)', display: 'inline-flex', alignItems: 'center', gap: 4 }}><Check size={12} className="lucide-inline" /> Synced</span>
          <span style={{ fontSize: 11, color: 'var(--muted)' }}>restart gateway to apply the new build</span>
          <span style={{ flex: 1 }} />
          <Clickable aria-label="Toggle log" onClick={() => setSyncLogOpen((o) => !o)} style={{ background: 'none', border: 'none', color: 'var(--muted)', cursor: 'pointer', fontSize: 11, padding: 2 } as CSSProperties}>{syncLogOpen ? 'log \u25B4' : 'log \u25BE'}</Clickable>
          <Clickable aria-label="Dismiss sync status" onClick={() => dismissSync(syncRun?.rid)} style={{ background: 'none', border: 'none', color: 'var(--muted)', cursor: 'pointer', fontSize: 14, padding: 2 } as CSSProperties}>&times;</Clickable>
        </div>
      )
    }
    // error
    return (
      <div style={{ gridColumn: '4 / -1', display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 } as CSSProperties}>
        <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--danger)' }}>Pull+Build failed</span>
        <span style={{ ...mono, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 } as CSSProperties} title={syncRun.last}>{syncRun.last}</span>
        <Clickable aria-label="Toggle log" onClick={() => setSyncLogOpen((o) => !o)} style={{ background: 'none', border: 'none', color: 'var(--muted)', cursor: 'pointer', fontSize: 11, padding: 2 } as CSSProperties}>{syncLogOpen ? 'log \u25B4' : 'log \u25BE'}</Clickable>
        <Clickable aria-label="Dismiss sync status" onClick={() => dismissSync(syncRun?.rid)} style={{ background: 'none', border: 'none', color: 'var(--muted)', cursor: 'pointer', fontSize: 14, padding: 2 } as CSSProperties}>&times;</Clickable>
      </div>
    )
  }

  const columnHeader = (
    <div style={{ display: 'grid', gridTemplateColumns: '16px 84px minmax(0,1fr) 64px 48px 44px 212px', gap: 8, alignItems: 'center', padding: '2px 0 4px', fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.06em', color: 'var(--muted)' } as CSSProperties}>
      <span /><span>Pod</span><span>Worktree</span><span>PR</span><span title="Commits behind main">Behind</span><span title="Last commit activity">Updated</span><span style={{ textAlign: 'right' }}>Actions</span>
    </div>
  )

  function renderRow(w: Worktree) {
    const open = !!expanded[w.name]; const rs = reviewState(w)
    const mut: CSSProperties = { fontSize: 12.5, color: 'var(--muted)', fontVariantNumeric: 'tabular-nums', fontFamily: 'ui-monospace, SF Mono, Menlo, monospace' }
    const prUrl = w.pr?.url || ''
    const isMainWithStepper = w.is_main && syncRun
    return (
      <div key={w.name}>
        <div style={{ display: 'grid', gridTemplateColumns: '16px 84px minmax(0,1fr) 64px 48px 44px 212px', gap: 8, alignItems: 'center', padding: '5px 0', borderTop: '1px solid var(--border)', minHeight: 30 } as CSSProperties}>
          {w.is_main
            ? <span style={{ width: 15 }} />
            : <Clickable aria-label={open ? 'Collapse' : 'Expand'} aria-expanded={open} onClick={() => toggleExpand(w.name)} style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--muted)', display: 'flex', padding: 0, transform: open ? 'rotate(90deg)' : 'none', transition: 'transform .12s' } as CSSProperties}><ChevronRight size={15} className="lucide-inline" /></Clickable>}
          <span style={{ overflow: 'hidden', display: 'flex' } as CSSProperties}>{stateDot(w)}</span>
          <div style={{ minWidth: 0, display: 'flex', alignItems: 'baseline', gap: 6, whiteSpace: 'nowrap', overflow: 'hidden' } as CSSProperties}>
            <span style={{ fontFamily: 'ui-monospace, monospace', fontSize: 13.5, fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis' }}>{w.name}</span>
            {w.dirty ? <span title="uncommitted changes">&bull;</span> : null}
            {w.is_main ? <span style={mut}>&middot; main</span> : null}
            {w.is_live ? <Badge variant="aim" className="text-[10px] px-1.5 py-0" title="The live gateway on this port runs from this checkout">live</Badge> : null}
            {w.summary ? <span title={w.summary} style={{ fontSize: 11.5, color: 'var(--muted)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0, flex: '0 1 auto' } as CSSProperties}>{w.summary}</span> : null}
          </div>
          {isMainWithStepper ? renderSyncStepper() : (
            <>
              {rs && prUrl ? <a href={prUrl} target="_blank" rel="noopener noreferrer" title={w.pr?.title || rs.word} style={{ textDecoration: 'none' }}><Badge variant={rs.variant}>{rs.word}</Badge></a> : <span style={{ ...mut, opacity: 0.5 }}>&mdash;</span>}
              <span style={{ ...mut, opacity: (w.behind ?? 0) > 0 ? 1 : 0.5 }} title={(w.behind ?? 0) > 0 ? w.behind + ' commits behind main' : 'up to date with main'}>{(w.behind ?? 0) > 0 ? '\u2193' + w.behind : '\u2014'}</span>
              <span style={{ ...mut, opacity: 0.85 }}>{relTime(w.last_updated_at).replace(' ago', '')}</span>
              <div style={{ display: 'flex', gap: 6, justifyContent: 'flex-end', alignItems: 'center', minWidth: 0 } as CSSProperties}>{rowButtons(w)}</div>
            </>
          )}
        </div>
        {w.is_main && syncRun && syncLogOpen ? (
          <pre style={{ margin: '2px 0 8px 32px', padding: '8px 10px', maxHeight: 180, overflow: 'auto', fontSize: 11, lineHeight: 1.45, background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, whiteSpace: 'pre-wrap', wordBreak: 'break-all' } as CSSProperties}>{filterStepMarkers(syncRun.lines || []).join('\n') || '(no output yet)'}</pre>
        ) : null}
        {open && detailLoading[w.name] ? <ContentSkeleton rows={3} /> : null}
        {open && detail[w.name] ? (
          <div style={{ padding: '4px 0 14px 30px', fontSize: 12 }}>
            {detail[w.name].error
              ? <span style={{ color: 'var(--danger)' }}>{detail[w.name].error}</span>
              : <DetailPanel w={w} d={detail[w.name]} busy={busy} onRemove={() => removeWorktree(w.name, { ...w, ...detail[w.name] })} onLoadLogs={() => loadPodLogs(w.name)} logs={podLogs[w.name]} logsLoading={podLogsLoading[w.name]} />}
          </div>
        ) : null}
      </div>
    )
  }

  const legacyToggle = legacyAll.length > 0 ? (
    <Btn onClick={() => setShowLegacy((v) => !v)} style={{ display: 'block', width: '100%', textAlign: 'left', marginTop: 4, fontSize: 11.5, color: 'var(--muted)', background: 'transparent', border: '1px dashed var(--border)' }} title="Worktrees created under a previous repository name. Hidden by default; still covered by Prune merged.">
      {showLegacy ? `Hide ${legacyAll.length} legacy worktrees` : `${legacyAll.length} legacy worktrees hidden \u00b7 Show`}
    </Btn>
  ) : null
  let body: ReactNode
  if (loading && !fleet) body = <ContentSkeleton rows={5} />
  else if (error) body = isDiscoveryError
    ? <div role="alert" style={{ padding: 24, borderRadius: 8, border: '1px solid var(--danger)', background: 'var(--danger-subtle, rgba(239,68,68,0.08))' }}><p style={{ margin: 0, fontWeight: 600, color: 'var(--danger)' }}>Discovery Error</p><p style={{ margin: '8px 0 0', color: 'var(--text)', fontSize: 14 }}>{error}</p></div>
    : <EmptyState icon={<Server size={28} className="lucide-inline" />} title="Backend unavailable" subtitle={error} />
  else if (!wts.length) body = <EmptyState icon={<Server size={28} className="lucide-inline" />} title="No worktrees found" subtitle="Nothing under the worktrees root yet." />
  else body = <div>{columnHeader}{visible.map(renderRow)}{legacyToggle}</div>

  const confirmDialog = (
    <Modal open={!!confirmReq} onClose={() => settleConfirm(false)} title={confirmReq?.title ?? ''} maxWidth={confirmReq?.width || 400} footer={<><Btn onClick={() => settleConfirm(false)}>Cancel</Btn><Btn primary={!confirmReq?.danger} danger={!!confirmReq?.danger} onClick={() => settleConfirm(true)}>{confirmReq?.confirmLabel || 'Confirm'}</Btn></>}>
      <p className="text-sm text-muted m-0">{confirmReq?.desc}</p>
    </Modal>
  )

  const pruneVerdictLabel = (code?: string): string => {
    switch (code) {
      case 'merged': return 'PR merged'
      case 'empty': return 'no commits, stale'
      case 'merged_dirty': return 'PR merged, uncommitted changes'
      case 'fresh': return 'created recently'
      case 'active': return 'PR open or unmerged commits'
      case 'merged_new_commits': return 'PR merged but new commits pushed after merge'
      case 'merged_unverified': return 'PR merged but verification unavailable — retry'
      case 'dirty_check_failed': return 'git status failed'
      default: return code || ''
    }
  }

  const pruneReviewDialog = pruneDialog && (() => {
    return (
      <Modal open={true} onClose={() => setPruneDialog(null)} title="Prune worktrees" maxWidth={480} footer={<><Btn onClick={() => setPruneDialog(null)}>Cancel</Btn><Btn danger onClick={() => pruneExecute(pruneDialog.candidates.filter((c) => pruneSelected.has(c.name)).map((c) => c.name))}>Remove selected</Btn></>}>
        <div style={{ maxHeight: 360, overflowY: 'auto' }}>
          {pruneDialog.candidates.length > 0 && (
            <div style={{ marginBottom: 10 }}>
              <div style={{ fontSize: 10, letterSpacing: '0.08em', color: 'var(--muted)', textTransform: 'uppercase', borderBottom: '1px solid var(--border)', paddingBottom: 3, marginBottom: 4 }}>Remove</div>
              {pruneDialog.candidates.map((c) => (
                <label key={c.name} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '4px 0', cursor: 'pointer' }}>
                  <Checkbox checked={pruneSelected.has(c.name)} onChange={(e) => setPruneSelected((prev) => { const next = new Set(prev); if (e.target.checked) next.add(c.name); else next.delete(c.name); return next })} aria-label={`Select ${c.name}`} />
                  <span style={{ fontFamily: 'ui-monospace, SF Mono, Menlo, monospace', fontSize: 12, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }}>{c.name}</span>
                  <span style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--muted)', whiteSpace: 'nowrap' }}>{pruneVerdictLabel(c.code)}</span>
                </label>
              ))}
            </div>
          )}
          {pruneDialog.kept.length > 0 && (
            <div style={{ marginBottom: 10 }}>
              <div style={{ fontSize: 10, letterSpacing: '0.08em', color: 'var(--muted)', textTransform: 'uppercase', borderBottom: '1px solid var(--border)', paddingBottom: 3, marginBottom: 4 }}>Kept</div>
              {pruneDialog.kept.map((k) => (
                <div key={k.name} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '4px 0' }}>
                  <span style={{ width: 13 }} />
                  <span style={{ fontFamily: 'ui-monospace, SF Mono, Menlo, monospace', fontSize: 12, color: 'var(--muted)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }}>{k.name}</span>
                  <span style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--muted)', whiteSpace: 'nowrap' }}>{pruneVerdictLabel(k.code)}</span>
                </div>
              ))}
            </div>
          )}
          {pruneDialog.candidates.length === 0 && <p style={{ fontSize: 12, color: 'var(--muted)' }}>No candidates found.</p>}
          <p style={{ fontSize: 11, color: 'var(--muted)', margin: '8px 0 0' }}>Removes worktrees and stops pods. Cannot be undone.</p>
        </div>
      </Modal>
    )
  })()

  const pruneDone = pruneProgress != null && pruneProgress.done >= pruneProgress.total && !pruneProgress.current
  const pruneProgressModal = pruneProgress && (
    <Modal
      open={true}
      onClose={() => { if (pruneDone) setPruneProgress(null) }}
      title={pruneDone ? 'Prune complete' : 'Pruning worktrees'}
      maxWidth={440}
      footer={pruneDone ? <Btn onClick={() => setPruneProgress(null)}>Close</Btn> : undefined}
    >
      <div style={{ fontSize: 12 }}>
        <div style={{ fontWeight: 600, marginBottom: 6 }}>
          {pruneDone ? 'Finished' : 'Removing'} {pruneProgress.done}/{pruneProgress.total}
          {pruneProgress.current ? <span style={{ fontWeight: 400, color: 'var(--muted)' }}> {'\u2014'} {pruneProgress.current}</span> : null}
        </div>
        {pruneProgress.results.map((r) => (
          <div key={r.name} style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '2px 0' }}>
            <span style={{ color: r.ok ? 'var(--ok)' : 'var(--danger)', fontSize: 11 }}>{r.ok ? 'removed' : 'failed'}</span>
            <span style={{ fontFamily: 'monospace', fontSize: 11 }}>{r.name}</span>
            {!r.ok && r.error ? <span style={{ color: 'var(--muted)', fontSize: 11, marginLeft: 'auto', maxWidth: 220, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{r.error}</span> : null}
          </div>
        ))}
      </div>
    </Modal>
  )

  const diskGb = disk?.total_mb != null ? (disk.total_mb / 1024).toFixed(0) + ' GB' : '\u2026'

  return (
    <>
      {confirmDialog}
      <ToastHost />
      {pruneReviewDialog}
      {pruneProgressModal}
      {restarting && (
        <div style={{ position: 'fixed', inset: 0, zIndex: 9999, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', background: 'var(--bg)', color: 'var(--text)' }}>
          <LoaderCircle size={32} className="lucide-inline" style={{ animation: 'spin 1s linear infinite' }} />
          <p style={{ marginTop: 16, fontSize: 16, fontWeight: 600 }}>Restarting gateway...</p>
          <p style={{ fontSize: 12, color: 'var(--muted)' }}>The page will reload automatically when the gateway is back.</p>
        </div>
      )}
      <div className="flex flex-1 min-h-0 overflow-hidden">
        <div className="flex-1 min-w-0 flex flex-col min-h-0">
          <PageHeader title="Dev Fleet" subtitle="Manage the git worktrees of your main checkout — sync, rebase, QA pods, and cleanup in one place." />
          <div className="flex-1 overflow-y-auto px-6 pb-8 min-h-0">
            <p className="text-[12.5px] text-muted leading-relaxed mt-3 mb-1 max-w-[860px]">
              Each row below is a git worktree discovered from the main checkout. Use{' '}
              <span className="text-text-strong">Pull + Build</span> on the main row to fast-forward it from origin and rebuild
              (then restart the gateway to apply). <span className="text-text-strong">Pod</span> boots any worktree as an
              isolated throwaway gateway so you can QA a feature branch without touching your live instance.{' '}
              <span className="text-text-strong">Rebase</span> moves a feature branch onto the latest main, and{' '}
              <span className="text-text-strong">Prune</span> safely removes worktrees whose PR has already merged.
            </p>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, minmax(0, 1fr))', gap: 12, margin: '14px 0' } as CSSProperties}>
              <StatCard label="Running pods" value={running} accent />
              <StatCard label="Worktrees" value={wts.length} />
              <StatCard label="Needs provision" value={needsProv} />
              <StatCard label="Disk (worktrees)" value={diskGb} />
            </div>
            <Card>
              <CardTitle><span className="flex items-center gap-1.5">Worktrees ({wts.length})<InfoTip text="Every git worktree of the main checkout. Pull+Build syncs main; pods are isolated gateways booted from a worktree." /></span></CardTitle>
              <div style={{ display: 'flex', gap: 10, alignItems: 'center', margin: '12px 0 4px' } as CSSProperties}>
                <div className="flex-1 min-w-0">
                  <SearchInput placeholder={'Filter worktrees\u2026'} value={q} onChange={(e) => setQ((e.target as HTMLInputElement).value)} aria-label="Filter worktrees" />
                </div>
                <span style={{ fontSize: 11.5, color: 'var(--muted)', flexShrink: 0 }}>{ql ? others.length + ' / ' : ''}{wts.length} rows</span>
                <Select
                  value={sortBy}
                  onChange={(e) => setSortBy(e.target.value)}
                  aria-label="Sort worktrees"
                >
                  <option value="status">Sort: status</option>
                  <option value="recent">Sort: recent</option>
                  <option value="name">Sort: name</option>
                  <option value="behind">Sort: behind</option>
                </Select>
                <Btn danger onClick={pruneShipped} disabled={!!busy['__prune']}>{iconLabel(<Trash2 size={13} className="lucide-inline" />, 'Prune merged')}</Btn>
                <Btn onClick={() => invalidateAll()} disabled={loading} aria-label="Refresh fleet">{iconLabel(<RefreshCw size={14} className="lucide-inline" />, 'Refresh')}</Btn>
              </div>
              {body}
            </Card>
          </div>
        </div>
      </div>
    </>
  )
}
