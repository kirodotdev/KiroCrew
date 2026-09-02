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
import { Copy, Check, ChevronLeft } from 'lucide-react'
import { Btn } from '../../components/ui'
import { i18nT } from '../../i18n/t'

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
