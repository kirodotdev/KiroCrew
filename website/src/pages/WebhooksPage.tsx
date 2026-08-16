/**
 * Inbound webhooks — rail-and-detail surface for `POST /api/hooks/agent`.
 *
 * Shape (Option C, adapted to the full top-level content width):
 *
 *   ┌──────────────┬──────────────────────────────────────────┐
 *   │ RAIL (300px) │ DETAIL                                   │
 *   │ Setup pin    │ Setup & endpoint | Token | Context | Run  │
 *   │ Tokens       │ (whichever the rail selection points at)  │
 *   │ Contexts     │                                          │
 *   │ Recent runs  │                                          │
 *   └──────────────┴──────────────────────────────────────────┘
 *
 * Both columns are edge-to-edge and full height with flush headers at the same
 * height; the rail is a real resizable column via the shared `useColumnResize`
 * hook (persisted px width, drag-past-minimum collapse).
 *
 * Everything server-side is read through GET /api/webhooks in one shot, so the
 * rail and every pane always describe the same snapshot. The raw secrets — the
 * bearer token and, when signing is on, the HMAC signing secret — exist in this
 * page's memory only between the create response and the user dismissing the
 * one-time reveal; neither is ever re-fetchable.
 *
 * The kill switch is separate from "has a token": `switch_on` off means
 * every call is refused with 503 before any auth work, while tokens and run
 * history are kept, so the Setup pane has THREE states, not two.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle, Check, Circle, Clock, Copy, KeyRound, Minus, Play, Power,
  ShieldAlert, ShieldCheck, Trash2, Webhook, X,
} from 'lucide-react'

import { api } from '../api/client'
import { exampleFor } from './webhooks/requestExamples'
import type { ExampleMode } from './webhooks/requestExamples'
import type {
  WebhookFreshness, WebhookOutcome, WebhookRunRecord,
  WebhookTestResult, WebhookTokenCreated, WebhookTokenEntry, WebhooksView,
} from '../api/client'
import SegmentedControl from '../components/SegmentedControl'
import { Badge, Btn, Checkbox, IconButton, Input, PageHeader, SearchInput, Skeleton } from '../components/ui'
import { useColumnResize, type CollapseConfig } from '../hooks/useColumnResize'
import { useIsMobile } from '../hooks/useIsMobile'
import { timeAgo } from '../utils/timeAgo'

import { fmtBytes, fmtDateTime, fmtDuration, fmtNumber, fmtUnit } from '../i18n/format'
import { i18nT } from '../i18n/t'
/* ── rail geometry ─────────────────────────────────────────────────────── */

const RAIL_WIDTH_KEY = 'webhooks.railWidth'
const RAIL_COLLAPSED_KEY = 'webhooks.railCollapsed'
const DEFAULT_RAIL_WIDTH = 300
const MIN_RAIL_WIDTH = 240
const MAX_RAIL_WIDTH = 480
const COLLAPSED_RAIL_WIDTH = 44

// Module-level so the hook's memoised resolver isn't invalidated every render.
const RAIL_COLLAPSE: CollapseConfig = { width: COLLAPSED_RAIL_WIDTH, storageKey: RAIL_COLLAPSED_KEY }

function loadRailWidth(): number {
  try {
    const raw = Number(localStorage.getItem(RAIL_WIDTH_KEY))
    if (!Number.isFinite(raw) || raw <= 0) return DEFAULT_RAIL_WIDTH
    return Math.min(MAX_RAIL_WIDTH, Math.max(MIN_RAIL_WIDTH, raw))
  } catch { return DEFAULT_RAIL_WIDTH }
}

function loadRailCollapsed(): boolean {
  try { return localStorage.getItem(RAIL_COLLAPSED_KEY) === '1' } catch { return false }
}

/* ── vocabulary ────────────────────────────────────────────────────────── */

type BadgeVariant = 'ok' | 'warn' | 'err' | 'muted' | 'aim'

/** Freshness is computed server-side from the same thresholds the injection
 *  path uses, so these blurbs describe what actually happens on the next call. */
const FRESHNESS: Record<WebhookFreshness, {
  label: () => string; badge: BadgeVariant; dot: string; effect: () => string
}> = {
  fresh: {
    label: () => i18nT('pages.webhooksPage.fresh'), badge: 'ok', dot: 'bg-ok',
    effect: () => i18nT('pages.webhooksPage.fresh_effect'),
  },
  stale: {
    label: () => i18nT('pages.webhooksPage.stale'), badge: 'warn', dot: 'bg-warn',
    effect: () => i18nT('pages.webhooksPage.stale_effect'),
  },
  expired: {
    label: () => i18nT('pages.webhooksPage.expired'), badge: 'muted', dot: 'bg-muted-strong',
    effect: () => i18nT('pages.webhooksPage.expired_effect'),
  },
}

const OUTCOME: Record<WebhookOutcome, {
  label: () => string; badge: BadgeVariant; dot: string; blurb: () => string
}> = {
  completed: {
    label: () => i18nT('pages.webhooksPage.completed'), badge: 'ok', dot: 'bg-ok',
    blurb: () => i18nT('pages.webhooksPage.completed_blurb'),
  },
  timeout: {
    label: () => i18nT('pages.webhooksPage.timed_out'), badge: 'warn', dot: 'bg-warn',
    blurb: () => i18nT('pages.webhooksPage.timed_out_blurb'),
  },
  error: {
    label: () => i18nT('pages.webhooksPage.error'), badge: 'err', dot: 'bg-danger',
    blurb: () => i18nT('pages.webhooksPage.error_blurb'),
  },
  rejected_capacity: {
    label: () => i18nT('pages.webhooksPage.rejected_capacity'), badge: 'err', dot: 'bg-danger',
    blurb: () => i18nT('pages.webhooksPage.rejected_capacity_blurb'),
  },
  unauthorized: {
    label: () => i18nT('pages.webhooksPage.unauthorized'), badge: 'err', dot: 'bg-danger',
    blurb: () => i18nT('pages.webhooksPage.unauthorized_blurb'),
  },
  disabled: {
    label: () => i18nT('pages.webhooksPage.disabled'), badge: 'muted', dot: 'bg-muted',
    blurb: () => i18nT('pages.webhooksPage.disabled_blurb'),
  },
}

/* ── selection ─────────────────────────────────────────────────────────── */

type Selection =
  | { kind: 'setup' }
  | { kind: 'token'; id: string }
  | { kind: 'context'; hookId: string }
  | { kind: 'run'; id: string }

const SETUP: Selection = { kind: 'setup' }

/** Shown until the first response lands, and whenever the endpoint is
 *  unavailable — the page still explains itself with zero data. The kill switch
 *  defaults to ON here so an unreachable endpoint is never mistaken for a
 *  deliberate shutdown. */
const EMPTY_VIEW: WebhooksView = {
  enabled: false,
  switch_on: true,
  has_tokens: false,
  url: '',
  slots: { in_use: 0, max: 6 },
  limits: {
    session_key_prefix: 'hook:', message_max: 49999,
    timeout_default: 599, timeout_max: 3593, max_concurrent: 6,
    body_max_bytes: 262144,
    signature_window_seconds: 300,
  },
  tokens: [], contexts: [], runs: [],
}

/* ── small helpers ─────────────────────────────────────────────────────── */

function copyText(text: string) {
  // Clipboard access is unavailable in insecure contexts and in jsdom; copying
  // is a convenience, so a failure must never surface as an error.
  try { void navigator.clipboard?.writeText(text)?.catch(() => {}) } catch { /* no clipboard */ }
}

function usedAgo(ts: number | null | undefined): string {
  return ts ? timeAgo(ts) : i18nT('pages.webhooksPage.never_used')
}

// Latency and payload size go through the `src/i18n/format.ts` seam rather than
// gluing a unit onto a number: a hardcoded `ms`/`s`/`KB` cannot be translated,
// leaves the digits unlocalized, and puts the separator in the wrong place
// outside English. `fmtUnit`/`fmtDuration` render narrow (`90m`, and de `90 Min.`),
// so the tight log-row chrome these sit in is preserved.
function durationLabel(ms: number): string {
  if (!ms) return '—'
  if (ms < 1000) return fmtUnit(ms, 'millisecond')
  if (ms < 60_000) {
    return fmtUnit(ms / 1000, 'second', { maximumFractionDigits: ms < 10_000 ? 1 : 0 })
  }
  return fmtDuration(
    [[Math.floor(ms / 60_000), 'minute'], [Math.round((ms % 60_000) / 1000), 'second']],
  )
}

function sizeLabel(chars: number): string {
  if (!chars) return i18nT('pages.webhooksPage.0_b')
  return fmtBytes(chars)
}

function absolute(ts: number): string {
  return ts ? fmtDateTime(ts * 1000) : '—'
}

/* ── presentational primitives (flush, full-bleed — no floating cards) ─── */

function Section({ title, right, flush, children }: {
  title: string; right?: React.ReactNode; flush?: boolean; children: React.ReactNode
}) {
  return (
    <section className="border-b border-border">
      <div className="flex items-center gap-2 px-4 py-2 bg-bg-accent border-b border-border">
        <h2 className="text-[11px] font-semibold uppercase tracking-[.08em] text-muted">{title}</h2>
        {right && <div className="ml-auto flex items-center gap-1.5">{right}</div>}
      </div>
      <div className={flush ? '' : 'px-4 py-3.5'}>{children}</div>
    </section>
  )
}

const BANNER_TONE: Record<'ok' | 'warn' | 'danger' | 'muted', string> = {
  ok: 'bg-ok-subtle', warn: 'bg-warn-subtle', danger: 'bg-danger-subtle', muted: 'bg-bg-elevated',
}

function Banner({ tone, icon, title, children, right, testId }: {
  tone: 'ok' | 'warn' | 'danger' | 'muted'
  icon: React.ReactNode
  title: string
  children?: React.ReactNode
  right?: React.ReactNode
  testId?: string
}) {
  return (
    <div
      data-testid={testId}
      className={`flex gap-2.5 items-start px-4 py-3 border-b border-border ${BANNER_TONE[tone]}`}
    >
      <span className="mt-[1px] shrink-0">{icon}</span>
      <div className="min-w-0">
        <div className="text-[13px] font-semibold text-text-strong">{title}</div>
        {children && <div className="text-[13px] text-text mt-0.5">{children}</div>}
      </div>
      {right && <div className="ml-auto shrink-0 flex items-center gap-1.5">{right}</div>}
    </div>
  )
}

function Kv({ rows }: { rows: [string, React.ReactNode][] }) {
  return (
    <dl className="grid grid-cols-[150px_1fr] gap-x-4 gap-y-2 text-[13px]">
      {rows.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-[11px] uppercase tracking-[.06em] text-muted pt-[2px]">{k}</dt>
          <dd className="text-text min-w-0 break-words">{v}</dd>
        </div>
      ))}
    </dl>
  )
}

function CopyField({ value, label, mask }: { value: string; label: string; mask?: boolean }) {
  return (
    <div className="flex items-center gap-2 bg-bg-elevated border border-border rounded-md pl-3 pr-1.5 py-1.5">
      <span className={`flex-1 min-w-0 font-mono text-[12px] overflow-x-auto whitespace-nowrap scrollbar-none ${mask ? 'text-muted' : 'text-card-fg'}`}>
        {value}
      </span>
      <IconButton aria-label={label} onClick={() => copyText(value)} title={label}>
        <Copy size={14} />
      </IconButton>
    </div>
  )
}

function CodeBlock({ code, label }: { code: string; label: string }) {
  return (
    <pre className="bg-bg-elevated border border-border rounded-md px-3 py-2.5 font-mono text-[12px] leading-relaxed text-card-fg overflow-x-auto" aria-label={label}>
      {code}
    </pre>
  )
}

function Hint({ children }: { children: React.ReactNode }) {
  return <div className="text-[12px] text-muted mt-2">{children}</div>
}

/** Rail group heading with its count. */
function GroupLabel({ label, count }: { label: string; count: number }) {
  return (
    <div className="flex items-center gap-1.5 px-3 pt-2.5 pb-1.5 text-[11px] uppercase tracking-[.08em] text-muted">
      <span>{label}</span>
      <span className="tracking-normal text-muted-strong">{count}</span>
    </div>
  )
}

function GroupEmpty({ children }: { children: React.ReactNode }) {
  return <div className="px-3 pb-2.5 text-[12px] text-muted-strong leading-snug">{children}</div>
}

/** One rail row. `dot` is a static glyph — never a spinner. */
function RailRow({ active, onClick, dot, title, subtitle, age, testId, extraProps }: {
  active: boolean
  onClick: () => void
  dot?: React.ReactNode
  title: React.ReactNode
  subtitle: React.ReactNode
  age?: string
  testId: string
  extraProps?: Record<string, string>
}) {
  return (
    <button
      type="button"
      data-testid={testId}
      aria-current={active ? 'true' : undefined}
      onClick={onClick}
      className={`flex w-full items-center gap-2.5 text-left px-3 py-2 border-l-2 border-b border-b-border transition-colors ${
        active ? 'bg-bg-elevated border-l-accent' : 'border-l-transparent hover:bg-bg-hover'
      }`}
      {...extraProps}
    >
      {dot}
      <span className="flex-1 min-w-0">
        <span className="block text-[12px] text-text-strong truncate">{title}</span>
        <span className="block text-[11px] text-muted truncate">{subtitle}</span>
      </span>
      {age && <span className="text-[11px] text-muted-strong shrink-0">{age}</span>}
    </button>
  )
}

function Dot({ className, square, testId, extra }: {
  className: string; square?: boolean; testId?: string; extra?: Record<string, string>
}) {
  return (
    <span
      data-testid={testId}
      className={`w-[7px] h-[7px] shrink-0 ${square ? 'rounded-sm' : 'rounded-full'} ${className}`}
      {...extra}
    />
  )
}

/* ── page ──────────────────────────────────────────────────────────────── */

export default function WebhooksPage() {
  const rail = useColumnResize(
    RAIL_WIDTH_KEY, loadRailWidth, MIN_RAIL_WIDTH, MAX_RAIL_WIDTH, RAIL_COLLAPSE, loadRailCollapsed,
  )
  // A fixed-width rail beside the detail pane does not fit a phone: at 375px a
  // 300px rail leaves the detail controls about 70px wide. Collapse to the icon
  // strip whenever the viewport is narrow, which keeps navigation reachable and
  // gives the detail pane the rest. Desktop behaviour is untouched, and the
  // user's dragged width is remembered for when the viewport widens again.
  const isMobile = useIsMobile()
  // Collapse when the viewport BECOMES narrow, once — not on every render.
  // Re-asserting it continuously would also undo the user's own expand, making
  // the rail impossible to open on a phone and leaving no way to navigate.
  const collapsedForMobile = useRef(false)
  useEffect(() => {
    if (!isMobile) {
      collapsedForMobile.current = false
      return
    }
    if (!collapsedForMobile.current) {
      collapsedForMobile.current = true
      rail.collapse()
    }
  }, [isMobile, rail.collapse])
  // On a phone the rail and the detail pane cannot share the width, so the two
  // become a drill-down: the rail opens full-width to browse, and choosing an
  // entry collapses it back to the strip and hands the screen to the detail.
  const mobileRailOpen = isMobile && !rail.collapsed
  const [selection, setSelection] = useState<Selection>(SETUP)
  /** Choose a rail entry. On a phone this also closes the rail, so the detail
   *  pane gets the screen instead of being squeezed beside it. */
  const select = (next: Selection) => {
    setSelection(next)
    if (isMobile) rail.collapse()
  }
  const [filter, setFilter] = useState('')
  const [label, setLabel] = useState('')
  const [requireSignature, setRequireSignature] = useState(true)
  const [revealed, setRevealed] = useState<WebhookTokenCreated | null>(null)
  // Two-step dismissal for the one-time reveal: the secrets are unrecoverable
  // once the banner closes, and the dismiss button sits next to the copy
  // buttons, so a single mis-click would destroy them.
  const [dismissArmed, setDismissArmed] = useState(false)
  // Two-step arm for turning the kill switch OFF. Turning it back ON is a single
  // click — that direction is not destructive.
  const [switchArmed, setSwitchArmed] = useState(false)
  const [confirmRevoke, setConfirmRevoke] = useState<string | null>(null)
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null)
  const [testResult, setTestResult] = useState<WebhookTestResult | null>(null)
  // null ⇒ follow whatever the current tokens require.
  const [exampleMode, setExampleMode] = useState<ExampleMode | null>(null)
  const queryClient = useQueryClient()

  const { data, isLoading, error, refetch } = useQuery<WebhooksView>({
    queryKey: ['webhooks'],
    // Called defensively: a partially-mocked api module (common in tests) and a
    // gateway without the endpoint yet both land on the empty view rather than
    // throwing on mount.
    queryFn: async () => {
      const r = await Promise.resolve(api.webhooks?.())
      return (r ?? EMPTY_VIEW) as WebhooksView
    },
    retry: false,
  })
  const view = data ?? EMPTY_VIEW
  // Only a failure with NOTHING to show is an "unavailable" state. A failed
  // background refetch while a snapshot is already on screen must not claim the
  // page is broken.
  const loadError = error && !data
    ? i18nT('pages.webhooksPage.could_not_load_webhook_settings', {
      detail: error instanceof Error ? error.message : String(error),
    })
    : null

  const reload = () => { void refetch() }

  // Read both flags defensively: a gateway that predates the kill switch reports
  // neither, and in that case webhooks are on whenever a token exists.
  const switchOn = view.switch_on ?? true
  const hasTokens = view.has_tokens ?? view.tokens.length > 0
  // Effective state — what an inbound call actually meets right now.
  const live = switchOn && hasTokens
  const signatureWindow = view.limits.signature_window_seconds ?? 300

  const setSwitch = useMutation({
    mutationFn: (enabled: boolean) => Promise.resolve(api.setWebhooksEnabled?.(enabled)),
    onSuccess: () => {
      setSwitchArmed(false)
      void queryClient.invalidateQueries({ queryKey: ['webhooks'] })
    },
    onError: () => setSwitchArmed(false),
  })

  const createToken = useMutation({
    mutationFn: (name: string) =>
      Promise.resolve(api.createWebhookToken?.(name, requireSignature)) as Promise<WebhookTokenCreated>,
    onSuccess: (r) => {
      setLabel('')
      setSelection(SETUP)
      // The secret is in this response and nowhere else — hold it in state only
      // until the user dismisses the reveal.
      if (r?.token) setRevealed(r)
      reload()
    },
  })

  const revokeToken = useMutation({
    mutationFn: (id: string) => Promise.resolve(api.deleteWebhookToken?.(id)),
    onSuccess: (_r, id) => {
      setConfirmRevoke(null)
      if (revealed?.entry?.id === id) setRevealed(null)
      setSelection(SETUP)
      reload()
    },
    onError: () => setConfirmRevoke(null),
  })

  const deleteContext = useMutation({
    mutationFn: (hookId: string) => Promise.resolve(api.deleteWebhookContext?.(hookId)),
    onSuccess: () => { setConfirmDelete(null); setSelection(SETUP); reload() },
    onError: () => setConfirmDelete(null),
  })

  const sendTest = useMutation({
    mutationFn: (message?: string) =>
      Promise.resolve(api.testWebhook?.(message)) as Promise<WebhookTestResult>,
    onSuccess: (r) => { setTestResult(r ?? null); reload() },
    onError: (e: Error) => setTestResult({ ok: false, status: 0, error: e.message }),
  })

  const mutationError = createToken.error || revokeToken.error || deleteContext.error
    || setSwitch.error
  const mutationMessage = mutationError instanceof Error ? mutationError.message : null

  /* filter applies to every group at once */
  const q = filter.trim().toLowerCase()
  const match = (...parts: (string | null | undefined)[]) =>
    !q || parts.some(p => (p || '').toLowerCase().includes(q))

  const tokens = useMemo(
    () => view.tokens.filter(t => match(t.label, t.display_prefix, t.last4)),
    // `match` closes over `q` only; listing it keeps the memo honest.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [view.tokens, q],
  )
  const contexts = useMemo(
    () => view.contexts.filter(c => match(c.hook_id, c.session_key, c.freshness)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [view.contexts, q],
  )
  const runs = useMemo(
    () => view.runs.filter(r => match(r.hook_id, r.name, r.outcome, OUTCOME[r.outcome]?.label())),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [view.runs, q],
  )

  const selectedToken = selection.kind === 'token'
    ? view.tokens.find(t => t.id === selection.id) ?? null : null
  const selectedContext = selection.kind === 'context'
    ? view.contexts.find(c => c.hook_id === selection.hookId) ?? null : null
  const selectedRun = selection.kind === 'run'
    ? view.runs.find(r => r.id === selection.id) ?? null : null

  // A selection whose target disappeared (revoked token, deleted context) falls
  // back to Setup rather than rendering a blank pane.
  const pane: Selection['kind'] =
    selection.kind === 'token' && !selectedToken ? 'setup'
      : selection.kind === 'context' && !selectedContext ? 'setup'
        : selection.kind === 'run' && !selectedRun ? 'setup'
          : selection.kind

  const leaf = pane === 'token' ? selectedToken?.label ?? i18nT('pages.webhooksPage.token')
    : pane === 'context' ? selectedContext?.hook_id ?? i18nT('pages.webhooksPage.context')
      : pane === 'run' ? `${OUTCOME[selectedRun?.outcome ?? 'completed'].label()} · ${selectedRun?.hook_id ?? i18nT('pages.webhooksPage.unknown_caller')}`
        : i18nT('pages.webhooksPage.setup_endpoint')

  // New tokens require signing by default, so a fresh install previews the
  // signed form rather than a snippet that would start failing with 401 the
  // moment a token exists.
  const defaultMode: ExampleMode =
    view.tokens.length === 0 || view.tokens.some(t => t.require_signature) ? 'signed' : 'bearer'
  const mode = exampleMode ?? defaultMode
  const sharedExample = exampleFor(
    mode, view.url, 'hook:my-job', i18nT('pages.webhooksPage.job_finished_3_findings'), signatureWindow,
  )

  /* ── rail ── */

  const railBody = (
    <>
      <RailRow
        active={pane === 'setup'}
        onClick={() => select(SETUP)}
        dot={live
          ? <Check size={14} className="text-ok shrink-0" />
          : switchOn
            ? <AlertTriangle size={14} className="text-warn shrink-0" />
            : <Power size={14} className="text-muted shrink-0" />}
        title={<span className="font-semibold">{i18nT('pages.webhooksPage.setup_endpoint')}</span>}
        subtitle={!switchOn
          ? i18nT('pages.webhooksPage.switched_off_credentials_kept', { count: view.tokens.length })
          : live
            ? i18nT('pages.webhooksPage.enabled_credentials', { count: view.tokens.length })
            : i18nT('pages.webhooksPage.disabled_no_token_set')}
        testId="webhook-row-setup"
      />

      <GroupLabel label={i18nT('pages.webhooksPage.tokens')} count={tokens.length} />
      {tokens.length === 0
        ? (
          <GroupEmpty>
            {view.tokens.length === 0
              ? i18nT('pages.webhooksPage.no_tokens_yet_until_one_exists_every_inbound_cal')
              : i18nT('pages.webhooksPage.no_token_matches_the_filter')}
          </GroupEmpty>
        )
        : tokens.map(t => (
          <RailRow
            key={t.id}
            active={pane === 'token' && selectedToken?.id === t.id}
            onClick={() => select({ kind: 'token', id: t.id })}
            dot={<KeyRound size={13} className="text-muted shrink-0" />}
            title={t.label}
            subtitle={<span className="font-mono">{t.display_prefix}…{t.last4}</span>}
            age={usedAgo(t.last_used_at)}
            testId={`webhook-row-token-${t.id}`}
          />
        ))}

      <GroupLabel label={i18nT('pages.webhooksPage.registered_contexts')} count={contexts.length} />
      {contexts.length === 0
        ? (
          <GroupEmpty>
            {view.contexts.length === 0
              ? i18nT('pages.webhooksPage.nothing_registered_an_agent_calls_the_register_h')
              : i18nT('pages.webhooksPage.no_context_matches_the_filter')}
          </GroupEmpty>
        )
        : contexts.map(c => (
          <RailRow
            key={c.hook_id}
            active={pane === 'context' && selectedContext?.hook_id === c.hook_id}
            onClick={() => select({ kind: 'context', hookId: c.hook_id })}
            dot={(
              <Dot
                className={FRESHNESS[c.freshness].dot}
                testId={`webhook-freshness-${c.hook_id}`}
                extra={{ 'data-freshness': c.freshness }}
              />
            )}
            title={<span className="font-mono">{c.hook_id}</span>}
            subtitle={`${FRESHNESS[c.freshness].label()} — ${c.freshness === 'fresh' ? i18nT('pages.webhooksPage.context_injected_verbatim')
              : c.freshness === 'stale' ? i18nT('pages.webhooksPage.injected_with_a_warning') : i18nT('pages.webhooksPage.context_dropped')}`}
            age={timeAgo(c.registered_at)}
            testId={`webhook-row-context-${c.hook_id}`}
          />
        ))}

      <GroupLabel label={i18nT('pages.webhooksPage.recent_runs')} count={runs.length} />
      {runs.length === 0
        ? (
          <GroupEmpty>
            {view.runs.length === 0
              ? i18nT('pages.webhooksPage.no_calls_recorded_yet_every_accepted_rejected_an')
              : i18nT('pages.webhooksPage.no_run_matches_the_filter')}
          </GroupEmpty>
        )
        : runs.map(r => (
          <RailRow
            key={r.id}
            active={pane === 'run' && selectedRun?.id === r.id}
            onClick={() => select({ kind: 'run', id: r.id })}
            dot={<Dot className={OUTCOME[r.outcome].dot} square />}
            title={<span className="font-mono">{r.hook_id ?? i18nT('pages.webhooksPage.unknown_caller')}</span>}
            subtitle={`${OUTCOME[r.outcome].label()}${r.outcome === 'completed' ? ` — ${durationLabel(r.duration_ms)} · ${sizeLabel(r.result_chars)}` : ''}`}
            age={timeAgo(r.started_at)}
            testId={`webhook-row-run-${r.id}`}
          />
        ))}

      <div className="px-3 pt-3 pb-6 text-[11px] text-muted-strong leading-relaxed">
        {i18nT('pages.webhooksPage.a_webhook_lets_an_outside_system_ci_a_review_bot')}
      </div>
    </>
  )

  /* ── panes ── */

  const setupPane = (
    <>
      <div
        data-testid="webhook-switch-row"
        className="flex items-center gap-2.5 flex-wrap px-4 py-2.5 border-b border-border bg-bg-accent"
      >
        <Power size={15} className={switchOn ? 'text-ok shrink-0' : 'text-muted shrink-0'} />
        <span className="text-[13px] font-semibold text-text-strong">{i18nT('pages.webhooksPage.inbound_webhooks')}</span>
        <Badge variant={switchOn ? (hasTokens ? 'ok' : 'warn') : 'muted'}>
          {switchOn ? (hasTokens ? i18nT('pages.webhooksPage.on') : i18nT('pages.webhooksPage.no_credential_yet')) : i18nT('pages.webhooksPage.off')}
        </Badge>
        <span className="text-[12px] text-muted min-w-0">
          {switchOn
            ? i18nT('pages.webhooksPage.inbound_webhooks_are_on_calls_are_answered_by_th')
            : i18nT('pages.webhooksPage.inbound_webhooks_are_off_calls_are_refused_befor')}
        </span>
        <span className="ml-auto flex items-center gap-1.5 flex-wrap justify-end">
          {switchOn
            ? (switchArmed
              ? (
                <>
                  <span className="text-[12px] text-muted">
                    {i18nT('pages.webhooksPage.tokens_and_run_history_are_kept_you_can_switch_i')}
                  </span>
                  <Btn
                    danger
                    data-testid="webhook-switch-off-confirm"
                    disabled={setSwitch.isPending}
                    onClick={() => setSwitch.mutate(false)}
                  >
                    <Power size={13} /> {i18nT('pages.webhooksPage.confirm_turn_off')}
                  </Btn>
                  <Btn onClick={() => setSwitchArmed(false)}>{i18nT('pages.webhooksPage.keep_it_on')}</Btn>
                </>
              )
              : (
                <Btn
                  danger
                  data-testid="webhook-switch-off"
                  onClick={() => setSwitchArmed(true)}
                >
                  <Power size={13} /> {i18nT('pages.webhooksPage.turn_off')}
                </Btn>
              ))
            : (
              <Btn
                primary
                data-testid="webhook-switch-on"
                disabled={setSwitch.isPending}
                onClick={() => setSwitch.mutate(true)}
              >
                <Power size={13} /> {i18nT('pages.webhooksPage.turn_on')}
              </Btn>
            )}
        </span>
      </div>

      {revealed && (
        <Banner
          tone="warn"
          testId="webhook-token-reveal"
          icon={<KeyRound size={16} className="text-warn" />}
          title={revealed.signing_secret
            ? i18nT('pages.webhooksPage.copy_both_secrets_now_they_are_shown_once')
            : i18nT('pages.webhooksPage.copy_this_token_now_it_is_shown_once')}
        >
          <div className="flex flex-col gap-2">
            <span>
              {revealed.signing_secret
                ? i18nT('pages.webhooksPage.shown_once_signing_pair_warning')
                : i18nT('pages.webhooksPage.shown_once_bearer_only_warning')}
            </span>

            <div className="flex flex-col gap-1">
              <span className="text-[11px] uppercase tracking-[.06em] text-muted">
                {i18nT('pages.webhooksPage.bearer_token_proves_who_is_calling')}
              </span>
              <CopyField value={revealed.token} label={i18nT('pages.webhooksPage.copy_webhook_token')} />
            </div>

            {revealed.signing_secret && (
              <div className="flex flex-col gap-1" data-testid="webhook-reveal-signing-secret">
                <span className="text-[11px] uppercase tracking-[.06em] text-muted">
                  {i18nT('pages.webhooksPage.signing_secret_proves_the_body_was_not_tampered')}
                </span>
                <CopyField value={revealed.signing_secret} label={i18nT('pages.webhooksPage.copy_signing_secret')} />
                <span className="text-[12px] text-muted">
                  {i18nT('pages.webhooksPage.equally_unrecoverable_calls_with_this_token_must')}
                </span>
              </div>
            )}

            <div className="flex items-center gap-2 flex-wrap">
              <Btn onClick={() => copyText(revealed.token)}><Copy size={13} /> {i18nT('pages.webhooksPage.copy_token')}</Btn>
              {revealed.signing_secret && (
                <Btn
                  data-testid="webhook-reveal-copy-signing"
                  onClick={() => copyText(revealed.signing_secret ?? '')}
                >
                  <Copy size={13} /> {i18nT('pages.webhooksPage.copy_signing_secret')}
                </Btn>
              )}
              {dismissArmed
                ? (
                  <Btn
                    danger
                    onClick={() => { setRevealed(null); setDismissArmed(false) }}
                    data-testid="webhook-reveal-dismiss-confirm"
                  >
                    <X size={13} /> {i18nT('pages.webhooksPage.confirm_hide')} {revealed.signing_secret ? 'them' : 'it'} {i18nT('pages.webhooksPage.permanently')}
                  </Btn>
                )
                : (
                  <Btn onClick={() => setDismissArmed(true)} data-testid="webhook-reveal-dismiss">
                    <X size={13} /> {i18nT('pages.webhooksPage.dismiss_i_have_saved')} {revealed.signing_secret ? 'both' : i18nT('pages.webhooksPage.this_token')}
                  </Btn>
                )}
            </div>
            <span className="text-[12px] text-muted">
              {i18nT('pages.webhooksPage.this_panel_stays_on_setup_endpoint_until_you_dis')}
            </span>
          </div>
        </Banner>
      )}

      {!switchOn
        ? (
          <Banner
            tone="warn"
            testId="webhook-banner-off"
            icon={<Power size={16} className="text-warn" />}
            title={i18nT('pages.webhooksPage.inbound_webhooks_are_switched_off')}
            right={<Badge variant="muted">{i18nT('pages.webhooksPage.off')}</Badge>}
          >
            {i18nT('pages.webhooksPage.no_call_will_be_accepted_every_request_is_answer')}{' '}
            <code className="font-mono">503</code> {i18nT('pages.webhooksPage.before_its_token_is_even_checked_your_tokens_and')}
          </Banner>
        )
        : hasTokens
          ? (
            <Banner
              tone="ok"
              testId="webhook-banner-live"
              icon={<Check size={16} className="text-ok" />}
              title={i18nT('pages.webhooksPage.inbound_webhooks_are_enabled')}
              right={<Badge variant="ok">{i18nT('pages.webhooksPage.live')}</Badge>}
            >
              {i18nT('pages.webhooksPage.any_caller_that_can_reach_this_gateway_and_prese')}
            </Banner>
          )
          : (
            <Banner
              tone="muted"
              testId="webhook-banner-no-tokens"
              icon={<Webhook size={16} className="text-muted" />}
              title={i18nT('pages.webhooksPage.let_an_outside_system_wake_up_your_agent')}
            >
              {i18nT('pages.webhooksPage.a_webhook_is_a_private_web_address_another_progr')}
            </Banner>
          )}

      <Banner
        tone="danger"
        icon={<ShieldAlert size={16} className="text-danger" />}
        title={i18nT('pages.webhooksPage.a_webhook_token_is_a_remote_execution_credential')}
      >
        {i18nT('pages.webhooksPage.a_valid_token_lets_a_caller_make_the_agent_run_c')}
      </Banner>

      <Section title={i18nT('pages.webhooksPage.tokens')}>
        <div className="flex items-center gap-2 flex-wrap">
          <Input
            className="max-w-[260px]"
            placeholder={i18nT('pages.webhooksPage.label_e_g_review_bot')}
            aria-label={i18nT('pages.webhooksPage.new_token_label')}
            value={label}
            onChange={e => setLabel(e.target.value)}
          />
          <Btn
            primary
            // Blocked while a freshly minted secret is still on screen. The
            // reveal pane holds the ONLY copy of that token and signing secret —
            // a second mint would call setRevealed and overwrite them while the
            // first credential stays active, leaving it unusable and
            // unrecoverable. The operator dismisses the pane to mint again.
            disabled={!label.trim() || createToken.isPending || !!revealed}
            onClick={() => createToken.mutate(label.trim())}
          >
            <KeyRound size={13} /> {createToken.isPending ? i18nT('pages.webhooksPage.generating') : i18nT('pages.webhooksPage.generate_token')}
          </Btn>
          {/* eslint-disable-next-line jsx-a11y/label-has-for -- deprecated rule can't
              resolve the control through the custom <Checkbox>; the label is both
              nested around it and linked by htmlFor→id. */}
          <label
            htmlFor="webhook-require-signature"
            className="flex items-center gap-1.5 text-[12px] text-muted cursor-pointer"
          >
            <Checkbox
              id="webhook-require-signature"
              checked={requireSignature}
              onChange={e => setRequireSignature(e.target.checked)}
            />
            {i18nT('pages.webhooksPage.require_request_signing')}
          </label>
          <span className="text-[12px] text-muted">
            {i18nT('pages.webhooksPage.name_the_system_that_will_call_this_so_you_can_r')}
          </span>
        </div>

        {view.tokens.length === 0
          ? (
            <div className="mt-3 text-[13px] text-muted">
              {i18nT('pages.webhooksPage.no_tokens_yet_a_token_is_the_whole_authenticatio')}
            </div>
          )
          : (
            // Six columns cannot reflow below about 560px, so on a narrow
            // viewport the table scrolls sideways inside its own box rather than
            // being clipped by the pane. `min-w-max` keeps the columns at their
            // natural width instead of crushing the token strings.
            <div className="mt-3 -mx-1 px-1 overflow-x-auto">
              <table className="w-full min-w-max text-[13px] border-collapse table-striped">
              <thead>
                <tr>
                  {[i18nT('pages.webhooksPage.label'), i18nT('pages.webhooksPage.token'), i18nT('pages.webhooksPage.signing'), i18nT('pages.webhooksPage.created'), i18nT('pages.webhooksPage.last_used'), ''].map(h => (
                    <th key={h} className="text-left text-[11px] uppercase tracking-[.07em] text-muted font-semibold py-1.5 border-b border-border">
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {view.tokens.map(t => (
                  <tr key={t.id} className="border-b border-border last:border-b-0">
                    <td className="py-2 pr-3 text-text-strong">
                      {t.label}
                      {t.legacy && <Badge variant="muted" className="ml-2">{i18nT('pages.webhooksPage.config')}</Badge>}
                    </td>
                    <td className="py-2 pr-3 font-mono text-muted">{t.display_prefix}…{t.last4}</td>
                    <td className="py-2 pr-3">
                      <SigningBadge required={t.require_signature} tokenId={t.id} />
                    </td>
                    <td className="py-2 pr-3 text-muted">{timeAgo(t.created_at)}</td>
                    <td className="py-2 pr-3 text-muted">{usedAgo(t.last_used_at)}</td>
                    <td className="py-2 text-right">
                      <RevokeButton
                        token={t}
                        confirming={confirmRevoke === t.id}
                        onArm={() => setConfirmRevoke(t.id)}
                        onConfirm={() => revokeToken.mutate(t.id)}
                        onCancel={() => setConfirmRevoke(null)}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
              </table>
            </div>
          )}
      </Section>

      <Section title={i18nT('pages.webhooksPage.endpoint')} right={<Badge variant="muted">{i18nT('pages.webhooksPage.post')}</Badge>}>
        <CopyField
          value={view.url || i18nT('pages.webhooksPage.endpoint_url_unavailable_the_gateway_did_not_rep')}
          label={i18nT('pages.webhooksPage.copy_endpoint_url')}
          mask={!view.url}
        />
        <Hint>
          {i18nT('pages.webhooksPage.endpoint_checks_only_the_credential')}{' '}
          <code className="font-mono">{i18nT('pages.webhooksPage.ssh_nl_6776_127_0_0_1_6776_host')}</code>{' '}
          {i18nT('pages.webhooksPage.prefer_the_tunnel_to_a_public_interface')}
        </Hint>
      </Section>

      <Section
        title={i18nT('pages.webhooksPage.request_example')}
        right={(
          <>
            <ModeSwitch mode={mode} onChange={setExampleMode} />
            <Btn onClick={() => copyText(sharedExample)}><Copy size={13} /> {i18nT('pages.webhooksPage.copy')}</Btn>
          </>
        )}
      >
        <CodeBlock
          code={sharedExample}
          label={mode === 'signed' ? i18nT('pages.webhooksPage.example_signed_request') : i18nT('pages.webhooksPage.example_curl_request')}
        />
        <Hint>
          {mode === 'signed'
            ? (
              <>
                {i18nT('pages.webhooksPage.signed_mode_requirements', { seconds: signatureWindow })}
              </>
            )
            : (
              <>
                {i18nT('pages.webhooksPage.bearer_only_form_for_a_token_generated_with_sign')}
              </>
            )}
          {' '}{i18nT('pages.webhooksPage.response_is_a_receipt_only')}
        </Hint>
      </Section>

      <Section title={i18nT('pages.webhooksPage.limits_behaviour')} flush>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3">
          {[
            [i18nT('pages.webhooksPage.session_key'), <span key="k"><code className="font-mono">{view.limits.session_key_prefix}</code> {i18nT('pages.webhooksPage.prefix_required')}</span>, i18nT('pages.webhooksPage.anything_else_is_rejected_before_the_turn_starts')],
            [i18nT('pages.webhooksPage.message_size'), i18nT('pages.webhooksPage.max_chars', { chars: fmtNumber(view.limits.message_max) }), i18nT('pages.webhooksPage.longer_bodies_are_refused_not_truncated')],
            [i18nT('pages.webhooksPage.request_body'), i18nT('pages.webhooksPage.max_kib', { kib: Math.round((view.limits.body_max_bytes ?? 262144) / 1024) }), i18nT('pages.webhooksPage.the_raw_body_is_capped_before_it_is_parsed_large')],
            [i18nT('pages.webhooksPage.timeout'), i18nT('pages.webhooksPage.timeout_default_and_max', { default: view.limits.timeout_default, max: view.limits.timeout_max }), i18nT('pages.webhooksPage.override_per_call_with_the_timeout_field')],
            [i18nT('pages.webhooksPage.concurrency'), i18nT('pages.webhooksPage.max_runs', { runs: view.limits.max_concurrent }), i18nT('pages.webhooksPage.callers_over_the_cap_get_http_429_retry_with_bac')],
            [i18nT('pages.webhooksPage.request_signing'), i18nT('pages.webhooksPage.hmac_sha256_window', { seconds: signatureWindow }), i18nT('pages.webhooksPage.signed_over_timestamp_and_raw_body_detail', { seconds: signatureWindow })],
            [i18nT('pages.webhooksPage.failed_auth'), '10 per minute', i18nT('pages.webhooksPage.a_source_that_keeps_sending_bad_tokens_or_bad_si')],
            [i18nT('pages.webhooksPage.kill_switch'), switchOn ? 'on' : 'off', i18nT('pages.webhooksPage.when_off_every_call_gets_http_503_before_its_tok')],
            [i18nT('pages.webhooksPage.delivery_model'), i18nT('pages.webhooksPage.fire_and_forget_200'), i18nT('pages.webhooksPage.returns_immediately_the_answer_is_not_in_the_res')],
            [i18nT('pages.webhooksPage.results_go_to'), i18nT('pages.webhooksPage.dashboard_slack_dm'), i18nT('pages.webhooksPage.when_deliver_is_true')],
          ].map(([t, v, d], i) => (
            <div key={i} className="px-4 py-3 border-b border-r border-border">
              <div className="text-[11px] uppercase tracking-[.07em] text-muted">{t}</div>
              <div className="text-[13px] text-text-strong mt-1">{v}</div>
              <div className="text-[12px] text-muted mt-0.5">{d}</div>
            </div>
          ))}
        </div>
      </Section>

      <Section
        title={i18nT('pages.webhooksPage.capacity')}
        right={<Badge variant={view.slots.in_use >= view.slots.max ? 'err' : 'ok'}>
          {view.slots.in_use} / {view.slots.max} {i18nT('pages.webhooksPage.slots_in_use')}
        </Badge>}
      >
        <div className="flex items-center gap-1.5">
          {Array.from({ length: view.slots.max }).map((_, i) => (
            <span
              key={i}
              className={`h-2 flex-1 max-w-[80px] rounded-sm ${i < view.slots.in_use ? 'bg-accent' : 'bg-border'}`}
            />
          ))}
          <span className="text-[12px] text-muted ml-2">
            {Math.max(0, view.slots.max - view.slots.in_use)} {i18nT('pages.webhooksPage.free')}{' '}
            {view.slots.in_use >= view.slots.max ? 'new calls get 429' : i18nT('pages.webhooksPage.new_calls_accepted')}
          </span>
        </div>
      </Section>
    </>
  )

  const tokenPane = selectedToken && (
    <>
      <Banner
        tone={selectedToken.last_used_at ? 'ok' : 'muted'}
        icon={<KeyRound size={16} className={selectedToken.last_used_at ? 'text-ok' : 'text-muted'} />}
        title={selectedToken.label}
        right={<Badge variant={selectedToken.legacy ? 'muted' : 'ok'}>
          {selectedToken.legacy ? i18nT('pages.webhooksPage.from_config') : i18nT('pages.webhooksPage.active')}
        </Badge>}
      >
        {selectedToken.last_used_at
          ? i18nT('pages.webhooksPage.last_authorized_a_call', { when: usedAgo(selectedToken.last_used_at) })
          : i18nT('pages.webhooksPage.this_token_has_not_authorized_a_call_yet')}
      </Banner>

      <Section
        title={i18nT('pages.webhooksPage.token')}
        right={(
          <RevokeButton
            token={selectedToken}
            confirming={confirmRevoke === selectedToken.id}
            onArm={() => setConfirmRevoke(selectedToken.id)}
            onConfirm={() => revokeToken.mutate(selectedToken.id)}
            onCancel={() => setConfirmRevoke(null)}
          />
        )}
      >
        <Kv rows={[
          [i18nT('pages.webhooksPage.label'), selectedToken.label],
          [i18nT('pages.webhooksPage.token'), <span key="v" className="font-mono">{selectedToken.display_prefix}…{selectedToken.last4}</span>],
          [i18nT('pages.webhooksPage.request_signing'), (
            <span key="rs" className="flex items-start gap-2 flex-wrap">
              <SigningBadge required={selectedToken.require_signature} tokenId={selectedToken.id} />
              <span className="text-muted">
                {selectedToken.require_signature
                  ? i18nT('pages.webhooksPage.calls_must_send_signature_headers_detail', { seconds: signatureWindow })
                  : i18nT('pages.webhooksPage.this_token_is_accepted_on_the_bearer_header_alon')}
              </span>
            </span>
          )],
          [i18nT('pages.webhooksPage.created'), `${timeAgo(selectedToken.created_at)} · ${absolute(selectedToken.created_at)}`],
          [i18nT('pages.webhooksPage.last_used'), selectedToken.last_used_at
            ? `${usedAgo(selectedToken.last_used_at)} · ${absolute(selectedToken.last_used_at)}`
            : i18nT('pages.webhooksPage.never_used')],
          [i18nT('pages.webhooksPage.source'), selectedToken.legacy
            ? i18nT('pages.webhooksPage.hooks_webhook_token_in_config_json')
            : i18nT('pages.webhooksPage.generated_in_the_dashboard')],
        ]}
        />
        <Hint>
          {selectedToken.require_signature
            ? i18nT('pages.webhooksPage.credentials_not_recoverable_signed')
            : i18nT('pages.webhooksPage.credential_not_recoverable_bearer')}
          {selectedToken.legacy && i18nT('pages.webhooksPage.a_legacy_config_token_has_no_signing_secret_so_i')}
        </Hint>
      </Section>

      <Section
        title={i18nT('pages.webhooksPage.call_with_this_token')}
        right={(
          <Btn
            onClick={() => copyText(exampleFor(
              selectedToken.require_signature ? 'signed' : 'bearer',
              view.url, 'hook:my-job', i18nT('pages.webhooksPage.job_finished_3_findings'), signatureWindow,
            ))}
          >
            <Copy size={13} /> {i18nT('pages.webhooksPage.copy')}
          </Btn>
        )}
      >
        <CodeBlock
          code={exampleFor(
            selectedToken.require_signature ? 'signed' : 'bearer',
            view.url, 'hook:my-job', i18nT('pages.webhooksPage.job_finished_3_findings'), signatureWindow,
          )}
          label={selectedToken.require_signature
            ? i18nT('pages.webhooksPage.example_signed_request_for', { label: selectedToken.label })
            : i18nT('pages.webhooksPage.example_curl_request_for', { label: selectedToken.label })}
        />
        <Hint>
          {selectedToken.require_signature
            ? i18nT('pages.webhooksPage.this_token_rejects_an_unsigned_call_so_the_examp')
            : i18nT('pages.webhooksPage.this_token_was_generated_with_signing_switched_o')}
        </Hint>
      </Section>

      <Section title={i18nT('pages.webhooksPage.runs_this_token_authorized')} flush>
        <RunList
          runs={view.runs.filter(r => r.token_id === selectedToken.id)}
          empty={i18nT('pages.webhooksPage.no_recorded_call_has_used_this_token_yet')}
          onSelect={id => setSelection({ kind: 'run', id })}
        />
      </Section>
    </>
  )

  const contextPane = selectedContext && (
    <>
      <Banner
        tone={selectedContext.freshness === 'fresh' ? 'ok' : selectedContext.freshness === 'stale' ? 'warn' : 'muted'}
        testId="webhook-context-banner"
        icon={selectedContext.freshness === 'expired'
          ? <Minus size={16} className="text-muted" />
          : <Clock size={16} className={selectedContext.freshness === 'fresh' ? 'text-ok' : 'text-warn'} />}
        title={i18nT('pages.webhooksPage.freshness_context_registered_when', {
          freshness: FRESHNESS[selectedContext.freshness].label(),
          when: timeAgo(selectedContext.registered_at),
        })}
        right={<Badge variant={FRESHNESS[selectedContext.freshness].badge}>
          {FRESHNESS[selectedContext.freshness].label()}
        </Badge>}
      >
        {FRESHNESS[selectedContext.freshness].effect()} {i18nT('pages.webhooksPage.context_goes_stale_after_1_hour_and_expires_afte')}
      </Banner>

      <Section
        title={i18nT('pages.webhooksPage.hook')}
        right={(
          <Btn
            danger
            data-testid="webhook-delete-context"
            onClick={() => (confirmDelete === selectedContext.hook_id
              ? deleteContext.mutate(selectedContext.hook_id)
              : setConfirmDelete(selectedContext.hook_id))}
          >
            <Trash2 size={13} />
            {confirmDelete === selectedContext.hook_id ? i18nT('pages.webhooksPage.confirm_delete') : i18nT('pages.webhooksPage.delete_context')}
          </Btn>
        )}
      >
        <Kv rows={[
          [i18nT('pages.webhooksPage.hook_id'), <code key="h" className="font-mono">{selectedContext.hook_id}</code>],
          [i18nT('pages.webhooksPage.session_key'), <code key="s" className="font-mono">{selectedContext.session_key}</code>],
          [i18nT('pages.webhooksPage.registered'), `${timeAgo(selectedContext.registered_at)} · ${absolute(selectedContext.registered_at)}`],
          [i18nT('pages.webhooksPage.freshness'), (
            <span key="f" className="flex items-center gap-2">
              <Badge variant={FRESHNESS[selectedContext.freshness].badge}>
                {FRESHNESS[selectedContext.freshness].label()}
              </Badge>
              <span className="text-muted">{FRESHNESS[selectedContext.freshness].effect()}</span>
            </span>
          )],
          [i18nT('pages.webhooksPage.stored_size'), `${fmtNumber(selectedContext.context_chars)} chars`],
        ]}
        />
        {confirmDelete === selectedContext.hook_id && (
          <Hint>
            {i18nT('pages.webhooksPage.deleting_removes_the_stored_context', { file: i18nT('pages.webhooksPage.hooks_json') })}
          </Hint>
        )}
      </Section>

      <Section
        title={i18nT('pages.webhooksPage.stored_context_summary')}
        right={<Badge variant="muted">{fmtNumber(selectedContext.context_chars)} {i18nT('pages.webhooksPage.chars')}</Badge>}
      >
        {selectedContext.context_summary
          ? (
            <div className={`border-l-2 border-border-strong pl-3 font-mono text-[12px] whitespace-pre-wrap ${
              selectedContext.freshness === 'expired' ? 'text-muted' : 'text-text'
            }`}
            >
              {selectedContext.context_summary}
            </div>
          )
          : <div className="text-[13px] text-muted">{i18nT('pages.webhooksPage.no_summary_stored_for_this_hook')}</div>}
        <Hint>
          {i18nT('pages.webhooksPage.written_by_the')} <code className="font-mono">{i18nT('pages.webhooksPage.register_hook')}</code> {i18nT('pages.webhooksPage.tool_the_agent_reads_it_before_the_inbound_messa')}
        </Hint>
      </Section>

      <Section title={i18nT('pages.webhooksPage.runs_for_this_hook')} flush>
        <RunList
          runs={view.runs.filter(r => r.hook_id === selectedContext.hook_id)}
          empty={i18nT('pages.webhooksPage.no_call_has_arrived_for_this_hook_yet')}
          onSelect={id => setSelection({ kind: 'run', id })}
        />
      </Section>

      <Section
        title={i18nT('pages.webhooksPage.call_this_hook')}
        right={(
          <Btn onClick={() => copyText(exampleFor(
            defaultMode, view.url, selectedContext.session_key, i18nT('pages.webhooksPage.status_update'), signatureWindow,
          ))}
          >
            <Copy size={13} /> {i18nT('pages.webhooksPage.copy')}
          </Btn>
        )}
      >
        <CodeBlock
          code={exampleFor(
            defaultMode, view.url, selectedContext.session_key, i18nT('pages.webhooksPage.status_update'), signatureWindow,
          )}
          label={i18nT('pages.webhooksPage.example_request_for', { hookId: selectedContext.hook_id })}
        />
        <Hint>
          {defaultMode === 'signed'
            ? i18nT('pages.webhooksPage.shown_in_the_signing_form_because_the_tokens_tha')
            : i18nT('pages.webhooksPage.shown_in_the_bearer_only_form_because_no_existin')}
        </Hint>
      </Section>
    </>
  )

  const runPane = selectedRun && (
    <>
      <Banner
        tone={selectedRun.outcome === 'completed' ? 'ok'
          : selectedRun.outcome === 'timeout' ? 'warn' : 'danger'}
        icon={selectedRun.outcome === 'completed'
          ? <Check size={16} className="text-ok" />
          : <AlertTriangle size={16} className={selectedRun.outcome === 'timeout' ? 'text-warn' : 'text-danger'} />}
        title={`${OUTCOME[selectedRun.outcome].label()} · ${timeAgo(selectedRun.started_at)}`}
        right={<Badge variant={OUTCOME[selectedRun.outcome].badge}>
          {OUTCOME[selectedRun.outcome].label()}
        </Badge>}
      >
        {OUTCOME[selectedRun.outcome].blurb()}
      </Banner>

      <Section title={i18nT('pages.webhooksPage.run')}>
        <Kv rows={[
          [i18nT('pages.webhooksPage.hook_id'), selectedRun.hook_id
            ? <code key="h" className="font-mono">{selectedRun.hook_id}</code>
            : <span key="h" className="text-muted">{i18nT('pages.webhooksPage.unknown_the_call_never_authenticated')}</span>],
          [i18nT('pages.webhooksPage.session_key'), selectedRun.session_key
            ? <code key="s" className="font-mono">{selectedRun.session_key}</code>
            : <span key="s" className="text-muted">—</span>],
          [i18nT('pages.webhooksPage.caller_name'), selectedRun.name || <span key="n" className="text-muted">{i18nT('pages.webhooksPage.not_supplied')}</span>],
          [i18nT('pages.webhooksPage.started'), `${timeAgo(selectedRun.started_at)} · ${absolute(selectedRun.started_at)}`],
          [i18nT('pages.webhooksPage.duration'), durationLabel(selectedRun.duration_ms)],
          [i18nT('pages.webhooksPage.result_size'), sizeLabel(selectedRun.result_chars)],
          // `delivered` is `bool(destinations)` on the backend, i.e. true when
          // EITHER destination succeeded. Naming both here claimed a Slack DM
          // had gone out on runs where only the dashboard notification landed.
          // `detail` carries the exact set ("Delivered to ..."), so show that.
          [i18nT('pages.webhooksPage.delivered_to'), selectedRun.delivered
            ? (selectedRun.detail || i18nT('pages.webhooksPage.dashboard_notification_slack_dm'))
            : <span key="d" className="text-muted">{i18nT('pages.webhooksPage.not_delivered')}</span>],
          [i18nT('pages.webhooksPage.authorized_by'), selectedRun.token_id
            ? (
              <TokenLink
                tokenId={selectedRun.token_id}
                tokens={view.tokens}
                onSelect={id => setSelection({ kind: 'token', id })}
              />
            )
            : <span key="t" className="text-muted">{i18nT('pages.webhooksPage.no_valid_token')}</span>],
        ]}
        />
      </Section>

      {selectedRun.outcome !== 'completed' && (
        <Section title={i18nT('pages.webhooksPage.failure_detail')}>
          <div className="text-[13px] text-text">
            {selectedRun.detail || OUTCOME[selectedRun.outcome].blurb()}
          </div>
        </Section>
      )}
    </>
  )

  /* ── render ── */

  return (
    // A column: the shared PageHeader owns the page title and the two
    // page-level actions, and the rail-and-detail body fills what is left.
    // `min-w-0` on the body is load-bearing: it is a flex ITEM of the dashboard
    // shell, and a flex item defaults to `min-width: auto`, i.e. it refuses to
    // shrink below its min-content width. Without it the rail plus detail pane
    // forced ~846px regardless of the viewport, so on a phone the whole page
    // overflowed and the detail side was clipped rather than laid out narrow.
    <div className="flex flex-col h-full w-full min-w-0 min-h-0 bg-bg text-text">
      <PageHeader
        title={i18nT('pages.webhooksPage.webhooks')}
        actions={(
          <>
            <Btn
              aria-label={i18nT('pages.webhooksPage.copy_example')}
              title={i18nT('pages.webhooksPage.copy_example')}
              onClick={() => copyText(sharedExample)}
            >
              <Copy size={14} /> <span className="hidden sm:inline">{i18nT('pages.webhooksPage.copy_example')}</span>
            </Btn>
            <Btn
              primary={live}
              disabled={sendTest.isPending || !live}
              aria-label={i18nT('pages.webhooksPage.send_test_request')}
              title={live
                ? i18nT('pages.webhooksPage.send_test_request')
                : switchOn
                  ? i18nT('pages.webhooksPage.create_an_access_token_first_there_is_nothing_to')
                  : i18nT('pages.webhooksPage.inbound_webhooks_are_switched_off_turn_them_back')}
              onClick={() => sendTest.mutate(undefined)}
            >
              <Play size={14} />{' '}
              <span className="hidden sm:inline">
                {sendTest.isPending ? i18nT('pages.webhooksPage.sending') : i18nT('pages.webhooksPage.send_test_request')}
              </span>
            </Btn>
          </>
        )}
      />

      <div className="flex flex-1 w-full min-w-0 min-h-0">
      {rail.collapsed
        ? (
          <aside
            style={{ width: rail.width }}
            className="shrink-0 flex flex-col min-h-0 border-r border-border bg-bg-accent"
          >
            <div className="h-[46px] shrink-0 flex items-center justify-center border-b border-border">
              <IconButton aria-label={i18nT('pages.webhooksPage.expand_webhooks_rail')} onClick={rail.expand}>
                <Webhook size={16} className="text-accent" />
              </IconButton>
            </div>
          </aside>
        )
        : (
          <aside
            style={{ width: mobileRailOpen ? '100%' : rail.width }}
            data-testid="webhook-rail"
            className="shrink-0 flex flex-col min-h-0 border-r border-border bg-bg-accent"
          >
            <div className="h-[46px] shrink-0 flex items-center gap-2.5 px-3 border-b border-border">
              <Webhook size={15} className="text-accent shrink-0" />
              {/* The page title lives in PageHeader now, so repeating it here
                  would say "Webhooks" twice within 40 vertical pixels. The rail
                  keeps the icon for orientation when it is narrow. */}
              <span
                className="ml-auto flex items-center gap-1.5 text-[11px] text-muted border border-border rounded-md px-1.5 py-[2px] bg-bg-elevated"
                title={i18nT('pages.webhooksPage.concurrent_hook_runs')}
              >
                <span className="w-[34px] h-1 rounded-sm bg-border overflow-hidden">
                  <span
                    className="block h-full bg-accent"
                    style={{ width: `${Math.min(100, (view.slots.in_use / Math.max(1, view.slots.max)) * 100)}%` }}
                  />
                </span>
                {view.slots.in_use} / {view.slots.max} {i18nT('pages.webhooksPage.slots')}
              </span>
            </div>
            <div className="p-2 border-b border-border">
              <SearchInput
                placeholder={i18nT('pages.webhooksPage.filter_tokens_contexts_and_runs')}
                aria-label={i18nT('pages.webhooksPage.filter_webhooks')}
                value={filter}
                onChange={e => setFilter(e.target.value)}
              />
            </div>
            <div className="flex-1 min-h-0 overflow-y-auto">
              {isLoading ? <RailSkeleton /> : railBody}
            </div>
          </aside>
        )}

      {/* Rail resize edge — behaviour comes from the shared useColumnResize hook;
          dragging well past the minimum collapses the rail to its icon strip.
          Hidden on a narrow viewport: there is no width there that leaves both
          columns usable, so the rail stays the icon strip and dragging it wider
          would only re-create the clipped detail pane. */}
      {!isMobile && (
        <div
          {...rail.handleProps}
          role="separator"
          aria-orientation="vertical"
          aria-label={i18nT('pages.webhooksPage.resize_webhooks_rail')}
          className="w-1.5 shrink-0 cursor-col-resize hover:bg-accent/30 transition-colors"
          style={{ touchAction: 'none' }}
        />
      )}

      <main className={`flex-1 min-w-0 min-h-0 flex-col bg-bg ${mobileRailOpen ? 'hidden' : 'flex'}`}>
        <div className="h-[46px] shrink-0 flex items-center gap-2 px-4 border-b border-border bg-bg-accent">
          {/* Just the selected entity. PageHeader carries "Webhooks" directly
              above this bar, so a "Webhooks /" prefix here printed the word
              twice within 90 vertical pixels. */}
          <div className="flex items-center gap-1.5 text-[12px] min-w-0">
            <span className="text-text-strong font-semibold truncate" data-testid="webhook-detail-title">{leaf}</span>
          </div>
        </div>

        <div className="flex-1 min-h-0 overflow-y-auto" data-testid="webhook-detail" data-pane={pane}>
          {loadError && (
            <Banner
              tone="warn"
              icon={<AlertTriangle size={16} className="text-warn" />}
              title={i18nT('pages.webhooksPage.webhook_settings_are_unavailable')}
              right={<Btn onClick={reload}>{i18nT('pages.webhooksPage.retry')}</Btn>}
            >
              {loadError} {i18nT('pages.webhooksPage.reference_below_still_describes_the_endpoint')}
            </Banner>
          )}
          {mutationMessage && (
            <Banner
              tone="danger"
              icon={<AlertTriangle size={16} className="text-danger" />}
              title={i18nT('pages.webhooksPage.that_action_did_not_go_through')}
            >
              {mutationMessage}
            </Banner>
          )}
          {testResult && (
            <Banner
              tone={testResult.ok ? 'ok' : 'danger'}
              testId="webhook-test-result"
              icon={testResult.ok
                ? <Check size={16} className="text-ok" />
                : <AlertTriangle size={16} className="text-danger" />}
              title={testResult.ok
                ? i18nT('pages.webhooksPage.test_request_accepted_http', { status: testResult.status })
                : testResult.status
                  ? i18nT('pages.webhooksPage.test_request_failed_http', { status: testResult.status })
                  : i18nT('pages.webhooksPage.test_request_failed')}
              right={<Btn onClick={() => setTestResult(null)}><X size={13} /> {i18nT('pages.webhooksPage.dismiss')}</Btn>}
            >
              {testResult.ok
                ? <>{i18nT('pages.webhooksPage.session')} <code className="font-mono">{testResult.session_key}</code> {i18nT('pages.webhooksPage.started_the_agent_s_answer_arrives_in_notificati')}</>
                : testResult.error || i18nT('pages.webhooksPage.the_gateway_refused_the_call')}
            </Banner>
          )}

          {isLoading
            ? <DetailSkeleton />
            : pane === 'token' ? tokenPane
              : pane === 'context' ? contextPane
                : pane === 'run' ? runPane
                  : setupPane}
        </div>
      </main>
      </div>
    </div>
  )
}

/* ── sub-components ────────────────────────────────────────────────────── */

/** Whether a given token demands an HMAC signature. Bearer-only is not an error
 *  — it is a deliberate escape hatch for callers that cannot sign — so it reads
 *  as muted, not danger. */
function SigningBadge({ required, tokenId }: { required: boolean; tokenId: string }) {
  return (
    <span
      className="inline-flex items-center gap-1.5"
      data-testid={`webhook-signing-${tokenId}`}
      data-signing={required ? 'required' : 'bearer-only'}
    >
      {required
        ? <ShieldCheck size={13} className="text-ok shrink-0" />
        : <ShieldAlert size={13} className="text-muted shrink-0" />}
      <Badge variant={required ? 'ok' : 'muted'}>
        {required ? i18nT('pages.webhooksPage.signature_required') : i18nT('pages.webhooksPage.bearer_only')}
      </Badge>
    </span>
  )
}

/** Picks which form the shared request example is shown in. Static labels, no
 *  motion — the snippet below it is what changes. */
function ModeSwitch({ mode, onChange }: {
  mode: ExampleMode
  onChange: (mode: ExampleMode) => void
}) {
  return (
    // `data-mode` is the contract the tests and the capture harness read; the
    // wrapper keeps it while SegmentedControl owns the control itself.
    //
    // `collapse={false}` is required, for two reasons the component documents:
    // the parent hugs its content (`inline-flex`), which makes its responsive
    // measurement circular, and with collapsing off it never renders the
    // dropdown whose overlay `.card-glow`'s `> * { z-index: 1 }` would trap
    // beneath the rows below — this control sits inside such a Card.
    // SegmentedControl sets no role or accessible name of its own, so the two
    // segments would otherwise be announced with no indication of what they
    // switch. The group name lives here.
    <span
      data-testid="webhook-example-mode"
      data-mode={mode}
      className="inline-flex"
      role="group"
      aria-label={i18nT('pages.webhooksPage.request_example_mode')}
    >
      <SegmentedControl<ExampleMode>
        segments={[
          { key: 'signed', label: i18nT('pages.webhooksPage.signed') },
          { key: 'bearer', label: i18nT('pages.webhooksPage.bearer_only') },
        ]}
        value={mode}
        onChange={onChange}
        collapse={false}
        layoutId="webhook-example-mode"
      />
    </span>
  )
}

/** Revoking is destructive and irreversible for callers, so it takes two
 *  deliberate clicks. The legacy config token cannot be revoked here at all. */
function RevokeButton({ token, confirming, onArm, onConfirm, onCancel }: {
  token: WebhookTokenEntry
  confirming: boolean
  onArm: () => void
  onConfirm: () => void
  onCancel: () => void
}) {
  if (token.legacy) {
    return (
      <span className="text-[12px] text-muted">
        {i18nT('pages.webhooksPage.remove_setting_from_config_to_revoke', { setting: i18nT('pages.webhooksPage.hooks_webhook_token') })}
      </span>
    )
  }
  return (
    <span className="inline-flex items-center gap-1.5">
      {confirming && (
        <span className="text-[12px] text-muted">{i18nT('pages.webhooksPage.calls_using_it_start_failing_with_401')}</span>
      )}
      <Btn
        danger
        data-testid={`webhook-revoke-${token.id}`}
        onClick={() => (confirming ? onConfirm() : onArm())}
      >
        <Trash2 size={13} /> {confirming ? i18nT('pages.webhooksPage.confirm_revoke') : i18nT('pages.webhooksPage.revoke')}
      </Btn>
      {confirming && <Btn onClick={onCancel}>{i18nT('pages.webhooksPage.keep')}</Btn>}
    </span>
  )
}

function TokenLink({ tokenId, tokens, onSelect }: {
  tokenId: string
  tokens: WebhookTokenEntry[]
  onSelect: (id: string) => void
}) {
  const token = tokens.find(t => t.id === tokenId)
  if (!token) return <span className="text-muted">{tokenId} {i18nT('pages.webhooksPage.revoked')}</span>
  return (
    <button type="button" className="text-accent hover:underline" onClick={() => onSelect(token.id)}>
      {token.label}
    </button>
  )
}

/** Run timeline shared by the token and context panes. */
function RunList({ runs, empty, onSelect }: {
  runs: WebhookRunRecord[]
  empty: string
  onSelect: (id: string) => void
}) {
  if (runs.length === 0) {
    return <div className="px-4 py-3.5 text-[13px] text-muted">{empty}</div>
  }
  return (
    <div>
      {runs.map(r => (
        <button
          key={r.id}
          type="button"
          onClick={() => onSelect(r.id)}
          className="flex w-full text-left gap-3 px-4 py-2.5 border-b border-border last:border-b-0 hover:bg-bg-accent transition-colors"
        >
          <Dot className={`${OUTCOME[r.outcome].dot} mt-1.5`} />
          <span className="flex-1 min-w-0">
            <span className="flex items-center gap-2 flex-wrap">
              <Badge variant={OUTCOME[r.outcome].badge}>{OUTCOME[r.outcome].label()}</Badge>
              <span className="text-[13px] text-text-strong truncate">{r.name || r.hook_id || i18nT('pages.webhooksPage.unknown_caller')}</span>
              <span className="ml-auto text-[11px] text-muted-strong">{timeAgo(r.started_at)}</span>
            </span>
            <span className="flex gap-3.5 mt-1 text-[11px] text-muted-strong font-mono">
              <span>{durationLabel(r.duration_ms)}</span>
              <span>{sizeLabel(r.result_chars)}</span>
              <span>{r.delivered ? 'delivered' : i18nT('pages.webhooksPage.not_delivered')}</span>
            </span>
          </span>
        </button>
      ))}
    </div>
  )
}

/* Shimmer placeholders — never a rotating spinner. */

function RailSkeleton() {
  return (
    <div className="p-3 flex flex-col gap-2.5" aria-hidden>
      {Array.from({ length: 7 }).map((_, i) => (
        <div key={i} className="flex items-center gap-2">
          <Circle size={7} className="text-border" />
          <Skeleton className="h-3.5 flex-1" style={{ maxWidth: `${55 + (i * 13) % 35}%` }} />
        </div>
      ))}
    </div>
  )
}

function DetailSkeleton() {
  return (
    <div className="px-4 py-4 flex flex-col gap-3" aria-hidden>
      <Skeleton className="h-12 w-full" />
      <Skeleton className="h-4 w-40" />
      <Skeleton className="h-24 w-full" />
      <Skeleton className="h-4 w-56" />
      <Skeleton className="h-20 w-full" />
    </div>
  )
}
