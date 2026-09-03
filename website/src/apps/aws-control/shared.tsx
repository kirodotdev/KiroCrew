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
import { Copy, Check, ChevronLeft, RefreshCw } from 'lucide-react'
import { Btn } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { i18nT } from '../../i18n/t'
import { errorReportOf } from './api'

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

/* ── shared section header ───────────────────────────────────────────────── */

export function SectionHeader({ icon, title, actions }: { icon: ReactNode; title: string; actions?: ReactNode }) {
  return (
    <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
      <h2 className="flex items-center gap-1.5 text-sm font-semibold text-text-strong">
        <span className="text-accent">{icon}</span>
        {title}
      </h2>
      {actions}
    </div>
  )
}

/* ── pane header for the flat-rail layout ────────────────────────────────── */

/**
 * Title row for one rail pane: an accent icon, the pane's name at the SAME
 * title metrics as `PageHeader` (`text-2xl font-bold tracking-tight`), and the
 * pane's own actions on the right. The rail already answers "where am I", so
 * unlike `CrumbHeader` there is no back-crumb — a pane is a sibling, not a
 * descent. Actions wrap under the title on narrow viewports rather than
 * clipping.
 */
export function PaneHeader({ icon, title, meta, actions }: {
  icon?: ReactNode
  title: string
  /** Identifying metadata after the title (counts, mono ids). */
  meta?: ReactNode
  actions?: ReactNode
}) {
  return (
    <div className="mb-4 flex flex-wrap items-center gap-x-3 gap-y-2">
      {icon && <span className="shrink-0 text-accent">{icon}</span>}
      <span className="min-w-0 max-w-full truncate text-2xl font-bold tracking-tight text-text-strong" data-testid="page-title">{title}</span>
      {meta}
      <span className="flex-1" />
      {actions}
    </div>
  )
}

/* ── shared page header for the inner surfaces ───────────────────────────── */

/**
 * Crumb + title header for the console and drive pages.
 *
 * The entry page renders the standard `PageHeader`; the two inner surfaces used
 * to hand-roll their own smaller title rows, so descending a level also dropped
 * the type scale — three levels of one app read as three different products.
 * This pins the inner pages to the SAME title metrics as `PageHeader`
 * (`text-2xl font-bold tracking-tight`) and the same content-column gutters,
 * with the back-crumb above and the page's identifying metadata inline after
 * the title. Callers own the crumb wording and the meta content; the type
 * scale and spacing live here so the levels cannot drift apart again.
 */
export function CrumbHeader({ onBack, crumb, crumbTestId, leading, title, meta }: {
  onBack: () => void
  /** Crumb content after the chevron, e.g. `账户 / <name>`. */
  crumb: ReactNode
  crumbTestId: string
  /** Small leading glyph before the title (health dot, drive icon). */
  leading?: ReactNode
  title: string
  /** Identifying metadata after the title (mono id + copy, bucket + usage). */
  meta?: ReactNode
}) {
  return (
    <div className="px-4 pt-2 pb-3 md:px-6">
      <button
        onClick={onBack}
        className="mb-1 inline-flex max-w-full items-center gap-1 text-[13px] text-muted hover:text-text cursor-pointer bg-transparent border-none p-0"
        data-testid={crumbTestId}
      >
        <ChevronLeft size={14} className="shrink-0" />
        {/* min-w-0 + truncate: a long valid account name must shorten inside
            the crumb, not push the row past a 320px viewport. */}
        <span className="min-w-0 truncate">{crumb}</span>
      </button>
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        {leading}
        <span className="min-w-0 max-w-full truncate text-2xl font-bold tracking-tight text-text-strong" data-testid="page-title">{title}</span>
        {meta}
      </div>
    </div>
  )
}
