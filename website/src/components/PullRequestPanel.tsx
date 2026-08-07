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
  GitMerge,
  GitPullRequest,
  GitPullRequestClosed,
  GitPullRequestDraft,
  Loader,
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
  PullRequestStatus,
  PullRequestStatusBatch,
} from '../types'
import {
  MAX_PULL_REQUEST_SOURCES,
  type PullRequestLink,
} from '../utils/pullRequestLinks'
import { parseUnifiedDiff } from '../utils/parseUnifiedDiff'
import CopyBranchButton from './CopyBranchButton'
import hljs from '../utils/hljs'
import DOMPurify from 'dompurify'
import { DIFF_BG, DIFF_NUM, DIFF_EDGE } from '../utils/diffUtils'
import UnchangedSeparator from './UnchangedSeparator'
import GithubLogo from './icons/GithubLogo'
import GitlabLogo from './icons/GitlabLogo'
import { timeAgo } from '../utils/timeAgo'
import MarkdownRenderer from './MarkdownRenderer'
import CommentThreads from './CommentThreads'
import { Btn } from './ui'


import { i18nT } from '../i18n/t'
import ErrorNotice from './ErrorNotice'
const CHECK_POLL_BASE_MS = 10_000
const CHECK_POLL_MAX_MS = 60_000
// Strip-wide status poll. Steady state is paced by the TTL the server reports
// for its chip-status cache (falling back to 60s when absent), so the client
// never hardcodes a copy of it. While the server says a background refresh is in
// flight the panel polls again quickly instead — otherwise a state change that
// lands just after a poll would stay invisible for a further full TTL.
const STATUS_POLL_FALLBACK_MS = 60_000
const STATUS_POLL_MIN_MS = 5_000
const STATUS_POLL_MAX_MS = 300_000
const STATUS_FOLLOWUP_MS = 5_000
/** First delay after a failed poll; doubles per consecutive failure up to
 *  STATUS_POLL_MAX_MS. Polling is never abandoned — a transient network or
 *  gateway blip must not freeze the strip's glyphs until the user intervenes. */
const STATUS_ERROR_BACKOFF_MS = 30_000
/** Consecutive in-flight-refresh polls allowed at the fast interval before
 *  falling back to TTL pacing — bounds a provider that never settles. */
export const STATUS_FOLLOWUP_MAX = 3

/** Delay until the next strip-status poll: bounded exponential backoff while
 * polls are failing, the fast follow-up while the server reports a refresh in
 * flight (bounded by STATUS_FOLLOWUP_MAX), otherwise the server-reported cache
 * TTL clamped to a sane range. */
export function statusPollDelay(
  batch: PullRequestStatusBatch | undefined,
  consecutiveFollowups: number,
  consecutiveFailures = 0,
): number {
  if (consecutiveFailures > 0) {
    return Math.min(
      STATUS_ERROR_BACKOFF_MS * 2 ** (consecutiveFailures - 1),
      STATUS_POLL_MAX_MS,
    )
  }
  if (batch?.refreshing?.length && consecutiveFollowups <= STATUS_FOLLOWUP_MAX) {
    return STATUS_FOLLOWUP_MS
  }
  const ttl = Number(batch?.ttlSecs)
  const paced = Number.isFinite(ttl) && ttl > 0 ? ttl * 1000 : STATUS_POLL_FALLBACK_MS
  return Math.min(Math.max(paced, STATUS_POLL_MIN_MS), STATUS_POLL_MAX_MS)
}
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
  /** The server refused pending an acknowledgement the client may now offer. */
  confirmationRequired: boolean
} {
  let message = error instanceof Error ? error.message : String(error || '')
  let confirmationRequired = false
  // ApiError already unwraps the human message, which discards every other
  // field, so the structured marker is read from the raw body it preserves.
  const raw = typeof (error as { body?: unknown })?.body === 'string'
    ? (error as { body: string }).body
    : message
  try {
    const payload = JSON.parse(raw) as { error?: unknown; confirmationRequired?: unknown }
    if (typeof payload.error === 'string') message = payload.error
    confirmationRequired = payload.confirmationRequired === true
  } catch {
    // Provider and network errors may already be plain text.
  }
  const authenticationFailure = /\b(?:not logged in(?:to)?|unauthenticated|authentication (?:failed|required)|requires authentication)\b/i.test(message)
  const loginCommand = authenticationFailure && /(?:`|\b)gh auth login(?:`|\b)/i.test(message)
    ? 'gh auth login'
    : authenticationFailure && /(?:`|\b)glab auth login(?:`|\b)/i.test(message)
      ? 'glab auth login'
      : ''
  return { message, loginCommand, confirmationRequired }
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

export interface PullRequestMergeBlocker {
  tone: 'danger' | 'warn'
  title: string
  detail: string
  /** Prefilled chat handoff for blockers the agent can act on. */
  handoff?: string
}

/** Derive the merge blocker to surface for an open pull request, or null when
 * nothing blocks the merge (or the PR is not plainly open — draft, merged,
 * closed, locked, ... — where merge state is either expected or moot).
 * Conflicts and behind-base are agent-actionable and carry a chat handoff;
 * branch-protection blocks need a human. The behind handoff deliberately
 * avoids history rewriting (merge base into head); rebase + force-push is
 * reserved for genuine conflicts. */
export function pullRequestMergeBlocker(source: PullRequestSource): PullRequestMergeBlocker | null {
  // GitLab reports open MRs as 'opened'; the full payload carries the raw
  // provider state (matching stateTone below), so accept both spellings.
  const state = source.state.toLowerCase()
  if ((state !== 'open' && state !== 'opened') || source.mergedAt || source.draft) return null
  const base = source.baseBranch || 'the base branch'
  const label = source.provider === 'github' ? `PR #${source.number}` : `MR !${source.number}`
  const sourceUrl = safeExternalUrl(source.url)
  const handoffHeader = (problem: string) => [
    `${problem} on ${label} (${source.title}):`,
    '',
    `- Branch: ${source.headBranch || 'the feature branch'} -> ${base}`,
    ...(sourceUrl ? [`- Pull request: ${sourceUrl}`] : []),
    '',
  ]
  if (source.mergeable === 'conflicting') {
    return {
      tone: 'danger',
      title: i18nT('components.pullRequestPanel.merge_conflicts'),
      detail: i18nT('components.pullRequestPanel.merge_conflicts_detail', { base }),
      handoff: [
        ...handoffHeader('Merge conflict'),
        `Resolve the conflicts with ${base}. If the branch is shared with other contributors, prefer merging ${base} into the branch; otherwise rebase onto ${base} and push with \`git push --force-with-lease\` (abort if the lease fails).`,
      ].join('\n'),
    }
  }
  if (source.mergeStateStatus === 'need_rebase') {
    // GitLab-specific: the project requires a rebase (e.g. fast-forward-only
    // merge method) -- a merge commit cannot unblock this MR.
    return {
      tone: 'warn',
      title: i18nT('components.pullRequestPanel.rebase_required'),
      detail: i18nT('components.pullRequestPanel.rebase_required_detail', { base }),
      handoff: [
        ...handoffHeader('Rebase required'),
        `This project requires a rebase (merge commits will not unblock the MR). If the branch is shared with other contributors, coordinate with them before rewriting history; otherwise rebase onto ${base} and push with \`git push --force-with-lease\` (abort if the lease fails).`,
      ].join('\n'),
    }
  }
  if (source.mergeStateStatus === 'behind') {
    return {
      tone: 'warn',
      title: i18nT('components.pullRequestPanel.branch_is_behind'),
      detail: i18nT('components.pullRequestPanel.branch_is_behind_detail', { base }),
      handoff: [
        ...handoffHeader('Out-of-date branch'),
        `Update the branch without rewriting history: merge ${base} into the branch (or use the provider's update-branch option) and push normally. Only rebase if the repository requires a linear history, and never force-push a shared branch without checking with its other contributors.`,
      ].join('\n'),
    }
  }
  if (source.mergeStateStatus === 'blocked') {
    return {
      tone: 'warn',
      title: i18nT('components.pullRequestPanel.merge_blocked'),
      detail: i18nT('components.pullRequestPanel.branch_protection_requirements_approving_reviews'),
    }
  }
  return null
}

function stateTone(source: PullRequestSource): string {
  const state = source.state.toLowerCase()
  if (source.mergedAt || state === 'merged') return 'bg-aim/15 text-aim'
  if (state === 'open' || state === 'opened') return 'bg-ok/15 text-ok'
  return 'bg-bg-hover text-muted'
}

export function stateLabel(source: PullRequestSource): string {
  const state = source.state.toLowerCase()
  // Terminal states win over draft, matching pullRequestLifecycleState below:
  // GitLab keeps `draft` set on a merge request that was closed while still a
  // draft, and the badge must not contradict the tab's lifecycle glyph.
  if (source.mergedAt || state === 'merged') return i18nT('components.pullRequestPanel.merged')
  if (state === 'closed') return i18nT('components.pullRequestPanel.closed')
  if (source.draft) return i18nT('components.pullRequestPanel.draft')
  const label = source.state || 'Open'
  return label.charAt(0).toUpperCase() + label.slice(1).toLowerCase()
}

type LifecycleState = NonNullable<PullRequestStatus['state']>

/** Lifecycle state of a fully loaded pull request, in the same vocabulary the
 * lightweight status endpoint uses — so the selected tab's chip can be driven by
 * the authoritative payload instead of waiting for the next status poll.
 * Merged wins over draft: a merged pull request is terminal. Returns undefined
 * for anything outside the known set (GitLab also reports states like `locked`),
 * so the tab shows no lifecycle glyph rather than mislabeling it "Open". */
export function pullRequestLifecycleState(
  source: PullRequestSource,
): LifecycleState | undefined {
  const state = source.state.toLowerCase()
  if (source.mergedAt || state === 'merged') return 'merged'
  if (state === 'closed') return 'closed'
  if (source.draft) return 'draft'
  if (state === 'open' || state === 'opened') return 'open'
  return undefined
}

/** Roll a check list up to one CI signal, matching the backend's chip rollup
 * (any failure fails; otherwise any pending runs; otherwise passed). */
export function pullRequestCiSignal(
  checks: PullRequestCheck[] | undefined,
): PullRequestStatus['ci'] {
  if (!checks?.length) return undefined
  if (checks.some(check => check.bucket === 'failed')) return 'failed'
  if (checks.some(check => check.bucket === 'pending')) return 'running'
  return 'passed'
}

/**
 * Lifecycle glyph and tone for a pull request's state.
 *
 * Presentational fields only. The display copy lives in `LIFECYCLE_LABEL_KEY`
 * below, named once so the two cannot drift apart.
 */
const LIFECYCLE_META: Record<LifecycleState, { icon: typeof GitMerge; tone: string }> = {
  merged: { icon: GitMerge, tone: 'text-aim' },
  closed: { icon: GitPullRequestClosed, tone: 'text-danger' },
  draft: { icon: GitPullRequestDraft, tone: 'text-muted' },
  open: { icon: GitPullRequest, tone: 'text-ok' },
}

/**
 * Catalog keys for the lifecycle states, flat and indexed inline at the
 * `i18nT()` call so the key gate can resolve them. Split out of
 * `LIFECYCLE_META` for that reason alone — the glyph/tone table below still
 * carries the presentational fields.
 */
const LIFECYCLE_LABEL_KEY: Record<LifecycleState, string> = {
  merged: 'components.pullRequestPanel.state_merged',
  closed: 'components.pullRequestPanel.state_closed',
  draft: 'components.pullRequestPanel.state_draft',
  open: 'components.pullRequestPanel.state_open',
}

/** Catalog keys for the CI rollup. See LIFECYCLE_LABEL_KEY for why it is flat. */
const CI_LABEL_KEY: Record<NonNullable<PullRequestStatus['ci']>, string> = {
  running: 'components.pullRequestPanel.checks_running',
  passed: 'components.pullRequestPanel.checks_passed',
  failed: 'components.pullRequestPanel.checks_failed',
}

/** CI rollup glyph, tone, and catalog KEY. Keys not strings — see LIFECYCLE_META. */
const CI_META: Record<NonNullable<PullRequestStatus['ci']>, { icon: typeof Check; tone: string; spin?: boolean }> = {
  running: { icon: Loader, tone: 'text-warn', spin: true },
  passed: { icon: Check, tone: 'text-ok' },
  failed: { icon: XCircle, tone: 'text-danger' },
}

/** State markers for one pull-request tab in the source strip: lifecycle glyph
 * plus, while the pull request is still live, its CI rollup. CI is suppressed
 * once merged or closed — the lifecycle glyph is the terminal signal there.
 * `ChatSidebar.tsx::showsChipCi` applies the same rule to the sidebar chip; the
 * two render the same pull request and must not disagree about its lifecycle. */
function SourceTabState({ status }: { status: PullRequestStatus | undefined }) {
  const lifecycle = status?.state
  const ci = lifecycle === 'merged' || lifecycle === 'closed' ? undefined : status?.ci
  if (!lifecycle && !ci) return null
  const life = lifecycle ? LIFECYCLE_META[lifecycle] : null
  const check = ci ? CI_META[ci] : null
  const LifeIcon = life?.icon
  const CheckIcon = check?.icon
  // Resolved here, in the component body, so a language switch re-renders into
  // the new locale — LIFECYCLE_META / CI_META hold keys, not strings.
  // Indexed inline off the discriminant, not `life.labelKey`: reading the key
  // through a looked-up object is a shape `scripts/check-i18n-keys.mjs` cannot
  // resolve, which would exempt these sites from every catalog check.
  const lifeLabel = lifecycle ? i18nT(LIFECYCLE_LABEL_KEY[lifecycle]) : ''
  const checkLabel = ci ? i18nT(CI_LABEL_KEY[ci]) : ''
  return (
    <>
      {LifeIcon && life && (
        <span className={`inline-flex shrink-0 ${life.tone}`} aria-label={lifeLabel} title={lifeLabel}>
          <LifeIcon className="lucide-inline" aria-hidden="true" />
        </span>
      )}
      {CheckIcon && check && (
        <span className={`inline-flex shrink-0 ${check.tone}`} aria-label={checkLabel} title={checkLabel}>
          <CheckIcon className={`lucide-inline ${check.spin ? 'animate-spin' : ''}`} aria-hidden="true" />
        </span>
      )}
    </>
  )
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
  if (!ready) return <div className="px-3 py-3 text-[11px] text-muted">{i18nT('components.pullRequestPanel.loading_diff')}</div>
  return (
    <div className="text-[11px] leading-5 font-mono">
      {rows.map((row, index) => {
        if (row.kind === 'hunk-gap') {
          // Leading gap (diff starts mid-file): the gutter numbers already
          // carry the position — render nothing.
          if (index === 0) return null
          if (row.hiddenCount <= 0) return <div key={index} className="border-t border-border/60" />
          return <UnchangedSeparator key={index} count={row.hiddenCount} />
        }
        const tone = row.kind === 'add' ? DIFF_BG.add : row.kind === 'del' ? DIFF_BG.del : ''
        const edge = row.kind === 'add' || row.kind === 'del' ? ` ${DIFF_EDGE[row.kind]}` : ''
        const html = highlighted?.[index]
        return (
          <div key={index} className={`flex ${tone}${edge}`}>
            <span className={`w-10 shrink-0 px-1 text-right select-none border-r border-border ${DIFF_NUM[row.kind]}`}>{(row.kind === 'del' ? row.oldLine : row.newLine) ?? ''}</span>
            {html !== undefined && html !== '' ? (
              <span className="hljs flex-1 min-w-0 whitespace-pre-wrap break-words px-2 !bg-transparent" dangerouslySetInnerHTML={{ __html: html }} />
            ) : (
              <span className="flex-1 min-w-0 whitespace-pre-wrap break-words px-2 text-text">{row.text}</span>
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
        <div className="border-t border-border">
          {file.patch ? (
            <DiffView patch={file.patch} path={file.path} />
          ) : (
            <div className="px-3 py-4 text-[12px] text-muted">{i18nT('components.pullRequestPanel.the_provider_did_not_return_a_patch_for_this_fil')}</div>
          )}
        </div>
      )}
    </div>
  )
}

/**
 * Per-check bucket glyph, colour, and label.
 *
 * `label` is a GETTER, not a string: this table is module-level, so a plain
 * `i18nT()` call in it would resolve once at import and keep the boot language
 * forever. An accessor moves the lookup to whichever consumer reads it, each of
 * which runs per render. The key is a literal inside the getter, which is the
 * shape `scripts/check-i18n-keys.mjs` resolves statically.
 */
const CHECK_META = {
  failed: { icon: XCircle, color: 'text-danger', get label() { return i18nT('components.pullRequestPanel.check_failed') } },
  pending: { icon: Loader, color: 'text-warn', get label() { return i18nT('components.pullRequestPanel.check_in_progress') } },
  passed: { icon: Check, color: 'text-ok', get label() { return i18nT('components.pullRequestPanel.check_passed') } },
  skipped: { icon: SkipForward, color: 'text-muted', get label() { return i18nT('components.pullRequestPanel.check_skipped') } },
} as const

function CheckRow({ check, source, onAddToChat }: { check: PullRequestCheck; source: PullRequestSource; onAddToChat?: (text: string) => void }) {
  const meta = CHECK_META[check.bucket]
  const Icon = meta.icon
  // `meta.label` is the last-resort status, read where it renders (and in the
  // handoff closure) so a language switch picks it up; `check.conclusion` /
  // `check.status` ahead of it are raw provider tokens and stay verbatim.
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
    onAddToChat?.(lines.join('\n'))
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
      {check.bucket === 'failed' && onAddToChat && (
        <Btn
          type="button"
          onClick={handoff}
          className="text-[11px] shrink-0 mr-3 px-2 py-1 rounded-md border border-border bg-transparent text-muted hover:text-text hover:bg-bg-hover cursor-pointer"
        >
          {i18nT('components.pullRequestPanel.add_to_chat')}
        </Btn>
      )}
    </div>
  )
}

/** The text handed to the composer for one comment.
 *
 *  The frame is localized and the comment body is quoted verbatim. The composer
 *  is something the USER reads before sending, so the scaffolding follows the
 *  dashboard's language (the same reason the failing-check handoff reads its
 *  status label through the catalog); the reviewer's own words are never
 *  rewritten. */
function commentHandoff(c: PullRequestComment): string {
  const location = c.path ? `${c.path}${c.line ? `:${c.line}` : ''}` : ''
  const author = c.author || i18nT('components.pullRequestPanel.a_reviewer')
  const frame = location
    ? i18nT('components.pullRequestPanel.quoting_a_pull_request_comment_by_author_on_loca', { author, location })
    : i18nT('components.pullRequestPanel.quoting_a_pull_request_comment_by_author', { author })
  // Markdown block quote: joined rather than interpolated so no prose-shaped
  // template literal sits here for the string gate to (rightly) flag.
  const quoted = '> ' + (c.body || '').split('\n').join('\n> ')
  return [frame, '', quoted].join('\n')
}

function EmptyTab({ children }: { children: string }) {
  return <div className="flex flex-col items-center justify-center gap-2 py-12 text-[13px] text-muted"><Circle className="lucide-inline" />{children}</div>
}

/** Whether the pull request is still live — actions are pointless (and the
 * provider rejects them) once it is merged or closed. */
export function pullRequestIsLive(source: PullRequestSource): boolean {
  const state = source.state.toLowerCase()
  if (source.mergedAt) return false
  return state === 'open' || state === 'opened' || state === 'draft'
}

/** Provider-write actions on the loaded pull request: take it out of draft, and
 * arm auto-merge. Both are owner-only server-side; failures surface inline
 * rather than as a toast, so the reason stays next to the button that caused it.
 *
 * Auto-merge takes a second confirming click because it authorizes a merge. That
 * click sends no acknowledgement flag: if the server finds nothing would defer
 * the merge (GitLab with no pipeline pending) it refuses and says so, and only
 * that refusal escalates the UI to a third, explicitly-worded confirmation which
 * carries the flag. The acknowledgement is therefore never a constant the client
 * asserts up front, and the wording always describes the real situation.
 *
 * Confirm and Cancel are separate buttons, so the second half of an accidental
 * double-click cannot land on a confirm that appeared under the cursor. */
export function PullRequestActions({ source }: { source: PullRequestSource }) {
  const queryClient = useQueryClient()
  const [confirmAutoMerge, setConfirmAutoMerge] = useState(false)
  const [immediateMergeWarning, setImmediateMergeWarning] = useState('')
  const isGitHub = source.provider === 'github'

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['pull-request-source'] })
    void queryClient.invalidateQueries({ queryKey: ['pull-request-statuses'] })
  }
  const readyMutation = useMutation({
    mutationFn: () => api.markPullRequestReady(source.url),
    onSuccess: invalidate,
  })
  const autoMergeMutation = useMutation({
    mutationFn: (acknowledgeImmediateMerge: boolean) =>
      api.enablePullRequestAutoMerge(source.url, acknowledgeImmediateMerge),
    onSuccess: () => {
      setConfirmAutoMerge(false)
      setImmediateMergeWarning('')
      invalidate()
    },
    onError: (error: unknown) => {
      // The server is the only thing that knows whether a merge would actually
      // be deferred, so its refusal — not a client guess — is what escalates.
      const details = pullRequestErrorDetails(error)
      if (details.confirmationRequired) setImmediateMergeWarning(details.message)
    },
  })

  const dismissAutoMerge = () => {
    setConfirmAutoMerge(false)
    setImmediateMergeWarning('')
    autoMergeMutation.reset()
  }

  if (!pullRequestIsLive(source)) return null
  const showReady = source.draft
  const showAutoMerge = !source.draft && !source.autoMerge
  const errorDetails = pullRequestErrorDetails(readyMutation.error || autoMergeMutation.error)
  // The immediate-merge refusal is rendered as its own confirmation prompt, so
  // repeating it as a failure would read as a dead end rather than a question.
  const error = immediateMergeWarning ? '' : errorDetails.message
  const busy = readyMutation.isPending || autoMergeMutation.isPending
  const armedMethod = autoMergeMutation.data?.mergeMethod
  if (!showReady && !showAutoMerge && !source.autoMerge && !error) return null

  return (
    <div className="mt-2 flex flex-wrap items-center gap-2">
      {showReady && (
        <Btn
          type="button"
          onClick={() => readyMutation.mutate()}
          disabled={busy}
          title={isGitHub
            ? i18nT('components.pullRequestPanel.take_this_pull_request_out_of_draft')
            : i18nT('components.pullRequestPanel.take_this_merge_request_out_of_draft')}
          className="inline-flex items-center gap-1.5 px-2 py-1 rounded-md border border-border bg-transparent text-[11px] text-muted hover:text-text hover:bg-bg-hover cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {readyMutation.isPending ? <Loader className="lucide-inline animate-spin" /> : <GitPullRequest className="lucide-inline" />}
          {i18nT('components.pullRequestPanel.ready_for_review')}
        </Btn>
      )}
      {source.autoMerge && (
        <span className="inline-flex items-center gap-1.5 px-2 py-1 rounded-md bg-aim/15 text-[11px] text-aim" title={isGitHub ? i18nT('components.pullRequestPanel.github_will_merge_this_pull_request_once_its_req') : i18nT('components.pullRequestPanel.gitlab_will_merge_this_merge_request_when_the_pi')}>
          <GitMerge className="lucide-inline" /> {i18nT('components.pullRequestPanel.auto_merge_enabled')}{armedMethod ? ` (${armedMethod})` : ''}
        </span>
      )}
      {showAutoMerge && (confirmAutoMerge || immediateMergeWarning) && (
        <Btn
          type="button"
          onClick={dismissAutoMerge}
          disabled={busy}
          className="inline-flex items-center gap-1.5 px-2 py-1 rounded-md border border-border bg-transparent text-[11px] text-muted hover:text-text hover:bg-bg-hover cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {i18nT('components.pullRequestPanel.cancel')}
        </Btn>
      )}
      {showAutoMerge && (
        <Btn
          type="button"
          onClick={() => {
            if (immediateMergeWarning) autoMergeMutation.mutate(true)
            else if (confirmAutoMerge) autoMergeMutation.mutate(false)
            else setConfirmAutoMerge(true)
          }}
          disabled={busy}
          title={isGitHub
            ? i18nT('components.pullRequestPanel.github_merges_this_pull_request_automatically_on')
            : i18nT('components.pullRequestPanel.gitlab_merges_this_merge_request_when_the_pipeli')}
          className={`inline-flex items-center gap-1.5 px-2 py-1 rounded-md border text-[11px] cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed ${confirmAutoMerge || immediateMergeWarning ? 'border-warn text-warn hover:bg-warn/10' : 'border-border bg-transparent text-muted hover:text-text hover:bg-bg-hover'}`}
        >
          {autoMergeMutation.isPending ? <Loader className="lucide-inline animate-spin" /> : <GitMerge className="lucide-inline" />}
          {immediateMergeWarning ? i18nT('components.pullRequestPanel.merge_now') : confirmAutoMerge ? i18nT('components.pullRequestPanel.confirm_auto_merge') : i18nT('components.pullRequestPanel.enable_auto_merge')}
        </Btn>
      )}
      {immediateMergeWarning && !autoMergeMutation.isPending && (
        <span role="alert" className="text-[11px] text-warn">{immediateMergeWarning}</span>
      )}
      {confirmAutoMerge && !immediateMergeWarning && !autoMergeMutation.isPending && (
        <span className="text-[11px] text-warn">
          {isGitHub
            ? i18nT('components.pullRequestPanel.this_authorizes_the_merge_as_soon_as_requirement')
            : i18nT('components.pullRequestPanel.this_authorizes_the_merge_when_the_pipeline_succ')}
        </span>
      )}
      <ErrorNotice message={error} variant="inline" askAgent />
    </div>
  )
}

function PullRequestBody({ source, tab, onAddToChat }: { source: PullRequestSource; tab: SourceTab; onAddToChat?: (text: string) => void }) {
  if (tab === 'description') {
    return source.description
      ? <div className="px-4 py-4 text-[13px]"><MarkdownRenderer content={source.description} /></div>
      : <EmptyTab>{i18nT('components.pullRequestPanel.no_description_was_provided')}</EmptyTab>
  }
  if (tab === 'changes') {
    if (!source.files.length) return <EmptyTab>{i18nT('components.pullRequestPanel.no_changed_files_were_returned')}</EmptyTab>
    const totalAdds = source.files.reduce((sum, file) => sum + file.additions, 0)
    const totalDels = source.files.reduce((sum, file) => sum + file.deletions, 0)
    return (
      <div>
        <div className="sticky top-0 z-[1] flex items-center gap-2 px-3 py-2 border-b border-border bg-bg text-[12px]">
          <span className="font-medium text-text">{i18nT('components.pullRequestPanel.files_changed', { count: source.files.length })}</span>
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
                <div className="text-[13px] font-medium text-text">{commit.title || i18nT('components.pullRequestPanel.untitled_commit')}</div>
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
    ) : <EmptyTab>{i18nT('components.pullRequestPanel.no_commits_were_returned')}</EmptyTab>
  }
  if (tab === 'checks') {
    if (!source.checks.length) return <EmptyTab>{i18nT('components.pullRequestPanel.no_ci_checks_were_returned')}</EmptyTab>
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
  // Threads, replies, resolve/unresolve and a top-level comment box all live in
  // the shared `CommentThreads`. The provider returns a FLAT comment list, but
  // inline comments carry a `threadId`, so the conversation structure is
  // recoverable -- and a reply separated from the line it answers is not a review.
  //
  // Rendered unconditionally: it owns the "comment on this pull request" composer,
  // so gating it on an existing comment would make the FIRST comment the one you
  // cannot post. It draws its own empty state.
  return (
    <div className="p-3">
      <CommentThreads
        src={source}
        // The panel owns what it says to the agent; the thread list only
        // reports which comment was picked.
        onAddToChat={onAddToChat && ((c) => onAddToChat(commentHandoff(c)))}
      />
    </div>
  )}

export default function PullRequestPanel({
  sources,
  selectedUrl,
  onSelect,
  onReconcile,
  onAddToChat,
}: {
  sources: PullRequestLink[]
  selectedUrl: string
  onSelect: (url: string) => void
  // Called when this panel normalizes an out-of-range selection on its own,
  // rather than the user picking a tab. Kept separate from onSelect because the
  // parent persists an explicit choice and must NOT persist this one: a
  // transcript that lacks the remembered url may simply be a cached one that is
  // still being refetched. Defaults to onSelect for callers that do not care.
  onReconcile?: (url: string) => void
  // Optional so the panel can render outside chat (e.g. the Code Review Sage
  // page), where there is no composer to hand text to. When absent the
  // chat-handoff affordances are hidden rather than rendered inert.
  onAddToChat?: (text: string) => void
}) {
  const cappedSources = sources.slice(0, MAX_PULL_REQUEST_SOURCES)
  const selected = cappedSources.find(source => source.url === selectedUrl) || cappedSources[0]
  const [tab, setTab] = useState<SourceTab>('changes')
  const [checkPollState, setCheckPollState] = useState({ url: '', failures: 0 })
  const checkPollStateRef = useRef({ url: '', failures: 0 })
  const forceRefreshRef = useRef(false)
  const queryClient = useQueryClient()

  useEffect(() => {
    if (selected && selected.url !== selectedUrl) (onReconcile || onSelect)(selected.url)
  }, [selected, selectedUrl, onSelect, onReconcile])

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
  // Lifecycle + CI state for EVERY tab in the strip, so a merged, closed, or
  // failing pull request is legible without selecting it. Served from the
  // gateway's short-TTL chip cache (one bounded request per poll, no provider
  // call on the request path), so entries appear as background refreshes land.
  const statusUrls = useMemo(
    () => sources.slice(0, MAX_PULL_REQUEST_SOURCES).map(item => item.url),
    [sources],
  )
  const statusFollowupsRef = useRef(0)
  useEffect(() => { statusFollowupsRef.current = 0 }, [statusUrls])
  const statusQuery = useQuery<PullRequestStatusBatch>({
    queryKey: ['pull-request-statuses', statusUrls] as const,
    queryFn: async () => {
      const result = await api.pullRequestStatuses(statusUrls)
      // Count consecutive polls that found a refresh in flight, so a provider
      // that never settles can't hold the panel on the fast interval forever.
      statusFollowupsRef.current = result.refreshing?.length
        ? statusFollowupsRef.current + 1
        : 0
      return result
    },
    enabled: statusUrls.length > 0,
    staleTime: 0,
    refetchOnWindowFocus: false,
    retry: false,
    // A dropped connection is exactly when the chips went stale, so re-poll on
    // reconnect instead of waiting out the backoff.
    refetchOnReconnect: true,
    refetchInterval: currentQuery => statusPollDelay(
      currentQuery.state.data,
      statusFollowupsRef.current,
      // Failing polls back off but never stop: parking permanently would leave
      // every unselected tab's glyph stale after one transient error, with no
      // recovery until the user hit refresh or the source list changed.
      currentQuery.state.fetchFailureCount,
    ),
  })
  const handleRefresh = () => {
    forceRefreshRef.current = true
    // Also force the strip-wide status poll now, instead of waiting out its
    // current interval (or its post-error backoff).
    void statusQuery.refetch()
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
  const mergeBlocker = source ? pullRequestMergeBlocker(source) : null
  const statusByUrl = useMemo(() => {
    const merged: Record<string, PullRequestStatus> = { ...(statusQuery.data?.statuses || {}) }
    // The selected pull request already has a full, user-refreshable payload —
    // prefer it over the cached chip status so its own chip never lags the
    // header badge it sits above.
    if (source) {
      merged[source.url] = {
        state: pullRequestLifecycleState(source),
        ci: pullRequestCiSignal(source.checks),
      }
    }
    return merged
  }, [statusQuery.data, source])

  const tabs: Array<{ id: SourceTab; label: string; count?: number; tone?: string }> = source ? [
    { id: 'changes', label: i18nT('components.pullRequestPanel.changes'), count: source.files.length },
    { id: 'description', label: i18nT('components.pullRequestPanel.description') },
    { id: 'commits', label: i18nT('components.pullRequestPanel.commits'), count: source.commits.length },
    {
      id: 'checks',
      label: checksUnavailable
        ? i18nT('components.pullRequestPanel.checks_unavailable')
        : checksRunning
          ? i18nT('components.pullRequestPanel.checks_running')
          : showAllChecksPassed
            ? i18nT('components.pullRequestPanel.all_checks_passed')
            : i18nT('components.pullRequestPanel.checks'),
      count: checkCounts.total,
      tone: checkCounts.failed
        ? 'text-danger'
        : checksUnavailable || checksRunning
          ? 'text-warn'
          : showAllChecksPassed
            ? 'text-ok'
            : '',
    },
    { id: 'reviews', label: i18nT('components.pullRequestPanel.reviews'), count: source.comments.length },
  ] : []

  return (
    <div className="flex flex-col h-full min-h-0">
      {/* The source strip only earns its row when there is a choice to make. A
          single-source host (the Code Review Sage detail pane, where the left rail
          already picked the pull request) would otherwise get a tab bar holding
          exactly one tab that does nothing. */}
      {cappedSources.length > 1 && (
      <div role="tablist" aria-label={i18nT('components.pullRequestPanel.pull_requests')} className="shrink-0 border-b border-border px-2 py-2 flex items-center gap-1 overflow-x-auto">
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
            <SourceTabState status={statusByUrl[item.url]} />
          </Btn>
        ))}
      </div>
      )}

      {query.isLoading && <div className="flex-1 flex items-center justify-center gap-2 text-[13px] text-muted"><Loader className="lucide-inline animate-spin" />{i18nT('components.pullRequestPanel.loading_source_provider')}</div>}
      {query.error && (
        <div className="flex-1 flex items-center justify-center px-6">
          <div role="alert" className="max-w-md flex flex-col items-center">
            <AlertCircle className={`lucide-inline mb-2 ${queryError.loginCommand ? 'text-warn' : 'text-danger'}`} />
            <div className="text-[13px] font-medium text-text">
              {queryError.loginCommand
                ? queryError.loginCommand === 'gh auth login'
                  ? i18nT('components.pullRequestPanel.github_cli_login_required')
                  : i18nT('components.pullRequestPanel.gitlab_cli_login_required')
                : i18nT('components.pullRequestPanel.could_not_load_this_pull_request')}
            </div>
            {queryError.loginCommand ? (
              <>
                <div className="text-[12px] text-muted mt-1 text-center">{i18nT('components.pullRequestPanel.kiro_crew_uses_your_local_provider_cli_to_load_p')}</div>
                <code className="inline-block mt-2 px-2 py-1 rounded bg-bg-hover text-[12px] text-text">{queryError.loginCommand}</code>
              </>
            ) : (
              <div className="mt-2 w-full max-h-64 overflow-y-auto rounded-md bg-bg-hover/50 border border-border px-3 py-2 text-left text-[12px] text-muted whitespace-pre-wrap break-words font-mono leading-relaxed">{queryError.message}</div>
            )}
            <Btn type="button" onClick={handleRefresh} className="mt-3 inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-md border border-border bg-transparent text-[12px] text-muted hover:text-text hover:bg-bg-hover cursor-pointer"><RefreshCw className="lucide-inline" />{i18nT('components.pullRequestPanel.retry')}</Btn>
          </div>
        </div>
      )}

      {source && (
        <>
          <div className="shrink-0 px-4 py-3 border-b border-border">
            <div className="flex items-center gap-2 text-[11px] text-muted">
              <span className={`px-1.5 py-0.5 rounded font-medium ${stateTone(source)}`}>{stateLabel(source)}</span>
              <span>{source.provider === 'github' ? 'GitHub' : 'GitLab'}</span>
              {source.headBranch && source.baseBranch && (
                <span className="min-w-0 flex items-center gap-1 truncate"><CopyBranchButton branch={source.headBranch} /><ArrowRight className="lucide-inline shrink-0" /><span className="truncate">{source.baseBranch}</span></span>
              )}
              <Btn
                type="button"
                onClick={handleRefresh}
                disabled={query.isFetching}
                className="ml-auto p-1 rounded border-none bg-transparent text-muted hover:text-text hover:bg-bg-hover cursor-pointer disabled:opacity-60 disabled:cursor-default"
                aria-label={query.isFetching ? i18nT('components.pullRequestPanel.refreshing_pull_request') : i18nT('components.pullRequestPanel.refresh_pull_request')}
                title={query.isFetching ? i18nT('components.pullRequestPanel.refreshing_pull_request') : i18nT('components.pullRequestPanel.refresh_pull_request')}
              >
                <RefreshCw className={`lucide-inline ${query.isFetching ? 'animate-spin' : ''}`} />
              </Btn>
              {sourceUrl && <a href={sourceUrl} target="_blank" rel="noopener noreferrer" className="p-1 rounded text-muted hover:text-text hover:bg-bg-hover" aria-label={i18nT('components.pullRequestPanel.open_pull_request')} title={i18nT('components.pullRequestPanel.open_pull_request')}><ExternalLink className="lucide-inline" /></a>}
            </div>
            <div className="mt-2 text-[15px] font-semibold text-text-strong leading-snug">{source.title} <span className="font-normal text-muted">{source.provider === 'github' ? '#' : '!'}{source.number}</span></div>
            <div className="mt-1 flex items-center gap-2 text-[11px] text-muted">
              {source.author && <span>{source.author}</span>}
              <span><span className="text-ok">+{source.additions}</span> <span className="text-danger">-{source.deletions}</span></span>
              {source.updatedAt && <span>{i18nT('components.pullRequestPanel.updated')} {age(source.updatedAt)}</span>}
            </div>
            <PullRequestActions key={source.url} source={source} />
          </div>

          {mergeBlocker && (
            <div role="alert" className={`shrink-0 flex items-start gap-2 px-4 py-2 border-b border-border text-[11px] text-muted ${mergeBlocker.tone === 'danger' ? 'bg-danger/10' : 'bg-warn/10'}`}>
              <GitMerge className={`lucide-inline shrink-0 mt-0.5 ${mergeBlocker.tone === 'danger' ? 'text-danger' : 'text-warn'}`} />
              <span className="min-w-0 flex-1">
                <span className={`font-medium ${mergeBlocker.tone === 'danger' ? 'text-danger' : 'text-warn'}`}>{mergeBlocker.title}.</span> {mergeBlocker.detail}
              </span>
              {mergeBlocker.handoff && onAddToChat && (
                <Btn
                  type="button"
                  onClick={() => onAddToChat(mergeBlocker.handoff!)}
                  className="text-[11px] shrink-0 px-2 py-1 rounded-md border border-border bg-transparent text-muted hover:text-text hover:bg-bg-hover cursor-pointer"
                >
                  {i18nT('components.pullRequestPanel.add_to_chat')}
                </Btn>
              )}
            </div>
          )}

          {source.partialSections && source.partialSections.length > 0 && (
            <div role="status" className="shrink-0 flex items-start gap-2 px-4 py-2 border-b border-border bg-warn/10 text-[11px] text-muted">
              <AlertCircle className="lucide-inline shrink-0 mt-0.5 text-warn" />
              <span>
                {source.provider === 'github'
                  ? i18nT('components.pullRequestPanel.provider_results_may_be_partial_pull_request', { sections: source.partialSections.join(', ') })
                  : i18nT('components.pullRequestPanel.provider_results_may_be_partial_merge_request', { sections: source.partialSections.join(', ') })}
              </span>
            </div>
          )}

          <div role="tablist" aria-label={i18nT('components.pullRequestPanel.pull_request_sections')} className="shrink-0 border-b border-border px-2 py-2 flex items-center gap-1 overflow-x-auto">
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
