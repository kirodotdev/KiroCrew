/**
 * Pieces both AWS Control surfaces render: the per-account console and the cloud
 * drive page.
 *
 * They live here rather than in either surface because importing across the two
 * would be circular - the console navigates INTO the drive page, so the drive
 * page cannot import from the console.
 */
import { useState } from 'react'
import type { ReactNode } from 'react'
import { Copy, Check, RefreshCw } from 'lucide-react'
import { Btn, StatCard } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import Clickable from '../../components/Clickable'
import { i18nT } from '../../i18n/t'
import { fmtBytes } from '../../i18n/format'
import { errorReportOf } from './api'
import type { DriveSection, DriveUsage } from './types'

/* ── the one error surface ───────────────────────────────────────────────── */

/**
 * Every error this app shows renders through here, and through nothing else.
 *
 * The shared `ErrorNotice` recovers an error's context (endpoint, status, the
 * backend's `code`, the raw body) from the error journal by MESSAGE — which
 * works for the rest of the dashboard because those surfaces render
 * `e.message`. This app renders a localised sentence instead, so the lookup can
 * never match. `error` is the thrown value, and `errorReportOf` reads the journal
 * entry the client attached to it — that is the whole reason this wrapper
 * exists. A surface with no thrown value (a reason the backend reported inside
 * a 200) omits it, and the hand-off carries the sentence.
 *
 * The agent hand-off keeps `ErrorNotice`'s own opt-in default and is stated at
 * every call site. The sites that leave it off are the two notices beside a
 * live input (the folder-name field, the share note, the ticked profiles of
 * the Add-accounts form), where the navigation would take the typed text
 * with the page, and the client-side name checks,
 * which never reached AWS and have nothing for the agent to read. Everywhere
 * else an AWS failure (AccessDenied on a bucket, an expired SSO session, a
 * region mismatch) is exactly the kind the agent can diagnose from the report
 * and the reader cannot from the sentence, and there is nothing on screen to
 * lose — so every other notice opts in. Two panes go one step further because
 * all of their notices share the screen with one draft: the Files pane gates
 * on the folder-name disclosure being closed (`handOff` in `DriveSectionView`),
 * and the accounts pane gates on no profile being ticked in the Add-accounts
 * form (`handOff` in `AccountsPane`, fed by `AddAccounts`'s `onDraftChange`).
 */
export function AwsErrorNotice({ error, message, title, variant = 'block', className, testId, onRetry, askAgent }: {
  /** The thrown value, when there is one. Its journal entry rides along to the agent. */
  error?: unknown
  /** The localised sentence. Falsy renders nothing, so callers need no `&&` guard. */
  message?: string | null
  /** Optional bold lead before the sentence. */
  title?: string
  /** `block` = boxed banner; `inline` = compact text for an existing flex row. */
  variant?: 'block' | 'inline'
  className?: string
  testId?: string
  /**
   * A READ that the reader can re-issue renders a Try-again button under the
   * notice (`<testId>-retry`). A transient read is the one failure the reader
   * can clear alone, so every read notice offers it; a mutation's retry is the
   * control that fired it, which is still on screen, so those pass nothing.
   */
  onRetry?: () => void
  /**
   * Offer the agent hand-off. Same default as `ErrorNotice` — OFF — for the
   * same reason: the hand-off navigates to the chat, and a forgotten prop must
   * cost a convenience rather than whatever is typed on screen. Every notice in
   * this app states it explicitly, and the ones that leave it off are exactly
   * the notices beside a live input (the folder-name field, the share note,
   * the Add-accounts checkboxes) and
   * the client-side name checks, which never reached AWS and have nothing for
   * the agent to read.
   */
  askAgent?: boolean
}) {
  const notice = (
    <ErrorNotice
      message={message}
      report={errorReportOf(error)}
      title={title}
      variant={variant}
      askAgent={askAgent}
      className={onRetry ? 'w-full' : className}
      testId={testId}
    />
  )
  if (!onRetry || !message) return notice
  return (
    <div className={`flex flex-col items-start gap-2 ${className ?? ''}`}>
      {notice}
      <Btn onClick={onRetry} data-testid={testId ? `${testId}-retry` : undefined}>
        <RefreshCw size={13} />
        {i18nT('apps.awsControl.console.retry')}
      </Btn>
    </div>
  )
}

/** Copy-to-clipboard button that flips to a check for ~1.5s. */
export function CopyBtn({ text, testId, ariaLabel }: { text: string; testId?: string; ariaLabel?: string }) {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch { /* clipboard unavailable — the text is still selectable by hand */ }
  }
  return (
    <Btn onClick={copy} data-testid={testId} aria-label={ariaLabel}>
      {copied ? <Check size={13} className="text-ok" /> : <Copy size={13} />}
      {copied ? i18nT('apps.awsControl.console.copied') : i18nT('apps.awsControl.console.copy')}
    </Btn>
  )
}

/* ── pane header for the flat-rail layout ────────────────────────────────── */

/**
 * Title row for one rail pane: an accent icon, the pane's name at the SAME
 * title metrics as `PageHeader` (`text-2xl font-bold tracking-tight`), and the
 * pane's own actions on the right. The rail already answers "where am I", so
 * there is no back-crumb — a pane is a sibling, not a descent. Actions wrap
 * under the title on narrow viewports rather than clipping.
 *
 * `subtitle` is the pane's one-line orientation, and it belongs HERE rather
 * than as a floating `<p>` under the header: a paragraph the panes each placed
 * themselves drifted in gutter, size and gap from one pane to the next, and it
 * is the same defect the shared title metrics already fixed one line above it.
 * It takes a node, not just a string, so a caller can keep a test id or a
 * mono fragment on the sentence it owns while the type scale stays here.
 */
export function PaneHeader({ icon, title, meta, subtitle, actions }: {
  icon?: ReactNode
  title: string
  /** Identifying metadata after the title (counts, mono ids). */
  meta?: ReactNode
  /** One line under the title. Wrapped in this component's own type scale. */
  subtitle?: ReactNode
  actions?: ReactNode
}) {
  return (
    <div className="mb-4">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        {icon && <span className="shrink-0 text-accent">{icon}</span>}
        <span className="min-w-0 max-w-full truncate text-2xl font-bold tracking-tight text-text-strong" data-testid="page-title">{title}</span>
        {meta}
        <span className="flex-1" />
        {actions}
      </div>
      {subtitle && (
        <p className="mt-1 max-w-[80ch] text-[13px] leading-relaxed text-muted" data-testid="pane-subtitle">
          {subtitle}
        </p>
      )}
    </div>
  )
}

/* ── metric card with a sub-line ─────────────────────────────────────────── */

/**
 * `StatCard` plus ONE line of context under its value, opening the pane that
 * owns the figure.
 *
 * The sub-line is what turns a number into a reading — "2" says little beside
 * "1 needs attention" — and `StatCard` has no slot for it: `value` is typed
 * `string | number` and the component renders no children. So this COMPOSES the
 * shared card rather than forking a lookalike beside it, and lays the line over
 * the bottom inset the card is asked to reserve (`pb-9`). One surface, one
 * border, one entrance animation.
 *
 * `onClick` is required rather than optional because `StatCard` lifts on hover
 * whether or not it is a target, so a metric card that went nowhere signalled a
 * click it did not have; every figure this app restates has a pane that owns
 * it, and the card is the way through.
 *
 * `pointer-events-none` is what keeps the card's own hover working underneath
 * the line, and `group-hover` mirrors the card's lift so the two cannot drift
 * apart mid-transition. `tabular-nums` sits on the card because
 * `font-variant-numeric` inherits, and the value it decorates is inside.
 */
export function MetricCard({ label, value, sub, accent, delay, testId, onClick }: {
  label: string
  /** Undefined/null renders the card's own skeleton, so a pending query never jumps. */
  value?: string | number | null
  /** The line under the value. Omitted when there is nothing true to say. */
  sub?: string
  accent?: boolean
  delay?: number
  testId?: string
  /** Open the pane that owns this figure. */
  onClick: () => void
}) {
  return (
    <div className="group relative">
      <StatCard
        label={label}
        value={value}
        accent={accent}
        delay={delay}
        onClick={onClick}
        className="h-full pb-9 tabular-nums"
        data-testid={testId}
      />
      {sub && (
        <span
          className="pointer-events-none absolute inset-x-4 bottom-3 truncate text-[12px] text-muted transition-all group-hover:-translate-y-0.5"
          data-testid={testId ? `${testId}-sub` : undefined}
        >
          {sub}
        </span>
      )}
    </div>
  )
}

/* ── drive storage: one bar, one legend ──────────────────────────────────── */

const STORAGE_SECTIONS: DriveSection[] = ['drive', 'library', 'backup']

/**
 * Section → fill token. Three distinguishable hues from the theme, no hex.
 * `info`, not `aim`, for the library: in the shipped purple themes aim sits on
 * accent, and the two largest segments became one purple with a seam.
 */
const STORAGE_TONE: Record<DriveSection, string> = {
  drive: 'bg-accent',
  library: 'bg-info',
  backup: 'bg-muted/45',
}

const STORAGE_LABEL: Record<DriveSection, string> = {
  drive: 'apps.awsControl.console.section_files',
  library: 'apps.awsControl.console.section_library',
  backup: 'apps.awsControl.console.section_backup',
}

/**
 * The drive's bytes split three ways: a single 6px track with one segment per
 * section, and a legend that names every section including the 0-byte ones.
 *
 * An empty drive draws the bare track rather than nothing, so the card never
 * reads as "failed to load". Colour is never the only cue — the legend spells
 * out each section with its own size beside its swatch.
 */
export function StorageBar({ usage, testId = 'storage-bar' }: { usage: DriveUsage; testId?: string }) {
  const total = usage.bytes
  return (
    <div data-testid={testId}>
      <div
        className="flex h-1.5 w-full overflow-hidden rounded-full bg-bg-hover"
        data-testid={`${testId}-bar`}
        role="img"
        aria-label={i18nT('apps.awsControl.overview.drive_bar_label', { size: fmtBytes(total) })}
      >
        {total > 0 && STORAGE_SECTIONS.map((s) => {
          const pct = (usage.sections[s].bytes / total) * 100
          if (pct <= 0) return null
          return (
            <span
              key={s}
              className={`h-full ${STORAGE_TONE[s]}`}
              style={{ width: `${pct}%` }}
              data-testid={`${testId}-segment-${s}`}
            />
          )
        })}
      </div>
      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1" data-testid={`${testId}-legend`}>
        {STORAGE_SECTIONS.map((s) => (
          <span key={s} className="flex items-center gap-1.5 text-[12px]" data-testid={`${testId}-legend-${s}`}>
            <span className={`h-2 w-2 shrink-0 rounded-full ${STORAGE_TONE[s]}`} aria-hidden="true" />
            <span className="text-muted">{i18nT(STORAGE_LABEL[s])}</span>
            <span className="font-mono tabular-nums text-text">{fmtBytes(usage.sections[s].bytes)}</span>
          </span>
        ))}
      </div>
    </div>
  )
}

/**
 * One small tile inside a card: an icon, a name, and one counted line under it.
 *
 * Two shapes from one prop. With `onClick` the tile is a bordered, hover-lit
 * target that opens what it names, on `Clickable` so role, tab order and
 * Enter/Space come from the primitive. Without it the same three facts render
 * as a flat row with no border — a bordered box that does nothing reads as a
 * button, and readers clicked the read-only ones expecting to land somewhere.
 *
 * The bordered shape is `bg-bg-elevated` rather than `bg-card` because it sits
 * INSIDE a card, and repeating the card fill would make the two surfaces
 * indistinguishable.
 */
export function QuickTile({ icon, label, count, testId, onClick }: {
  icon: ReactNode
  label: string
  count: string
  testId?: string
  /** Open what the tile names. Omitted = a read-only row, drawn without a border. */
  onClick?: () => void
}) {
  const body = (
    <>
      <span className="mt-0.5 shrink-0 text-muted">{icon}</span>
      <span className="min-w-0">
        <span className="block text-[13px] font-medium leading-snug text-text-strong">{label}</span>
        <span className="block truncate font-mono text-[12px] tabular-nums text-muted" data-testid={testId ? `${testId}-count` : undefined}>
          {count}
        </span>
      </span>
    </>
  )
  return onClick ? (
    <Clickable
      onClick={onClick}
      aria-label={label}
      className="flex min-w-0 cursor-pointer items-start gap-2 rounded-lg border border-border bg-bg-elevated p-3 text-left transition-colors hover:border-border-strong hover:bg-bg-hover focus-ring"
      data-testid={testId}
    >
      {body}
    </Clickable>
  ) : (
    <div className="flex min-w-0 items-start gap-2 py-1.5" data-testid={testId}>
      {body}
    </div>
  )
}

/* There is deliberately no `CrumbHeader` here any more.
 *
 * It was the crumb+title header for the two inner surfaces back when the app
 * descended into a per-account console. The flat-rail layout made every surface
 * a SIBLING pane, so the back-crumb had nothing to point at and `PaneHeader`
 * took over — leaving `CrumbHeader` with zero call sites and 12 catalogs of
 * copy nothing rendered. */

