import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertCircle,
  ArrowRight,
  Check,
  ChevronDown,
  ChevronRight,
  Circle,
  ExternalLink,
  GitCommitHorizontal,
  Loader,
  MessageSquare,
  RefreshCw,
  SkipForward,
  XCircle,
} from 'lucide-react'
import { api } from '../api/client'
import type {
  PullRequestCheck,
  PullRequestComment,
  PullRequestFile,
  PullRequestSource,
} from '../types'
import {
  MAX_PULL_REQUEST_SOURCES,
  type PullRequestLink,
} from '../utils/pullRequestLinks'
import { parseUnifiedDiff } from '../utils/parseUnifiedDiff'
import hljs from '../utils/hljs'
import DOMPurify from 'dompurify'
import { DIFF_BG, DIFF_FG } from '../utils/diffUtils'
import GithubLogo from './icons/GithubLogo'
import GitlabLogo from './icons/GitlabLogo'
import { timeAgo } from '../utils/timeAgo'
import MarkdownRenderer from './MarkdownRenderer'
import { Btn } from './ui'


const CHECK_POLL_BASE_MS = 10_000
const CHECK_POLL_MAX_MS = 60_000
export const CHECK_POLL_MAX_FAILURES = 3

export function pullRequestCheckPollDelay(
  checks: PullRequestCheck[] | undefined,
  failureCount: number,
): number | false {
  if (!checks?.some(check => check.bucket === 'pending')) return false
  if (failureCount >= CHECK_POLL_MAX_FAILURES) return false
  return Math.min(CHECK_POLL_BASE_MS * (2 ** failureCount), CHECK_POLL_MAX_MS)
}
type SourceTab = 'changes' | 'description' | 'commits' | 'checks' | 'reviews'

function age(value: string): string {
  const ms = Date.parse(value)
  return timeAgo(Number.isFinite(ms) ? ms / 1000 : 0)
}

export function pullRequestErrorDetails(error: unknown): {
  message: string
  loginCommand: 'gh auth login' | 'glab auth login' | ''
} {
  let message = error instanceof Error ? error.message : String(error || '')
  try {
    const payload = JSON.parse(message) as { error?: unknown }
    if (typeof payload.error === 'string') message = payload.error
  } catch {
    // Provider and network errors may already be plain text.
  }
  const authenticationFailure = /\b(?:not logged in(?:to)?|unauthenticated|authentication (?:failed|required)|requires authentication)\b/i.test(message)
  const loginCommand = authenticationFailure && /(?:`|\b)gh auth login(?:`|\b)/i.test(message)
    ? 'gh auth login'
    : authenticationFailure && /(?:`|\b)glab auth login(?:`|\b)/i.test(message)
      ? 'glab auth login'
      : ''
  return { message, loginCommand }
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

function stateTone(source: PullRequestSource): string {
  const state = source.state.toLowerCase()
  if (source.mergedAt || state === 'merged') return 'bg-aim/15 text-aim'
  if (state === 'open' || state === 'opened') return 'bg-ok/15 text-ok'
  return 'bg-bg-hover text-muted'
}

function stateLabel(source: PullRequestSource): string {
  if (source.draft) return 'Draft'
  if (source.mergedAt || source.state.toLowerCase() === 'merged') return 'Merged'
  const state = source.state || 'Open'
  return state.charAt(0).toUpperCase() + state.slice(1).toLowerCase()
}

function diffLanguage(path: string): string | null {
  const ext = path.split('.').pop()?.toLowerCase() || ''
  return ext && hljs.getLanguage(ext) ? ext : null
}

/** Defer heavy subtree mounting until just after the drawer's slide-in
 * animation (120ms), so opening the panel animates with lightweight file
 * headers instead of stuttering on thousands of highlighted diff rows. */
function useDeferredMount(delayMs = 140): boolean {
  const [ready, setReady] = useState(false)
  useEffect(() => {
    const id = window.setTimeout(() => setReady(true), delayMs)
    return () => window.clearTimeout(id)
  }, [delayMs])
  return ready
}

function DiffView({ patch, path }: { patch: string; path: string }) {
  const ready = useDeferredMount()
  const rows = useMemo(() => parseUnifiedDiff(patch), [patch])
  const language = useMemo(() => diffLanguage(path), [path])
  // Per-line highlighting keyed by file extension. Lines are highlighted
  // independently (multi-line constructs may reset), which matches the
  // fidelity GitHub's own diff view accepts. hljs escapes the input, so
  // its HTML output is safe to inject.
  const highlighted = useMemo(() => {
    if (!language || !ready) return null
    return rows.map(row =>
      row.kind === 'hunk-gap' ? '' : DOMPurify.sanitize(hljs.highlight(row.text, { language, ignoreIllegals: true }).value),
    )
  }, [rows, language, ready])
  if (!ready) return <div className="px-3 py-3 text-[11px] text-muted">Loading diff…</div>
  return (
    <div className="min-w-max text-[11px] leading-5 font-mono">
      {rows.map((row, index) => {
        if (row.kind === 'hunk-gap') {
          return (
            <div key={index} className="flex items-center gap-2 px-3 py-1 bg-bg-elevated/60 text-muted select-none">
              {row.hiddenCount > 0 ? `${row.hiddenCount} unmodified ${row.hiddenCount === 1 ? 'line' : 'lines'}` : <span className="w-full border-t border-border" />}
            </div>
          )
        }
        const tone = row.kind === 'add' ? DIFF_BG.add : row.kind === 'del' ? DIFF_BG.del : ''
        const marker = row.kind === 'add' ? '+' : row.kind === 'del' ? '-' : ' '
        const markerTone = row.kind === 'add' ? DIFF_FG.add : row.kind === 'del' ? DIFF_FG.del : 'text-muted/40'
        const html = highlighted?.[index]
        return (
          <div key={index} className={`flex min-w-fit ${tone}`}>
            <span className="w-10 shrink-0 px-1 text-right text-muted/50 select-none border-r border-border/30">{row.oldLine ?? ''}</span>
            <span className="w-10 shrink-0 px-1 text-right text-muted/50 select-none border-r border-border/30">{row.newLine ?? ''}</span>
            <span className={`w-4 shrink-0 text-center select-none ${markerTone}`}>{marker}</span>
            {html !== undefined && html !== '' ? (
              <span className="hljs flex-1 whitespace-pre px-2 !bg-transparent" dangerouslySetInnerHTML={{ __html: html }} />
            ) : (
              <span className="flex-1 whitespace-pre px-2 text-text">{row.text}</span>
            )}
          </div>
        )
      })}
    </div>
  )
}

function ChangeRow({ file }: { file: PullRequestFile }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="border-b border-border last:border-b-0">
      <Btn
        type="button"
        onClick={() => setOpen(value => !value)}
        className="w-full flex items-center gap-2 px-3 py-2.5 bg-transparent border-none text-left cursor-pointer hover:bg-bg-hover transition-colors"
        aria-expanded={open}
      >
        {open ? <ChevronDown className="lucide-inline shrink-0 text-muted" /> : <ChevronRight className="lucide-inline shrink-0 text-muted" />}
        <span className="text-[13px] text-text truncate min-w-0 flex-1">{file.path}</span>
        <span className="text-[11px] text-muted capitalize shrink-0">{file.status}</span>
        <span className="text-[11px] shrink-0"><span className="text-ok">+{file.additions}</span> <span className="text-danger">-{file.deletions}</span></span>
      </Btn>
      {open && (
        <div className="border-t border-border overflow-x-auto">
          {file.patch ? (
            <DiffView patch={file.patch} path={file.path} />
          ) : (
            <div className="px-3 py-4 text-[12px] text-muted">The provider did not return a patch for this file.</div>
          )}
        </div>
      )}
    </div>
  )
}

const CHECK_META = {
  failed: { icon: XCircle, color: 'text-danger', label: 'Failed' },
  pending: { icon: Loader, color: 'text-warn', label: 'In progress' },
  passed: { icon: Check, color: 'text-ok', label: 'Passed' },
  skipped: { icon: SkipForward, color: 'text-muted', label: 'Skipped' },
} as const

function CheckRow({ check, source, onAddToChat }: { check: PullRequestCheck; source: PullRequestSource; onAddToChat: (text: string) => void }) {
  const meta = CHECK_META[check.bucket]
  const Icon = meta.icon
  const checkUrl = safeExternalUrl(check.url)
  const sourceUrl = safeExternalUrl(source.url)
  const handoff = () => {
    const label = source.provider === 'github' ? `PR #${source.number}` : `MR !${source.number}`
    const lines = [
      `Failing CI check on ${label} (${source.title}):`,
      '',
      `- Check: ${check.name}${check.workflow ? ` (${check.workflow})` : ''}`,
      `- Status: ${check.conclusion || check.status || meta.label}`,
    ]
    if (checkUrl) lines.push(`- Details: ${checkUrl}`)
    if (sourceUrl) lines.push(`- Pull request: ${sourceUrl}`)
    lines.push('', 'Investigate why this check is failing and propose a fix.')
    onAddToChat(lines.join('\n'))
  }
  const details = (
    <>
      <Icon className={`lucide-inline shrink-0 ${meta.color} ${check.bucket === 'pending' ? 'animate-spin' : ''}`} />
      <div className="min-w-0 flex-1">
        <div className="text-[13px] text-text truncate">{check.name}</div>
        {check.workflow && <div className="text-[11px] text-muted truncate mt-0.5">{check.workflow}</div>}
      </div>
      <span className={`text-[11px] shrink-0 ${meta.color}`}>{check.conclusion || check.status || meta.label}</span>
      {checkUrl && <ExternalLink className="lucide-inline shrink-0 text-muted" aria-hidden="true" />}
    </>
  )
  return (
    <div className="flex items-center border-b border-border last:border-b-0">
      {checkUrl ? (
        <a
          href={checkUrl}
          target="_blank"
          rel="noopener noreferrer"
          className="flex min-w-0 flex-1 items-center gap-2.5 px-3 py-2.5 no-underline hover:bg-bg-hover transition-colors"
          aria-label={`Open ${check.name} check details`}
        >
          {details}
        </a>
      ) : (
        <div className="flex min-w-0 flex-1 items-center gap-2.5 px-3 py-2.5">{details}</div>
      )}
      {check.bucket === 'failed' && (
        <Btn
          type="button"
          onClick={handoff}
          className="text-[11px] shrink-0 mr-3 px-2 py-1 rounded-md border border-border bg-transparent text-muted hover:text-text hover:bg-bg-hover cursor-pointer"
        >
          Add to chat
        </Btn>
      )}
    </div>
  )
}

function CommentCard({ comment, url, onAddToChat }: { comment: PullRequestComment; url: string; onAddToChat: (text: string) => void }) {
  const location = comment.path ? `${comment.path}${comment.line ? `:${comment.line}` : ''}` : ''
  const commentUrl = safeExternalUrl(comment.url)
  const [expanded, setExpanded] = useState(true)
  const queryClient = useQueryClient()
  const resolveMutation = useMutation({
    mutationFn: () => api.resolvePullRequestThread(url, comment.threadId || ''),
    onSuccess: () => { void queryClient.invalidateQueries({ queryKey: ['pull-request-source'] }) },
  })
  const canResolve = !!comment.resolvable && !comment.resolved && !!comment.threadId
  return (
    <article className="border border-border rounded-lg bg-card overflow-hidden">
      <div className="flex items-center gap-2 px-3 py-2 border-b border-border bg-bg-elevated/30">
        <Btn
          type="button"
          onClick={() => setExpanded(value => !value)}
          className="shrink-0 p-0.5 rounded border-none bg-transparent text-muted hover:text-text hover:bg-bg-hover cursor-pointer"
          aria-expanded={expanded}
          aria-label={expanded ? 'Collapse comment' : 'Expand comment'}
        >
          {expanded ? <ChevronDown className="lucide-inline" /> : <ChevronRight className="lucide-inline" />}
        </Btn>
        <MessageSquare className="lucide-inline text-muted shrink-0" />
        <span className="text-[12px] font-medium text-text truncate">{comment.author || 'Unknown reviewer'}</span>
        {comment.state && <span className="text-[10px] px-1.5 py-0.5 rounded bg-bg-hover text-muted capitalize shrink-0">{comment.state.toLowerCase()}</span>}
        <div className="ml-auto flex items-center gap-2 shrink-0">
          <span className="text-[11px] text-muted">{age(comment.createdAt)}</span>
          {commentUrl && (
            <a href={commentUrl} target="_blank" rel="noopener noreferrer" className="text-[11px] text-accent hover:underline inline-flex items-center gap-1">
              Open <ExternalLink className="lucide-inline" />
            </a>
          )}
          {resolveMutation.error && (
            <span className="text-[11px] text-danger">Could not resolve</span>
          )}
          {canResolve && (
            <Btn
              type="button"
              onClick={() => resolveMutation.mutate()}
              disabled={resolveMutation.isPending}
              className="text-[11px] px-2 py-1 rounded-md border border-border bg-transparent text-muted hover:text-text hover:bg-bg-hover cursor-pointer inline-flex items-center gap-1 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <Check className="lucide-inline" /> Resolve
            </Btn>
          )}
          {comment.resolved && (
            <span className="text-[11px] text-muted inline-flex items-center gap-1"><Check className="lucide-inline text-ok" /> Resolved</span>
          )}
          <Btn
            type="button"
            onClick={() => onAddToChat(`PR comment from ${comment.author || 'a reviewer'}${location ? ` on ${location}` : ''}:\n\n> ${comment.body.replace(/\n/g, '\n> ')}`)}
            className="text-[11px] px-2 py-1 rounded-md border border-border bg-transparent text-muted hover:text-text hover:bg-bg-hover cursor-pointer"
          >
            Add to chat
          </Btn>
        </div>
      </div>
      {expanded && (
        <>
          {location && <div className="px-3 pt-2 text-[11px] text-muted truncate">{location}</div>}
          <div className="px-3 py-2 text-[13px] text-text">
            {comment.body ? <MarkdownRenderer content={comment.body} /> : <span className="text-muted">No written comment.</span>}
          </div>
        </>
      )}
    </article>
  )
}

function EmptyTab({ children }: { children: string }) {
  return <div className="flex flex-col items-center justify-center gap-2 py-12 text-[13px] text-muted"><Circle className="lucide-inline" />{children}</div>
}

function PullRequestBody({ source, tab, onAddToChat }: { source: PullRequestSource; tab: SourceTab; onAddToChat: (text: string) => void }) {
  if (tab === 'description') {
    return source.description
      ? <div className="px-4 py-4 text-[13px]"><MarkdownRenderer content={source.description} /></div>
      : <EmptyTab>No description was provided.</EmptyTab>
  }
  if (tab === 'changes') {
    if (!source.files.length) return <EmptyTab>No changed files were returned.</EmptyTab>
    const totalAdds = source.files.reduce((sum, file) => sum + file.additions, 0)
    const totalDels = source.files.reduce((sum, file) => sum + file.deletions, 0)
    return (
      <div>
        <div className="sticky top-0 z-[1] flex items-center gap-2 px-3 py-2 border-b border-border bg-bg text-[12px]">
          <span className="font-medium text-text">{source.files.length} {source.files.length === 1 ? 'File' : 'Files'} Changed</span>
          <span className="text-ok">+{totalAdds}</span>
          <span className="text-danger">-{totalDels}</span>
        </div>
        {source.files.map(file => <ChangeRow key={file.path} file={file} />)}
      </div>
    )
  }
  if (tab === 'commits') {
    return source.commits.length ? (
      <div>
        {source.commits.map(commit => {
          const commitUrl = safeExternalUrl(commit.url)
          const content = (
            <>
              <GitCommitHorizontal className="lucide-inline text-muted shrink-0 mt-0.5" />
              <div className="min-w-0 flex-1">
                <div className="text-[13px] font-medium text-text">{commit.title || 'Untitled commit'}</div>
                <div className="flex items-center gap-2 mt-1 text-[11px] text-muted">
                  {commit.author && <span className="truncate">{commit.author}</span>}
                  {commit.date && <span className="shrink-0">{age(commit.date)}</span>}
                  {commit.sha && <code className="shrink-0 bg-bg-hover rounded px-1 py-0.5">{commit.sha.slice(0, 7)}</code>}
                </div>
              </div>
            </>
          )
          const className = "flex gap-3 px-3 py-3 border-b border-border last:border-b-0 no-underline transition-colors"
          return commitUrl ? (
            <a key={commit.sha} href={commitUrl} target="_blank" rel="noopener noreferrer" className={`${className} hover:bg-bg-hover`}>
              {content}
            </a>
          ) : (
            <div key={commit.sha} className={className}>{content}</div>
          )
        })}
      </div>
    ) : <EmptyTab>No commits were returned.</EmptyTab>
  }
  if (tab === 'checks') {
    if (!source.checks.length) return <EmptyTab>No CI checks were returned.</EmptyTab>
    const groups = (['failed', 'pending', 'passed', 'skipped'] as const)
      .map(bucket => ({ bucket, rows: source.checks.filter(check => check.bucket === bucket) }))
      .filter(group => group.rows.length)
    return (
      <div className="py-1">
        {groups.map(group => (
          <section key={group.bucket}>
            <div className="px-3 pt-3 pb-1.5 text-[11px] font-semibold text-muted uppercase tracking-wide">
              {CHECK_META[group.bucket].label} {group.rows.length}
            </div>
            {group.rows.map((check, index) => <CheckRow key={`${check.name}-${index}`} check={check} source={source} onAddToChat={onAddToChat} />)}
          </section>
        ))}
      </div>
    )
  }
  return source.comments.length ? (
    <div className="p-3 flex flex-col gap-3">
      {source.comments.map((comment, index) => <CommentCard key={comment.id || index} comment={comment} url={source.url} onAddToChat={onAddToChat} />)}
    </div>
  ) : <EmptyTab>No PR comments or reviews were returned.</EmptyTab>
}

export default function PullRequestPanel({
  sources,
  selectedUrl,
  onSelect,
  onAddToChat,
}: {
  sources: PullRequestLink[]
  selectedUrl: string
  onSelect: (url: string) => void
  onAddToChat: (text: string) => void
}) {
  const cappedSources = sources.slice(0, MAX_PULL_REQUEST_SOURCES)
  const selected = cappedSources.find(source => source.url === selectedUrl) || cappedSources[0]
  const [tab, setTab] = useState<SourceTab>('changes')
  const [checkPollState, setCheckPollState] = useState({ url: '', failures: 0 })
  const checkPollStateRef = useRef({ url: '', failures: 0 })
  const forceRefreshRef = useRef(false)
  const queryClient = useQueryClient()

  useEffect(() => {
    if (selected && selected.url !== selectedUrl) onSelect(selected.url)
  }, [selected, selectedUrl, onSelect])

  useEffect(() => {
    setTab('changes')
  }, [selected?.url])

  const queryKey = useMemo(
    () => ['pull-request-source', selected?.url] as const,
    [selected?.url],
  )
  const query = useQuery<PullRequestSource>({
    queryKey,
    queryFn: () => {
      const force = forceRefreshRef.current
      forceRefreshRef.current = false
      return api.pullRequestSource(selected!.url, force)
    },
    enabled: !!selected,
    staleTime: Infinity,
    retry: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  })
  const source = query.data
  const queryError = pullRequestErrorDetails(query.error)
  const sourceUrl = safeExternalUrl(source?.url || '')
  const sourceHasPendingChecks = Boolean(
    source?.checks.some(check => check.bucket === 'pending'),
  )
  const checksQueryKey = useMemo(
    () => ['pull-request-checks', selected?.url] as const,
    [selected?.url],
  )
  const checksQuery = useQuery<{ checks: PullRequestCheck[] }>({
    queryKey: checksQueryKey,
    queryFn: async () => {
      const url = selected!.url
      try {
        const result = await api.pullRequestChecks(url)
        const nextState = { url, failures: 0 }
        checkPollStateRef.current = nextState
        setCheckPollState(nextState)
        return result
      } catch (error) {
        const previousFailures = checkPollStateRef.current.url === url
          ? checkPollStateRef.current.failures
          : 0
        const nextState = {
          url,
          failures: Math.min(previousFailures + 1, CHECK_POLL_MAX_FAILURES),
        }
        checkPollStateRef.current = nextState
        setCheckPollState(nextState)
        throw error
      }
    },
    enabled: Boolean(selected && sourceHasPendingChecks && !query.isFetching),
    retry: false,
    staleTime: 0,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    refetchInterval: currentQuery => pullRequestCheckPollDelay(
      currentQuery.state.data?.checks || source?.checks,
      checkPollStateRef.current.url === selected?.url
        ? checkPollStateRef.current.failures
        : 0,
    ),
  })

  useEffect(() => {
    const checks = checksQuery.data?.checks
    if (!checks || checksQuery.dataUpdatedAt < query.dataUpdatedAt) return
    queryClient.setQueryData<PullRequestSource>(queryKey, current =>
      current ? { ...current, checks } : current,
    )
  }, [
    checksQuery.data,
    checksQuery.dataUpdatedAt,
    query.dataUpdatedAt,
    queryClient,
    queryKey,
  ])

  const checkPollFailures = checkPollState.url === selected?.url
    ? checkPollState.failures
    : 0
  const checksPollingPaused = sourceHasPendingChecks
    && checkPollFailures >= CHECK_POLL_MAX_FAILURES
  const handleRefresh = () => {
    forceRefreshRef.current = true
    void query.refetch().then(result => {
      if (
        selected
        && result.data?.checks.some(check => check.bucket === 'pending')
        && checkPollFailures >= CHECK_POLL_MAX_FAILURES
      ) {
        const nextState = { url: selected.url, failures: 0 }
        checkPollStateRef.current = nextState
        setCheckPollState(nextState)
        void queryClient.resetQueries({ queryKey: checksQueryKey, exact: true })
      }
    })
  }
  const checkCounts = useMemo(() => {
    const checks = source?.checks || []
    return {
      complete: checks.filter(check => check.bucket === 'passed' || check.bucket === 'skipped').length,
      failed: checks.filter(check => check.bucket === 'failed').length,
      pending: checks.filter(check => check.bucket === 'pending').length,
      total: checks.length,
    }
  }, [source?.checks])
  const checksUnavailable = checksPollingPaused
  const checksRunning = checkCounts.pending > 0 && !checksUnavailable
  const allChecksPassed = checkCounts.total > 0
    && checkCounts.failed === 0
    && checkCounts.pending === 0
    && checkCounts.complete === checkCounts.total
  const showAllChecksPassed = allChecksPassed && !query.isFetching

  const tabs: Array<{ id: SourceTab; label: string; count?: number; tone?: string }> = source ? [
    { id: 'changes', label: 'Changes', count: source.files.length },
    { id: 'description', label: 'Description' },
    { id: 'commits', label: 'Commits', count: source.commits.length },
    {
      id: 'checks',
      label: checksUnavailable
        ? 'Checks unavailable'
        : checksRunning
          ? 'Checks running'
          : showAllChecksPassed
            ? 'All checks passed'
            : 'Checks',
      count: checkCounts.total,
      tone: checkCounts.failed
        ? 'text-danger'
        : checksUnavailable || checksRunning
          ? 'text-warn'
          : showAllChecksPassed
            ? 'text-ok'
            : '',
    },
    { id: 'reviews', label: 'Reviews', count: source.comments.length },
  ] : []

  return (
    <div className="flex flex-col h-full min-h-0">
      <div role="tablist" aria-label="Pull requests" className="shrink-0 border-b border-border px-2 py-2 flex items-center gap-1 overflow-x-auto">
        {cappedSources.map(item => (
          <Btn
            key={item.url}
            type="button"
            role="tab"
            aria-selected={item.url === selected?.url}
            onClick={() => onSelect(item.url)}
            className={`shrink-0 flex items-center gap-1.5 px-2.5 py-1.5 rounded-md border-none cursor-pointer text-[12px] transition-colors ${item.url === selected?.url ? 'bg-bg-hover text-text' : 'bg-transparent text-muted hover:text-text hover:bg-bg-hover/60'}`}
            title={item.url}
          >
            {item.provider === 'github' ? <GithubLogo size={13} className="shrink-0" /> : <GitlabLogo size={13} className="shrink-0" />}
            <span>{item.provider === 'github' ? 'PR' : 'MR'} {item.provider === 'github' ? '#' : '!'}{item.number}</span>
          </Btn>
        ))}
      </div>

      {query.isLoading && <div className="flex-1 flex items-center justify-center gap-2 text-[13px] text-muted"><Loader className="lucide-inline animate-spin" />Loading source provider…</div>}
      {query.error && (
        <div className="flex-1 flex items-center justify-center px-6">
          <div role="alert" className="max-w-md flex flex-col items-center">
            <AlertCircle className={`lucide-inline mb-2 ${queryError.loginCommand ? 'text-warn' : 'text-danger'}`} />
            <div className="text-[13px] font-medium text-text">
              {queryError.loginCommand
                ? `${queryError.loginCommand === 'gh auth login' ? 'GitHub' : 'GitLab'} CLI login required`
                : 'Could not load this pull request'}
            </div>
            {queryError.loginCommand ? (
              <>
                <div className="text-[12px] text-muted mt-1 text-center">Kiro Crew uses your local provider CLI to load pull requests. Run this command in your terminal, then retry.</div>
                <code className="inline-block mt-2 px-2 py-1 rounded bg-bg-hover text-[12px] text-text">{queryError.loginCommand}</code>
              </>
            ) : (
              <div className="mt-2 w-full max-h-64 overflow-y-auto rounded-md bg-bg-hover/50 border border-border px-3 py-2 text-left text-[12px] text-muted whitespace-pre-wrap break-words font-mono leading-relaxed">{queryError.message}</div>
            )}
            <Btn type="button" onClick={handleRefresh} className="mt-3 inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-md border border-border bg-transparent text-[12px] text-muted hover:text-text hover:bg-bg-hover cursor-pointer"><RefreshCw className="lucide-inline" />Retry</Btn>
          </div>
        </div>
      )}

      {source && (
        <>
          <div className="shrink-0 px-4 py-3 border-b border-border">
            <div className="flex items-center gap-2 text-[11px] text-muted">
              <span className={`px-1.5 py-0.5 rounded font-medium ${stateTone(source)}`}>{stateLabel(source)}</span>
              <span className="capitalize">{source.provider}</span>
              {source.headBranch && source.baseBranch && (
                <span className="min-w-0 flex items-center gap-1 truncate"><span className="truncate">{source.headBranch}</span><ArrowRight className="lucide-inline shrink-0" /><span className="truncate">{source.baseBranch}</span></span>
              )}
              <Btn
                type="button"
                onClick={handleRefresh}
                disabled={query.isFetching}
                className="ml-auto p-1 rounded border-none bg-transparent text-muted hover:text-text hover:bg-bg-hover cursor-pointer disabled:opacity-60 disabled:cursor-default"
                aria-label={query.isFetching ? 'Refreshing pull request' : 'Refresh pull request'}
                title={query.isFetching ? 'Refreshing pull request' : 'Refresh pull request'}
              >
                <RefreshCw className={`lucide-inline ${query.isFetching ? 'animate-spin' : ''}`} />
              </Btn>
              {sourceUrl && <a href={sourceUrl} target="_blank" rel="noopener noreferrer" className="p-1 rounded text-muted hover:text-text hover:bg-bg-hover" aria-label="Open pull request" title="Open pull request"><ExternalLink className="lucide-inline" /></a>}
            </div>
            <div className="mt-2 text-[15px] font-semibold text-text-strong leading-snug">{source.title} <span className="font-normal text-muted">{source.provider === 'github' ? '#' : '!'}{source.number}</span></div>
            <div className="mt-1 flex items-center gap-2 text-[11px] text-muted">
              {source.author && <span>{source.author}</span>}
              <span><span className="text-ok">+{source.additions}</span> <span className="text-danger">-{source.deletions}</span></span>
              {source.updatedAt && <span>Updated {age(source.updatedAt)}</span>}
            </div>
          </div>

          {source.partialSections && source.partialSections.length > 0 && (
            <div role="status" className="shrink-0 flex items-start gap-2 px-4 py-2 border-b border-border bg-warn/10 text-[11px] text-muted">
              <AlertCircle className="lucide-inline shrink-0 mt-0.5 text-warn" />
              <span>
                Provider results may be partial for {source.partialSections.join(', ')}. Open the {source.provider === 'github' ? 'pull request' : 'merge request'} for the complete set.
              </span>
            </div>
          )}

          <div role="tablist" aria-label="Pull request sections" className="shrink-0 border-b border-border px-2 py-2 flex items-center gap-1 overflow-x-auto">
            {tabs.map(item => (
              <Btn
                key={item.id}
                type="button"
                role="tab"
                id={`pr-tab-${item.id}`}
                aria-selected={tab === item.id}
                aria-controls="pr-tabpanel"
                onClick={() => setTab(item.id)}
                className={`shrink-0 flex items-center gap-1.5 px-2 py-1.5 rounded-md border-none cursor-pointer text-[11px] transition-colors ${tab === item.id ? 'bg-bg-hover text-text' : `bg-transparent text-muted hover:text-text ${item.tone || ''}`}`}
              >
                {item.id === 'checks' && checksUnavailable ? (
                  <AlertCircle className="lucide-inline text-warn" />
                ) : item.id === 'checks' && checksRunning ? (
                  <Loader className="lucide-inline text-warn animate-spin" />
                ) : item.id === 'checks' && showAllChecksPassed ? (
                  <Check className="lucide-inline text-ok" />
                ) : null}
                {item.label}
                {item.count !== undefined && <span className="text-muted">{item.id === 'checks' && checkCounts.total ? `${checkCounts.complete}/${item.count}` : item.count}</span>}
              </Btn>
            ))}
          </div>

          <div id="pr-tabpanel" role="tabpanel" aria-labelledby={`pr-tab-${tab}`} className="flex-1 min-h-0 overflow-y-auto">
            <PullRequestBody source={source} tab={tab} onAddToChat={onAddToChat} />
          </div>
        </>
      )}
    </div>
  )
}
