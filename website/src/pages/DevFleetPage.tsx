/**
 * Dev Fleet — worktree management page ported to KiroCrew SPA.
 * Manages git worktrees, pod instances, syncing, pruning, and rebasing.
 */
import { useState, useRef, useCallback, useEffect, type CSSProperties, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Card, CardTitle, Btn, Checkbox, StatCard, EmptyState, ContentSkeleton, PageHeader, SearchInput, Badge } from '../components/ui'
import SimpleSelect from '../components/SimpleSelect'
import InfoTip from '../components/InfoTip'
import Modal from '../components/Modal'
import Clickable from '../components/Clickable'
import { useNavigate } from 'react-router-dom'
import { useAppDispatch } from '../store'
import { addNotification } from '../store/notificationsSlice'
import { setPendingInput } from '../store/chatSlice'
import {
  Server, RefreshCw, Play, Square, ExternalLink, ChevronRight, Trash2,
  LoaderCircle, Check, Video, X,
  Ellipsis, RotateCw, FileText, GitCommit, Rocket, Info, AlertTriangle,
} from 'lucide-react'
import * as api from './devFleetApi'

import { i18nT } from '../i18n/t'
import { compareText } from '../i18n/format'
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
// build), weighted by typical duration. Shown as a single coarse percentage
// with no per-step labels, which would imply more precision than we have.
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

/* ─── Restart identity handshake ─── */
// POST /restart-gateway and /make-live return the unit's start identity
// captured BEFORE the bounce; GET /apps/dev-fleet/api/health reports the CURRENT
// one. (It must be the /api/ path: the gateway only proxies /apps/dev-fleet/api/*
// to the backend -- the bare /health is the gateway's own internal liveness
// poll and never reaches the browser.) The UI holds "Restarting — reconnecting"
// until it observes a DIFFERENT identity, so a 200 from the OLD process still
// winding down never counts as recovered (the re-click trap this prevents).
const RESTART_TIMEOUT_MS = 60000

// Recovered iff we captured an identity AND the gateway now reports a different
// one. A null captured id (platform can't report identity) or a null/absent
// current id is NOT recovery — the caller degrades or keeps waiting.
export function gatewayRecovered(
  capturedId: string | null | undefined,
  currentId: string | null | undefined,
): boolean {
  if (capturedId == null || currentId == null) return false
  return String(currentId) !== String(capturedId)
}

/* ─── Provision progress model ─── */
// The last non-blank output line — the "current activity" shown inline.
function lastLine(lines: string[] | undefined): string {
  if (!lines) return ''
  for (let i = lines.length - 1; i >= 0; i--) { if (lines[i]?.trim()) return lines[i] }
  return ''
}

// Coarse phase tag derived from provision.py's markers ("[provision] creating
// venv …" then "[provision] building dist …"). Scans newest→oldest so the tag
// reflects the current step; returns null when nothing recognizable is in view.
function provPhase(lines: string[] | undefined): string | null {
  if (!lines) return null
  for (let i = lines.length - 1; i >= 0; i--) {
    const l = (lines[i] || '').toLowerCase()
    if (l.includes('building dist') || l.includes('npm run build') || l.includes('vite') || l.includes('tsc ')) return 'dist'
    if (l.includes('creating venv') || l.includes('pip install') || l.includes('venv')) return 'venv'
  }
  return null
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

/**
 * Map a machine prune verdict code to a human-readable reason. Used both for
 * candidate rows and for surfacing WHY a kept row was not pruned. Exported so
 * the mapping can be unit-tested directly.
 */
export function pruneVerdictLabel(code?: string): string {
  switch (code) {
    case 'merged': return i18nT('pages.devFleetPage.pr_merged')
    case 'empty': return i18nT('pages.devFleetPage.no_commits_stale')
    case 'merged_dirty': return i18nT('pages.devFleetPage.pr_merged_uncommitted_changes')
    case 'fresh': return i18nT('pages.devFleetPage.created_recently')
    case 'active': return i18nT('pages.devFleetPage.pr_open_or_unmerged_commits')
    case 'merged_new_commits': return i18nT('pages.devFleetPage.pr_merged_but_new_commits_pushed_after_merge')
    case 'merged_unverified': return i18nT('pages.devFleetPage.pr_merged_but_verification_unavailable_retry')
    case 'dirty_check_failed': return i18nT('pages.devFleetPage.git_status_failed')
    default: return code || ''
  }
}

// Per-item prune status -> visual kind, driving the checklist's icon and badge.
export const PRUNE_STATUS_META: Record<string, { kind: 'idle' | 'spin' | 'done' | 'failed' }> = {
  pending: { kind: 'idle' },
  verifying: { kind: 'spin' },
  stopping_pod: { kind: 'spin' },
  removing: { kind: 'spin' },
  done: { kind: 'done' },
  failed: { kind: 'failed' },
}

/**
 * Catalog KEY for each prune status's chip label — kept in its own flat table,
 * beside PRUNE_STATUS_META (add a status to both).
 *
 * Keys, not strings: this is module scope, evaluated once at import, so an
 * `i18nT()` call here would freeze the boot language. `pruneStatusLabel()` does
 * the lookup during render. Flat `Record` of full literal keys indexed inline at
 * the `i18nT()` call, because that is the form `scripts/check-i18n-keys.mjs` can
 * resolve statically. `removing` / `failed` reuse the keys this page already
 * ships for those two words rather than adding duplicates.
 */
const PRUNE_STATUS_LABEL_KEY: Record<string, string> = {
  pending: 'pages.devFleetPage.pending',
  verifying: 'pages.devFleetPage.verifying',
  stopping_pod: 'pages.devFleetPage.stopping_pod',
  removing: 'pages.devFleetPage.removing',
  done: 'pages.devFleetPage.removed',
  failed: 'pages.devFleetPage.failed',
}

/** Localised chip label for a prune status, falling back to the `pending` copy for
 *  the same reason the caller falls back to its meta — the status arrives from the
 *  /api/run poll, so an unrecognised value must still render something.
 *
 *  `hasOwnProperty`, not `in`: a status of `toString` would otherwise resolve to an
 *  inherited Object.prototype member and hand a function to i18next. */
function pruneStatusLabel(status: string): string {
  return Object.prototype.hasOwnProperty.call(PRUNE_STATUS_LABEL_KEY, status)
    ? i18nT(PRUNE_STATUS_LABEL_KEY[status])
    : i18nT(PRUNE_STATUS_LABEL_KEY.pending)
}

// Auto-scrolling <pre> for the FULL provision log (mirrors the sync log panel's
// styling). Sticks to the bottom while output is still streaming.
function ProvLogPre({ lines, streaming }: { lines: string[]; streaming: boolean }) {
  const ref = useRef<HTMLPreElement | null>(null)
  useEffect(() => {
    if (streaming && ref.current) ref.current.scrollTop = ref.current.scrollHeight
  }, [lines, streaming])
  return (
    <pre ref={ref} style={{ margin: '2px 0 8px 32px', padding: '8px 10px', maxHeight: 180, overflow: 'auto', fontSize: 11, lineHeight: 1.45, background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, whiteSpace: 'pre-wrap', wordBreak: 'break-all' } as CSSProperties}>{lines.join('\n') || '(no output yet)'}</pre>
  )
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
  if (s < 60) return i18nT('pages.devFleetPage.just_now')
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
// <Card overflow> can't clip it; these drive fixed positioning.
const MENU_GAP = 6        // gap between trigger and menu
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
    // The menu is portaled to <body>, so it is not a DOM descendant of
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
      <Btn ref={triggerRef} onClick={toggle} title={i18nT('pages.devFleetPage.more_actions')} aria-label={i18nT('pages.devFleetPage.more_actions')} aria-haspopup="menu" aria-expanded={open}>
        <Ellipsis size={15} className="lucide-inline" />
      </Btn>
      {open && rect && createPortal(
        <div
          ref={menuRef}
          role="menu"
          aria-label={i18nT('pages.devFleetPage.more_actions')}
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
// Confirm popover width, and the height estimate that drives the flip
// decision. The estimate only picks a side; `maxHeight` + `overflowY` below
// keep the popover inside the viewport even when a locale's `desc` wraps to
// more lines than assumed here.
const CONFIRM_W = 264
const CONFIRM_EST_H = 140
function ConfirmBtn({ title, desc, confirmLabel, onConfirm, btn, children }: ConfirmBtnProps) {
  const [open, setOpen] = useState(false)
  // Trigger rect captured on open; drives the portaled popover's fixed
  // position. Same approach as MenuBtn above: an absolutely positioned
  // popover is clipped by the row's `.card-glow { overflow: hidden }`
  // ancestor, so it must be portaled to <body> instead.
  const [rect, setRect] = useState<DOMRect | null>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const popRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    // Portaled to <body>, so the popover is not a DOM descendant of the
    // trigger — the outside-click guard must exclude BOTH, or every click
    // inside the popover (including Cancel/Start) would close it first.
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node
      if (!triggerRef.current?.contains(t) && !popRef.current?.contains(t)) setOpen(false)
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

  // Right-align to the trigger (as before), clamped so the popover never sits
  // flush against a viewport edge. Open downward by default; flip up when
  // there is no room below and more room above. Either `top` or `bottom` is
  // set, never both.
  const spaceBelow = rect ? window.innerHeight - rect.bottom - MENU_GAP : 0
  const spaceAbove = rect ? rect.top - MENU_GAP : 0
  const openUp = !!rect && spaceBelow < CONFIRM_EST_H + MENU_MARGIN && spaceAbove > spaceBelow
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
      <Btn ref={triggerRef} {...(btn || {})} onClick={toggle} aria-haspopup="dialog" aria-expanded={open}>{children}</Btn>
      {open && rect && createPortal(
        <div
          ref={popRef}
          role="dialog"
          aria-label={title}
          data-placement={openUp ? 'up' : 'down'}
          style={{ ...posStyle, zIndex: 4000, overflowY: 'auto', background: 'var(--card, #16161a)', border: '1px solid var(--border)', borderRadius: 10, padding: '10px 12px', width: CONFIRM_W, boxShadow: '0 8px 24px rgba(0,0,0,0.45)', textAlign: 'left' as const } as CSSProperties}
        >
          <div style={{ fontSize: 12.5, fontWeight: 600, marginBottom: 4 }}>{title}</div>
          <div style={{ fontSize: 11.5, color: 'var(--muted)', lineHeight: 1.5, marginBottom: 9 }}>{desc}</div>
          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' } as CSSProperties}>
            <Btn onClick={() => setOpen(false)}>{i18nT('pages.devFleetPage.cancel')}</Btn>
            <Btn primary onClick={() => { setOpen(false); onConfirm() }}>{confirmLabel || i18nT('pages.devFleetPage.start')}</Btn>
          </div>
        </div>,
        document.body,
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
  own_commits?: number; real_dirty?: boolean; is_live?: boolean; is_staged?: boolean; legacy?: boolean
  path?: string
}
interface FleetData { worktrees: Worktree[]; error?: string; base_branch?: string; sync_run_id?: string; build_pending?: boolean; gateway_service_active?: boolean; gateway_service_reason?: string | null; pods_available?: boolean; pods_unavailable_reason?: string | null; serving_install_reason?: string | null; staged_target?: string | null; manual_restart?: string }
interface SyncRun { rid: string; status: 'running' | 'done' | 'error'; phase: number; phaseAt?: number; lines: string[]; startedAt: number; exit?: number | null; last?: string; stepLabel?: string }
// Provision run state: the FULL output is kept (not just the last
// line) so the expandable log panel can show everything, and a failed run
// persists (failed=true) until the user dismisses it rather than vanishing.
interface ProvRun { status: 'starting' | 'running' | 'done' | 'failed'; lines: string[]; startedAt: number; exit?: number | null; failed?: boolean; done?: boolean }
interface RebaseResult { kind: 'ok' | 'conflict' | 'error'; text: string }

/* ─── Detail Panel (expanded row) ─── */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function DetailPanel({ w, d, busy, onRemove, onLoadLogs, logs, logsLoading }: { w: Worktree; d: any; busy: Record<string, boolean>; onRemove: () => void; onLoadLogs: () => void; logs?: string; logsLoading?: boolean }) {
  const mono: CSSProperties = { fontFamily: 'ui-monospace, SF Mono, Menlo, monospace', fontSize: 11.5 }
  const mutedSm: CSSProperties = { fontSize: 11, color: 'var(--muted)', lineHeight: 1.6 }
  const [logsOpen, setLogsOpen] = useState(false)
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      <div style={mutedSm}>{i18nT('pages.devFleetPage.branch')} <span style={{ ...mono, color: 'var(--text)' }}>{d.branch || '?'}</span></div>
      {d.pr ? (
        <div style={mutedSm}>
          {i18nT('pages.devFleetPage.pr')} <a href={d.pr.url || '#'} target="_blank" rel="noopener noreferrer" title={d.pr.title || undefined} style={{ color: 'var(--accent)' }}>
            #{d.pr.number || '?'}{d.pr.title ? ' \u2014 ' + d.pr.title : ''}
          </a>{' '}
          <Badge variant={d.pr.state === 'MERGED' ? 'aim' : d.pr.state === 'OPEN' ? 'ok' : 'warn'}>
            {(d.pr.state || '').toLowerCase()}
          </Badge>
        </div>
      ) : null}
      {d.summary ? (
        <div style={mutedSm}>{i18nT('pages.devFleetPage.purpose')} <span style={{ color: 'var(--text)' }}>{d.summary}</span></div>
      ) : null}
      {d.issues?.length > 0 ? (
        <div style={mutedSm}>
          {i18nT('pages.devFleetPage.issues')}{' '}
          {d.issues.map((it: IssueRef, i: number) => (
            it.url
              ? <a key={i} href={it.url} target="_blank" rel="noopener noreferrer" style={{ color: 'var(--accent)', marginRight: 8 }}>#{it.number}</a>
              : <span key={i} style={{ color: 'var(--text)', marginRight: 8 }}>#{it.number}</span>
          ))}
        </div>
      ) : null}
      {d.tickets?.length > 0 ? (
        <div style={mutedSm}>
          {i18nT('pages.devFleetPage.tickets')}{' '}
          {d.tickets.map((t: TicketRef, i: number) => (
            t.url
              ? <a key={i} href={t.url} target="_blank" rel="noopener noreferrer" style={{ color: 'var(--accent)', marginRight: 8 }}>{t.id}</a>
              : <span key={i} style={{ color: 'var(--text)', marginRight: 8 }}>{t.id}</span>
          ))}
        </div>
      ) : null}
      {d.design_docs?.length > 0 ? (
        <div style={mutedSm}>
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}><FileText size={11} className="lucide-inline" /> {i18nT('pages.devFleetPage.design_docs')}</span>
          <ul style={{ margin: '2px 0 0 16px', padding: 0, listStyle: 'none' }}>
            {d.design_docs.map((doc: string, i: number) => <li key={i} style={mono}>{doc}</li>)}
          </ul>
        </div>
      ) : null}
      {d.commits?.length > 0 ? (
        <div style={mutedSm}>
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}><GitCommit size={11} className="lucide-inline" /> {i18nT('pages.devFleetPage.commits')}</span>
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
      {d.disk_mb != null ? <div style={mutedSm}>{i18nT('pages.devFleetPage.disk')} {d.disk_mb} {i18nT('pages.devFleetPage.mb')}</div> : null}
      {d.pod_running ? (
        <div style={mutedSm}>
          {i18nT('pages.devFleetPage.pod_running_on')}{d.pod_port || '?'}
        </div>
      ) : null}
      <div style={{ display: 'flex', gap: 8, marginTop: 4 }}>
        {d.pod_running ? (
          <Btn onClick={() => { if (!logsOpen) { setLogsOpen(true); onLoadLogs() } else setLogsOpen(false) }} disabled={!!logsLoading}>
            {iconLabel(<FileText size={12} className="lucide-inline" />, logsLoading ? i18nT('pages.devFleetPage.loading') : logsOpen ? i18nT('pages.devFleetPage.hide_logs') : i18nT('pages.devFleetPage.load_pod_logs'))}
          </Btn>
        ) : null}
        {!w.is_main ? (
          <Btn danger onClick={onRemove} disabled={!!busy[w.name + ':remove']}>
            {iconLabel(<Trash2 size={13} className="lucide-inline" />, i18nT('pages.devFleetPage.remove'))}
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
  const navigate = useNavigate()
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
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const [prov, setProv] = useState<Record<string, ProvRun | null>>({})
  const [provLogOpen, setProvLogOpen] = useState<Record<string, boolean>>({})
  const provDoneTimersRef = useRef<Record<string, ReturnType<typeof setTimeout>>>({})
  const [rebaseResult, setRebaseResult] = useState<Record<string, RebaseResult>>({})
  // A failed restart is the one error on this page that carries an instruction
  // rather than just a symptom. Toasts are pointer-events:none and self-dismiss,
  // so they cannot be selected or copied and a long message vanishes mid-read —
  // keep the text on the page until it is dealt with.
  const [gatewayError, setGatewayError] = useState<string | null>(null)
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
  function dismissProv(name: string) {
    clearTimeout(provDoneTimersRef.current[name])
    setProv((p) => { const n = { ...p }; delete n[name]; return n })
    setProvLogOpen((o) => { const n = { ...o }; delete n[name]; return n })
    invalidateFleet()
  }
  function toggleProvLog(name: string) { setProvLogOpen((o) => ({ ...o, [name]: !o[name] })) }
  const [confirmReq, setConfirmReq] = useState<{ title: string; desc: ReactNode; confirmLabel?: string; danger?: boolean; width?: number; resolve: (v: boolean) => void } | null>(null)
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
  const [pruneDialog, setPruneDialog] = useState<{ candidates: { name: string; code?: string }[]; kept: { name: string; code?: string }[]; scanned: number } | null>(null)
  const [pruneSelected, setPruneSelected] = useState<Set<string>>(new Set())
  const [pruneProgress, setPruneProgress] = useState<{ names: string[]; items: Record<string, { status: string; error?: string | null }>; done: number; total: number; running: boolean } | null>(null)
  const askConfirm = (title: string, desc: ReactNode, opts?: { confirmLabel?: string; danger?: boolean; width?: number }) => new Promise<boolean>((resolve) => setConfirmReq({ title, desc, ...(opts || {}), resolve }))
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
    syncAttachedRef.current = true
    const rid = fleet.sync_run_id
    api.get<{ status?: string; output?: string[]; started?: number; step?: number; step_label?: string }>('/run?id=' + rid)
      .then((run) => {
        if (run?.status === 'running') {
          const t0 = run.started ? run.started * 1000 : Date.now()
          setSyncRun({ rid, status: 'running', phase: typeof run.step === 'number' ? run.step : syncPhaseFromLines(run.output || [], 0), lines: run.output || [], startedAt: t0, stepLabel: run.step_label })
          pollSyncRun(rid, t0)
        }
      })
      .catch(() => { /* run endpoint unreachable — nothing to reattach */ })
  }, [fleet?.sync_run_id]) // eslint-disable-line react-hooks/exhaustive-deps

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
    let phase = 0
    let phaseAt = Date.now()
    for (let i = 0; i < 900; i++) {
      await sleep(2000)
      if (!pollAliveRef.current || cancelledRunsRef.current.has(rid)) return
      let run: { status?: string; output?: string[]; exit_code?: number; started?: number; step?: number; step_label?: string } | null = null
      let gone = false
      try { run = await api.get('/run?id=' + rid) } catch (e) {
        // 404 = the gateway restarted and dropped the run registry — the run
        // is unrecoverable; freezing the bar forever was a real user trap.
        if ((e as { status?: number })?.status === 404) gone = true
        else continue
      }
      if (gone || !run) {
        if (gone) {
          setSyncRun({ rid, status: 'error', phase: 0, lines: [], startedAt, last: i18nT('pages.devFleetPage.gateway_restarted_mid_sync_run_lost_check_git_st') })
          setFlag('__syncmain', false)
          notify(i18nT('pages.devFleetPage.sync_run_lost_gateway_restarted_mid_sync_re_run'), { type: 'error' })
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
        if (okRun) notify(i18nT('pages.devFleetPage.synced_restart_gateway_to_apply_the_new_build'), { type: 'success' })
        else notify(i18nT('pages.devFleetPage.pull_build_failed_exit_code_detail', { code: run.exit_code, detail: last }), { type: 'error' })
        invalidateFleet()
        return
      }
      setSyncRun({ rid, status: 'running', phase, phaseAt, lines: out, startedAt: t0, last, stepLabel: run.step_label })
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
  // fresh provision and by reattaching to an already-in-flight run.
  async function pollProvisionRun(name: string, rid: string, startedAt: number) {
    let acc: string[] = []
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
          setProv((p) => ({ ...p, [name]: { status: 'done', done: true, lines, startedAt, exit: 0 } }))
          invalidateFleet()
          provDoneTimersRef.current[name] = setTimeout(() => {
            setProv((p) => { const n = { ...p }; delete n[name]; return n })
            setProvLogOpen((o) => { const n = { ...o }; delete n[name]; return n })
          }, 2500)
        } else {
          // FAILURE PERSISTENCE: keep the run, auto-expand the log, hold until
          // the user dismisses it — a multi-minute failed provision must not
          // vanish into an empty row.
          setProv((p) => ({ ...p, [name]: { status: 'failed', failed: true, lines, startedAt, exit: run.exit_code } }))
          setProvLogOpen((o) => ({ ...o, [name]: true }))
          invalidateFleet()
        }
        return
      }
      if (run.status !== 'running') {
        notify(run.status === 'timeout' ? i18nT('pages.devFleetPage.provision_timed_out') : i18nT('pages.devFleetPage.provision_failed_status', { status: run.status }), { type: 'error' })
        setProv((p) => ({ ...p, [name]: { status: 'failed', failed: true, lines: lines.length ? lines : ['Provision ' + run.status], startedAt, exit: run.exit_code ?? null } }))
        setProvLogOpen((o) => ({ ...o, [name]: true }))
        invalidateFleet()
        return
      }
      setProv((p) => ({ ...p, [name]: { status: 'running', lines, startedAt } }))
    }
    // Poll budget exhausted (e.g. run id lost across a gateway restart): keep
    // the failed marker + accumulated log so the user has something to act on.
    notify(i18nT('pages.devFleetPage.provision_polling_timed_out_check_pod_logs'), { type: 'error' })
    setProv((p) => ({ ...p, [name]: { status: 'failed', failed: true, lines: acc.length ? acc : ['Provision polling timed out \u2014 check pod logs'], startedAt, exit: null } }))
    setProvLogOpen((o) => ({ ...o, [name]: true }))
    invalidateFleet()
  }

  async function provision(name: string) {
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
      await pollProvisionRun(name, r.run_id, startedAt)
    } catch (e: unknown) {
      const msg = (e as Error)?.message || String(e)
      notify(msg, { type: 'error' })
      setProv((p) => ({ ...p, [name]: { status: 'failed', failed: true, lines: [msg], startedAt, exit: null } }))
      setProvLogOpen((o) => ({ ...o, [name]: true }))
    }
  }

  async function removeWorktree(name: string, d: Worktree) {
    if (d?.is_main) { notify(i18nT('pages.devFleetPage.cannot_remove_the_main_worktree'), { type: 'error' }); return }
    const shipped = !!d?.shipped; const empty = d && d.own_commits === 0 && d.real_dirty === false
    const desc = shipped ? i18nT('pages.devFleetPage.pr_merged_safe_to_remove_runs_git_worktree_remov') : empty ? i18nT('pages.devFleetPage.empty_worktree_cannot_be_undone') : i18nT('pages.devFleetPage.has_unmerged_work_removing_deletes_permanently')
    const ok = await askConfirm(i18nT('pages.devFleetPage.remove_name', { name }), desc, { confirmLabel: shipped || empty ? i18nT('pages.devFleetPage.remove') : i18nT('pages.devFleetPage.delete_anyway'), danger: true })
    if (!ok) return
    setFlag(name + ':remove', true)
    try { const r = await api.post<{ ok?: boolean; error?: string }>('/worktree/remove', { name, force: !shipped && !empty }); if (r?.ok) { notify(i18nT('pages.devFleetPage.removed_name', { name }), { type: 'success' }); invalidateAll() } else notify(r?.error || i18nT('pages.devFleetPage.failed'), { type: 'error' }) }
    catch (e: unknown) { notify((e as Error)?.message || String(e), { type: 'error' }) }
    finally { setFlag(name + ':remove', false) }
  }

  async function syncMain() {
    setFlag('__syncmain', true)
    try {
      const r = await api.post<{ ok?: boolean; run_id?: string; error?: string }>('/sync', {})
      if (!r?.ok || !r.run_id) { notify(r?.error || i18nT('pages.devFleetPage.pull_build_failed_to_start'), { type: 'error' }); setFlag('__syncmain', false); return }
      setSyncRun({ rid: r.run_id, status: 'running', phase: 0, lines: [], startedAt: Date.now() })
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
      const r = await api.get<{ ok?: boolean; candidates?: { name: string; code?: string }[]; kept?: { name: string; code?: string }[]; scanned?: number; error?: string }>('/prune-candidates')
      if (!r || r.ok === false) { notify(r?.error || i18nT('pages.devFleetPage.prune_preview_failed'), { type: 'error' }); return }
      const cands = r.candidates || []
      const kept = r.kept || []
      if (!cands.length && !kept.length) { notify(i18nT('pages.devFleetPage.nothing_to_prune'), { type: 'info' }); return }
      setPruneSelected(new Set(cands.map((c: { name: string }) => c.name)))
      setPruneDialog({ candidates: cands, kept, scanned: r.scanned || 0 })
    } catch (e: unknown) { notify((e as Error)?.message || String(e), { type: 'error' }) }
    finally { setFlag('__prune', false) }
  }

  async function pruneExecute(rawNames: string[]) {
    // Mirror the backend's order-preserving dedup: a duplicate would render
    // duplicate checklist rows and inflate the total for a batch the server
    // processes once.
    const names = Array.from(new Set(rawNames))
    if (!names.length) { notify(i18nT('pages.devFleetPage.nothing_selected'), { type: 'info' }); return }
    setPruneDialog(null)
    const seed: Record<string, { status: string; error?: string | null }> =
      Object.fromEntries(names.map((n) => [n, { status: 'pending', error: null }]))
    setPruneProgress({ names, items: seed, done: 0, total: names.length, running: true })
    try {
      // A rejected run ("prune already running") comes back ok:false with
      // HTTP 200 — starting the poll loop anyway would track the OTHER run's
      // items and render every row as a misleading "Pending".
      const start = await api.post<{ ok?: boolean; error?: string }>('/prune-run', { names })
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
        // Rebuild the item map in the ORIGINAL selection order; fall back to
        // the pending seed for any name the backend has not populated yet.
        const raw = st.items || {}
        const backendTotal = Object.keys(raw).length || names.length
        const items: Record<string, { status: string; error?: string | null }> =
          Object.fromEntries(names.map((n) => [n, {
            status: raw[n]?.status || 'pending',
            error: raw[n]?.error ?? null,
          }]))
        const running = st.running !== false && (st.done || 0) < backendTotal
        if (!running) {
          // A name the backend never tracked (filtered server-side, e.g. the
          // worktree vanished between preview and execute) must terminate as
          // an explained failure, not sit "Pending" in a finished checklist.
          for (const n of names) {
            if (!raw[n]) items[n] = { status: 'failed', error: 'not processed (unknown or no longer a worktree)' }
          }
        }
        setPruneProgress({ names, items, done: st.done || 0, total: names.length, running })
        if (!running) {
          const removed = names.filter((n) => items[n]?.status === 'done').length
          const failed = names.filter((n) => items[n]?.status === 'failed').length
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
  // `capturedId == null` means the platform can't report identity (non-Linux /
  // no systemctl) — degrade to the legacy "reload on first response" so those
  // hosts don't hang in the overlay forever. Returns only on the timeout path;
  // the success path reloads the page, and the caller clears its own state.
  async function awaitGatewayBack(capturedId: string | null): Promise<void> {
    const deadline = Date.now() + RESTART_TIMEOUT_MS
    await sleep(3000)  // let the detached systemd-run tear the old listener down
    while (Date.now() < deadline) {
      if (!pollAliveRef.current) return  // component unmounted — stop the loop
      try {
        if (capturedId == null) {
          // Legacy degrade: no identity to compare, so any answer means "back".
          await fetch('/', { signal: AbortSignal.timeout(3000) })
          window.location.reload()
          return
        }
        const res = await fetch('/apps/dev-fleet/api/health', { credentials: 'same-origin', signal: AbortSignal.timeout(3000) })
        if (res.status === 404) {
          // The route answered 404, which means a gateway IS serving us — just
          // one whose dev-fleet backend predates /api/health. That is the normal
          // outcome of a cutover to an older worktree, and its identity can never
          // appear, so waiting for one would burn the full timeout. A reachable
          // 404 during the handshake is therefore recovery: reload into it.
          window.location.reload()
          return
        }
        if (res.ok) {
          const j = (await res.json().catch(() => null)) as { start_id?: string | null } | null
          if (gatewayRecovered(capturedId, j?.start_id)) { window.location.reload(); return }
          // A reachable health with the SAME id is the OLD process still winding
          // down (or identity unavailable) — keep waiting, never reload here.
        }
      } catch { /* gateway is down mid-bounce — keep polling */ }
      await sleep(2000)
    }
    setRestarting(false)
    // Same treatment as a failed restart: the user may have walked away during
    // the 60s overlay, and a self-dismissing toast leaves a stale page with no
    // explanation for why it never came back.
    const timedOut = i18nT('pages.devFleetPage.gateway_did_not_come_back_within_60s_reload_the')
    notify(timedOut, { type: 'error' })
    setGatewayError(timedOut)
  }

  async function restartGateway() {
    const ok = await askConfirm(i18nT('pages.devFleetPage.restart_gateway_2'), i18nT('pages.devFleetPage.applies_the_last_pull_build_the_dashboard_will_b'), { confirmLabel: i18nT('pages.devFleetPage.restart') })
    if (!ok) return
    setRestarting(true)
    setGatewayError(null)
    try {
      const r = await api.post<{ ok?: boolean; error?: string; start_id?: string | null }>('/restart-gateway', {})
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

  async function makeLive(w: Worktree) {
    // Only the already-live row is blocked. Main is a valid target when it is
    // NOT live (after a cutover to a feature worktree, this is the way back).
    if (w.is_live) return
    if (!w.path) { notify(i18nT('pages.devFleetPage.cannot_resolve_worktree_path_for_name', { name: w.name }), { type: 'error' }); return }
    // The dialog must not promise an automatic restart on a host where Dev Fleet
    // cannot drive the service: there the cutover only STAGES, and the operator
    // finishes it by hand. Keyed off the same signal the backend uses to decide,
    // so the copy cannot drift from what actually happens.
    const canRestart = fleet?.gateway_service_active === true
    const ok = await askConfirm(i18nT('pages.devFleetPage.make_name_live', { name: w.name }),
      canRestart
        ? i18nT('pages.devFleetPage.swaps_the_code_behind_the_live_dashboard_to_this')
        : i18nT('pages.devFleetPage.stages_the_code_behind_the_live_dashboard_manual', { cmd: fleet?.manual_restart || 'kirocrew restart' }),
      { confirmLabel: i18nT('pages.devFleetPage.make_live') })
    if (!ok) return
    setFlag(w.name + ':makelive', true)
    try {
      const r = await api.post<{
        ok?: boolean; error?: string; start_id?: string | null
        staged_only?: boolean; notice?: string
      }>('/make-live', { path: w.path })
      if (!r?.ok) {
        // Same treatment as a failed restart: this branch surfaces
        // restart_detached's message, which names a remedy the operator has to
        // act on — useless in a 7s toast.
        const msg = r?.error || i18nT('pages.devFleetPage.make_live_failed')
        notify(msg, { type: 'error' }); setGatewayError(msg); setFlag(w.name + ':makelive', false); return
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
      const msg = `${i18nT('pages.devFleetPage.make_live_failed')}: ${(e as Error)?.message || String(e)}`
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
      // (matches pod/runtime.py health() contract; anonymous probes get 403).
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

  function rowButtons(w: Worktree): ReactNode[] {
    if (w.is_main) {
      const out: ReactNode[] = [
        <ConfirmBtn key="sync" title={i18nT('pages.devFleetPage.pull_build_main')} desc={i18nT('pages.devFleetPage.pulls_main_and_rebuilds_6_min_does_not_restart')} confirmLabel={i18nT('pages.devFleetPage.start')} onConfirm={() => syncMain()} btn={{ disabled: !!busy['__syncmain'] || syncRun?.status === 'running' || gatewayMutating }}>
          {iconLabel(<RefreshCw size={13} className="lucide-inline" />, busy['__syncmain'] || syncRun?.status === 'running' ? i18nT('pages.devFleetPage.building') : i18nT('pages.devFleetPage.pull_build_2'))}
        </ConfirmBtn>,
      ]
      if (fleet?.gateway_service_active) {
        out.push(
          <Btn key="restart" onClick={() => restartGateway()} disabled={gatewayMutating} aria-label={i18nT('pages.devFleetPage.restart_gateway')}>
            {iconLabel(<RotateCw size={13} className="lucide-inline" />, i18nT('pages.devFleetPage.restart'))}
          </Btn>
        )
      }
      // After a cutover to a feature worktree, main is dormant (is_live=false)
      // and this inline control is the only way back to running main live. It sits
      // OUTSIDE the gateway_service_active gate on purpose: staging a cutover
      // needs no drivable service, and a host without one is precisely where
      // gating it would strand the operator on a feature worktree with no route
      // back. Consistent with makeLive()'s guard: shown iff the row is NOT live.
      if (!w.is_live) {
        out.push(
          <Btn key="makelive" onClick={() => makeLive(w)} disabled={gatewayMutating} title={i18nT('pages.devFleetPage.repoint_the_live_gateway_back_at_main_restarts_t')}>
            {iconLabel(<Rocket size={13} className="lucide-inline" />, i18nT('pages.devFleetPage.make_live'))}
          </Btn>
        )
      }
      if (fleet?.build_pending) {
        // Keep the visible text short: the ACTIONS grid column is fixed-width and
        // the Badge pill is whitespace-nowrap, so long text overflows leftward
        // into the UPDATED/BEHIND columns. Full instruction lives in the tooltip.
        out.push(<Badge key="bp" variant="warn" title={i18nT('pages.devFleetPage.build_pending_restart_gateway_to_apply_kirocrew')}>{i18nT('pages.devFleetPage.build_pending')}</Badge>)
      }
      return out
    }
    const out: ReactNode[] = []
    if (!w.has_dist) {
      // Active/failed provisioning is rendered as a row-spanning stepper (see
      // renderProvStepper), so this branch only offers the entry-point button.
      out.push(<Btn key="prov" onClick={() => provision(w.name)}>{i18nT('pages.devFleetPage.provision')}</Btn>)
    } else if (w.running) {
      out.push(<Btn key="open" onClick={() => act(w.name, 'open')}>{iconLabel(<ExternalLink size={13} className="lucide-inline" />, i18nT('pages.devFleetPage.open'))}</Btn>)
    }
    const podBusy = busy[w.name + ':up'] || busy[w.name + ':down'] || busy[w.name + ':restart']
    if (podBusy) out.push(<span key="podbusy" style={{ display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 11, color: 'var(--muted)' } as CSSProperties}><LoaderCircle size={12} className="lucide-inline" /> {i18nT('pages.devFleetPage.pod')}{"\u2026"}</span>)
    out.push(<MenuBtn key="menu" items={[
      podsAvailable && w.has_dist && !w.running ? { label: i18nT('pages.devFleetPage.spin_up_pod'), icon: <Play size={13} className="lucide-inline" />, onClick: () => act(w.name, 'up') } : null,
      podsAvailable && w.running ? { label: i18nT('pages.devFleetPage.restart_pod'), icon: <RefreshCw size={13} className="lucide-inline" />, onClick: () => act(w.name, 'restart') } : null,
      { label: i18nT('pages.devFleetPage.rebase_onto_main'), icon: <RefreshCw size={13} className="lucide-inline" />, onClick: () => rebaseWorktree(w.name), disabled: !!busy[w.name + ':rebase'] },
      // Staging a cutover writes only the live-target pointer, so it needs no
      // pod support and no drivable service — gating it on podsAvailable would
      // hide it on exactly the hosts it exists to serve.
      !w.is_live ? { label: i18nT('pages.devFleetPage.make_live'), icon: <Rocket size={13} className="lucide-inline" />, onClick: () => makeLive(w), disabled: gatewayMutating, title: i18nT('pages.devFleetPage.repoint_the_live_gateway_at_this_worktree_restar') } : null,
      // QA + video drives the pod-e2e suite, which brings a pod up.
      podsAvailable ? { label: i18nT('pages.devFleetPage.qa_video'), icon: <Video size={13} className="lucide-inline" />, onClick: () => launchQa(w.name) } : null,
      podsAvailable && w.running ? { label: i18nT('pages.devFleetPage.stop_pod'), icon: <Square size={13} className="lucide-inline" />, onClick: () => act(w.name, 'down'), danger: true } : null,
    ]} />)
    const rr = rebaseResult[w.name]
    if (rr) out.push(<Clickable key="rr" aria-label={i18nT('pages.devFleetPage.dismiss')} onClick={() => dismissRebaseResult(w.name)} style={{ fontSize: 11, color: rr.kind === 'ok' ? 'var(--ok)' : 'var(--danger)', cursor: 'pointer', maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', background: 'none', border: 'none', padding: 0 } as CSSProperties}>{rr.text}</Clickable>)
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
          <span style={{ fontSize: 11, fontWeight: 600, flexShrink: 0 }}>{i18nT('pages.devFleetPage.syncing')}</span>
          {syncRun.stepLabel ? <span style={{ ...mono, flexShrink: 0 } as CSSProperties} title={i18nT('pages.devFleetPage.current_step')}>{syncRun.stepLabel}</span> : null}
          <span role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100} aria-label={i18nT('pages.devFleetPage.sync_progress')} style={{ flex: 1, height: 4, borderRadius: 2, background: 'var(--border)', overflow: 'hidden', minWidth: 60 } as CSSProperties}>
            <span style={{ display: 'block', height: '100%', width: pct + '%', background: 'var(--accent)', borderRadius: 2, transition: 'width 0.6s ease' } as CSSProperties} />
          </span>
          <span style={{ ...mono, flexShrink: 0 } as CSSProperties}>{'~' + pct + '%'}</span>
          <span style={mono}>{fmtElapsed(Date.now() - syncRun.startedAt)}</span>
          <Clickable aria-label={i18nT('pages.devFleetPage.toggle_log')} onClick={() => setSyncLogOpen((o) => !o)} style={{ background: 'none', border: 'none', color: 'var(--muted)', cursor: 'pointer', fontSize: 11, padding: 2 } as CSSProperties}>{syncLogOpen ? i18nT('pages.devFleetPage.log') : i18nT('pages.devFleetPage.log_2')}</Clickable>
          <Clickable aria-label={i18nT('pages.devFleetPage.dismiss_sync_status')} onClick={() => dismissSync(syncRun?.rid)} style={{ background: 'none', border: 'none', color: 'var(--muted)', cursor: 'pointer', fontSize: 14, padding: 2 } as CSSProperties}>{"\u00d7"}</Clickable>
        </div>
      )
    }
    if (syncRun.status === 'done') {
      return (
        <div style={{ gridColumn: '4 / -1', display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 } as CSSProperties}>
          <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--ok)', display: 'inline-flex', alignItems: 'center', gap: 4 }}><Check size={12} className="lucide-inline" /> {i18nT('pages.devFleetPage.synced')}</span>
          <span style={{ fontSize: 11, color: 'var(--muted)' }}>{i18nT('pages.devFleetPage.restart_gateway_to_apply_the_new_build')}</span>
          <span style={{ flex: 1 }} />
          <Clickable aria-label={i18nT('pages.devFleetPage.toggle_log')} onClick={() => setSyncLogOpen((o) => !o)} style={{ background: 'none', border: 'none', color: 'var(--muted)', cursor: 'pointer', fontSize: 11, padding: 2 } as CSSProperties}>{syncLogOpen ? i18nT('pages.devFleetPage.log') : i18nT('pages.devFleetPage.log_2')}</Clickable>
          <Clickable aria-label={i18nT('pages.devFleetPage.dismiss_sync_status')} onClick={() => dismissSync(syncRun?.rid)} style={{ background: 'none', border: 'none', color: 'var(--muted)', cursor: 'pointer', fontSize: 14, padding: 2 } as CSSProperties}>{"\u00d7"}</Clickable>
        </div>
      )
    }
    // error
    return (
      <div style={{ gridColumn: '4 / -1', display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 } as CSSProperties}>
        <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--danger)' }}>{i18nT('pages.devFleetPage.pull_build_failed')}</span>
        <span style={{ ...mono, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 } as CSSProperties} title={syncRun.last}>{syncRun.last}</span>
        <Clickable aria-label={i18nT('pages.devFleetPage.toggle_log')} onClick={() => setSyncLogOpen((o) => !o)} style={{ background: 'none', border: 'none', color: 'var(--muted)', cursor: 'pointer', fontSize: 11, padding: 2 } as CSSProperties}>{syncLogOpen ? i18nT('pages.devFleetPage.log') : i18nT('pages.devFleetPage.log_2')}</Clickable>
        <Clickable aria-label={i18nT('pages.devFleetPage.dismiss_sync_status')} onClick={() => dismissSync(syncRun?.rid)} style={{ background: 'none', border: 'none', color: 'var(--muted)', cursor: 'pointer', fontSize: 14, padding: 2 } as CSSProperties}>{"\u00d7"}</Clickable>
      </div>
    )
  }

  /* ─── Provision stepper (inline at a worktree row) ─── */
  function renderProvStepper(w: Worktree) {
    const pr = prov[w.name]
    if (!pr) return null
    const mono: CSSProperties = { fontFamily: 'ui-monospace, monospace', fontVariantNumeric: 'tabular-nums', fontSize: 11, color: 'var(--muted)' }
    const open = !!provLogOpen[w.name]
    const logToggle = (
      <Clickable aria-label={i18nT('pages.devFleetPage.toggle_provision_log')} onClick={() => toggleProvLog(w.name)} style={{ background: 'none', border: 'none', color: 'var(--muted)', cursor: 'pointer', fontSize: 11, padding: 2 } as CSSProperties}>{open ? i18nT('pages.devFleetPage.log') : i18nT('pages.devFleetPage.log_2')}</Clickable>
    )
    if (pr.failed) {
      return (
        <div style={{ gridColumn: '4 / -1', display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 } as CSSProperties}>
          <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--danger)', flexShrink: 0, display: 'inline-flex', alignItems: 'center', gap: 4 }}><X size={12} className="lucide-inline" />{pr.exit != null ? i18nT('pages.devFleetPage.provision_failed_exit_code', { code: pr.exit }) : i18nT('pages.devFleetPage.provision_failed')}</span>
          <span style={{ ...mono, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 } as CSSProperties} title={lastLine(pr.lines)}>{lastLine(pr.lines)}</span>
          {logToggle}
          <Clickable aria-label={i18nT('pages.devFleetPage.dismiss_provision_status')} onClick={() => dismissProv(w.name)} style={{ background: 'none', border: 'none', color: 'var(--muted)', cursor: 'pointer', fontSize: 14, padding: 2 } as CSSProperties}>{"\u00d7"}</Clickable>
        </div>
      )
    }
    if (pr.done) {
      return (
        <div style={{ gridColumn: '4 / -1', display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 } as CSSProperties}>
          <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--ok)', display: 'inline-flex', alignItems: 'center', gap: 4 }}><Check size={12} className="lucide-inline" /> {i18nT('pages.devFleetPage.provisioned')}</span>
        </div>
      )
    }
    // starting / running
    const phase = provPhase(pr.lines)
    const last = lastLine(pr.lines)
    return (
      <div style={{ gridColumn: '4 / -1', display: 'flex', alignItems: 'center', gap: 10, minWidth: 0 } as CSSProperties}>
        <LoaderCircle size={12} className="lucide-inline" style={{ color: 'var(--accent)', flexShrink: 0 } as CSSProperties} />
        <span style={{ fontSize: 11, fontWeight: 600, flexShrink: 0 }}>{i18nT('pages.devFleetPage.provisioning')}</span>
        {phase ? <span style={{ fontSize: 10, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.05em', color: 'var(--accent)', background: 'var(--accent-subtle, rgba(99,102,241,0.14))', borderRadius: 5, padding: '1px 6px', flexShrink: 0 } as CSSProperties}>{phase}</span> : null}
        <span style={{ ...mono, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 } as CSSProperties} title={last}>{last || i18nT('pages.devFleetPage.starting')}</span>
        <span style={mono}>{fmtElapsed(Date.now() - pr.startedAt)}</span>
        {logToggle}
      </div>
    )
  }

  const columnHeader = (
    <div style={{ display: 'grid', gridTemplateColumns: '16px 84px minmax(0,1fr) 64px 48px 44px 212px', gap: 8, alignItems: 'center', padding: '2px 0 4px', fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.06em', color: 'var(--muted)' } as CSSProperties}>
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
        <div style={{ display: 'grid', gridTemplateColumns: '16px 84px minmax(0,1fr) 64px 48px 44px 212px', gap: 8, alignItems: 'center', padding: '5px 0', borderTop: '1px solid var(--border)', minHeight: 30 } as CSSProperties}>
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
            {w.is_live ? <Badge variant="aim" className="text-[10px] px-1.5 py-0" title={i18nT('pages.devFleetPage.the_live_gateway_on_this_port_runs_from_this_che')}>{i18nT('pages.devFleetPage.live')}</Badge> : null}
            {/* A staged cutover outlives the toast that announced it: without a
                persistent marker an operator who dismissed or missed the toast
                reads the old running image as the new one. */}
            {w.is_staged ? <Badge variant="warn" className="text-[10px] px-1.5 py-0" title={i18nT('pages.devFleetPage.cutover_staged_run_the_restart_command_to_finish', { cmd: fleet?.manual_restart || 'kirocrew restart' })}>{i18nT('pages.devFleetPage.restart_pending')}</Badge> : null}
            {w.summary ? <span title={w.summary} style={{ fontSize: 11.5, color: 'var(--muted)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0, flex: '0 1 auto' } as CSSProperties}>{w.summary}</span> : null}
          </div>
          {isMainWithStepper ? renderSyncStepper() : provActive ? renderProvStepper(w) : (
            <>
              {rs && prUrl ? <a href={prUrl} target="_blank" rel="noopener noreferrer" title={w.pr?.title || rs.word} style={{ textDecoration: 'none' }}><Badge variant={rs.variant}>{rs.word}</Badge></a> : <span style={{ ...mut, opacity: 0.5 }}>{"\u2014"}</span>}
              <span style={{ ...mut, opacity: (w.behind ?? 0) > 0 ? 1 : 0.5 }} title={(w.behind ?? 0) > 0 ? i18nT('pages.devFleetPage.commits_behind_main_2', { count: w.behind ?? 0 }) : i18nT('pages.devFleetPage.up_to_date_with_main')}>{(w.behind ?? 0) > 0 ? '\u2193' + w.behind : '\u2014'}</span>
              <span style={{ ...mut, opacity: 0.85 }}>{relTime(w.last_updated_at).replace(' ago', '')}</span>
              <div style={{ display: 'flex', gap: 6, justifyContent: 'flex-end', alignItems: 'center', minWidth: 0, flexWrap: 'wrap' } as CSSProperties}>{rowButtons(w)}</div>
            </>
          )}
        </div>
        {w.is_main && syncRun && syncLogOpen ? (
          <pre style={{ margin: '2px 0 8px 32px', padding: '8px 10px', maxHeight: 180, overflow: 'auto', fontSize: 11, lineHeight: 1.45, background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, whiteSpace: 'pre-wrap', wordBreak: 'break-all' } as CSSProperties}>{filterStepMarkers(syncRun.lines || []).join('\n') || '(no output yet)'}</pre>
        ) : null}
        {provActive && provLogOpen[w.name] && pr ? (
          <ProvLogPre lines={pr.lines || []} streaming={pr.status === 'running' || pr.status === 'starting'} />
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
    <Btn onClick={() => setShowLegacy((v) => !v)} style={{ display: 'block', width: '100%', textAlign: 'left', marginTop: 4, fontSize: 11.5, color: 'var(--muted)', background: 'transparent', border: '1px dashed var(--border)' }} title={i18nT('pages.devFleetPage.worktrees_created_under_a_previous_repository_na')}>
      {showLegacy ? i18nT('pages.devFleetPage.hide_legacy_worktrees', { n: legacyAll.length }) : i18nT('pages.devFleetPage.legacy_worktrees_hidden_show', { n: legacyAll.length })}
    </Btn>
  ) : null
  let body: ReactNode
  if (loading && !fleet) body = <ContentSkeleton rows={5} />
  else if (error) body = isDiscoveryError
    ? <div role="alert" style={{ padding: 24, borderRadius: 8, border: '1px solid var(--danger)', background: 'var(--danger-subtle, rgba(239,68,68,0.08))' }}><p style={{ margin: 0, fontWeight: 600, color: 'var(--danger)' }}>{i18nT('pages.devFleetPage.discovery_error')}</p><p style={{ margin: '8px 0 0', color: 'var(--text)', fontSize: 14 }}>{error}</p></div>
    : <EmptyState icon={<Server size={28} className="lucide-inline" />} title={i18nT('pages.devFleetPage.backend_unavailable')} subtitle={error} />
  else if (!wts.length) body = <EmptyState icon={<Server size={28} className="lucide-inline" />} title={i18nT('pages.devFleetPage.no_worktrees_found')} subtitle={i18nT('pages.devFleetPage.nothing_under_the_worktrees_root_yet')} />
  else body = <div>{columnHeader}{visible.map(renderRow)}{legacyToggle}</div>

  const confirmDialog = (
    <Modal open={!!confirmReq} onClose={() => settleConfirm(false)} title={confirmReq?.title ?? ''} maxWidth={confirmReq?.width || 400} footer={<><Btn onClick={() => settleConfirm(false)}>{i18nT('pages.devFleetPage.cancel')}</Btn><Btn primary={!confirmReq?.danger} danger={!!confirmReq?.danger} onClick={() => settleConfirm(true)}>{confirmReq?.confirmLabel || i18nT('pages.devFleetPage.confirm')}</Btn></>}>
      <p className="text-sm text-muted m-0">{confirmReq?.desc}</p>
    </Modal>
  )

  const pruneReviewDialog = pruneDialog && (() => {
    return (
      <Modal open={true} onClose={() => setPruneDialog(null)} title={i18nT('pages.devFleetPage.prune_worktrees')} maxWidth={480} footer={<><Btn onClick={() => setPruneDialog(null)}>{i18nT('pages.devFleetPage.cancel')}</Btn><Btn danger onClick={() => pruneExecute(pruneDialog.candidates.filter((c) => pruneSelected.has(c.name)).map((c) => c.name))}>{i18nT('pages.devFleetPage.remove_selected')}</Btn></>}>
        <div style={{ maxHeight: 360, overflowY: 'auto' }}>
          {pruneDialog.candidates.length > 0 && (
            <div style={{ marginBottom: 10 }}>
              <div style={{ fontSize: 10, letterSpacing: '0.08em', color: 'var(--muted)', textTransform: 'uppercase', borderBottom: '1px solid var(--border)', paddingBottom: 3, marginBottom: 4 }}>{i18nT('pages.devFleetPage.remove')}</div>
              {pruneDialog.candidates.map((c) => (
                <label key={c.name} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '4px 0', cursor: 'pointer' }}>
                  <Checkbox checked={pruneSelected.has(c.name)} onChange={(e) => setPruneSelected((prev) => { const next = new Set(prev); if (e.target.checked) next.add(c.name); else next.delete(c.name); return next })} aria-label={i18nT('pages.devFleetPage.select', { name: c.name })} />
                  <span style={{ fontFamily: 'ui-monospace, SF Mono, Menlo, monospace', fontSize: 12, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }}>{c.name}</span>
                  <span style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--muted)', whiteSpace: 'nowrap' }}>{pruneVerdictLabel(c.code)}</span>
                </label>
              ))}
            </div>
          )}
          {pruneDialog.kept.length > 0 && (
            <div style={{ marginBottom: 10 }}>
              <div style={{ fontSize: 10, letterSpacing: '0.08em', color: 'var(--muted)', textTransform: 'uppercase', borderBottom: '1px solid var(--border)', paddingBottom: 3, marginBottom: 4 }}>{i18nT('pages.devFleetPage.kept')}</div>
              {pruneDialog.kept.map((k) => (
                <div key={k.name} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '4px 0' }}>
                  <span style={{ width: 13 }} />
                  <span style={{ fontFamily: 'ui-monospace, SF Mono, Menlo, monospace', fontSize: 12, color: 'var(--muted)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }}>{k.name}</span>
                  <span style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--muted)', whiteSpace: 'nowrap' }}>{pruneVerdictLabel(k.code)}</span>
                </div>
              ))}
            </div>
          )}
          {pruneDialog.candidates.length === 0 && <p style={{ fontSize: 12, color: 'var(--muted)' }}>{i18nT('pages.devFleetPage.no_candidates_found')}</p>}
          <p style={{ fontSize: 11, color: 'var(--muted)', margin: '8px 0 0' }}>{i18nT('pages.devFleetPage.removes_worktrees_and_stops_pods_cannot_be_undon')}</p>
        </div>
      </Modal>
    )
  })()

  const pruneDone = pruneProgress != null && !pruneProgress.running
  const pruneProgressModal = pruneProgress && (
    <Modal
      open={true}
      onClose={() => { if (pruneDone) setPruneProgress(null) }}
      title={pruneDone ? i18nT('pages.devFleetPage.prune_complete') : i18nT('pages.devFleetPage.pruning_worktrees')}
      maxWidth={460}
      footer={pruneDone ? <Btn onClick={() => setPruneProgress(null)}>{i18nT('pages.devFleetPage.close')}</Btn> : undefined}
    >
      <div style={{ fontSize: 12 }}>
        <div style={{ fontWeight: 600, marginBottom: 8 }}>
          {pruneDone ? i18nT('pages.devFleetPage.finished') : i18nT('pages.devFleetPage.removing')} {pruneProgress.done}/{pruneProgress.total}
        </div>
        <div role="list" style={{ display: 'flex', flexDirection: 'column', maxHeight: 320, overflowY: 'auto' }}>
          {pruneProgress.names.map((nm) => {
            const it = pruneProgress.items[nm] || { status: 'pending', error: null }
            const meta = PRUNE_STATUS_META[it.status] || PRUNE_STATUS_META.pending
            return (
              <div key={nm} role="listitem" data-testid={`prune-item-${nm}`} data-status={it.status} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '3px 0', borderBottom: '1px solid var(--border)' }}>
                <span style={{ width: 14, display: 'inline-flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>
                  {meta.kind === 'spin'
                    ? <LoaderCircle size={12} className="lucide-inline animate-spin" style={{ color: 'var(--muted)' }} />
                    : meta.kind === 'done'
                      ? <Check size={13} style={{ color: 'var(--ok)' }} />
                      : meta.kind === 'failed'
                        ? <X size={13} style={{ color: 'var(--danger)' }} />
                        : <span style={{ width: 6, height: 6, borderRadius: '50%', background: 'var(--muted)', opacity: 0.4 }} />}
                </span>
                <span style={{ fontFamily: 'ui-monospace, SF Mono, Menlo, monospace', fontSize: 12, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flexShrink: 0, maxWidth: 200 }}>{nm}</span>
                <span style={{ marginLeft: 'auto', display: 'inline-flex', alignItems: 'center', gap: 6, minWidth: 0, paddingLeft: 8 }}>
                  {it.status === 'failed' && it.error && (
                    <span title={it.error} style={{ color: 'var(--danger)', fontSize: 11, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{it.error}</span>
                  )}
                  <Badge variant={meta.kind === 'done' ? 'ok' : meta.kind === 'failed' ? 'err' : 'muted'} className="text-[10.5px] px-1.5 py-0">{pruneStatusLabel(it.status)}</Badge>
                </span>
              </div>
            )
          })}
        </div>
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
        <div role="alert" aria-busy="true" style={{ position: 'fixed', inset: 0, zIndex: 9999, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', background: 'var(--bg)', color: 'var(--text)' }}>
          <LoaderCircle size={32} className="lucide-inline" style={{ animation: 'spin 1s linear infinite' }} />
          <p style={{ marginTop: 16, fontSize: 16, fontWeight: 600 }}>{i18nT('pages.devFleetPage.restarting_reconnecting')}</p>
          <p style={{ fontSize: 12, color: 'var(--muted)' }}>{i18nT('pages.devFleetPage.waiting_for_the_new_gateway_process_the_page_rel')}</p>
        </div>
      )}
      <div className="flex flex-1 min-h-0 overflow-hidden">
        <div className="flex-1 min-w-0 flex flex-col min-h-0">
          <PageHeader title={i18nT('pages.devFleetPage.dev_fleet')} subtitle={i18nT('pages.devFleetPage.manage_the_git_worktrees_of_your_main_checkout_s')} />
          <div className="flex-1 overflow-y-auto px-6 pb-8 min-h-0">
            <p className="text-[12.5px] text-muted leading-relaxed mt-3 mb-1 max-w-[860px]">
              {i18nT('pages.devFleetPage.each_row_below_is_a_git_worktree_discovered_from')}{' '}
              <span className="text-text-strong">{i18nT('pages.devFleetPage.pull_build')}</span> {i18nT('pages.devFleetPage.on_the_main_row_to_fast_forward_it_from_origin_a')} <span className="text-text-strong">{i18nT('pages.devFleetPage.pod_2')}</span> {i18nT('pages.devFleetPage.boots_any_worktree_as_an_isolated_throwaway_gate')}{' '}
              <span className="text-text-strong">{i18nT('pages.devFleetPage.rebase')}</span> {i18nT('pages.devFleetPage.moves_a_feature_branch_onto_the_latest_main_and')}{' '}
              <span className="text-text-strong">{i18nT('pages.devFleetPage.prune')}</span> {i18nT('pages.devFleetPage.safely_removes_worktrees_whose_pr_has_already_me')}
            </p>
            {gatewayError && (
              <div
                role="alert"
                data-testid="gateway-restart-error"
                className="flex items-start gap-2 rounded-md border border-danger/40 bg-danger-subtle px-3 py-2.5 mt-3 max-w-[860px] text-[12.5px] leading-relaxed text-danger"
              >
                <AlertTriangle size={14} className="lucide-inline shrink-0 mt-0.5" />
                {/* select-text + break-words: the message can be a pair of
                    commands with absolute paths that the operator has to run. */}
                <span className="min-w-0 flex-1 break-words select-text">{gatewayError}</span>
                <Btn
                  onClick={() => setGatewayError(null)}
                  aria-label={i18nT('app.dismiss')}
                  title={i18nT('app.dismiss')}
                  className="shrink-0"
                >
                  <X size={13} className="lucide-inline" />
                </Btn>
              </div>
            )}
            {servingReason && (
              <div
                role="alert"
                data-testid="serving-install-warning"
                className="flex items-start gap-2 rounded-md border border-warn/40 bg-warn-subtle px-3 py-2.5 mt-3 max-w-[860px] text-[12.5px] leading-relaxed text-warn"
              >
                <AlertTriangle size={14} className="lucide-inline shrink-0 mt-0.5" />
                <div className="min-w-0">
                  {/* break-words: the two embedded install paths are unbroken
                      tokens and CSS does not wrap at '/'. */}
                  <span className="break-words">{servingReason}</span>
                </div>
              </div>
            )}
            {!podsAvailable && (
              <div
                role="note"
                className="flex items-start gap-2 rounded-md border border-border bg-bg-elevated px-3 py-2.5 mt-3 max-w-[860px] text-[12.5px] leading-relaxed"
              >
                <Info size={14} className="lucide-inline shrink-0 mt-0.5 text-muted" />
                <div className="min-w-0">
                  <span className="text-text-strong">{i18nT('pages.devFleetPage.pods_are_unavailable_on_this_host')}</span>{' '}
                  {podsReason ? <><span className="text-muted">{podsReason}</span>{' '}</> : null}
                  <span className="text-muted">{i18nT('pages.devFleetPage.pod_and_make_live_actions_are_hidden_everything')}</span>
                </div>
              </div>
            )}
            {gatewayReason && (
              <div
                role="note"
                className="flex items-start gap-2 rounded-md border border-border bg-bg-elevated px-3 py-2.5 mt-3 max-w-[860px] text-[12.5px] leading-relaxed"
              >
                <Info size={14} className="lucide-inline shrink-0 mt-0.5 text-muted" />
                <div className="min-w-0">
                  <span className="text-muted">{gatewayReason}</span>
                </div>
              </div>
            )}
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, minmax(0, 1fr))', gap: 12, margin: '14px 0' } as CSSProperties}>
              <StatCard label={i18nT('pages.devFleetPage.running_pods')} value={running} accent />
              <StatCard label={i18nT('pages.devFleetPage.worktrees')} value={wts.length} />
              <StatCard label={i18nT('pages.devFleetPage.needs_provision')} value={needsProv} />
              <StatCard label={i18nT('pages.devFleetPage.disk_worktrees')} value={diskGb} />
            </div>
            <Card>
              <CardTitle><span className="flex items-center gap-1.5">{i18nT('pages.devFleetPage.worktrees_count', { count: wts.length })}<InfoTip text={i18nT('pages.devFleetPage.every_git_worktree_of_the_main_checkout_pull_bui')} /></span></CardTitle>
              <div style={{ display: 'flex', gap: 10, alignItems: 'center', margin: '12px 0 4px' } as CSSProperties}>
                <div className="flex-1 min-w-0">
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
                <Btn danger onClick={pruneShipped} disabled={!!busy['__prune']}>{iconLabel(<Trash2 size={13} className="lucide-inline" />, i18nT('pages.devFleetPage.prune_merged'))}</Btn>
                <Btn onClick={() => invalidateAll()} disabled={loading} aria-label={i18nT('pages.devFleetPage.refresh_fleet')}>{iconLabel(<RefreshCw size={14} className="lucide-inline" />, i18nT('pages.devFleetPage.refresh'))}</Btn>
              </div>
              {body}
            </Card>
          </div>
        </div>
      </div>
    </>
  )
}
