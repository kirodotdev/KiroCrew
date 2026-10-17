/**
 * Dev Fleet — worktree management page ported to KiroCrew SPA.
 * Manages git worktrees, pod instances, syncing, pruning, and rebasing.
 *
 * This file is the page's import path. It keeps the page's state, effects and
 * actions (their poll loops and toasts hold strings that are not translated,
 * and the page assigns the notification bus's module state), that bus, the
 * log-window merge and the relative-time label. The owners beside it take new
 * work for their part: devFleetPageModel.ts (types and React-free logic),
 * DevFleetPageMenus.tsx (row menus and confirms), DevFleetPageRow.tsx (a row's
 * parts), DevFleetPagePrune.tsx (the prune dialogs) and DevFleetPageNotices.tsx
 * (the notices above the table).
 */
import { useState, useRef, useCallback, useEffect, type CSSProperties, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Card, CardTitle, Btn, EmptyState, ContentSkeleton, PageHeader, SearchInput, Badge,
} from '../components/ui'
import SimpleSelect from '../components/SimpleSelect'
import InfoTip from '../components/InfoTip'
import Modal from '../components/Modal'
import Clickable from '../components/Clickable'
import ErrorNotice from '../components/ErrorNotice'
import { useNavigate } from 'react-router-dom'
import { useAppDispatch } from '../store'
import { addNotification } from '../store/notificationsSlice'
import { setPendingInput } from '../store/chatSlice'
import { Server, RefreshCw, ChevronRight, Trash2, LoaderCircle } from 'lucide-react'
import * as api from './devFleetApi'
import { ApiError } from '../api/client'

import { i18nT } from '../i18n/t'
import { compareText } from '../i18n/format'
import {
  readDismissedUndoKey, persistDismissedUndoKey, POLL_MS, sleep, filterStepMarkers, syncFailureTail,
  _activeSyncPolls, awaitGatewayBackGlobal, type Worktree, type UndoTarget, type FleetData, type SyncRun,
  type ProvRun, type RebaseResult, type PruneDialog, type PruneProgress,
} from './devFleetPageModel'
import {
  ProvLogPre, iconLabel, PodReadout, DetailPanel, DevFleetRowActions, DevFleetSyncStepper,
  DevFleetProvStepper,
} from './DevFleetPageRow'
import { DevFleetPruneReview, DevFleetPruneProgress } from './DevFleetPagePrune'
import { DevFleetPageNotices } from './DevFleetPageNotices'
export { gatewayRecovered, pruneVerdictLabel, PRUNE_STATUS_META } from './devFleetPageModel'
/* ─── Notification helper (replaces useNotify) ─── */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
let _dispatch: any = null

type Toast = { id: number; msg: string; type: 'success' | 'error' | 'info' }
const _toastListeners = new Set<(t: Toast) => void>()
// Error fan-out. A toast auto-dismisses in 7s and used to be the ONLY report
// ~30 catch sites in this file make of a failed pod / remove / rebase / prune /
// restart — so errors now go here instead, and the page keeps the latest
// failure in view (through ErrorNotice, with the agent hand-off) until it is
// dismissed. Routing on `type` inside `notify` gives every existing site the
// in-page surface without touching them; success/info keep the toast.
const _actionErrorListeners = new Set<(msg: string) => void>()
// The latest failure also lives here, outside the component: a background
// poll (Pull+Build, provision) can fail while the page is unmounted, when no
// listener is registered. The page seeds its notice from this on mount and
// clears it on dismiss, so a failure that landed while the user was elsewhere
// is still on the page when they come back, not only in the notification bell.
let _lastActionError: string | null = null

/** Test seam: the latest-error slot and dismissal receipt are module/browser state. */
export function __resetDevFleetNoticesForTests(): void {
  _lastActionError = null
  persistDismissedUndoKey(null)
}
let _toastSeq = 1

function notify(msg: string, opts?: { type?: 'success' | 'error' | 'info' }) {
  const t: Toast = { id: _toastSeq++, msg, type: opts?.type || 'info' }
  if (t.type === 'error') {
    _lastActionError = msg
    _actionErrorListeners.forEach((fn) => fn(msg))
  } else _toastListeners.forEach((fn) => fn(t))
  if (!_dispatch) return
  _dispatch(addNotification({
    ts: String(Date.now()),
    title: msg,
    body: '',
    kind: opts?.type === 'error' ? 'error' : opts?.type === 'success' ? 'success' : 'info',
  }))
}

// The /api/run endpoint returns only the last ~60 output lines (server-side
// tail). Long provisions scroll early lines out of that window, so we
// accumulate client-side: merge each polled window into the running buffer by
// finding the longest suffix of the buffer that is also a prefix of the new
// window, then appending only the non-overlapping remainder. Robust to the
// window sliding forward between polls; the only unrecoverable case is output
// that scrolls more than a full window between two polls -- detected via zero
// overlap and surfaced with a visible LOG_GAP_MARKER line (documented in
// dev-fleet.md as an honest limitation).
export const LOG_GAP_MARKER = '[\u2026 lines missed \u2026]'

export function mergeLogWindow(buffer: string[], window: string[]): string[] {
  if (!window.length) return buffer
  if (!buffer.length) return window.slice()
  const max = Math.min(buffer.length, window.length)
  let overlap = 0
  for (let k = max; k > 0; k--) {
    let match = true
    for (let i = 0; i < k; i++) {
      if (buffer[buffer.length - k + i] !== window[i]) { match = false; break }
    }
    if (match) { overlap = k; break }
  }
  if (overlap === 0) {
    // Zero overlap with a non-empty buffer means the server's tail window slid
    // completely past what we last saw -- lines were (or may have been) missed.
    // Insert a visible gap marker so the panel never overstates completeness.
    return buffer.concat([LOG_GAP_MARKER], window)
  }
  return buffer.concat(window.slice(overlap))
}

function relTime(epoch: number | null | undefined): string {
  if (!epoch) return ''
  const s = Math.max(0, Math.floor(Date.now() / 1000 - epoch))
  if (s < 60) return i18nT('pages.devFleetPage.just_now')
  const m = Math.floor(s / 60); if (m < 60) return m + 'm ago'
  const h = Math.floor(m / 60); if (h < 24) return h + 'h ago'
  const d = Math.floor(h / 24); if (d < 30) return d + 'd ago'
  return Math.floor(d / 30) + 'mo ago'
}

/* ═══════════ Main component ═══════════ */
function ToastHost() {
  const [toasts, setToasts] = useState<Toast[]>([])
  useEffect(() => {
    const on = (t: Toast) => {
      setToasts((ts) => [...ts, t])
      window.setTimeout(() => setToasts((ts) => ts.filter((x) => x.id !== t.id)), 4000)
    }
    _toastListeners.add(on)
    return () => { _toastListeners.delete(on) }
  }, [])
  if (!toasts.length) return null
  return (
    <div role="status" aria-live="polite" className="fixed top-safe-offset-3.5" style={{ left: '50%', transform: 'translateX(-50%)', zIndex: 9997, display: 'flex', flexDirection: 'column', gap: 6, alignItems: 'center', pointerEvents: 'none' } as CSSProperties}>
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
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  /* ─── react-query: fleet data ─── */
  const { data: fleet, isLoading: loading, error: fleetError } = useQuery<FleetData>({
    queryKey: ['dev-fleet', 'fleet'],
    queryFn: () => api.get<FleetData>('/fleet'),
    refetchInterval: POLL_MS,
    refetchOnMount: 'always',
  })

  /* ─── react-query: disk data ─── */
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const { data: disk, isError: diskFailed, error: diskError } = useQuery<any>({
    queryKey: ['dev-fleet', 'disk'],
    queryFn: () => api.get('/disk'),
    refetchInterval: 30000,
  })

  // Latest failed action (see `_actionErrorListeners`). Cleared by its own
  // dismiss only — a later success does not erase it, because the failure it
  // names may be a different worktree's.
  const [actionError, setActionError] = useState<string | null>(() => _lastActionError)
  const actionErrorRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const on = (msg: string) => setActionError(msg)
    _actionErrorListeners.add(on)
    return () => { _actionErrorListeners.delete(on) }
  }, [])
  const dismissActionError = useCallback(() => { _lastActionError = null; setActionError(null) }, [])
  // The notice lives at the top of a long, scrolling list while the click that
  // failed may be far below it — bring it into view so the failure is not read
  // as a dead click. (Replaces what the viewport-anchored toast used to do.)
  useEffect(() => {
    if (actionError) actionErrorRef.current?.scrollIntoView?.({ block: 'nearest', behavior: 'smooth' })
  }, [actionError])

  // Every call below happens right after a user-initiated mutation (or the
  // explicit Refresh button), so the fleet has to be REBUILT rather than
  // re-read: a plain refetch hits the backend's stale-while-revalidate cache,
  // which serves the PRE-mutation snapshot and only rebuilds behind it — so a
  // pruned worktree would keep rendering until that rebuild lands. `fresh=1`
  // forces the rebuild and the backend coalesces concurrent ones onto a single
  // build. Falls back to a plain invalidate if the fresh fetch fails.
  const refetchFleetFresh = useCallback(async () => {
    try {
      const data = await api.get<FleetData>('/fleet?fresh=1')
      if (data) queryClient.setQueryData(['dev-fleet', 'fleet'], data)
      else queryClient.invalidateQueries({ queryKey: ['dev-fleet', 'fleet'] })
    } catch {
      queryClient.invalidateQueries({ queryKey: ['dev-fleet', 'fleet'] })
    }
  }, [queryClient])
  const invalidateFleet = useCallback(() => { void refetchFleetFresh() }, [refetchFleetFresh])
  const invalidateAll = useCallback(() => {
    void refetchFleetFresh()
    queryClient.invalidateQueries({ queryKey: ['dev-fleet', 'disk'] })
  }, [refetchFleetFresh, queryClient])

  const [busy, setBusy] = useState<Record<string, boolean>>({})
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const [detail, setDetail] = useState<Record<string, any>>({})
  const [detailLoading, setDetailLoading] = useState<Record<string, boolean>>({})
  const [prov, setProv] = useState<Record<string, ProvRun | null>>({})
  const [provLogOpen, setProvLogOpen] = useState<Record<string, boolean>>({})
  const provDoneTimersRef = useRef<Record<string, ReturnType<typeof setTimeout>>>({})
  const [rebaseResult, setRebaseResult] = useState<Record<string, RebaseResult>>({})
  // A failed restart is the one error on this page that carries an instruction
  // rather than just a symptom. Toasts are pointer-events:none and self-dismiss,
  // so they cannot be selected or copied and a long message vanishes mid-read —
  // keep the text on the page until it is dealt with.
  const [gatewayError, setGatewayError] = useState<string | null>(null)
  // Dismissal is scoped to one current→previous pair. A later Make Live changes
  // the key and automatically reveals the new banner without persisting UI-only
  // state into the keystone live-target pointer.
  const [dismissedUndoKey, setDismissedUndoKey] = useState<string | null>(readDismissedUndoKey)
  const [podLogs, setPodLogs] = useState<Record<string, string>>({})
  const [podLogsLoading, setPodLogsLoading] = useState<Record<string, boolean>>({})
  const rebaseTimersRef = useRef<Record<string, ReturnType<typeof setTimeout>>>({})
  const [q, setQ] = useState('')
  const [sortBy, setSortBy] = useState('status')
  const [showLegacy, setShowLegacy] = useState(false)
  const [syncRun, setSyncRun] = useState<SyncRun | null>(null)
  const [syncLogOpen, setSyncLogOpen] = useState(false)
  const syncAttachedRef = useRef(false)
  // Provision run ids already being tracked (started locally or reattached),
  // so the fleet-driven reattach below never starts a second poll loop for a
  // run this session is already polling.
  const provAttachedRef = useRef<Set<string>>(new Set())
  // Synchronous per-worktree in-flight guard for the provision() entry point.
  // React state updates are asynchronous: setProv({ status: 'starting' })
  // does not disable the Provision button until the next render commit.
  // A rapid double-click therefore sends two POST requests before any re-render.
  // This ref is checked and set BEFORE the first `await`, so the second click
  // in the same render turn is blocked synchronously rather than racing the DOM.
  const provInFlightRef = useRef<Set<string>>(new Set())
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
  async function dismissProv(name: string) {
    const rid = prov[name]?.rid
    if (rid) {
      try {
        await api.post('/pod/provision/dismiss', { name, run_id: rid })
      } catch (e: unknown) {
        notify((e as Error)?.message || String(e), { type: 'error' })
        return
      }
    }
    // The POST above is awaited, so a REPLACEMENT provision can fail and
    // reattach to this worktree while the dismiss is in flight. Clear only the
    // strip the user actually dismissed: if `rid` moved underneath us a newer
    // failure is on screen, and deleting it would hide that run (and its log)
    // until the next reload.
    let stale = false
    setProv((p) => {
      if (p[name]?.rid !== rid) { stale = true; return p }
      const n = { ...p }; delete n[name]; return n
    })
    if (stale) return
    clearTimeout(provDoneTimersRef.current[name])
    setProvLogOpen((o) => { const n = { ...o }; delete n[name]; return n })
    invalidateFleet()
  }
  function toggleProvLog(name: string) { setProvLogOpen((o) => ({ ...o, [name]: !o[name] })) }
  const [confirmReq, setConfirmReq] = useState<{ title: string; desc: ReactNode; confirmLabel?: string; cancelLabel?: string; danger?: boolean; width?: number; resolve: (v: boolean) => void } | null>(null)
  const [restarting, setRestarting] = useState(false)
  // A cutover is dangerous BEFORE `restarting` goes true: makeLive() awaits the
  // /make-live POST, and that request stages the live-target pointer and issues the
  // daemon-reload. A Restart fired inside that window can tear the gateway down
  // between the write and the reload, leaving persisted and loaded unit state
  // inconsistent. `restarting` only covers the wait AFTER the POST returns, so
  // every global action predicate must also honour an in-flight cutover on ANY
  // row (the busy flag is per-worktree, the hazard is process-wide).
  const makeLivePending = Object.entries(busy).some(([k, v]) => v && k.endsWith(':makelive'))
  const gatewayMutating = restarting || makeLivePending
  // The gateway could not report which checkout is live or staged (see the notice
  // above the rows). With no row marked live, the "not live" guard on Make live would
  // otherwise offer a cutover on every row — the already-live one included — chosen
  // against a state nobody knows. Make live is disabled for the whole outage; the
  // notice's Ask-the-agent hand-off is the recovery path.
  const liveStateUnknown = fleet?.live_state_known === false
  // The worktree a cutover is staged onto (live-target pointer written, gateway
  // not yet restarted into it), but only while the backend would ACCEPT the
  // pointer-only cancel: on a drivable host /make-live refuses it
  // (staged_cutover_pending), so offering the control there would promise a
  // cancel that never happens. staged_cancel_available comes from the same
  // predicate the backend's cancel branch uses; absent (older backend) fails
  // closed to the pre-cancel-control behaviour. Non-null exactly while the
  // live row should offer "Cancel staged cutover".
  const stagedWorktree = fleet?.staged_cancel_available === true
    ? (fleet?.worktrees || []).find((x) => x.is_staged && !x.is_live) || null
    : null
  // The row the cancel actually posts (re-confirming it as the live target
  // is the cancel). Needed by the staged row's co-located menu item.
  const liveWorktree = stagedWorktree
    ? (fleet?.worktrees || []).find((x) => x.is_live && x.path) || null
    : null
  // ANY pending stage, cancellable or not: a restart boots the staged
  // checkout, so the restart confirm must say so — that hazard does not
  // depend on whether the pointer-only cancel is available.
  const pendingStage = (fleet?.worktrees || []).find((x) => x.is_staged && !x.is_live) || null
  const [pruneDialog, setPruneDialog] = useState<PruneDialog | null>(null)
  const [pruneSelected, setPruneSelected] = useState<Set<string>>(new Set())
  const [pruneForceSelected, setPruneForceSelected] = useState<Set<string>>(new Set())
  const [pruneProgress, setPruneProgress] = useState<PruneProgress | null>(null)
  const askConfirm = (title: string, desc: ReactNode, opts?: { confirmLabel?: string; cancelLabel?: string; danger?: boolean; width?: number }) => new Promise<boolean>((resolve) => setConfirmReq({ title, desc, ...(opts || {}), resolve }))
  const settleConfirm = (val: boolean) => setConfirmReq((c) => { if (c) c.resolve(val); return null })

  const setFlag = (k: string, v: boolean) => setBusy((b) => ({ ...b, [k]: v }))

  function showRebaseResult(name: string, res: RebaseResult) {
    setRebaseResult((m) => ({ ...m, [name]: res }))
    clearTimeout(rebaseTimersRef.current[name])
    rebaseTimersRef.current[name] = setTimeout(() => setRebaseResult((m) => { const n = { ...m }; delete n[name]; return n }), res.kind === 'ok' ? 15000 : 60000)
  }
  function dismissRebaseResult(name: string) { clearTimeout(rebaseTimersRef.current[name]); setRebaseResult((m) => { const n = { ...m }; delete n[name]; return n }) }

  /* ─── Sync reattach on page load ─── */
  useEffect(() => {
    if (!fleet?.sync_run_id || syncAttachedRef.current) return
    if (syncRun?.rid === fleet.sync_run_id) return // already tracking this run
    syncAttachedRef.current = true
    const rid = fleet.sync_run_id
    api.get<{ status?: string; output?: string[]; exit_code?: number; started?: number; step_label?: string; cause?: string }>('/run?id=' + rid)
      .then((run) => {
        if (!run) return
        const t0 = run.started ? run.started * 1000 : Date.now()
        const out = run.output || []
        // Same preference as the two poll paths: a reported cause outranks the
        // step's stderr tail, which outranks the last output line. Missing it
        // here meant a page RELOAD after a failed build showed the uninformative
        // line even though the diagnosis was stored on the run.
        const last = run.cause || syncFailureTail(out)
        if (run.status === 'running') {
          setSyncRun({ rid, status: 'running', lines: out, startedAt: t0, stepLabel: run.step_label })
          pollSyncRun(rid, t0)
        } else if (run.status === 'done' || run.status === 'timeout') {
          // Show the completed/failed result so user sees it on revisit
          const okRun = run.exit_code === 0
          setSyncRun({ rid, status: okRun ? 'done' : 'error', lines: out, startedAt: t0, exit: run.exit_code, last, lastIsCause: Boolean(run.cause) })
        }
      })
      .catch(() => { /* run endpoint unreachable — nothing to reattach */ })
  }, [fleet?.sync_run_id]) // eslint-disable-line react-hooks/exhaustive-deps

  /* ─── Provision reattach on page load ─── */
  // The fleet payload carries a provision_run_id per worktree while a
  // provision is running or after it failed (mirrors sync_run_id). On mount,
  // rehydrate the stepper/log for each: a running run resumes polling, a
  // failed run restores the persisted failure state with the log expanded.
  useEffect(() => {
    for (const w of fleet?.worktrees || []) {
      const rid = w.provision_run_id
      if (!rid || provAttachedRef.current.has(rid)) continue
      provAttachedRef.current.add(rid)
      const name = w.name
      api.get<{ status?: string; output?: string[]; exit_code?: number; started?: number }>('/run?id=' + rid)
        .then((run) => {
          if (!run) {
            // Nothing usable came back — allow a later fleet refetch to retry.
            provAttachedRef.current.delete(rid)
            return
          }
          const t0 = run.started ? run.started * 1000 : Date.now()
          const lines = run.output || []
          if (run.status === 'running') {
            setProv((p) => ({ ...p, [name]: { rid, status: 'running', lines, startedAt: t0 } }))
            void pollProvisionRun(name, rid, t0, lines)
          } else if (run.exit_code !== 0) {
            // Only unsuccessful runs are exposed by the backend, but guard
            // anyway: a successful run has nothing to reattach.
            setProv((p) => ({ ...p, [name]: { rid, status: 'failed', failed: true, lines, startedAt: t0, exit: run.exit_code ?? null } }))
            setProvLogOpen((o) => ({ ...o, [name]: true }))
          }
        })
        .catch(() => {
          // Transient failure (gateway restart mid-request, network blip):
          // un-dedupe so the next fleet refetch can attempt the reattach
          // again instead of permanently orphaning the run id.
          provAttachedRef.current.delete(rid)
        })
    }
  }, [fleet?.worktrees]) // eslint-disable-line react-hooks/exhaustive-deps

  /* ─── Tick for elapsed counter ─── */
  const [, setTick] = useState(0)
  // Elapsed counter ticks while a sync OR any provision is actively running.
  const provTicking = Object.values(prov).some((p) => !!p && (p.status === 'running' || p.status === 'starting'))
  useEffect(() => {
    if ((!syncRun || syncRun.status !== 'running') && !provTicking) return
    const t = setInterval(() => setTick((n) => n + 1), 1000)
    return () => clearInterval(t)
  }, [syncRun?.status, provTicking]) // eslint-disable-line react-hooks/exhaustive-deps

  async function pollSyncRun(rid: string, startedAt: number) {
    // A poll for this run is already in flight (it outlived a previous mount of
    // this page, which is intended — the build's auto-restart must not be lost
    // to navigation). Starting a second loop here would race it: both would see
    // `done` and both would fire the restart POST. Skip — but start a
    // lightweight state relay so this mount's UI stays updated.
    if (_activeSyncPolls.has(rid)) {
      _syncStateRelay(rid, startedAt)
      return
    }
    _activeSyncPolls.add(rid)
    try {
      await _pollSyncRunLoop(rid, startedAt)
    } finally {
      _activeSyncPolls.delete(rid)
    }
  }

  // Lightweight relay: polls /run to keep THIS mount's syncRun state in sync
  // while the primary poll (from a prior mount) handles the auto-restart logic.
  // Exits when the run finishes, the component unmounts, or the run is dismissed.
  async function _syncStateRelay(rid: string, startedAt: number) {
    for (let i = 0; i < 900; i++) {
      await sleep(2000)
      if (!pollAliveRef.current) return
      if (cancelledRunsRef.current.has(rid)) return
      let run: { status?: string; output?: string[]; exit_code?: number; started?: number; step_label?: string; cause?: string } | null = null
      try { run = await api.get('/run?id=' + rid) } catch { continue }
      if (!run) continue
      const t0 = run.started ? run.started * 1000 : startedAt
      const out = run.output || []
      const last = run.cause || syncFailureTail(out)
      if (run.status === 'done' || run.status === 'timeout') {
        const okRun = run.exit_code === 0
        setSyncRun({ rid, status: okRun ? 'done' : 'error', lines: out, startedAt: t0, exit: run.exit_code, last, lastIsCause: Boolean(run.cause) })
        return
      }
      setSyncRun({ rid, status: 'running', lines: out, startedAt: t0, last, lastIsCause: Boolean(run.cause), stepLabel: run.step_label })
    }
  }

  async function _pollSyncRunLoop(rid: string, startedAt: number) {
    for (let i = 0; i < 900; i++) {
      await sleep(2000)
      // Explicit dismissal aborts the poll entirely. Component unmount
      // (navigate-away) does NOT: the build can take ~6 min, and the whole
      // point of this loop is to auto-restart the gateway when the build
      // finishes — a restart the user must not lose by leaving the page. So we
      // keep polling and still issue the restart after unmount; the React state
      // setters below are safe no-ops once the component is gone.
      if (cancelledRunsRef.current.has(rid)) return
      let run: { status?: string; output?: string[]; exit_code?: number; started?: number; step_label?: string; cause?: string } | null = null
      let gone = false
      try { run = await api.get('/run?id=' + rid) } catch (e) {
        // 404 = the gateway restarted and dropped the run registry — the run
        // is unrecoverable; freezing the bar forever was a real user trap.
        if ((e as { status?: number })?.status === 404) gone = true
        else continue
      }
      if (gone || !run) {
        if (gone) {
          setSyncRun({ rid, status: 'error', lines: [], startedAt, last: i18nT('pages.devFleetPage.gateway_restarted_mid_sync_run_lost_check_git_st') })
          setFlag('__syncmain', false)
          notify(i18nT('pages.devFleetPage.sync_run_lost_gateway_restarted_mid_sync_re_run'), { type: 'error' })
          return
        }
        continue
      }
      const out = run.output || []
      const t0 = run.started ? run.started * 1000 : startedAt
      // Prefer the cause a step reported, then the failing step's stderr tail,
      // then the last output line -- see `syncFailureTail` for why the last line
      // is the worst of the three.
      const last = run.cause || syncFailureTail(out)
      if (run.status === 'done' || run.status === 'timeout') {
        const okRun = run.exit_code === 0
        setSyncRun({ rid, status: okRun ? 'done' : 'error', lines: out, startedAt: t0, exit: run.exit_code, last, lastIsCause: Boolean(run.cause) })
        setFlag('__syncmain', false)
        if (okRun) {
          // Auto-restart the gateway when the service is drivable — the build
          // just updated static/dist on the live checkout, so applying it only
          // needs a bounce. Skip the confirm dialog since the user already
          // explicitly started Pull+Build knowing it updates their live code.
          // The restart POST fires even after unmount (navigate-away during the
          // build): losing it would strand the user on a stale gateway. The
          // route-independent awaitGatewayBackGlobal handles the reload.
          if (fleet?.gateway_service_active) {
            notify(i18nT('pages.devFleetPage.build_finished_restarting_gateway'), { type: 'success' })
            setRestarting(true)
            setGatewayError(null)
            api.postGateway<{ ok?: boolean; error?: string; start_id?: string | null }>('/restart-gateway', {})
              .then(async (r) => {
                if (!r?.ok) {
                  const msg = r?.error || i18nT('pages.devFleetPage.restart_failed')
                  notify(msg, { type: 'error' })
                  setGatewayError(msg)
                  setRestarting(false)
                } else {
                  await awaitGatewayBack(r.start_id ?? null)
                }
              })
              .catch((e: unknown) => {
                const msg = `${i18nT('pages.devFleetPage.restart_failed')}: ${(e as Error)?.message || String(e)}`
                notify(msg, { type: 'error' })
                setGatewayError(msg)
                setRestarting(false)
              })
          } else {
            notify(i18nT('pages.devFleetPage.synced_restart_gateway_to_apply_the_new_build'), { type: 'success' })
          }
        }
        // With a named cause the detail is already a self-sufficient sentence
        // ending in the action to take, so prefixing it with "(exit 41)" adds a
        // reserved code that means nothing outside this codebase. Only the raw
        // log-tail fallback still carries the code, where it is the only
        // machine-readable thing the toast has.
        else if (run.cause) notify(`${i18nT('pages.devFleetPage.pull_build_failed')}: ${run.cause}`, { type: 'error' })
        else notify(i18nT('pages.devFleetPage.pull_build_failed_exit_code_detail', { code: run.exit_code, detail: last }), { type: 'error' })
        invalidateFleet()
        return
      }
      setSyncRun({ rid, status: 'running', lines: out, startedAt: t0, last, lastIsCause: Boolean(run.cause), stepLabel: run.step_label })
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
        // Minted by the GATEWAY: the pod refuses a mint from the sandboxed
        // backend (member_owner_token_refused). An auth refusal there is a
        // non-2xx that throws, so close the blank tab before reporting it.
        let r: { ok?: boolean; url?: string; error?: string } | undefined
        try { r = await api.postGateway<{ ok?: boolean; url?: string; error?: string }>('/pod-token', { name }) }
        catch (e: unknown) { w?.close(); throw e }
        if (r?.ok && r.url) { if (w) w.location.href = r.url; else window.open(r.url, '_blank', 'noopener') }
        else { w?.close(); notify(r?.error || i18nT('pages.devFleetPage.token_mint_failed'), { type: 'error' }) }
      }
      else if (kind === 'up') { notify(i18nT('pages.devFleetPage.starting_pod_for_name_can_take_1_min', { name }), { type: 'info' }); const r = await api.post<{ ok?: boolean; error?: string }>('/pod/up', { name }); notify(r?.ok ? i18nT('pages.devFleetPage.pod_up_name', { name }) : (r?.error || i18nT('pages.devFleetPage.pod_start_failed')), { type: r?.ok ? 'success' : 'error' }); invalidateFleet() }
      else if (kind === 'down') { const r = await api.post<{ ok?: boolean; error?: string }>('/pod/down', { name }); notify(r?.ok ? i18nT('pages.devFleetPage.stopped_name', { name }) : (r?.error || i18nT('pages.devFleetPage.failed')), { type: r?.ok ? 'success' : 'error' }); invalidateFleet() }
      else if (kind === 'restart') { const r = await api.post<{ ok?: boolean; error?: string }>('/pod/restart', { name }); notify(r?.ok ? i18nT('pages.devFleetPage.restarted_name', { name }) : (r?.error || i18nT('pages.devFleetPage.failed')), { type: r?.ok ? 'success' : 'error' }); invalidateFleet() }
    } catch (e: unknown) { notify((e as Error)?.message || String(e), { type: 'error' }) }
    finally { setFlag(flag, false) }
  }

  function launchQa(name: string) {
    const prompt =
      `Dev Fleet QA for worktree '${name}'. ` +
      'Steps: (1) load the pod-e2e skill (it manages pod lifecycle); ' +
      '(2) ensure the pod is up for this worktree; ' +
      '(3) run the pod-e2e QA suite (backend API + Playwright frontend) against that pod; ' +
      '(4) record a short demo video of the pod dashboard with the feature-demo-recording skill; ' +
      '(5) deliver the video and a concise pass/fail summary. English only.'
    dispatch(setPendingInput(prompt))
    navigate('/chat?autoSend=1&newSession=1')
  }

  // Poll a single provision run to completion, accumulating the server's
  // sliding 60-line output window into a full client-side buffer
  // (mergeLogWindow) so the "full log" panel keeps early output. Shared by a
  // fresh provision and by reattaching to an already-in-flight run; a
  // reattach passes the lines it already fetched as `seed` so fast output
  // between that fetch and the first poll can only produce a visible gap
  // marker, never a silently dropped prefix.
  async function pollProvisionRun(name: string, rid: string, startedAt: number, seed: string[] = []) {
    let acc: string[] = seed.slice()
    for (let i = 0; i < 900; i++) {
      await sleep(2000)
      if (!pollAliveRef.current) return
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      let run: any = null; try { run = await api.get('/run?id=' + rid) } catch { continue }
      if (!run) continue
      acc = mergeLogWindow(acc, run.output || [])
      const lines = acc
      if (run.status === 'done') {
        const ok = run.exit_code === 0
        notify(ok ? i18nT('pages.devFleetPage.provisioned') : i18nT('pages.devFleetPage.provision_failed_exit_code', { code: run.exit_code }), { type: ok ? 'success' : 'error' })
        if (ok) {
          // Flash a brief green "Provisioned", then clear. The
          // fleet refetch flips the row to its built state in the meantime.
          setProv((p) => ({ ...p, [name]: { rid, status: 'done', done: true, lines, startedAt, exit: 0 } }))
          invalidateFleet()
          provDoneTimersRef.current[name] = setTimeout(() => {
            setProv((p) => { const n = { ...p }; delete n[name]; return n })
            setProvLogOpen((o) => { const n = { ...o }; delete n[name]; return n })
          }, 2500)
        } else {
          // FAILURE PERSISTENCE: keep the run, auto-expand the log, hold until
          // the user dismisses it — a multi-minute failed provision must not
          // vanish into an empty row.
          setProv((p) => ({ ...p, [name]: { rid, status: 'failed', failed: true, lines, startedAt, exit: run.exit_code } }))
          setProvLogOpen((o) => ({ ...o, [name]: true }))
          invalidateFleet()
        }
        return
      }
      if (run.status !== 'running') {
        notify(run.status === 'timeout' ? i18nT('pages.devFleetPage.provision_timed_out') : i18nT('pages.devFleetPage.provision_failed_status', { status: run.status }), { type: 'error' })
        setProv((p) => ({ ...p, [name]: { rid, status: 'failed', failed: true, lines: lines.length ? lines : ['Provision ' + run.status], startedAt, exit: run.exit_code ?? null } }))
        setProvLogOpen((o) => ({ ...o, [name]: true }))
        invalidateFleet()
        return
      }
      setProv((p) => ({ ...p, [name]: { rid, status: 'running', lines, startedAt } }))
    }
    // Poll budget exhausted (e.g. run id lost across a gateway restart): keep
    // the failed marker + accumulated log so the user has something to act on.
    notify(i18nT('pages.devFleetPage.provision_polling_timed_out_check_pod_logs'), { type: 'error' })
    setProv((p) => ({ ...p, [name]: { rid, status: 'failed', failed: true, lines: acc.length ? acc : ['Provision polling timed out \u2014 check pod logs'], startedAt, exit: null } }))
    setProvLogOpen((o) => ({ ...o, [name]: true }))
    invalidateFleet()
  }

  async function provision(name: string) {
    // Synchronous guard: blocks re-entry before React re-renders the button into
    // its disabled state. A rapid double-click fires both event handlers in the
    // same render turn (before any setState takes effect), so checking React state
    // here would NOT catch the second click.  provInFlightRef is updated
    // synchronously and persists across renders, so it reliably blocks the second
    // invocation whether it arrives in the same turn or in a later one while the
    // request is still awaited. The finally block releases the guard after the
    // request/polling lifecycle exits; remounting creates a fresh ref.
    if (provInFlightRef.current.has(name)) {
      // The first invocation already owns the API request, polling, and UI state.
      // Returning here prevents both a duplicate POST and a second poll loop.
      return
    }
    provInFlightRef.current.add(name)
    const startedAt = Date.now()
    clearTimeout(provDoneTimersRef.current[name])
    setProvLogOpen((o) => { const n = { ...o }; delete n[name]; return n })
    setProv((p) => ({ ...p, [name]: { status: 'starting', lines: [], startedAt } }))
    try {
      const r = await api.post<{ ok?: boolean; run_id?: string }>('/pod/provision', { name })
      // The single-flight guard replies {ok:false, run_id:<in-flight rid>} when
      // a provision for this checkout is already running — that is NOT a
      // failure. Reattach to the existing run instead of rendering a false red
      // "Provision failed" state. Only a response with no run id to attach to
      // is a genuine failure.
      if (!r?.run_id) {
        notify(i18nT('pages.devFleetPage.provision_failed_to_start'), { type: 'error' })
        setProv((p) => ({ ...p, [name]: { status: 'failed', failed: true, lines: ['Provision failed to start'], startedAt, exit: null } }))
        setProvLogOpen((o) => ({ ...o, [name]: true }))
        return
      }
      // A poll loop for this run may already exist (the fleet-driven reattach
      // effect attaches to in-flight runs); never start a second one.
      if (provAttachedRef.current.has(r.run_id)) return
      provAttachedRef.current.add(r.run_id)
      await pollProvisionRun(name, r.run_id, startedAt)
    } catch (e: unknown) {
      const msg = (e as Error)?.message || String(e)
      notify(msg, { type: 'error' })
      setProv((p) => ({ ...p, [name]: { status: 'failed', failed: true, lines: [msg], startedAt, exit: null } }))
      setProvLogOpen((o) => ({ ...o, [name]: true }))
    } finally {
      // Release the per-name guard so a retry after failure or dismissal can
      // re-enter.  pollProvisionRun already owns its completion lifecycle;
      // this only gates the entry point.
      provInFlightRef.current.delete(name)
    }
  }

  async function removeWorktree(name: string, d: Worktree) {
    if (d?.is_main) { notify(i18nT('pages.devFleetPage.cannot_remove_the_main_worktree'), { type: 'error' }); return }
    const shipped = !!d?.shipped; const empty = d && d.own_commits === 0 && d.real_dirty === false
    // Dirt that is ONLY untracked files is session scratch (probe scripts,
    // capture harnesses, notes). The backend refuses both plain and forced
    // removal while any dirt remains, so without naming those files and asking
    // to discard them the row is a dead end. Tracked modifications never take
    // this path — they are unfinished work and stay protected.
    //
    // The paths list is capped by the server, so a discard is only offered when
    // it is COMPLETE: consent has to cover the whole set, and the server rejects
    // a partial one. Offering it on a truncated list would promise something the
    // backend refuses.
    const paths = d?.dirty_untracked_paths ?? []
    const untrackedOnly = d?.dirty_tracked === false && (d?.dirty_untracked ?? 0) > 0
      && paths.length === d?.dirty_untracked
    const files = paths.join(', ')
    // The discard line is ADDITIVE, never a replacement. A worktree can carry
    // unmerged commits AND a stray probe script at the same time, and this
    // removal still sends force — so telling the user only about the scratch
    // would describe a permanent branch deletion as harmless cleanup. The
    // stake-naming warning stays; the discard sentence is appended to it.
    const base = shipped ? i18nT('pages.devFleetPage.pr_merged_safe_to_remove_runs_git_worktree_remov') : empty ? i18nT('pages.devFleetPage.empty_worktree_cannot_be_undone') : i18nT('pages.devFleetPage.has_unmerged_work_removing_deletes_permanently')
    const desc = untrackedOnly
      ? base + ' ' + i18nT('pages.devFleetPage.discard_untracked_files', { count: d?.dirty_untracked ?? 0, files })
      : base
    const ok = await askConfirm(i18nT('pages.devFleetPage.remove_name', { name }), desc, { confirmLabel: shipped || empty ? i18nT('pages.devFleetPage.remove') : i18nT('pages.devFleetPage.delete_anyway'), danger: true })
    if (!ok) return
    setFlag(name + ':remove', true)
    try { const r = await api.post<{ ok?: boolean; error?: string }>('/worktree/remove', { name, force: !shipped && !empty, discard_untracked_paths: untrackedOnly ? paths : undefined }); if (r?.ok) { notify(i18nT('pages.devFleetPage.removed_name', { name }), { type: 'success' }); invalidateAll() } else notify(r?.error || i18nT('pages.devFleetPage.failed'), { type: 'error' }) }
    catch (e: unknown) { notify((e as Error)?.message || String(e), { type: 'error' }) }
    finally { setFlag(name + ':remove', false) }
  }

  // Normalize a failed POST /sync into the shape the caller below already
  // handles. The single-flight refusal is an HTTP 409 whose body names the run
  // already in flight, so it arrives as a thrown error and never as a returned
  // body — which is what left the `!ok && run_id` branch below unreachable.
  // Every other status is a real failure carrying its message.
  function syncPostFailure(e: unknown): { ok: false; run_id?: string; error: string } {
    let rid: unknown
    if (e instanceof ApiError && e.status === 409) {
      try { rid = (JSON.parse(e.body) as { run_id?: unknown })?.run_id } catch { /* not JSON */ }
    }
    return {
      ok: false,
      run_id: typeof rid === 'string' && rid ? rid : undefined,
      error: (e as Error)?.message || String(e),
    }
  }

  async function syncMain() {
    setFlag('__syncmain', true)
    try {
      const r = await api.post<{ ok?: boolean; run_id?: string; error?: string }>('/sync', {}).catch(syncPostFailure)
      if (!r?.ok && r?.run_id) {
        // Sync already running — reattach to the in-flight run instead of erroring.
        // A second press is a user who cannot see the run, so an error toast would
        // leave them exactly where they started. `startedAt` is provisional: the
        // poll loop recomputes elapsed from the run's own `started` on its first
        // tick, and it refuses a second loop for a rid already being polled.
        setSyncRun({ rid: r.run_id, status: 'running', lines: [], startedAt: Date.now() })
        pollSyncRun(r.run_id, Date.now())
        return
      }
      if (!r?.ok || !r.run_id) { notify(r?.error || i18nT('pages.devFleetPage.pull_build_failed_to_start'), { type: 'error' }); setFlag('__syncmain', false); return }
      setSyncRun({ rid: r.run_id, status: 'running', lines: [], startedAt: Date.now() })
      pollSyncRun(r.run_id, Date.now())
    } catch (e: unknown) { notify((e as Error)?.message || String(e), { type: 'error' }); setFlag('__syncmain', false) }
  }

  async function rebaseWorktree(name: string) {
    const ok = await askConfirm(i18nT('pages.devFleetPage.rebase_name', { name }), i18nT('pages.devFleetPage.fetches_latest_main_and_replays_refused_if_dirty'), { confirmLabel: i18nT('pages.devFleetPage.rebase') })
    if (!ok) return; setFlag(name + ':rebase', true)
    try {
      const r = await api.post<{ ok?: boolean; head?: string; ahead?: number; behind?: number; conflict?: boolean; error?: string }>('/rebase', { name })
      if (r?.ok) { const txt = i18nT('pages.devFleetPage.rebased_head', { head: (r.head || '?').slice(0, 7) }); showRebaseResult(name, { kind: 'ok', text: txt }); notify(txt, { type: 'success' }) }
      else if (r?.conflict) { showRebaseResult(name, { kind: 'conflict', text: i18nT('pages.devFleetPage.conflicts_aborted') }); notify(i18nT('pages.devFleetPage.rebase_conflicts'), { type: 'error' }) }
      else { showRebaseResult(name, { kind: 'error', text: r?.error || 'failed' }); notify(r?.error || i18nT('pages.devFleetPage.rebase_failed'), { type: 'error' }) }
      invalidateFleet()
    } catch (e: unknown) { notify((e as Error)?.message || String(e), { type: 'error' }) }
    finally { setFlag(name + ':rebase', false) }
  }

  async function pruneShipped() {
    setFlag('__prune', true)
    try {
      const r = await api.get<{ ok?: boolean; candidates?: { name: string; code?: string; unmerged_commits?: boolean }[]; kept?: { name: string; code?: string; dirty?: boolean; dirty_tracked?: boolean | null; dirty_untracked?: number; dirty_untracked_paths?: string[] }[]; scanned?: number; error?: string }>('/prune-candidates')
      if (!r || r.ok === false) { notify(r?.error || i18nT('pages.devFleetPage.prune_preview_failed'), { type: 'error' }); return }
      const cands = r.candidates || []
      const kept = r.kept || []
      if (!cands.length && !kept.length) { notify(i18nT('pages.devFleetPage.nothing_to_prune'), { type: 'info' }); return }
      setPruneSelected(new Set(cands.map((c: { name: string }) => c.name)))
      setPruneForceSelected(new Set())
      setPruneDialog({ candidates: cands, kept, scanned: r.scanned || 0 })
    } catch (e: unknown) { notify((e as Error)?.message || String(e), { type: 'error' }) }
    finally { setFlag('__prune', false) }
  }

  async function pruneExecute(rawNames: string[], rawForceNames: string[] = [], discardPaths: Record<string, string[]> = {}) {
    // Mirror the backend's order-preserving dedup: a duplicate would render
    // duplicate checklist rows and inflate the total for a batch the server
    // processes once.
    const names = Array.from(new Set(rawNames))
    const forceNames = Array.from(new Set(rawForceNames))
    // Rows whose only dirt is untracked scratch, each carrying the exact file
    // list that was displayed. Sent as its own field rather than folded into
    // force: it authorizes discarding those specific files and nothing more,
    // the server re-checks that the set is unchanged, and it refuses outright
    // if a tracked file is modified.
    const discardNames = Object.keys(discardPaths)
    const allNames = Array.from(new Set([...names, ...forceNames, ...discardNames]))
    if (!allNames.length) { notify(i18nT('pages.devFleetPage.nothing_selected'), { type: 'info' }); return }
    setPruneDialog(null)
    const seed: Record<string, { status: string; error?: string | null }> =
      Object.fromEntries(allNames.map((n) => [n, { status: 'pending', error: null }]))
    setPruneProgress({ names: allNames, items: seed, done: 0, total: allNames.length, running: true })
    try {
      // A rejected run ("prune already running") comes back ok:false with
      // HTTP 200 — starting the poll loop anyway would track the OTHER run's
      // items and render every row as a misleading "Pending".
      const start = await api.post<{ ok?: boolean; error?: string }>('/prune-run', { names, force_names: forceNames, discard_untracked_paths: discardPaths })
      if (!start || start.ok === false) {
        notify(start?.error || i18nT('pages.devFleetPage.prune_failed_to_start'), { type: 'error' })
        setPruneProgress(null)
        return
      }
      for (let i = 0; i < 400; i++) {
        await sleep(1500)
        if (!pollAliveRef.current) return
        let st: { running?: boolean; done?: number; items?: Record<string, { status?: string; error?: string | null }> } | null = null
        try { st = await api.get('/prune-status') } catch { continue }
        if (!st) continue
        // Rebuild the item map in the ORIGINAL selection order over the FULL
        // regular-plus-forced set: the checklist, denominator, and success
        // tally must all cover every name the backend tracks, forced worktrees
        // included. Counting over ``names`` alone drops the forced worktrees
        // from the denominator and the tally, restoring the ``1/0`` counter and
        // the false failure toast. Fall back to the pending seed for any name
        // the backend has not populated yet.
        const raw = st.items || {}
        const backendTotal = Object.keys(raw).length || allNames.length
        const items: Record<string, { status: string; error?: string | null }> =
          Object.fromEntries(allNames.map((n) => [n, {
            status: raw[n]?.status || 'pending',
            error: raw[n]?.error ?? null,
          }]))
        const running = st.running !== false && (st.done || 0) < backendTotal
        if (!running) {
          // A name the backend never tracked (filtered server-side, e.g. the
          // worktree vanished between preview and execute) must terminate as
          // an explained failure, not sit "Pending" in a finished checklist.
          for (const n of allNames) {
            if (!raw[n]) items[n] = { status: 'failed', error: 'not processed (unknown or no longer a worktree)' }
          }
        }
        setPruneProgress({ names: allNames, items, done: st.done || 0, total: allNames.length, running })
        if (!running) {
          const removed = allNames.filter((n) => items[n]?.status === 'done').length
          const failed = allNames.filter((n) => items[n]?.status === 'failed').length
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

  // Poll until the gateway reports a start identity DIFFERENT from the one
  // captured before the restart, then hard-reload into the fresh process.
  // Delegates to the route-independent global watcher so navigating away during
  // the build phase does not kill the restart poll. The component still manages
  // the overlay state; the global promise resolves even if the component unmounts.
  async function awaitGatewayBack(capturedId: string | null): Promise<void> {
    const result = await awaitGatewayBackGlobal(capturedId)
    if (result === 'reloaded') return
    if (result === 'aborted') return
    // timeout
    setRestarting(false)
    const timedOut = i18nT('pages.devFleetPage.gateway_did_not_come_back_within_60s_reload_the')
    notify(timedOut, { type: 'error' })
    setGatewayError(timedOut)
  }

  async function restartGateway() {
    const ok = await askConfirm(i18nT('pages.devFleetPage.restart_gateway_2'),
      // With a stage pending, a restart COMPLETES the cutover: the gateway
      // comes back on the staged checkout, the opposite of the cancel that
      // sits beside this control. The confirm must say which code comes up.
      pendingStage
        ? i18nT('pages.devFleetPage.restarting_completes_the_pending_cutover_boots', { staged: pendingStage.name })
        : i18nT('pages.devFleetPage.applies_the_last_pull_build_the_dashboard_will_b'),
      { confirmLabel: i18nT('pages.devFleetPage.restart') })
    if (!ok) return
    setRestarting(true)
    setGatewayError(null)
    try {
      const r = await api.postGateway<{ ok?: boolean; error?: string; start_id?: string | null }>('/restart-gateway', {})
      if (!r?.ok) {
        const msg = r?.error || i18nT('pages.devFleetPage.restart_failed')
        notify(msg, { type: 'error' }); setGatewayError(msg); setRestarting(false); return
      }
      // Wait for the NEW process (a different start identity), not "a 200 came
      // back" — see gatewayRecovered.
      await awaitGatewayBack(r.start_id ?? null)
    } catch (e: unknown) {
      // Bare transport text ("Failed to fetch") says nothing on its own, and this
      // lands in a persistent banner — lead with what failed.
      const msg = `${i18nT('pages.devFleetPage.restart_failed')}: ${(e as Error)?.message || String(e)}`
      notify(msg, { type: 'error' }); setGatewayError(msg); setRestarting(false)
    }
  }

  async function makeLive(w: Worktree | UndoTarget, opts?: { undo?: boolean }) {
    const undoing = opts?.undo === true
    // Only the already-live row is blocked. Main is a valid target when it is
    // NOT live (after a cutover to a feature worktree, this is the way back).
    // The live row is a valid target too while a cutover is staged onto another
    // worktree: re-confirming the running checkout as the live target is how
    // the stage is cancelled (the backend re-pins the pointer; nothing
    // restarts), and it is the only cancel the dashboard can offer.
    const cancellingStage = !undoing && !!('is_live' in w && w.is_live) && !!stagedWorktree
    if ('is_live' in w && w.is_live && !cancellingStage) return
    if (!w.path) { notify(i18nT('pages.devFleetPage.cannot_resolve_worktree_path_for_name', { name: w.name }), { type: 'error' }); return }
    // The dialog must not promise an automatic restart on a host where Dev Fleet
    // cannot drive the service: there the cutover only STAGES, and the operator
    // finishes it by hand. Keyed off the same signal the backend uses to decide,
    // so the copy cannot drift from what actually happens.
    const canRestart = fleet?.gateway_service_active === true
    const ok = await askConfirm(
      undoing
        ? i18nT('pages.devFleetPage.undo_to_name', { name: w.name })
        : cancellingStage
          ? i18nT('pages.devFleetPage.cancel_staged_cutover_2')
          : i18nT('pages.devFleetPage.make_name_live', { name: w.name }),
      undoing
        ? canRestart
          ? i18nT('pages.devFleetPage.switches_the_live_dashboard_back_to_name_and_restarts', { name: w.name })
          : i18nT('pages.devFleetPage.stages_name_as_the_live_target_to_finish_the_undo', { name: w.name, cmd: fleet?.manual_restart || 'kirocrew restart' })
        : cancellingStage
          ? i18nT('pages.devFleetPage.keeps_name_the_live_target_and_discards_the_stag', { name: w.name, staged: stagedWorktree?.name ?? '' })
          : canRestart
            ? i18nT('pages.devFleetPage.swaps_the_code_behind_the_live_dashboard_to_this')
            : i18nT('pages.devFleetPage.stages_the_code_behind_the_live_dashboard_manual', { cmd: fleet?.manual_restart || 'kirocrew restart' }),
      undoing
        ? { confirmLabel: i18nT('pages.devFleetPage.undo_to_name', { name: w.name }) }
        : cancellingStage
          ? { confirmLabel: i18nT('pages.devFleetPage.cancel_staged_cutover'), cancelLabel: i18nT('pages.devFleetPage.keep_cutover') }
          : { confirmLabel: i18nT('pages.devFleetPage.make_live') })
    if (!ok) return
    setFlag(w.name + ':makelive', true)
    try {
      const r = await api.postGateway<{
        ok?: boolean; error?: string; start_id?: string | null
        staged_only?: boolean; cancelled?: boolean; notice?: string
      }>('/make-live', cancellingStage && stagedWorktree?.path
        // Bind the cancel to the stage the operator confirmed: the backend
        // refuses (stage_changed) if another tab re-staged between the dialog
        // and this POST, instead of silently discarding a stage never seen.
        ? { path: w.path, expected_staged: stagedWorktree.path }
        : undoing
          ? { path: w.path, undo: true }
          : { path: w.path })
      if (!r?.ok) {
        // Same treatment as a failed restart: this branch surfaces
        // restart_detached's message, which names a remedy the operator has to
        // act on — useless in a 7s toast.
        const msg = r?.error || i18nT(undoing
          ? 'pages.devFleetPage.undo_make_live_failed'
          : 'pages.devFleetPage.make_live_failed')
        notify(msg, { type: 'error' }); setGatewayError(msg); setFlag(w.name + ':makelive', false); return
      }
      // Stage cancelled: the pointer is re-pinned at the running checkout and
      // no process is coming or going, so the restart overlay and the identity
      // handshake must both be skipped — waiting would strand the user on the
      // 60s timeout for a restart that never happens.
      if (r.cancelled) {
        if (r.notice) notify(r.notice, { type: 'info' })
        setFlag(w.name + ':makelive', false)
        invalidateFleet()
        return
      }
      // Staged, not bounced: this gateway is not a service Dev Fleet can
      // restart, so the operator finishes the cutover with the command the
      // backend names in `notice`. There is no replacement process coming, so
      // the restart overlay and the identity handshake must be skipped — waiting
      // would strand the user on a 60s timeout and bury the one instruction they
      // need.
      if (r.staged_only) {
        if (r.notice) notify(r.notice, { type: 'info' })
        setFlag(w.name + ':makelive', false)
        invalidateFleet()
        return
      }
      // Gateway is restarting into the new worktree — reuse the restart overlay
      // and the SAME identity handshake (a cutover is a restart into different
      // code, with the identical early-200 hazard). awaitGatewayBack reloads on
      // a fresh identity; only its timeout path returns here.
      setRestarting(true)
      await awaitGatewayBack(r.start_id ?? null)
      setFlag(w.name + ':makelive', false)
    } catch (e: unknown) {
      const operation = i18nT(undoing
        ? 'pages.devFleetPage.undo_make_live_failed'
        : 'pages.devFleetPage.make_live_failed')
      const msg = `${operation}: ${(e as Error)?.message || String(e)}`
      notify(msg, { type: 'error' }); setGatewayError(msg); setRestarting(false); setFlag(w.name + ':makelive', false)
    }
  }

  async function loadPodLogs(name: string) {
    setPodLogsLoading((l) => ({ ...l, [name]: true }))
    try {
      const r = await api.get<{ ok?: boolean; logs?: string; error?: string }>('/pod/logs?name=' + encodeURIComponent(name) + '&n=100')
      if (r?.ok) setPodLogs((l) => ({ ...l, [name]: r.logs || '(empty)' }))
      else notify(r?.error || i18nT('pages.devFleetPage.failed_to_load_logs'), { type: 'error' })
    } catch (e: unknown) { notify((e as Error)?.message || String(e), { type: 'error' }) }
    finally { setPodLogsLoading((l) => ({ ...l, [name]: false })) }
  }

  /* ─── Render ─── */
  const wts = fleet?.worktrees || []
  const running = wts.filter((w) => w.running).length
  const needsProv = wts.filter((w) => !w.is_main && !w.has_dist).length
  const undoTarget = fleet?.undo_target?.path ? fleet.undo_target : null
  const undoCurrent = undoTarget ? wts.find((w) => w.is_live && w.path) || null : null
  const undoKey = undoCurrent && undoTarget
    ? JSON.stringify([undoCurrent.path, undoTarget.path])
    : null
  const showUndoBanner = !!undoKey && dismissedUndoKey !== undoKey
  // A dismissal covers exactly one completed cutover. The moment the fleet
  // reports a different pair -- or none, because Switch back consumed the
  // history or another cutover overwrote it -- the receipt is spent. Without
  // this, "feature -> main, dismiss, back to main, feature again" recreates the
  // same path pair and the new cutover's banner would inherit the old dismissal.
  // Only a loaded, non-errored fleet payload can spend it: a reload must not
  // wipe the receipt before the fleet arrives, and an undo target whose live
  // row has not resolved yet is unknown, not different.
  useEffect(() => {
    if (dismissedUndoKey == null || !fleet || fleetError || fleet.error) return
    if (undoTarget && !undoCurrent) return
    if (undoKey !== dismissedUndoKey) {
      setDismissedUndoKey(null)
      persistDismissedUndoKey(null)
    }
  }, [dismissedUndoKey, fleet, fleetError, undoKey, undoTarget, undoCurrent])
  const error = fleetError ? (fleetError as Error).message : fleet?.error || null
  // Whether pods can run on this host. Default TRUE when the field is absent so
  // a dashboard talking to an older dev-fleet backend keeps its pod controls.
  const podsAvailable = fleet?.pods_available !== false
  const podsReason = fleet?.pods_unavailable_reason || null
  // Why Restart / Make live are unavailable, when they are. Rendered rather
  // than swallowed: hiding these controls with no explanation is what left a
  // macOS user with a successful Pull+Build and no way to apply it. Server-
  // provided prose, same as podsReason.
  const gatewayReason = fleet?.gateway_service_active === false
    ? (fleet?.gateway_service_reason || null)
    : null
  // Why the code being managed is not the code being run, when they differ.
  // Rendered ABOVE the other two notices because it explains them: an older
  // serving install is also what makes the Restart eligibility and the staged
  // bundle wrong, so reading those first sends you down the wrong trail.
  const servingReason = fleet?.serving_install_reason || null
  const isDiscoveryError = !fleetError && !!fleet?.error
  // Its own state, not an error: the backend found no Kiro Crew checkout to
  // manage, which on a first run is simply a question nobody has answered yet.
  const needsSetup = !fleetError && !!fleet?.needs_setup
  // Either way the fleet is UNKNOWN, so the same chrome is wrong: counts would
  // assert numbers nobody measured, and the row actions have nothing to act on.
  // A rejected fleet read is the same unknown: "Worktrees (0)" beside a
  // "Backend unavailable" notice would claim a measurement that never happened.
  const noFleet = needsSetup || isDiscoveryError || !!fleetError
  const ql = q.trim().toLowerCase()
  const matchesRow = (w: Worktree) => !ql || (w.name + ' ' + (w.branch || '')).toLowerCase().includes(ql)
  const statusRank = (w: Worktree) => (w.is_main ? 0 : w.running ? 1 : (!w.has_dist ? 3 : 2))
  // Secondary key for the status sort: the PR pill is the other "status" on a
  // row, so rows with equal pod status order by review state — active work
  // (open, then draft) floats up and finished work (closed, then merged)
  // sinks to the bottom of its group, next in spirit to the Prune-merged
  // button. Without this, a fleet that is mostly not-built degenerates into
  // a plain alphabetical list with open/merged pills interleaved at random.
  // Check order mirrors reviewState() below so the sort always agrees with
  // the rendered pill.
  const prRank = (w: Worktree) => {
    if (!w.pr) return 2
    const s = String(w.pr.state || '').toUpperCase()
    if (s === 'MERGED') return 4
    if (s === 'DRAFT' || w.pr.isDraft) return 1
    if (s === 'OPEN') return 0
    if (s === 'CLOSED') return 3
    return 2 // unknown state — rank with the PR-less rows
  }
  const mainRows = wts.filter((w) => w.is_main)
  const legacyAll = wts.filter((w) => !w.is_main && w.legacy)
  const others = wts.filter((w) => !w.is_main && matchesRow(w) && (showLegacy || !w.legacy))
  others.sort((a, b) => sortBy === 'name'
    ? compareText(a.name, b.name)
    : sortBy === 'recent'
      ? ((b.last_updated_at || 0) - (a.last_updated_at || 0)) || compareText(a.name, b.name)
      : sortBy === 'behind'
        ? ((b.behind || 0) - (a.behind || 0)) || compareText(a.name, b.name)
        : (statusRank(a) - statusRank(b)) || (prRank(a) - prRank(b)) || compareText(a.name, b.name))
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
    if (w.is_main) { variant = 'aim'; label = 'main'; title = i18nT('pages.devFleetPage.the_primary_checkout_this_fleet_is_discovered_fr') }
    else if (w.running) {
      // 200 = open; 401/403 = serving but auth-gated — all mean the pod is up
      // (matches pod/runtime_client.py health() contract; anonymous probes get 403).
      const healthy = !!w.health && ((w.health >= 200 && w.health < 400) || w.health === 401 || w.health === 403)
      variant = healthy ? 'ok' : 'err'
      label = healthy ? i18nT('pages.devFleetPage.pod_up') : i18nT('pages.devFleetPage.pod_sick')
      title = healthy ? i18nT('pages.devFleetPage.qa_pod_is_running_click_open_to_use_it') : i18nT('pages.devFleetPage.qa_pod_is_running_but_failing_its_health_check')
    }
    else if (!w.has_dist) { variant = 'muted'; label = i18nT('pages.devFleetPage.not_built'); title = i18nT('pages.devFleetPage.no_venv_ui_build_yet_provision_builds_this_workt') }
    else if (!podsAvailable) { variant = 'muted'; label = i18nT('pages.devFleetPage.built'); title = i18nT('pages.devFleetPage.built_but_pods_cannot_run_on_this_host_preview_i') }
    else { variant = 'muted'; label = 'ready'; title = i18nT('pages.devFleetPage.built_and_ready_spin_up_a_pod_from_the_row_menu') }
    return <Badge variant={variant} className="text-[10.5px] px-1.5 py-0" title={title}>{label}</Badge>
  }

  /** A failed or conflicting rebase, as its own block under the row. The inputs
   *  are all in git (nothing typed here), so the hand-off is on. */
  function renderRebaseFailure(w: Worktree) {
    const rr = rebaseResult[w.name]
    if (!rr || rr.kind === 'ok') return null
    return (
      <div style={{ margin: '2px 0 8px 32px' }}>
        <ErrorNotice message={rr.text} askAgent onDismiss={() => dismissRebaseResult(w.name)} testId={`rebase-error-${w.name}`} />
      </div>
    )
  }

  const columnHeader = (
    <div style={{ display: 'grid', gridTemplateColumns: '16px 84px minmax(0,1fr) 64px 48px 44px 212px', gap: 8, alignItems: 'center', padding: '2px 0 4px', fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.06em', color: 'var(--muted)', minWidth: 640 } as CSSProperties}>
      <span /><span>{i18nT('pages.devFleetPage.pod_2')}</span><span>{i18nT('pages.devFleetPage.worktree')}</span><span>{i18nT('pages.devFleetPage.pr_2')}</span><span title={i18nT('pages.devFleetPage.commits_behind_main')}>{i18nT('pages.devFleetPage.behind')}</span><span title={i18nT('pages.devFleetPage.last_commit_activity')}>{i18nT('pages.devFleetPage.updated')}</span><span style={{ textAlign: 'right' }}>{i18nT('pages.devFleetPage.actions')}</span>
    </div>
  )

  function renderRow(w: Worktree) {
    const open = !!expanded[w.name]; const rs = reviewState(w)
    const mut: CSSProperties = { fontSize: 12.5, color: 'var(--muted)', fontVariantNumeric: 'tabular-nums', fontFamily: 'ui-monospace, SF Mono, Menlo, monospace' }
    const prUrl = w.pr?.url || ''
    const isMainWithStepper = w.is_main && syncRun
    const pr = prov[w.name]
    const provActive = !w.is_main && !!pr
    return (
      <div key={w.name}>
        <div style={{ display: 'grid', gridTemplateColumns: '16px 84px minmax(0,1fr) 64px 48px 44px 212px', gap: 8, alignItems: 'center', padding: '5px 0', borderTop: '1px solid var(--border)', minHeight: 30, minWidth: 640 } as CSSProperties}>
          {w.is_main
            ? <span style={{ width: 15 }} />
            : <Clickable aria-label={open ? i18nT('pages.devFleetPage.collapse') : i18nT('pages.devFleetPage.expand')} aria-expanded={open} onClick={() => toggleExpand(w.name)} style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--muted)', display: 'flex', padding: 0, transform: open ? 'rotate(90deg)' : 'none', transition: 'transform .12s' } as CSSProperties}><ChevronRight size={15} className="lucide-inline" /></Clickable>}
          <span style={{ overflow: 'hidden', display: 'flex' } as CSSProperties}>{stateDot(w)}</span>
          <div style={{ minWidth: 0, display: 'flex', alignItems: 'baseline', gap: 6, whiteSpace: 'nowrap', overflow: 'hidden' } as CSSProperties}>
            <span style={{ fontFamily: 'ui-monospace, monospace', fontSize: 13.5, fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis' }}>{w.name}</span>
            {w.dirty ? <span title={i18nT('pages.devFleetPage.uncommitted_changes')}>{"\u2022"}</span> : null}
            {/* The primary checkout can be left parked on a feature branch (a
                past PR checked out in place and never switched back). The row's
                name is hardcoded to the base branch, so without this badge the
                fleet renders "main" while every git fact on the row (PR pill,
                behind count) describes the parked branch — and the user only
                learns the truth when Pull+Build refuses to sync. Requires
                base_branch in the payload so an absent field can never
                false-flag a repo whose base is not literally "main". */}
            {w.is_main ? (fleet?.base_branch && w.branch && w.branch !== fleet.base_branch
              ? <Badge variant="warn" className="text-[10px] px-1.5 py-0" title={i18nT('pages.devFleetPage.the_primary_checkout_is_on_branch_not_base', { branch: w.branch, base: fleet.base_branch })}>{i18nT('pages.devFleetPage.parked_on_branch', { branch: w.branch })}</Badge>
              : <span style={mut}>{i18nT('pages.devFleetPage.main')}</span>) : null}
            {/* live_verified false: the pointer names this row but a packaged gateway
                cannot confirm it runs this checkout, so the badge asks, not asserts,
                and the reason is visible text rather than a hover-only tooltip. */}
            {w.is_live ? (fleet?.live_verified === false
              ? <>
                  <Badge variant="warn" className="text-[10px] px-1.5 py-0" title={i18nT('pages.devFleetPage.marked_live_but_this_install_cant_confirm_it')}>{i18nT('pages.devFleetPage.live_unverified')}</Badge>
                  {/* Takes only the space left after the name (basis 0), so it
                      ellipsizes instead of squeezing the worktree name. */}
                  <span className="text-[10px] text-muted" data-testid="fleet-live-unverified-reason" title={i18nT('pages.devFleetPage.marked_live_but_this_install_cant_confirm_it')} style={{ overflow: 'hidden', textOverflow: 'ellipsis', minWidth: 0, flex: '1 1 0%', whiteSpace: 'nowrap' } as CSSProperties}>{i18nT('pages.devFleetPage.marked_live_but_this_install_cant_confirm_it')}</span>
                </>
              : <Badge variant="aim" className="text-[10px] px-1.5 py-0" title={i18nT('pages.devFleetPage.the_live_gateway_on_this_port_runs_from_this_che')}>{i18nT('pages.devFleetPage.live')}</Badge>) : null}
            {/* A staged cutover outlives the toast that announced it: without a
                persistent marker an operator who dismissed or missed the toast
                reads the old running image as the new one. */}
            {w.is_staged ? <Badge variant="warn" className="text-[10px] px-1.5 py-0" title={stagedWorktree?.name === w.name
              ? i18nT('pages.devFleetPage.cutover_staged_run_cmd_to_finish_or_cancel', { cmd: fleet?.manual_restart || 'kirocrew restart' })
              : i18nT('pages.devFleetPage.cutover_staged_run_the_restart_command_to_finish', { cmd: fleet?.manual_restart || 'kirocrew restart' })}>{i18nT('pages.devFleetPage.restart_pending')}</Badge> : null}
            {w.summary ? <span title={w.summary} style={{ fontSize: 11.5, color: 'var(--muted)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0, flex: '0 1 auto' } as CSSProperties}>{w.summary}</span> : null}
            {/* Inline system readout for a running pod: memory vs ceiling
                (colour-shifting near MemoryMax), CPU%, task count. Absent
                fields render nothing, so a pod the host cannot measure shows
                no readout rather than a fake 0. */}
            {w.running && !w.is_main ? <PodReadout r={w.pod_resources} /> : null}
          </div>
          {isMainWithStepper ? <DevFleetSyncStepper
            syncRun={syncRun} syncLogOpen={syncLogOpen} setSyncLogOpen={setSyncLogOpen}
            dismissSync={dismissSync}
          /> : provActive ? <DevFleetProvStepper
            w={w} prov={prov} provLogOpen={provLogOpen} dismissProv={dismissProv}
            toggleProvLog={toggleProvLog}
          /> : (
            <>
              {rs && prUrl ? <a href={prUrl} target="_blank" rel="noopener noreferrer" title={w.pr?.title || rs.word} style={{ textDecoration: 'none' }}><Badge variant={rs.variant}>{rs.word}</Badge></a> : <span style={{ ...mut, opacity: 0.5 }}>{"\u2014"}</span>}
              <span style={{ ...mut, opacity: (w.behind ?? 0) > 0 ? 1 : 0.5 }} title={(w.behind ?? 0) > 0 ? i18nT('pages.devFleetPage.commits_behind_main_2', { count: w.behind ?? 0 }) : i18nT('pages.devFleetPage.up_to_date_with_main')}>{(w.behind ?? 0) > 0 ? '\u2193' + w.behind : '\u2014'}</span>
              <span style={{ ...mut, opacity: 0.85 }}>{relTime(w.last_updated_at).replace(' ago', '')}</span>
              <div style={{ display: 'flex', gap: 6, justifyContent: 'flex-end', alignItems: 'center', minWidth: 0, flexWrap: 'wrap' } as CSSProperties}><DevFleetRowActions
                w={w} fleet={fleet} busy={busy} rebaseResult={rebaseResult} syncRun={syncRun}
                gatewayMutating={gatewayMutating} liveStateUnknown={liveStateUnknown}
                stagedWorktree={stagedWorktree} liveWorktree={liveWorktree}
                dismissRebaseResult={dismissRebaseResult} act={act} launchQa={launchQa}
                provision={provision} syncMain={syncMain} rebaseWorktree={rebaseWorktree}
                restartGateway={restartGateway} makeLive={makeLive} podsAvailable={podsAvailable}
              /></div>
            </>
          )}
        </div>
        {renderRebaseFailure(w)}
        {w.is_main && syncRun && syncLogOpen ? (
          <pre style={{ margin: '2px 0 8px 32px', padding: '8px 10px', maxHeight: 180, overflow: 'auto', fontSize: 11, lineHeight: 1.45, background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, whiteSpace: 'pre-wrap', wordBreak: 'break-all', minWidth: 640 } as CSSProperties}>{filterStepMarkers(syncRun.lines || []).join('\n') || '(no output yet)'}</pre>
        ) : null}
        {provActive && provLogOpen[w.name] && pr ? (
          <ProvLogPre lines={pr.lines || []} streaming={pr.status === 'running' || pr.status === 'starting'} />
        ) : null}
        {open && detailLoading[w.name] ? <ContentSkeleton rows={3} /> : null}
        {open && detail[w.name] ? (
          <div style={{ padding: '4px 0 14px 30px', fontSize: 12, minWidth: 640 }}>
            {detail[w.name].error
              // Read failure of the row's detail fetch — nothing typed, hand-off on.
              ? <ErrorNotice message={detail[w.name].error} askAgent testId={`worktree-detail-error-${w.name}`} />
              : <DetailPanel w={w} d={detail[w.name]} busy={busy} onRemove={() => removeWorktree(w.name, { ...w, ...detail[w.name] })} onLoadLogs={() => loadPodLogs(w.name)} logs={podLogs[w.name]} logsLoading={podLogsLoading[w.name]} />}
          </div>
        ) : null}
      </div>
    )
  }

  const legacyToggle = legacyAll.length > 0 ? (
    <Btn onClick={() => setShowLegacy((v) => !v)} style={{ display: 'block', width: '100%', textAlign: 'left', marginTop: 4, fontSize: 11.5, color: 'var(--muted)', background: 'transparent', border: '1px dashed var(--border)', minWidth: 640 }} title={i18nT('pages.devFleetPage.worktrees_created_under_a_previous_repository_na')}>
      {showLegacy ? i18nT('pages.devFleetPage.hide_legacy_worktrees', { n: legacyAll.length }) : i18nT('pages.devFleetPage.legacy_worktrees_hidden_show', { n: legacyAll.length })}
    </Btn>
  ) : null
  let body: ReactNode
  if (loading && !fleet) body = <ContentSkeleton rows={5} />
  else if (needsSetup) body = (
    <div role="region" aria-labelledby="devfleet-setup-title" data-testid="devfleet-needs-setup" style={{ padding: 24, borderRadius: 8, border: '1px solid var(--border)', background: 'var(--card)' }}>
      <h2 id="devfleet-setup-title" style={{ margin: 0, fontWeight: 600, fontSize: 15 }}>{i18nT('pages.devFleetPage.no_checkout_found')}</h2>
      <p style={{ margin: '8px 0 0', color: 'var(--muted)', fontSize: 14 }}>{i18nT('pages.devFleetPage.no_checkout_found_help')}</p>
      {/* The remedy is its own line: bundled into the explanation it has to be
          re-read to be acted on. Both halves are whole sentences, so neither
          key depends on the other's word order in translation. */}
      <p style={{ margin: '8px 0 0', fontSize: 14 }}>{i18nT('pages.devFleetPage.no_checkout_found_action')}</p>
      <p style={{ margin: '8px 0 0', fontFamily: 'ui-monospace, monospace', fontSize: 13, color: 'var(--text)', overflowWrap: 'anywhere' }}>{i18nT('pages.devFleetPage.no_checkout_found_env_example')}</p>
    </div>
  )
  // Both branches are read failures of the fleet itself (a backend {error}
  // body, or the request rejecting) — nothing on this page is typed, so the
  // hand-off is on. `title` keeps the two failure modes distinguishable.
  else if (error) body = isDiscoveryError
    ? <ErrorNotice title={i18nT('pages.devFleetPage.discovery_error')} message={error} askAgent testId="fleet-discovery-error" />
    : <ErrorNotice title={i18nT('pages.devFleetPage.backend_unavailable')} message={error} askAgent testId="fleet-backend-error" />
  else if (!wts.length) body = <EmptyState icon={<Server size={28} className="lucide-inline" />} title={i18nT('pages.devFleetPage.no_worktrees_found')} subtitle={i18nT('pages.devFleetPage.nothing_under_the_worktrees_root_yet')} />
  // Worktrees are discovered from git independently of the live-target pointer,
  // so a gateway that cannot report which checkout is live still yields rows.
  // Rendering those rows with no badge would read as "nothing is live" — the
  // opposite remedy (stage a cutover) from the true one (check the gateway) — so
  // the unknown state is an error notice above the list, never a bare list.
  // The copy ends by naming the hand-off ("Ask the agent before making a
  // checkout live"), so the link goes BELOW it, a row of its own inside the
  // text column: beside, it floats into the copy's first line and the sentence
  // reads around it at some widths ("use the ✨Ask the agent Ask-the-agent
  // link…") — #10831. Opt-in on this notice only; every other ErrorNotice keeps
  // its default layout.
  else body = (
    <div>
      {fleet?.live_state_known === false && (
        <ErrorNotice
          title={i18nT('pages.devFleetPage.live_state_unknown')}
          message={i18nT('pages.devFleetPage.live_state_unknown_help')}
          askAgent
          actionPlacement="below"
          testId="fleet-live-state-unknown"
        />
      )}
      {columnHeader}{visible.map(renderRow)}{legacyToggle}
    </div>
  )

  const confirmDialog = (
    <Modal open={!!confirmReq} onClose={() => settleConfirm(false)} title={confirmReq?.title ?? ''} maxWidth={confirmReq?.width || 400} footer={<><Btn onClick={() => settleConfirm(false)}>{confirmReq?.cancelLabel || i18nT('pages.devFleetPage.cancel')}</Btn><Btn primary={!confirmReq?.danger} danger={!!confirmReq?.danger} onClick={() => settleConfirm(true)}>{confirmReq?.confirmLabel || i18nT('pages.devFleetPage.confirm')}</Btn></>}>
      <p className="text-sm text-muted m-0">{confirmReq?.desc}</p>
    </Modal>
  )

  const pruneReviewDialog = pruneDialog && <DevFleetPruneReview
    fleet={fleet} pruneDialog={pruneDialog} setPruneDialog={setPruneDialog} pruneSelected={pruneSelected}
    setPruneSelected={setPruneSelected} pruneForceSelected={pruneForceSelected}
    setPruneForceSelected={setPruneForceSelected} askConfirm={askConfirm} pruneExecute={pruneExecute}
  />

  const pruneProgressModal = pruneProgress && <DevFleetPruneProgress
    pruneProgress={pruneProgress} setPruneProgress={setPruneProgress}
  />

  const diskGb = disk?.total_mb != null ? (disk.total_mb / 1024).toFixed(0) + ' GB' : '\u2026'

  return (
    <>
      {confirmDialog}
      <ToastHost />
      {pruneReviewDialog}
      {pruneProgressModal}
      {restarting && (
        <div role="alert" aria-busy="true" style={{ position: 'fixed', inset: 0, zIndex: 9999, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', background: 'var(--bg)', color: 'var(--text)' }}>
          <LoaderCircle size={32} className="lucide-inline animate-spin" />
          <p style={{ marginTop: 16, fontSize: 16, fontWeight: 600 }}>{i18nT('pages.devFleetPage.restarting_reconnecting')}</p>
          <p style={{ fontSize: 12, color: 'var(--muted)' }}>{i18nT('pages.devFleetPage.waiting_for_the_new_gateway_process_the_page_rel')}</p>
        </div>
      )}
      <div className="flex flex-1 min-h-0 overflow-hidden">
        <div className="flex-1 min-w-0 flex flex-col min-h-0">
          <PageHeader title={i18nT('pages.devFleetPage.dev_fleet')} subtitle={i18nT('pages.devFleetPage.manage_the_git_worktrees_of_your_main_checkout_s')} />
          <div className="flex-1 overflow-y-auto px-4 md:px-6 pb-8 min-h-0">
            <DevFleetPageNotices
              fleet={fleet} diskFailed={diskFailed} diskError={diskError} actionError={actionError}
              actionErrorRef={actionErrorRef} dismissActionError={dismissActionError}
              gatewayError={gatewayError} setGatewayError={setGatewayError}
              setDismissedUndoKey={setDismissedUndoKey} gatewayMutating={gatewayMutating}
              makeLive={makeLive} wts={wts} running={running} needsProv={needsProv}
              undoTarget={undoTarget} undoCurrent={undoCurrent} undoKey={undoKey}
              showUndoBanner={showUndoBanner} podsAvailable={podsAvailable} podsReason={podsReason}
              gatewayReason={gatewayReason} servingReason={servingReason} noFleet={noFleet}
              diskGb={diskGb}
            />
            <Card>
              {/* Same reasoning as the dashed stat cards: "(0)" is a count of a
                  fleet that was never read, so the title drops it entirely. */}
              <CardTitle><span className="flex items-center gap-1.5">{noFleet ? i18nT('pages.devFleetPage.worktrees') : i18nT('pages.devFleetPage.worktrees_count', { count: wts.length })}{!noFleet && <InfoTip text={i18nT('pages.devFleetPage.every_git_worktree_of_the_main_checkout_pull_bui')} />}</span></CardTitle>
              {/* Filter, sort, Prune merged and Refresh all act on a fleet that
                  could not be read. Rendering them beside the setup card or the
                  discovery error invites a click whose only possible answer is a
                  failure, in the states with the least context to interpret it. */}
              {!noFleet && (
              <div className="flex flex-wrap gap-2.5 items-center mt-3 mb-1">
                <div className="flex-1 min-w-[140px]">
                  <SearchInput placeholder={i18nT('pages.devFleetPage.filter_worktrees')} value={q} onChange={(e) => setQ((e.target as HTMLInputElement).value)} aria-label={i18nT('pages.devFleetPage.filter_worktrees_2')} />
                </div>
                <span style={{ fontSize: 11.5, color: 'var(--muted)', flexShrink: 0 }}>{ql ? others.length + ' / ' : ''}{wts.length} {i18nT('pages.devFleetPage.rows')}</span>
                <SimpleSelect
                  options={['status', 'recent', 'name', 'behind']}
                  optionLabels={[
                    i18nT('pages.devFleetPage.sort_status'),
                    i18nT('pages.devFleetPage.sort_recent'),
                    i18nT('pages.devFleetPage.sort_name'),
                    i18nT('pages.devFleetPage.sort_behind'),
                  ]}
                  value={sortBy}
                  onChange={setSortBy}
                  aria-label={i18nT('pages.devFleetPage.sort_worktrees')}
                  // The retired `Select` carried `flexShrink: 0` in its base
                  // style; keep the toolbar behaving the same way.
                  style={{ flexShrink: 0 }}
                />
                {/* The merged-scan runs git per worktree, so a large fleet keeps
                    the button pressed for seconds with no other surface to
                    report on: the trash glyph becomes a spinner in place so the
                    click is visibly still working rather than merely disabled.
                    While the scan runs the label names the read-only action and
                    the `danger` variant is suppressed: a spinner on a
                    destructive-styled "Prune merged" reads as "deletion in
                    progress", but nothing is deleted until the review dialog is
                    confirmed. `aria-busy` stays as-is for assistive tech. */}
                <Btn danger={!busy['__prune']} onClick={pruneShipped} disabled={!!busy['__prune']} aria-busy={!!busy['__prune']}>{iconLabel(busy['__prune'] ? <LoaderCircle className="lucide-inline animate-spin" /> : <Trash2 size={13} className="lucide-inline" />, i18nT(busy['__prune'] ? 'pages.devFleetPage.scanning_merged' : 'pages.devFleetPage.prune_merged'))}</Btn>
                <Btn onClick={() => invalidateAll()} disabled={loading} aria-label={i18nT('pages.devFleetPage.refresh_fleet')}>{iconLabel(<RefreshCw size={14} className="lucide-inline" />, i18nT('pages.devFleetPage.refresh'))}</Btn>
              </div>
              )}
              <div className="overflow-x-auto -mx-1 px-1">
                {body}
              </div>
            </Card>
          </div>
        </div>
      </div>
    </>
  )
}
