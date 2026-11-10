/**
 * One issue from `POST /api/source/issues`, in either of two views:
 *
 * - `condensed`: one row. Provider mark, display key, title, up to two label
 *   chips (hidden when the row is narrow), the status pill, the updated time.
 * - `expanded`: a card. Provider, key and project over the status pill; the
 *   title; a three-line excerpt; priority and labels; a footer with people,
 *   counts, the updated time and a link out to the provider.
 *
 * Everything the provider sends is rendered as text (`dir="auto"` so RTL text
 * reads in its own direction). A link opens only an http(s) url, and an avatar
 * is fetched only from one.
 */
import type { ReactNode } from 'react'
import { GitPullRequest, MessageSquare } from 'lucide-react'
import type { IssuePerson, IssueSummary } from '../../types'
import { labelChipStyle } from '../../components/IssuePanel'
import SourceProviderMark from '../../components/SourceProviderMark'
import { BrandGlyph } from '../../components/BrandIcon'
import linearLogoUrl from '../../pages/connections/logos/linear.svg'
import { safeHttpUrl } from '../../lib/safeUrl'
import { fmtDateTime, fmtNumber, fmtRelative } from '../../i18n/format'
import { i18nT } from '../../i18n/t'

export type IssueView = 'condensed' | 'expanded'

export interface IssueCardProps {
  issue: IssueSummary
  /** Defaults to `condensed`. */
  view?: IssueView
  /** Makes the row (condensed) or the title (expanded) a button. Without it the
   *  row links out to the issue. */
  onSelect?: (issue: IssueSummary) => void
}

/** Brand names are proper nouns; the catalog carries them so a locale may still
 *  transliterate one. Indexed inline so the key gate resolves each key. */
export function issueProviderName(provider: IssueSummary['provider']): string {
  if (provider === 'gitlab') return i18nT('appSdk.issues.provider_gitlab')
  if (provider === 'jira') return i18nT('appSdk.issues.provider_jira')
  if (provider === 'linear') return i18nT('appSdk.issues.provider_linear')
  return i18nT('appSdk.issues.provider_github')
}

function ProviderMark({ provider, size }: { provider: IssueSummary['provider']; size: number }) {
  if (provider === 'linear') return <BrandGlyph url={linearLogoUrl} size={size} testId="linear-provider-mark" />
  return <SourceProviderMark provider={provider} size={size} />
}

function StatusPill({ issue }: { issue: IssueSummary }) {
  const tone = issue.state === 'closed' ? 'bg-aim/15 text-aim' : 'bg-ok/15 text-ok-text'
  return (
    <span
      dir="auto"
      data-testid="issue-status"
      className={`inline-flex max-w-[10rem] shrink-0 items-center truncate rounded-full px-1.5 py-0.5 text-[11px] font-medium leading-none ${tone}`}
    >
      {issue.statusLabel || (issue.state === 'closed' ? i18nT('appSdk.issues.state_closed') : i18nT('appSdk.issues.state_open'))}
    </span>
  )
}

function LabelChip({ name, color, description }: { name: string; color: string; description: string }) {
  return (
    <span
      dir="auto"
      className="inline-block max-w-[9rem] truncate rounded-full px-1.5 py-0.5 text-[10px] font-medium leading-none"
      style={labelChipStyle(color)}
      title={description || name}
    >
      {name}
    </span>
  )
}

function Updated({ issue }: { issue: IssueSummary }) {
  if (!issue.updatedAt) return null
  return (
    <time dateTime={issue.updatedAt} title={fmtDateTime(issue.updatedAt)} className="shrink-0 whitespace-nowrap">
      {fmtRelative(issue.updatedAt)}
    </time>
  )
}

function Avatar({ person }: { person: IssuePerson }) {
  const src = safeHttpUrl(person.avatarUrl)
  if (!src) return null
  return <img src={src} alt="" width={16} height={16} referrerPolicy="no-referrer" className="h-4 w-4 shrink-0 rounded-full" />
}

/** An icon and a number; the words are for screen readers and the tooltip. */
function Count({ icon, value, label }: { icon: ReactNode; value: number; label: string }) {
  return (
    <span className="inline-flex items-center gap-1" title={label}>
      {icon}<span aria-hidden="true">{fmtNumber(value)}</span><span className="sr-only">{label}</span>
    </span>
  )
}

function personName(person: IssuePerson): string {
  return person.name || person.login
}

function titleText(issue: IssueSummary): string {
  return issue.title || i18nT('appSdk.issues.untitled')
}

function rowLabel(issue: IssueSummary): string {
  return i18nT('appSdk.issues.row_label', {
    provider: issueProviderName(issue.provider),
    key: issue.key,
    title: titleText(issue),
    status: issue.statusLabel || issue.state,
  })
}

function CondensedRow({ issue, onSelect }: { issue: IssueSummary; onSelect?: (issue: IssueSummary) => void }) {
  const href = safeHttpUrl(issue.url)
  const body = (
    <>
      <span className="inline-flex shrink-0 items-center text-muted">
        <ProviderMark provider={issue.provider} size={13} />
      </span>
      <span className="shrink-0 font-mono text-[11px] text-muted">{issue.key}</span>
      <span dir="auto" className="min-w-0 flex-1 truncate text-[13px] text-text">{titleText(issue)}</span>
      {issue.labels.length > 0 && (
        <span className="hidden shrink-0 items-center gap-1 @min-[420px]:inline-flex" data-testid="issue-row-labels">
          {issue.labels.slice(0, 2).map(label => <LabelChip key={label.name} {...label} />)}
        </span>
      )}
      <StatusPill issue={issue} />
      <span className="text-[11px] text-muted"><Updated issue={issue} /></span>
    </>
  )
  const className = 'flex w-full min-w-0 items-center gap-2 rounded-md px-2 py-1.5 text-left hover:bg-bg-hover focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring'
  if (onSelect) {
    return (
      <div className="@container">
        <button type="button" className={`${className} cursor-pointer`} aria-label={rowLabel(issue)} onClick={() => onSelect(issue)}>
          {body}
        </button>
      </div>
    )
  }
  if (href) {
    return (
      <div className="@container">
        <a href={href} target="_blank" rel="noopener noreferrer" className={`${className} no-underline`} aria-label={rowLabel(issue)}>
          {body}
        </a>
      </div>
    )
  }
  return (
    <div className="@container">
      <div className={className} role="group" aria-label={rowLabel(issue)}>{body}</div>
    </div>
  )
}

function ExpandedCard({ issue, onSelect }: { issue: IssueSummary; onSelect?: (issue: IssueSummary) => void }) {
  const href = safeHttpUrl(issue.url)
  const provider = issueProviderName(issue.provider)
  const assignees = issue.assignees
  const linked = issue.linkedChanges.length
  return (
    <article className="flex min-w-0 flex-col gap-2 rounded-lg border border-border bg-card p-3 text-card-fg" aria-label={rowLabel(issue)}>
      <div className="flex min-w-0 items-center gap-2 text-[11px] text-muted">
        <span className="inline-flex shrink-0 items-center gap-1">
          <ProviderMark provider={issue.provider} size={13} />
          <span>{provider}</span>
        </span>
        <span className="shrink-0 font-mono">{issue.key}</span>
        {issue.project && <span dir="auto" className="min-w-0 truncate">{issue.project}</span>}
        <span className="ms-auto shrink-0"><StatusPill issue={issue} /></span>
      </div>
      <h3 className="m-0 min-w-0 text-[14px] font-semibold leading-snug text-text-strong">
        {onSelect ? (
          <button
            type="button"
            dir="auto"
            className="w-full cursor-pointer break-words bg-transparent p-0 text-left font-semibold hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring"
            onClick={() => onSelect(issue)}
          >
            {titleText(issue)}
          </button>
        ) : <span dir="auto" className="break-words">{titleText(issue)}</span>}
      </h3>
      {issue.excerpt && (
        <p dir="auto" className="m-0 line-clamp-3 whitespace-pre-line break-words text-[12px] leading-relaxed text-muted" data-testid="issue-excerpt">
          {issue.excerpt}
        </p>
      )}
      {(issue.priority || issue.labels.length > 0) && (
        <div className="flex flex-wrap items-center gap-1">
          {issue.priority && (
            <span dir="auto" className="inline-block rounded-full border border-border px-1.5 py-0.5 text-[10px] font-medium leading-none text-text">
              {i18nT('appSdk.issues.priority', { priority: issue.priority })}
            </span>
          )}
          {issue.labels.map(label => <LabelChip key={label.name} {...label} />)}
        </div>
      )}
      <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted">
        {assignees.length > 0 ? (
          <span className="inline-flex min-w-0 items-center gap-1" title={i18nT('appSdk.issues.assigned_to', { names: assignees.map(personName).join(', ') })}>
            <Avatar person={assignees[0]} />
            <span dir="auto" className="truncate">{personName(assignees[0])}</span>
            {assignees.length > 1 && <span>{i18nT('appSdk.issues.more_people', { n: fmtNumber(assignees.length - 1) })}</span>}
          </span>
        ) : <span>{i18nT('appSdk.issues.unassigned')}</span>}
        {issue.author && (
          <span className="inline-flex min-w-0 items-center gap-1">
            <span dir="auto" className="truncate">{i18nT('appSdk.issues.opened_by', { name: personName(issue.author) })}</span>
          </span>
        )}
        <Count icon={<MessageSquare size={12} aria-hidden="true" />} value={issue.commentCount} label={i18nT('appSdk.issues.comments', { count: issue.commentCount })} />
        {linked > 0 && (
          <Count icon={<GitPullRequest size={12} aria-hidden="true" />} value={linked} label={i18nT('appSdk.issues.linked_changes', { count: linked })} />
        )}
        <Updated issue={issue} />
        {href && (
          <a href={href} target="_blank" rel="noopener noreferrer" className="ms-auto shrink-0 text-accent hover:underline">
            {i18nT('appSdk.issues.open_in', { provider })}
          </a>
        )}
      </div>
    </article>
  )
}

export default function IssueCard({ issue, view = 'condensed', onSelect }: IssueCardProps) {
  return view === 'expanded'
    ? <ExpandedCard issue={issue} onSelect={onSelect} />
    : <CondensedRow issue={issue} onSelect={onSelect} />
}
