import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  AlertCircle,
  Circle,
  CircleDot,
  CircleSlash,
  ExternalLink,
  GitPullRequest,
  Lock,
  MessageSquare,
  Milestone as MilestoneIcon,
  RefreshCw,
  Users,
} from 'lucide-react'
import { api } from '../api/client'
import type { IssueLabel, IssueSource } from '../types'
import {
  MAX_PULL_REQUEST_SOURCES,
  type PullRequestLink,
} from '../utils/pullRequestLinks'
import GithubLogo from './icons/GithubLogo'
import GitlabLogo from './icons/GitlabLogo'
import { timeAgo } from '../utils/timeAgo'
import MarkdownRenderer from './MarkdownRenderer'
import { pullRequestErrorDetails } from './PullRequestPanel'
import { Btn } from './ui'

import { i18nT } from '../i18n/t'

type IssueTab = 'description' | 'comments' | 'linked'

function age(value: string): string {
  const ms = Date.parse(value)
  return timeAgo(Number.isFinite(ms) ? ms / 1000 : 0)
}

function safeExternalUrl(value: string): string | undefined {
  if (!value) return undefined
  try {
    const url = new URL(value)
    return url.protocol === 'https:' || url.protocol === 'http:' ? url.href : undefined
  } catch {
    return undefined
  }
}

/**
 * Foreground colour for a label chip whose background is the provider-supplied
 * `color`. Providers let users pick any hex, so a fixed foreground is unreadable
 * on roughly half of them; pick black or white by the background's perceived
 * brightness (ITU-R BT.601 luma, the same weighting GitHub uses for its own
 * label chips). Exported for the test that pins the threshold behaviour.
 *
 * `color` arrives as a BARE 6-hex-digit string (no '#'). Anything else — empty,
 * short, non-hex — falls back to the theme's own text colour on a neutral chip,
 * so a malformed value degrades instead of producing an invisible label.
 */
export function labelChipStyle(color: string): { background: string; color: string } {
  if (!/^[0-9a-f]{6}$/i.test(color)) {
    return { background: 'var(--bg-hover)', color: 'var(--muted)' }
  }
  const r = parseInt(color.slice(0, 2), 16)
  const g = parseInt(color.slice(2, 4), 16)
  const b = parseInt(color.slice(4, 6), 16)
  const luma = (0.299 * r + 0.587 * g + 0.114 * b) / 255
  return { background: `#${color}`, color: luma > 0.6 ? '#1c1c1c' : '#ffffff' }
}

/**
 * Catalog keys for the reaction glyphs, flat and indexed inline off `entry.key`
 * at the `i18nT()` call. Reading the key through the row object instead is a
 * shape `scripts/check-i18n-keys.mjs` cannot resolve statically, and an
 * unresolvable key is one the catalog checks silently skip.
 */
const REACTION_LABEL_KEY: Record<string, string> = {
  plus1: 'components.issuePanel.reaction_plus1',
  minus1: 'components.issuePanel.reaction_minus1',
  laugh: 'components.issuePanel.reaction_laugh',
  hooray: 'components.issuePanel.reaction_hooray',
  confused: 'components.issuePanel.reaction_confused',
  heart: 'components.issuePanel.reaction_heart',
  rocket: 'components.issuePanel.reaction_rocket',
  eyes: 'components.issuePanel.reaction_eyes',
}

/** Reaction tallies worth showing, in a stable order. Zero counts are dropped —
 *  an issue with no thumbs-up should not render a "0" next to every reaction
 *  name. Copy lives in `REACTION_LABEL_KEY` above. */
const REACTION_KEYS: ReadonlyArray<{ key: keyof NonNullable<IssueSource['reactions']> }> = [
  { key: 'plus1' },
  { key: 'minus1' },
  { key: 'laugh' },
  { key: 'hooray' },
  { key: 'confused' },
  { key: 'heart' },
  { key: 'rocket' },
  { key: 'eyes' },
]

/** State badge tone + wording. `stateReason` distinguishes the two ways an issue
 *  closes on GitHub, which is the difference between "fixed" and "won't do" —
 *  the single most useful fact about a closed issue. GitLab reports no reason. */
export function issueStateLabel(source: IssueSource): string {
  if (source.state !== 'closed') return i18nT('components.issuePanel.open_state')
  if (source.stateReason === 'not_planned') return i18nT('components.issuePanel.closed_as_not_planned')
  if (source.stateReason === 'completed') return i18nT('components.issuePanel.closed_as_completed')
  return i18nT('components.issuePanel.closed')
}

function stateTone(source: IssueSource): string {
  if (source.state === 'closed') {
    return source.stateReason === 'not_planned' ? 'bg-bg-hover text-muted' : 'bg-aim/15 text-aim'
  }
  return 'bg-ok/15 text-ok'
}

/** Prefilled chat handoff for the whole issue: enough context for the agent to
 *  act without the user re-typing the url, title, and ask. */
export function issueHandoff(source: IssueSource): string {
  const url = safeExternalUrl(source.url)
  const lines = [
    `${source.provider === 'github' ? 'Issue' : 'Issue'} #${source.number} (${source.title}):`,
    '',
    `- State: ${issueStateLabel(source)}`,
    ...(source.author ? [`- Reported by: ${source.author}`] : []),
    ...(source.labels.length ? [`- Labels: ${source.labels.map(l => l.name).join(', ')}`] : []),
    ...(url ? [`- Issue: ${url}`] : []),
  ]
  if (source.description) {
    lines.push('', 'Description:', '', source.description.split('\n').map(line => `> ${line}`).join('\n'))
  }
  lines.push('', 'Investigate this issue and propose a fix.')
  return lines.join('\n')
}

function EmptyTab({ children }: { children: string }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 py-12 text-[13px] text-muted">
      <Circle className="lucide-inline" aria-hidden="true" />
      {children}
    </div>
  )
}

/** Static shimmer placeholder while the provider request is in flight. No
 *  rotating spinner: the panel shows the SHAPE of what is coming. */
function LoadingSkeleton() {
  return (
    <div role="status" aria-label={i18nT('components.issuePanel.loading_issue')} className="flex-1 px-4 py-4 flex flex-col gap-3">
      <div className="h-4 w-24 rounded bg-bg-hover animate-pulse" />
      <div className="h-5 w-3/4 rounded bg-bg-hover animate-pulse" />
      <div className="h-3 w-1/2 rounded bg-bg-hover animate-pulse" />
      <div className="mt-3 h-3 w-full rounded bg-bg-hover animate-pulse" />
      <div className="h-3 w-5/6 rounded bg-bg-hover animate-pulse" />
      <div className="h-3 w-2/3 rounded bg-bg-hover animate-pulse" />
    </div>
  )
}

function CommentCard({
  comment,
  onAddToChat,
}: {
  comment: IssueSource['comments'][number]
  onAddToChat: (text: string) => void
}) {
  const commentUrl = safeExternalUrl(comment.url)
  return (
    <article className="border border-border rounded-lg bg-card overflow-hidden">
      <div className="flex items-center gap-2 px-3 py-2 border-b border-border bg-bg-elevated/30">
        <MessageSquare className="lucide-inline text-muted shrink-0" aria-hidden="true" />
        <span className="text-[12px] font-medium text-text truncate">{comment.author || i18nT('components.issuePanel.unknown_author')}</span>
        <div className="ml-auto flex items-center gap-2 shrink-0">
          <span className="text-[11px] text-muted">{age(comment.createdAt)}</span>
          {commentUrl && (
            <a
              href={commentUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="text-[11px] text-accent hover:underline inline-flex items-center gap-1"
            >
              {i18nT('components.issuePanel.open')} <ExternalLink className="lucide-inline" aria-hidden="true" />
            </a>
          )}
          <Btn
            type="button"
            onClick={() => onAddToChat(
              `Issue comment from ${comment.author || 'a participant'}:\n\n> ${comment.body.replace(/\n/g, '\n> ')}`,
            )}
            className="text-[11px] px-2 py-1 rounded-md border border-border bg-transparent text-muted hover:text-text hover:bg-bg-hover cursor-pointer"
          >
            {i18nT('components.issuePanel.add_to_chat')}
          </Btn>
        </div>
      </div>
      <div className="px-3 py-2 text-[13px] text-text">
        {comment.body
          ? <MarkdownRenderer content={comment.body} />
          : <span className="text-muted">{i18nT('components.issuePanel.no_comment_body_was_returned')}</span>}
      </div>
    </article>
  )
}

function IssueBody({
  source,
  tab,
  onAddToChat,
}: {
  source: IssueSource
  tab: IssueTab
  onAddToChat: (text: string) => void
}) {
  if (tab === 'description') {
    return source.description
      ? <div className="px-4 py-4 text-[13px]"><MarkdownRenderer content={source.description} /></div>
      : <EmptyTab>{i18nT('components.issuePanel.no_description_was_provided')}</EmptyTab>
  }
  if (tab === 'comments') {
    return source.comments.length ? (
      <div className="p-3 flex flex-col gap-3">
        {source.comments.map((comment, index) => (
          <CommentCard key={comment.id || index} comment={comment} onAddToChat={onAddToChat} />
        ))}
      </div>
    ) : <EmptyTab>{i18nT('components.issuePanel.no_comments_were_returned')}</EmptyTab>
  }
  return source.linkedChanges.length ? (
    <div>
      {source.linkedChanges.map((change, index) => {
        const changeUrl = safeExternalUrl(change.url)
        const marker = change.provider === 'github' ? '#' : '!'
        const content = (
          <>
            <GitPullRequest className="lucide-inline text-muted shrink-0 mt-0.5" aria-hidden="true" />
            <div className="min-w-0 flex-1">
              <div className="text-[13px] font-medium text-text truncate">{change.title || i18nT('components.issuePanel.untitled')}</div>
              <div className="flex items-center gap-2 mt-1 text-[11px] text-muted">
                <span className="shrink-0">{marker}{change.number}</span>
                {change.state && <span className="capitalize shrink-0">{change.state.toLowerCase()}</span>}
              </div>
            </div>
            {changeUrl && <ExternalLink className="lucide-inline shrink-0 text-muted" aria-hidden="true" />}
          </>
        )
        const className = 'flex gap-3 px-3 py-3 border-b border-border last:border-b-0 no-underline transition-colors'
        return changeUrl ? (
          <a
            key={`${change.url}-${index}`}
            href={changeUrl}
            target="_blank"
            rel="noopener noreferrer"
            className={`${className} hover:bg-bg-hover`}
          >
            {content}
          </a>
        ) : (
          <div key={`${change.url}-${index}`} className={className}>{content}</div>
        )
      })}
    </div>
  ) : <EmptyTab>{i18nT('components.issuePanel.no_linked_pull_requests_were_returned')}</EmptyTab>
}

function LabelChips({ labels }: { labels: IssueLabel[] }) {
  if (!labels.length) return null
  return (
    <div className="mt-2 flex flex-wrap items-center gap-1">
      {labels.map(label => (
        <span
          key={label.name}
          className="px-1.5 py-0.5 rounded-full text-[10px] font-medium leading-none"
          style={labelChipStyle(label.color)}
          title={label.description || label.name}
        >
          {label.name}
        </span>
      ))}
    </div>
  )
}

/**
 * Issue detail panel. Deliberately simpler than PullRequestPanel: an issue has
 * no diff, no commits, and no CI, so there is nothing to poll — the payload is
 * fetched once per url (`staleTime: Infinity`) and only a user-driven refresh
 * re-requests it. That keeps the provider CLI off any timer.
 */
export default function IssuePanel({
  issues,
  selectedUrl,
  onSelect,
  onReconcile,
  onAddToChat,
}: {
  issues: PullRequestLink[]
  selectedUrl: string
  onSelect: (url: string) => void
  // See PullRequestPanel: the self-normalizing path must not be persisted.
  onReconcile?: (url: string) => void
  onAddToChat?: (text: string) => void
}) {
  const cappedIssues = issues.slice(0, MAX_PULL_REQUEST_SOURCES)
  const selected = cappedIssues.find(issue => issue.url === selectedUrl) || cappedIssues[0]
  const [tab, setTab] = useState<IssueTab>('description')
  // A ref, not state: the flag is consumed inside queryFn and must not itself
  // trigger a render (which would re-run the effect chain around the query).
  const forceRefreshRef = useRef(false)

  useEffect(() => {
    if (selected && selected.url !== selectedUrl) (onReconcile || onSelect)(selected.url)
  }, [selected, selectedUrl, onSelect, onReconcile])

  useEffect(() => { setTab('description') }, [selected?.url])

  const queryKey = useMemo(() => ['issue-source', selected?.url] as const, [selected?.url])
  const query = useQuery<IssueSource>({
    queryKey,
    queryFn: () => {
      const force = forceRefreshRef.current
      forceRefreshRef.current = false
      return api.fetchIssueSource(selected!.url, force)
    },
    enabled: !!selected,
    // Manual refresh ONLY — an issue has no CI or merge state that changes
    // under the user, so a background poll would spend provider calls (and SEL
    // audit entries) for nothing.
    staleTime: Infinity,
    retry: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  })
  const source = query.data
  const queryError = pullRequestErrorDetails(query.error)
  const sourceUrl = safeExternalUrl(source?.url || '')
  const handleRefresh = () => {
    forceRefreshRef.current = true
    void query.refetch()
  }

  const reactionRows = useMemo(() => {
    const reactions = source?.reactions
    if (!reactions) return []
    return REACTION_KEYS
      .map(entry => ({ ...entry, count: Number(reactions[entry.key]) || 0 }))
      .filter(entry => entry.count > 0)
  }, [source?.reactions])

  // 'linked' appears only when the provider actually reported linked changes —
  // an empty tab that can never fill is noise, not information.
  const tabs: Array<{ id: IssueTab; label: string; count?: number }> = source ? [
    { id: 'description', label: i18nT('components.issuePanel.description') },
    { id: 'comments', label: i18nT('components.issuePanel.comments'), count: source.comments.length || source.commentCount },
    ...(source.linkedChanges.length
      ? [{ id: 'linked' as const, label: i18nT('components.issuePanel.linked'), count: source.linkedChanges.length }]
      : []),
  ] : []
  // The selected tab can vanish (a refresh that drops linkedChanges), so the
  // rendered tab always falls back to one that exists.
  const effectiveTab: IssueTab = tab === 'linked' && !source?.linkedChanges.length ? 'description' : tab

  return (
    <div className="flex flex-col h-full min-h-0">
      {cappedIssues.length > 1 && (
        <div
          role="tablist"
          aria-label={i18nT('components.issuePanel.issues')}
          className="shrink-0 border-b border-border px-2 py-2 flex items-center gap-1 overflow-x-auto"
        >
          {cappedIssues.map(item => (
            <Btn
              key={item.url}
              type="button"
              role="tab"
              aria-selected={item.url === selected?.url}
              onClick={() => onSelect(item.url)}
              className={`shrink-0 flex items-center gap-1.5 px-2.5 py-1.5 rounded-md border-none cursor-pointer text-[12px] transition-colors ${item.url === selected?.url ? 'bg-bg-hover text-text' : 'bg-transparent text-muted hover:text-text hover:bg-bg-hover/60'}`}
              title={item.url}
            >
              {item.provider === 'github'
                ? <GithubLogo size={13} className="shrink-0" />
                : <GitlabLogo size={13} className="shrink-0" />}
              <span>#{item.number}</span>
            </Btn>
          ))}
        </div>
      )}

      {query.isLoading && <LoadingSkeleton />}
      {query.error && (
        <div className="flex-1 flex items-center justify-center px-6">
          <div role="alert" className="max-w-md flex flex-col items-center">
            <AlertCircle
              className={`lucide-inline mb-2 ${queryError.loginCommand ? 'text-warn' : 'text-danger'}`}
              aria-hidden="true"
            />
            <div className="text-[13px] font-medium text-text">
              {queryError.loginCommand
                ? i18nT('components.issuePanel.cli_login_required', {
                    provider: queryError.loginCommand === 'gh auth login' ? 'GitHub' : 'GitLab',
                  })
                : i18nT('components.issuePanel.could_not_load_this_issue')}
            </div>
            {queryError.loginCommand ? (
              <>
                <div className="text-[12px] text-muted mt-1 text-center">
                  {i18nT('components.issuePanel.kiro_crew_uses_your_local_provider_cli_to_load_i')}
                </div>
                <code className="inline-block mt-2 px-2 py-1 rounded bg-bg-hover text-[12px] text-text">
                  {queryError.loginCommand}
                </code>
              </>
            ) : (
              <div className="mt-2 w-full max-h-64 overflow-y-auto rounded-md bg-bg-hover/50 border border-border px-3 py-2 text-left text-[12px] text-muted whitespace-pre-wrap break-words font-mono leading-relaxed">
                {queryError.message}
              </div>
            )}
            <Btn
              type="button"
              onClick={handleRefresh}
              className="mt-3 inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-md border border-border bg-transparent text-[12px] text-muted hover:text-text hover:bg-bg-hover cursor-pointer"
            >
              <RefreshCw className="lucide-inline" aria-hidden="true" />{i18nT('components.issuePanel.retry')}
            </Btn>
          </div>
        </div>
      )}

      {source && (
        <>
          <div className="shrink-0 px-4 py-3 border-b border-border">
            <div className="flex items-center gap-2 text-[11px] text-muted">
              <span className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded font-medium ${stateTone(source)}`}>
                {source.state === 'closed'
                  ? <CircleSlash className="lucide-inline" aria-hidden="true" />
                  : <CircleDot className="lucide-inline" aria-hidden="true" />}
                {issueStateLabel(source)}
              </span>
              <span className="inline-flex items-center gap-1 shrink-0">
                {source.provider === 'github'
                  ? <GithubLogo size={12} className="shrink-0" />
                  : <GitlabLogo size={12} className="shrink-0" />}
                {/* Brand names are cased explicitly: a CSS `capitalize` on the
                    raw provider value renders "Github"/"Gitlab", which is wrong
                    for both marks. */}
                <span>{source.provider === 'github' ? 'GitHub' : 'GitLab'}</span>
              </span>
              {source.locked && (
                <span className="inline-flex items-center gap-1 shrink-0" title={i18nT('components.issuePanel.this_issue_is_locked')}>
                  <Lock className="lucide-inline" aria-hidden="true" />{i18nT('components.issuePanel.locked')}
                </span>
              )}
              <Btn
                type="button"
                onClick={handleRefresh}
                disabled={query.isFetching}
                className="ml-auto p-1 rounded border-none bg-transparent text-muted hover:text-text hover:bg-bg-hover cursor-pointer disabled:opacity-60 disabled:cursor-default"
                aria-label={query.isFetching ? i18nT('components.issuePanel.refreshing_issue') : i18nT('components.issuePanel.refresh_issue')}
                title={query.isFetching ? i18nT('components.issuePanel.refreshing_issue') : i18nT('components.issuePanel.refresh_issue')}
              >
                <RefreshCw className="lucide-inline" aria-hidden="true" />
              </Btn>
              {sourceUrl && (
                <a
                  href={sourceUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="p-1 rounded text-muted hover:text-text hover:bg-bg-hover"
                  aria-label={i18nT('components.issuePanel.open_issue')}
                  title={i18nT('components.issuePanel.open_issue')}
                >
                  <ExternalLink className="lucide-inline" aria-hidden="true" />
                </a>
              )}
            </div>
            <div className="mt-2 text-[15px] font-semibold text-text-strong leading-snug">
              {source.title} <span className="font-normal text-muted">#{source.number}</span>
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-2 text-[11px] text-muted">
              {source.author && <span>{source.author}</span>}
              {source.createdAt && <span>{i18nT('components.issuePanel.opened_time', { time: age(source.createdAt) })}</span>}
              {source.updatedAt && <span>{i18nT('components.issuePanel.updated_time', { time: age(source.updatedAt) })}</span>}
              {source.assignees.length > 0 && (
                <span className="inline-flex items-center gap-1" title={i18nT('components.issuePanel.assigned_to', { name: source.assignees.join(', ') })}>
                  <Users className="lucide-inline" aria-hidden="true" />
                  {source.assignees.join(', ')}
                </span>
              )}
              {source.milestone && (
                <span
                  className="inline-flex items-center gap-1"
                  title={source.milestone.dueOn ? i18nT('components.issuePanel.due', { time: source.milestone.dueOn }) : undefined}
                >
                  <MilestoneIcon className="lucide-inline" aria-hidden="true" />
                  {source.milestone.title}
                </span>
              )}
              {reactionRows.length > 0 && (
                <span className="inline-flex items-center gap-2">
                  {reactionRows.map(entry => {
                    // Resolved here, not in the `reactionRows` memo above: that
                    // memo's deps name only `source.reactions`, so a label
                    // resolved inside it would not re-resolve on a language
                    // switch under any finer-grained strategy than the current
                    // language-keyed remount of <App>.
                    const label = i18nT(REACTION_LABEL_KEY[entry.key])
                    return <span key={entry.key} title={label}>{label} {entry.count}</span>
                  })}
                </span>
              )}
            </div>
            <LabelChips labels={source.labels} />
            {onAddToChat && (
              <div className="mt-2 flex flex-wrap items-center gap-2">
                <Btn
                  type="button"
                  onClick={() => onAddToChat(issueHandoff(source))}
                  className="inline-flex items-center gap-1.5 px-2 py-1 rounded-md border border-border bg-transparent text-[11px] text-muted hover:text-text hover:bg-bg-hover cursor-pointer"
                  title={i18nT('components.issuePanel.put_this_issue_s_details_into_the_chat_composer')}
                >
                  {i18nT('components.issuePanel.add_to_chat')}
                </Btn>
              </div>
            )}
          </div>

          {source.partialSections && source.partialSections.length > 0 && (
            <div role="status" className="shrink-0 flex items-start gap-2 px-4 py-2 border-b border-border bg-warn/10 text-[11px] text-muted">
              <AlertCircle className="lucide-inline shrink-0 mt-0.5 text-warn" aria-hidden="true" />
              <span>
                {i18nT('components.issuePanel.provider_results_may_be_partial_for_sections_ope', {
                  sections: source.partialSections.join(', '),
                })}
              </span>
            </div>
          )}

          <div
            role="tablist"
            aria-label={i18nT('components.issuePanel.issue_sections')}
            className="shrink-0 border-b border-border px-2 py-2 flex items-center gap-1 overflow-x-auto"
          >
            {tabs.map(item => (
              <Btn
                key={item.id}
                type="button"
                role="tab"
                id={`issue-tab-${item.id}`}
                aria-selected={effectiveTab === item.id}
                aria-controls="issue-tabpanel"
                onClick={() => setTab(item.id)}
                className={`shrink-0 flex items-center gap-1.5 px-2 py-1.5 rounded-md border-none cursor-pointer text-[11px] transition-colors ${effectiveTab === item.id ? 'bg-bg-hover text-text' : 'bg-transparent text-muted hover:text-text'}`}
              >
                {item.label}
                {item.count !== undefined && <span className="text-muted">{item.count}</span>}
              </Btn>
            ))}
          </div>

          <div
            id="issue-tabpanel"
            role="tabpanel"
            aria-labelledby={`issue-tab-${effectiveTab}`}
            className="flex-1 min-h-0 overflow-y-auto"
          >
            <IssueBody source={source} tab={effectiveTab} onAddToChat={onAddToChat || (() => {})} />
          </div>
        </>
      )}
    </div>
  )
}
