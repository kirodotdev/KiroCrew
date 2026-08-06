// Right column: the full-width pull-request detail pane.
//
// A read-only PR analogue of IssueDetail. A non-scrolling header (title over a
// meta row: state pill + copy-link + #number linking out to GitHub + author +
// base←head branch + diff stat, plus the Review action) over a scroll area split
// into a main column (the PR DESCRIPTION pinned at the top, then the activity
// TIMELINE — comments, reviews, and commits on one dot-and-rail timeline,
// NEWEST-FIRST with the latest node pulsing) and a right sidebar led by the
// AUTO REVIEW results (failing/running checks written out, passing ones
// collapsed), then reviewers, labels, assignees, milestone, diff stat,
// mergeability, and dates.
//
// Instant paint comes from the list `pr` row (title / state / author / base /
// head / labels); the richer fields (diff stat, review counts, mergeability,
// timeline, checks) stream in from GET /pull and are cached.
import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Copy, Check, RefreshCw, GitPullRequest, GitMerge, GitPullRequestClosed, GitPullRequestDraft,
  MessageSquare, Tag, Users, CalendarDays, GitCommitHorizontal, FileDiff, Milestone as MilestoneIcon,
  Link2, CircleDot, CircleSlash, Pencil, UserPlus, UserMinus,
  CheckCircle2, XCircle, Eye, GitBranch, ChevronDown, ChevronUp, Loader2, ShieldCheck,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import RefMarkdown from './RefMarkdown'
import { CommentCardSkeleton, HeaderSkeleton, TimelineSkeleton } from './DetailSkeleton'
import { safeHttpUrl } from '../../../lib/safeUrl'
import LabelChip from './LabelChip'
import MemberBadge from './MemberBadge'
import AiSummaryCard from './AiSummaryCard'
import ReviewButton from './ReviewButton'
import PrActionsBar from './PrActionsBar'
import PrRunActions from './PrRunActions'
import { useIssueRadar } from '../context'
import { relativeTimeOrDate, asArray, detailPollMs } from '../lib/format'
import {
  issueRadarApi,
  type PullRequest, type TimelineEvent, type PrCheck, type DetailLabel,
  type PullDetailResponse,
  type RepoRef,
} from '../api'
import { commitUrlFor, userUrlFor, repoScopeKey } from '../lib/links'
import { providerTerms } from '../lib/links'

import { i18nT } from '../../../i18n/t'
import { fmtDateTime } from '../../../i18n/format'
/** A relative timestamp that flips to the absolute local date-time on click
 * (and shows it on hover). Renders nothing for a missing/unparseable value. */
function RelTime({ iso, className = '' }: { iso?: string | null; className?: string }) {
  const [abs, setAbs] = useState(false)
  if (!iso) return null
  const d = new Date(iso)
  if (isNaN(d.getTime())) return null
  const absolute = fmtDateTime(d)
  return (
    <span
      role="button"
      tabIndex={0}
      title={absolute}
      onClick={(e) => { e.stopPropagation(); setAbs((v) => !v) }}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setAbs((v) => !v) } }}
      className={`cursor-pointer rounded-sm hover:text-accent transition-colors focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 ${className}`}
    >
      {abs ? absolute : relativeTimeOrDate(iso)}
    </span>
  )
}

/** The PR state pill: merged (aim/purple) → closed-unmerged (danger) → draft
 * (muted) → open (ok/green), derived from state + draft + merged. */
function StatePill({ state, draft, merged }: { state: string; draft?: boolean; merged?: boolean }) {
  if (merged) {
    return (
      <span className="inline-flex items-center gap-1 text-[12px] font-medium px-2 py-0.5 rounded-full bg-aim-subtle text-aim">
        <GitMerge size={12} /> {i18nT('apps.issueRadar.components.prDetail.merged')}
      </span>
    )
  }
  if (state === 'closed') {
    return (
      <span className="inline-flex items-center gap-1 text-[12px] font-medium px-2 py-0.5 rounded-full bg-bg-elevated text-danger border border-border">
        <GitPullRequestClosed size={12} /> {i18nT('apps.issueRadar.components.prDetail.closed')}
      </span>
    )
  }
  if (draft) {
    return (
      <span className="inline-flex items-center gap-1 text-[12px] font-medium px-2 py-0.5 rounded-full bg-bg-elevated text-muted border border-border">
        <GitPullRequestDraft size={12} /> {i18nT('apps.issueRadar.components.prDetail.draft')}
      </span>
    )
  }
  return (
    <span className="inline-flex items-center gap-1 text-[12px] font-medium px-2 py-0.5 rounded-full bg-ok-subtle text-ok">
      <GitPullRequest size={12} /> {i18nT('apps.issueRadar.components.prDetail.open')}
    </span>
  )
}

/** Collapsed height for a COMMENT body — about three lines at the body's
 * 13px/~1.6 line-height. Comments start clamped to this so a wall-of-text review
 * never buries the timeline; clicking expands it ("Show less" collapses again).
 * The PR DESCRIPTION is deliberately exempt — it is the one body you always want
 * to read, so it renders in full. */
const COLLAPSED_BODY_PX = 66

/** A comment body clamped to ~3 lines with a click-to-toggle affordance.
 *
 * The clamp is a max-height + overflow-hidden rather than `line-clamp`, because
 * the body is rendered MARKDOWN: `-webkit-line-clamp` needs
 * `display:-webkit-box`, which collapses block children (paragraphs, lists, code
 * fences) into one flex line and mangles the layout. Max-height keeps normal
 * block flow and simply crops it.
 *
 * Takes the markdown STRING (not children) on purpose: the measure effect keys
 * off it, and a string is a stable dependency. Keying off `children` — a fresh
 * JSX object on every render — would re-run the effect on any unrelated
 * re-render (a react-query background refetch, a timestamp toggle) and silently
 * re-collapse a body the user had just expanded. Expanded state resets only when
 * the body itself actually changes (i.e. switching to a different PR). */
function CollapsibleBody({ body }: { body: string }) {
  const [expanded, setExpanded] = useState(false)
  const [overflowing, setOverflowing] = useState(false)
  const innerRef = useRef<HTMLDivElement>(null)

  // Measure after the markdown has rendered, and reset the toggle — but only
  // when the body text itself changed.
  useEffect(() => {
    const el = innerRef.current
    if (!el) return
    setOverflowing(el.scrollHeight > COLLAPSED_BODY_PX + 4)
    setExpanded(false)
  }, [body])

  const collapsed = overflowing && !expanded

  return (
    <div className="relative">
      <div
        ref={innerRef}
        style={collapsed ? { maxHeight: COLLAPSED_BODY_PX, overflow: 'hidden' } : undefined}
      >
        <RefMarkdown content={body} />
      </div>
      {collapsed && (
        <>
          {/* Fade the crop edge so it reads as "there is more", not as a cut. */}
          <div aria-hidden className="pointer-events-none absolute inset-x-0 bottom-0 h-7 bg-gradient-to-t from-card to-transparent" />
          <button
            onClick={() => setExpanded(true)}
            aria-label={i18nT('apps.issueRadar.components.prDetail.expand_comment')}
            aria-expanded={false}
            title={i18nT('apps.issueRadar.components.prDetail.click_to_expand')}
            className="absolute inset-0 w-full cursor-pointer bg-transparent"
          />
        </>
      )}
      {overflowing && (
        <button
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
          className="relative mt-1.5 inline-flex items-center gap-0.5 text-[11.5px] text-accent hover:underline cursor-pointer bg-transparent"
        >
          {expanded
            ? <>{i18nT('apps.issueRadar.components.prDetail.show_less')} <ChevronUp size={11} /></>
            : <>{i18nT('apps.issueRadar.components.prDetail.show_more')} <ChevronDown size={11} /></>}
        </button>
      )}
    </div>
  )
}

/** The opening post and every comment share this card (header + markdown).
 * The author carries the same identity badge as the issue pane (repo role, else
 * author_association).
 *
 * `opening` marks the PR DESCRIPTION: it renders in FULL, while every other
 * comment is clamped to ~3 lines and expands on click (see CollapsibleBody). */
function CommentCard({
  author, when, body, opening, role, assoc, repoRef,
}: {
  author: string | null; when?: string; body?: string; opening?: boolean
  role?: string | null; assoc?: string | null
  repoRef: RepoRef
}) {
  const terms = providerTerms(repoRef)
  const text = body?.trim() ? body : ''
  return (
    <div className="rounded-lg border border-border bg-card overflow-hidden">
      <div className="flex items-center gap-2 px-3.5 py-2 border-b border-border bg-bg-elevated/60 text-[12.5px] flex-wrap">
        <span className="font-semibold text-text-strong">{author ?? 'ghost'}</span>
        <MemberBadge role={role} assoc={assoc} />
        <span className="text-muted">
          {opening ? `opened this ${terms.changeRequestTitle}` : i18nT('apps.issueRadar.components.prDetail.commented')}
        </span>
        <span className="text-muted">· {when ? <RelTime iso={when} /> : ''}</span>
      </div>
      <div className="px-3.5 py-3">
        {!text
          ? <div className="text-[13px] text-muted italic">{i18nT('apps.issueRadar.components.prDetail.no_description_provided')}</div>
          : opening
            ? <RefMarkdown content={text} />
            : <CollapsibleBody body={text} />}
      </div>
    </div>
  )
}

/** A titled sidebar block, divider below (except the last). */
function Section({
  title, icon, children,
}: {
  title: string; icon?: React.ReactNode; children: React.ReactNode
}) {
  return (
    <div className="pb-3.5 mb-3.5 border-b border-border last:border-b-0 last:mb-0 last:pb-0">
      <div className="flex items-center gap-1.5 text-[11px] uppercase tracking-wider text-muted font-medium mb-2">
        {icon}{title}
      </div>
      {children}
    </div>
  )
}

/**
 * Catalog KEYS for the inline sentence a review event reads as ("<who> approved
 * these changes").
 *
 * Split out of `REVIEW_VISUAL` and kept FLAT on purpose: a nested
 * `REVIEW_VISUAL[state].verbKey` is not statically resolvable by
 * `scripts/check-i18n-keys.mjs`, so its keys could never be verified to exist.
 * Keys rather than strings because this table is evaluated at module load, where
 * an `i18nT()` call would freeze the boot language; the lookup happens in
 * `eventVisual()`, which runs during render.
 */
const REVIEW_VERB_KEY: Record<string, string> = {
  approved: 'apps.issueRadar.components.prDetail.approved_these_changes',
  changes_requested: 'apps.issueRadar.components.prDetail.requested_changes',
  commented: 'apps.issueRadar.components.prDetail.reviewed',
  dismissed: 'apps.issueRadar.components.prDetail.dismissed_a_review',
}

const REVIEW_VISUAL: Record<string, { Icon: LucideIcon; color: string }> = {
  approved: { Icon: CheckCircle2, color: 'text-ok' },
  changes_requested: { Icon: XCircle, color: 'text-danger' },
  commented: { Icon: MessageSquare, color: 'text-muted' },
  dismissed: { Icon: Eye, color: 'text-muted' },
}

/** One row of the timeline rail: [relative time | dot + connector | content]. */
function TimelineRow({
  time, icon, iconColor, connector, pulse = false, children,
}: {
  time?: string; icon: React.ReactNode; iconColor: string; connector: boolean
  pulse?: boolean; children: React.ReactNode
}) {
  return (
    <div className="grid" style={{ gridTemplateColumns: '64px 26px 1fr' }}>
      <div className="text-[11px] text-right text-muted pt-1.5 pr-1 leading-tight">
        {time ? <RelTime iso={time} /> : null}
      </div>
      <div className="flex flex-col items-center">
        <div className="relative shrink-0 w-[24px] h-[24px]">
          {pulse && <span aria-hidden className="absolute inset-0 rounded-full bg-accent/40 animate-ping motion-reduce:hidden" />}
          <div className={`relative w-[24px] h-[24px] rounded-full grid place-items-center border bg-bg-elevated ${pulse ? 'border-accent' : 'border-border'} ${iconColor}`}>
            {icon}
          </div>
        </div>
        {connector ? <div className="flex-1 w-px bg-border my-1" /> : <div className="flex-1" />}
      </div>
      <div className="min-w-0 pb-5 pt-0.5 pl-1">{children}</div>
    </div>
  )
}

/** Icon + colour + inline sentence for a non-comment PR timeline event. */
function eventVisual(
  ev: TimelineEvent, repoRef: RepoRef, colorByName: Map<string, string>,
  roleByLogin?: Map<string, string>,
): { Icon: LucideIcon; color: string; body: React.ReactNode } {
  const who = <span className="font-medium text-text">{ev.actor ?? 'someone'}</span>
  const commitUrl = ev.commit_id ? commitUrlFor(repoRef, ev.commit_id) : undefined
  const labelChip = ev.label
    ? <LabelChip name={ev.label.name} color={ev.label.color || colorByName.get(ev.label.name) || '888888'} small />
    : null

  switch (ev.kind) {
    case 'reviewed': {
      // `hasOwnProperty`, not a bare index with a `??` fallback: `review_state`
      // is provider data, so a value like `constructor` would otherwise resolve
      // to an inherited Object.prototype member — truthy, so the `??` never
      // fired, leaving `rv.Icon` undefined and crashing the row.
      const state = ev.review_state
        && Object.prototype.hasOwnProperty.call(REVIEW_VISUAL, ev.review_state)
        ? ev.review_state
        : 'commented'
      const rv = REVIEW_VISUAL[state]
      const reviewerRole = ev.actor ? roleByLogin?.get(ev.actor) ?? null : null
      return {
        Icon: rv.Icon, color: rv.color,
        body: (
          <>
            <div className="flex items-center gap-1.5 flex-wrap">
              {who}
              <MemberBadge role={reviewerRole} assoc={ev.author_association} />
              <span>{i18nT(REVIEW_VERB_KEY[state])}</span>
            </div>
            {/* A review's own text is a comment too — same 3-line clamp. */}
            {ev.body?.trim() && (
              <div className="mt-1.5 rounded-md border border-border bg-card px-3 py-2">
                <CollapsibleBody body={ev.body} />
              </div>
            )}
          </>
        ),
      }
    }
    case 'committed':
      return {
        Icon: GitCommitHorizontal, color: 'text-muted',
        body: (
          <>
            {who} {i18nT('apps.issueRadar.components.prDetail.added_a_commit')}
            {commitUrl && <> <a href={commitUrl} target="_blank" rel="noreferrer" className="font-mono text-accent hover:underline">{ev.commit_id!.slice(0, 7)}</a></>}
            {ev.message && <span className="text-muted"> — {ev.message}</span>}
          </>
        ),
      }
    case 'labeled':
      return { Icon: Tag, color: 'text-accent', body: <>{who} {i18nT('apps.issueRadar.components.prDetail.added_the')} {labelChip} {i18nT('apps.issueRadar.components.prDetail.label')}</> }
    case 'unlabeled':
      return { Icon: Tag, color: 'text-muted', body: <>{who} {i18nT('apps.issueRadar.components.prDetail.removed_the')} {labelChip} {i18nT('apps.issueRadar.components.prDetail.label')}</> }
    case 'assigned':
      return { Icon: UserPlus, color: 'text-muted', body: <>{who} {i18nT('apps.issueRadar.components.prDetail.assigned')} {ev.assignee ?? 'someone'}</> }
    case 'unassigned':
      return { Icon: UserMinus, color: 'text-muted', body: <>{who} {i18nT('apps.issueRadar.components.prDetail.unassigned')} {ev.assignee ?? 'someone'}</> }
    case 'closed':
      return {
        Icon: CircleSlash,
        color: 'text-danger',
        body: <>{who} {i18nT('apps.issueRadar.components.prDetail.closed_this')} {providerTerms(repoRef).changeRequestTitle}</>,
      }
    case 'reopened':
      return { Icon: CircleDot, color: 'text-ok', body: <>{who} {i18nT('apps.issueRadar.components.prDetail.reopened_this')}</> }
    case 'renamed':
      return {
        Icon: Pencil, color: 'text-muted',
        body: <>{who} {i18nT('apps.issueRadar.components.prDetail.changed_the_title')} <span className="line-through">{ev.rename?.from}</span> → <span className="text-text">{ev.rename?.to}</span></>,
      }
    case 'milestoned':
      return { Icon: MilestoneIcon, color: 'text-muted', body: <>{who} {i18nT('apps.issueRadar.components.prDetail.added_this_to_the')} <span className="font-medium text-text">{ev.milestone}</span> {i18nT('apps.issueRadar.components.prDetail.milestone')}</> }
    case 'cross-referenced':
      return {
        Icon: Link2, color: 'text-accent',
        body: (
          <>
            {who} {i18nT('apps.issueRadar.components.prDetail.referenced_this_in')}{' '}
            <a href={safeHttpUrl(ev.source?.url ?? '') ?? undefined} target="_blank" rel="noreferrer" className="text-accent hover:underline">
              {ev.source?.is_pr ? providerTerms(repoRef).changeRequestShort : 'issue'}
              {providerTerms(repoRef).sigil}{ev.source?.number}
            </a>
          </>
        ),
      }
    default:
      return { Icon: CircleDot, color: 'text-muted', body: <>{who} {ev.kind}</> }
  }
}

/** Per-bucket visual for a check row. Failures read as danger, in-flight runs as
 * accent (with a spinner), successes/other stay quiet. */
const CHECK_VISUAL: Record<PrCheck['bucket'], { Icon: LucideIcon; color: string }> = {
  failure: { Icon: XCircle, color: 'text-danger' },
  running: { Icon: Loader2, color: 'text-accent' },
  success: { Icon: CheckCircle2, color: 'text-ok' },
  other: { Icon: CircleSlash, color: 'text-muted' },
}

/** One check row: status glyph + the check NAME, nothing else — the sidebar is a
 * scan surface, so a second line of conclusion/summary text per row would bury
 * the signal (which checks are red / still running). The conclusion, summary and
 * reporting app live in the row's tooltip, and the row links out to the full run
 * when the provider gave a details URL. */
function CheckRow({ check }: { check: PrCheck }) {
  const { Icon, color } = CHECK_VISUAL[check.bucket]
  const inner = (
    <>
      <Icon
        size={12}
        className={`flex-shrink-0 ${color} ${check.bucket === 'running' ? 'animate-spin' : ''}`}
      />
      <span className="min-w-0 flex-1 text-[12px] text-text leading-snug truncate">{check.name}</span>
    </>
  )
  const title = [check.name, check.app, check.conclusion ?? check.status, check.summary]
    .filter(Boolean).join(' · ')
  // A check's URL comes from the reporting GitHub App (legacy statuses supply an
  // arbitrary target_url), so it is UNTRUSTED: a `javascript:` value would become
  // a clickable script-execution vector in the dashboard's origin. Only validated
  // http(s) URLs get a link; anything else renders as plain text.
  const href = check.url ? safeHttpUrl(check.url) : null
  return href ? (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      title={title}
      className="group flex items-center gap-1.5 rounded-md px-1 -mx-1 py-1 no-underline hover:bg-bg-hover transition-colors"
    >
      {inner}
    </a>
  ) : (
    <div title={title} className="flex items-center gap-1.5 px-1 -mx-1 py-1">{inner}</div>
  )
}

/** The "Auto review" sidebar section: everything that reviewed this PR
 * automatically — CI jobs, Checks-API review bots, legacy commit statuses.
 *
 * Deliberately asymmetric, because passing checks carry no information a
 * reviewer needs to act on: FAILING and STILL-RUNNING checks are written out in
 * full (name + conclusion + summary + link), while successes collapse into a
 * single "N passed" row that expands on click. Informational outcomes
 * (neutral / skipped / cancelled) collapse with them. */
/** A stable, UNIQUE React key for a check row.
 *
 * The name alone is not safe: when two rows share it (the same workflow started
 * twice for one head sha), the colliding keys make React unable to remove the
 * stale rows on the next render — which shows up as a group heading counting 4
 * while 6 rows are painted below it. The check-run URL disambiguates real rows;
 * the index is the last-resort tiebreaker. */
function checkKey(c: PrCheck, i: number): string {
  return `${c.name ?? ''}|${c.url ?? ''}|${i}`
}

function AutoReviewChecks(
  { checks, loading, failed, children }: {
    checks: PrCheck[]; loading: boolean; failed: boolean
    /** The CI run controls (PrRunActions), rendered under the rows — the remedy
     * belongs with the status it acts on. */
    children?: React.ReactNode
  },
) {
  const [showPassed, setShowPassed] = useState(false)
  const failing = checks.filter((c) => c.bucket === 'failure')
  const running = checks.filter((c) => c.bucket === 'running')
  const quiet = checks.filter((c) => c.bucket === 'success' || c.bucket === 'other')
  const passedCount = quiet.filter((c) => c.bucket === 'success').length
  const otherCount = quiet.length - passedCount

  return (
    <Section title={i18nT('apps.issueRadar.components.prDetail.auto_review')} icon={<ShieldCheck size={12} />}>
      {loading && checks.length === 0 && (
        <span className="inline-flex items-center gap-1.5 text-muted">
          <Loader2 size={12} className="animate-spin flex-shrink-0 text-accent" />
          {i18nT('apps.issueRadar.components.prDetail.loading_checks')}
        </span>
      )}
      {/* "Could not read" is NOT "none": reporting no checks after a failed fetch
          would hide failing CI behind a reassuring sentence. */}
      {!loading && checks.length === 0 && (
        <span className={failed ? 'text-warn' : 'text-muted'}>
          {failed ? i18nT('apps.issueRadar.components.prDetail.check_status_unavailable') : i18nT('apps.issueRadar.components.prDetail.no_automated_checks')}
        </span>
      )}

      {failing.length > 0 && (
        <div className="mb-2">
          <div className="text-[10.5px] uppercase tracking-wider text-danger font-medium mb-1">
            {failing.length} {i18nT('apps.issueRadar.components.prDetail.failing')}
          </div>
          <div className="flex flex-col">
            {failing.map((c, i) => <CheckRow key={checkKey(c, i)} check={c} />)}
          </div>
        </div>
      )}

      {running.length > 0 && (
        <div className="mb-2">
          <div className="text-[10.5px] uppercase tracking-wider text-accent font-medium mb-1">
            {running.length} {i18nT('apps.issueRadar.components.prDetail.running')}
          </div>
          <div className="flex flex-col">
            {running.map((c, i) => <CheckRow key={checkKey(c, i)} check={c} />)}
          </div>
        </div>
      )}

      {quiet.length > 0 && (
        <div>
          <button
            onClick={() => setShowPassed((v) => !v)}
            aria-expanded={showPassed}
            className="w-full flex items-center gap-1 text-muted hover:text-text cursor-pointer bg-transparent"
          >
            {showPassed ? <ChevronUp size={11} /> : <ChevronDown size={11} />}
            {/* Same typographic treatment as the "N failing" / "N running" group
                headings above, and the same colour coding — green for passing,
                so the three counts read as one set. No status icon: the colour
                already carries the state, and the chevron is the affordance. */}
            <span className="text-[10.5px] uppercase tracking-wider font-medium text-ok">
              {passedCount} {i18nT('apps.issueRadar.components.prDetail.passed')}{otherCount > 0 ? ` · ${otherCount} skipped` : ''}
            </span>
          </button>
          {showPassed && (
            <div className="flex flex-col mt-1">
              {quiet.map((c, i) => <CheckRow key={checkKey(c, i)} check={c} />)}
            </div>
          )}
        </div>
      )}
      {children}
    </Section>
  )
}

export default function PrDetail({ pull }: { pull: PullRequest }) {
  const { active, colorByName, memberRoleByLogin, canWrite, refreshPrefs } = useIssueRadar()
  const scopeKey = repoScopeKey(active)
  // GitLab calls these merge requests; the whole pane's copy follows the ref.
  const terms = providerTerms(active)

  const [copied, setCopied] = useState(false)
  const copyLink = async () => {
    try {
      await navigator.clipboard.writeText(detail?.url ?? pull.url)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch { /* clipboard unavailable */ }
  }

  const queryClient = useQueryClient()
  const refreshRef = useRef(false)
  const detailKey = ['issue-radar', 'pull', scopeKey, pull.number]
  // The lifecycle the POLL rate is derived from: whatever the pane last read, not
  // the list row it was opened from (which can be minutes old by then).
  const cachedDetail = queryClient.getQueryData<PullDetailResponse>(detailKey)?.detail
  const lifecycleMergedAt = cachedDetail?.merged_at ?? pull.merged_at
  const lifecycleState = cachedDetail?.state ?? pull.state
  const detailQuery = useQuery({
    queryKey: detailKey,
    queryFn: () => {
      // A plain GET is enough: the backend serves its detail cache only while it
      // is younger than PR_DETAIL_CACHE_TTL_SEC, so freshness is the route's job
      // rather than something this component has to force with refresh=1 on every
      // poll. The header button still forces a read on demand.
      const useRefresh = refreshRef.current
      refreshRef.current = false
      return issueRadarApi.pullDetail(active, pull.number, { refresh: useRefresh })
    },
    // Derived from the LATEST detail when it has arrived, falling back to the list
    // row: a PR merged elsewhere while the pane is open must start backing off
    // rather than keep polling every 30s against a frozen PR.
    refetchInterval: detailPollMs(
      !lifecycleMergedAt && lifecycleState !== 'closed', refreshPrefs.detailPollMs,
    ),
    refetchIntervalInBackground: refreshPrefs.pollInBackground,
  })
  const refreshDetail = () => { refreshRef.current = true; detailQuery.refetch() }

  // Push the freshly-read check state onto this PR's row in whichever cached
  // list holds it (plain list and search results both). Without this the card
  // keeps whatever the last LIST refresh computed, so a check that turns red in
  // the sidebar stays green on the card until the whole list is refetched — and
  // refetching a 50-PR list to update one row is the wrong trade. The backend
  // patches its own cache the same way, so the fix survives a reload too.
  const summary = detailQuery.data?.checks_summary
  useEffect(() => {
    if (!summary) return
    // Scoped to owner/repo: the prefix alone would match every cached repo, and
    // patchRow matches on PR number only — so opening repo A's #7 would overwrite
    // repo B's #7.
    queryClient.setQueriesData<{ pulls?: PullRequest[] }>(
      { queryKey: ['issue-radar', 'pulls', scopeKey] }, patchRow,
    )
    queryClient.setQueriesData<{ pulls?: PullRequest[] }>(
      { queryKey: ['issue-radar', 'pulls-search', scopeKey] }, patchRow,
    )
    function patchRow(old: { pulls?: PullRequest[] } | undefined) {
      if (!old?.pulls?.some((p) => p.number === pull.number)) return old
      return {
        ...old,
        pulls: old.pulls.map((p) => (
          p.number === pull.number
            ? {
              ...p,
              checks_counts: summary!.checks_counts,
              checks_state: summary!.checks_state,
              checks_truncated: summary!.checks_truncated ?? false,
            }
            : p
        )),
      }
    }
  }, [summary, pull.number, scopeKey, queryClient])

  const detail = detailQuery.data?.detail
  const timeline = asArray<TimelineEvent>(detailQuery.data?.timeline)
  const checks = asArray<PrCheck>(detailQuery.data?.checks)

  // AI summary: description + whole conversation + check state, in one model
  // call. Deliberately gated on the detail query — /pull-ai reads the PR detail
  // cache that /pull writes, so waiting means it reuses those bytes instead of
  // making its own duplicate round of `gh` calls on first open.
  //
  // The key is the PR IDENTITY only, deliberately NOT its updated_at: the pane
  // re-reads the PR every DETAIL_POLL_MS, and keying on updated_at would make
  // every new comment or push silently spend a model call while you sit on the
  // page. So the summary is generated once per opened PR and refreshed only when
  // you ask (the card's regenerate button), with its age shown so you can tell
  // whether it predates the latest activity.
  const aiRefreshRef = useRef(false)
  const aiQuery = useQuery({
    queryKey: ['issue-radar', 'pull-ai', scopeKey, pull.number],
    queryFn: () => {
      const useRefresh = aiRefreshRef.current
      aiRefreshRef.current = false
      return issueRadarApi.pullAi(active, pull.number, { refresh: useRefresh })
    },
    enabled: Boolean(detail),
    // The server owns freshness (its input fingerprint decides whether a request
    // costs a model call), so this never refetches on its own — the 30s detail
    // poll must not silently spend model calls while you sit on the page. But
    // RE-OPENING the pane is an explicit revisit, so it does ask again: the
    // fingerprint then returns the unchanged summary cheaply, or a fresh one if
    // the PR moved. Without this a summary could never catch up short of the
    // manual regenerate button.
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    refetchOnMount: 'always',
  })
  const regenerateAi = () => { aiRefreshRef.current = true; aiQuery.refetch() }

  // Prefer live detail, fall back to the instant list row for first paint.
  const state = detail?.state ?? pull.state ?? 'open'
  const draft = detail?.draft ?? pull.draft ?? false
  const merged = detail?.merged ?? !!pull.merged_at
  const body = detail?.body ?? pull.body ?? ''
  const author = detail?.author ?? pull.author ?? null
  const createdAt = detail?.created_at ?? pull.created_at
  const updatedAt = detail?.updated_at ?? pull.updated_at
  const base = detail?.base ?? pull.base ?? null
  const head = detail?.head ?? pull.head ?? null
  const assignees = asArray<string>(detail?.assignees ?? pull.assignees)
  const reviewers = asArray<string>(detail?.requested_reviewers ?? pull.requested_reviewers)
  const labelObjs: DetailLabel[] = asArray<DetailLabel>(detail?.labels).length
    ? asArray<DetailLabel>(detail?.labels)
    : asArray<string>(pull.labels).map((n) => ({ name: n, color: colorByName.get(n) ?? '888888', description: '' }))
  const milestone = detail?.milestone ?? null
  // Fall back to the LIST row's enrichment rather than to zero: when the detail
  // read has not landed (or failed) "0 files, +0 −0" would be a confident claim
  // that the PR changes nothing. `null` here means genuinely unknown.
  const additions = detail?.additions ?? pull.additions ?? null
  const deletions = detail?.deletions ?? pull.deletions ?? null
  const changedFiles = detail?.changed_files ?? pull.changed_files ?? null
  const commits = detail?.commits ?? null
  // What an unavailable metric renders as. "—" beats a zero, which would read as a
  // measured value.
  const unknownValue = <span className="text-muted">—</span>
  const mergeableState = detail?.mergeable_state ?? null
  // Identity, resolved exactly as the issue pane does: the authoritative member
  // roster wins (Admin/Maintainer/…), falling back to the per-PR
  // author_association (which is the only source of first-timer/contributor).
  const authorRole = author ? memberRoleByLogin.get(author) ?? null : null
  const association = detail?.author_association ?? pull.author_association ?? null

  const activityLoading = detailQuery.isLoading
  // No row to paint from and no detail yet — i.e. opened from a cross-reference.
  // Every field would otherwise render a fabricated value ("someone opened",
  // "ghost", "No description provided") for the length of one fetch, so show the
  // SHAPE of the content instead. A pane opened from the list never takes this
  // path: its row is the first paint.
  const awaitingFirstPaint = !detail && !pull.title
  // Surfaced even when react-query still holds the PREVIOUS payload: after a
  // failed poll or refresh it keeps that data, so gating on `!data` would leave
  // stale timeline and check rows on screen with nothing saying they are stale.
  const activityError = (detailQuery.error as Error | null) ?? null
  const activityStale = Boolean(detailQuery.error && detailQuery.data)

  // The newest activity the pane knows about. Checks are included because a check
  // finishing does NOT bump the PR's updated_at, and the summary reports check
  // state — so CI turning red after it was written is exactly when it misleads.
  const generatedAt = aiQuery.data?.generated_at ?? null
  const newestActivity = [
    detail?.updated_at ?? '',
    ...checks.map((c) => c.completed_at || c.started_at || ''),
  ].reduce((a, b) => (b > a ? b : a), '')
  const staleSummarySince =
    generatedAt && newestActivity && newestActivity > generatedAt ? newestActivity : null

  // Timeline: comments pinned nowhere (the opening post is pinned separately);
  // render every non-comment/comment event NEWEST-FIRST, latest node pulsing.
  const activityDesc = [...timeline].reverse()

  // The row handed to the child ACTIONS (review) — the live title/body when they
  // have arrived, for the same reason as IssueDetail's actionIssue: a pane opened
  // from a cross-reference starts from a placeholder row.
  const actionPull: PullRequest = { ...pull, title: detail?.title ?? pull.title, body }

  return (
    <article className="h-full flex flex-col">
      {/* ── Header (does not scroll) ── */}
      <header className="px-6 pt-5 pb-4 border-b border-border">
        <div className="flex items-start gap-3">
          <div className="flex-1 min-w-0">
            {awaitingFirstPaint ? <HeaderSkeleton /> : (<>
            <h1 className="text-[27px] font-bold leading-tight text-text-strong break-words">
              {detail?.title ?? pull.title}
            </h1>
            <div className="flex items-center gap-2 mt-3 flex-wrap text-[12.5px] text-muted">
              <StatePill state={state} draft={draft} merged={merged} />
              <span className="inline-flex items-center gap-1">
                <button
                  onClick={copyLink}
                  title={copied ? i18nT('apps.issueRadar.components.prDetail.link_copied') : i18nT('apps.issueRadar.components.prDetail.copy_link_to_this', { subject: terms.changeRequestTitle })}
                  aria-label={i18nT('apps.issueRadar.components.prDetail.copy_link_to_this', { subject: terms.changeRequestTitle })}
                  className="inline-flex items-center -ml-0.5 p-0.5 cursor-pointer bg-transparent text-muted hover:text-accent"
                >
                  {copied ? <Check size={13} className="text-ok" /> : <Copy size={13} />}
                </button>
                <a href={safeHttpUrl(detail?.url ?? pull.url ?? '') ?? undefined} target="_blank" rel="noreferrer" title={`Open on ${terms.providerName}`} className="font-mono text-muted hover:text-accent hover:underline">
                  #{pull.number}
                </a>
              </span>
              <MemberBadge role={authorRole} assoc={association} />
              <span>
                {author ? <span className="text-text font-medium">{author}</span> : 'someone'} {i18nT('apps.issueRadar.components.prDetail.opened')}{' '}
                {createdAt ? <RelTime iso={createdAt} /> : ''}
              </span>
            </div>
            </>)}
          </div>
          <div className="flex-shrink-0 flex items-center gap-1.5">
            {/* Same reason as IssueDetail's Investigate: the review seed prompt
                names the PR by title. */}
            {!awaitingFirstPaint && <ReviewButton repoRef={active} pull={actionPull} />}
            <button
              onClick={refreshDetail}
              disabled={detailQuery.isFetching}
              aria-label={i18nT('apps.issueRadar.components.prDetail.refresh_details', { subject: terms.changeRequestTitle })}
              title={i18nT('apps.issueRadar.components.prDetail.re_fetch_this_and_its_timeline_from', { subject: terms.changeRequestShort, provider: terms.providerName })}
              className="inline-flex items-center text-muted hover:text-text disabled:opacity-30 cursor-pointer bg-transparent p-1"
            >
              <RefreshCw size={14} className={detailQuery.isFetching ? 'animate-spin' : ''} />
            </button>
          </div>
        </div>

        {/* Actions on their own row rather than beside Review: the review/comment
            composer needs the full header width, and a row that grows in place
            keeps the title from reflowing when it opens. */}
        {!awaitingFirstPaint && (
          <div className="mt-3">
            <PrActionsBar
              repoRef={active}
              pull={actionPull}
              detail={detail}
              canWrite={canWrite}
            />
          </div>
        )}
      </header>

      {/* ── Scroll area: main column + sidebar ── */}
      <div className="flex-1 min-h-0 overflow-hidden">
        <div className="flex gap-6 px-6 py-5 h-full items-stretch">
          <main className="flex-1 min-w-0 overflow-y-auto scrollbar-none" style={{ scrollbarWidth: 'none' }}>
            <AiSummaryCard
              summary={aiQuery.data?.summary ?? ''}
              fromCache={aiQuery.data?.from_cache ?? false}
              // Still "loading" before the detail lands: the card is waiting on
              // the gated query, not idle.
              loading={aiQuery.isLoading || (!detail && !detailQuery.isError)}
              fetching={aiQuery.isFetching}
              // Surface a failed refetch too: react-query keeps the previous data
              // on a refetch error, so gating on !data would silently redisplay
              // the stale summary as if the regenerate had worked.
              error={(aiQuery.error as Error | null) ?? null}
              onRegenerate={regenerateAi}
              generatedAt={aiQuery.data?.generated_at ?? null}
              // The summary never regenerates on its own (see the query above), so
              // when the PR has moved since it was written, say so rather than
              // presenting it as current. Check transitions count as movement and
              // do NOT touch the PR's updated_at, so the newest check timestamp is
              // compared too — the summary names failing checks, so CI going red
              // after it was written is exactly when it misleads most.
              staleSince={staleSummarySince}
              subject={terms.changeRequestTitle}
            />

            {/* Description — pinned to the top, NOT on the timeline. */}
            <div className="mb-6">
              {awaitingFirstPaint
                ? <CommentCardSkeleton />
                : (
                  <CommentCard
                    opening
                    author={author}
                    when={createdAt}
                    body={body}
                    role={authorRole}
                    assoc={association}
                    repoRef={active}
                  />
                )}
            </div>

            {/* Activity timeline — newest first, latest node pulsing. */}
            <div className="flex items-center gap-1.5 text-[11px] uppercase tracking-wider text-muted mb-3 font-medium">
              <CircleDot size={12} /> {i18nT('apps.issueRadar.components.prDetail.timeline')}
              <span className="text-muted normal-case tracking-normal opacity-70">{i18nT('apps.issueRadar.components.prDetail.newest_first')}</span>
            </div>

            {activityDesc.map((ev, i) => {
              const isOldest = i === activityDesc.length - 1
              const isNewest = i === 0
              if (ev.kind === 'comment' || ev.kind === 'review_comment') {
                // An inline review comment is a comment that also says WHERE it
                // was made; the file:line is the part that makes it actionable.
                const anchor = ev.kind === 'review_comment' && ev.path
                  ? `${ev.path}${ev.line ? `:${ev.line}` : ''}`
                  : null
                return (
                  <TimelineRow
                    key={i}
                    time={ev.created_at}
                    icon={ev.kind === 'review_comment' ? <FileDiff size={13} /> : <MessageSquare size={13} />}
                    iconColor="text-muted"
                    connector={!isOldest}
                    pulse={isNewest}
                  >
                    {anchor && (
                      <div className="mb-1 text-[11px] font-mono text-muted truncate" title={anchor}>
                        {anchor}
                      </div>
                    )}
                    <CommentCard
                      repoRef={active}
                      author={ev.actor}
                      when={ev.created_at}
                      body={ev.body}
                      role={ev.actor ? memberRoleByLogin.get(ev.actor) ?? null : null}
                      assoc={ev.author_association}
                    />
                  </TimelineRow>
                )
              }
              const { Icon, color, body: evBody } = eventVisual(ev, active, colorByName, memberRoleByLogin)
              return (
                <TimelineRow key={i} time={ev.created_at} icon={<Icon size={13} />} iconColor={color} connector={!isOldest} pulse={isNewest}>
                  <div className="text-[12.5px] text-muted leading-snug pt-1">{evBody}</div>
                </TimelineRow>
              )
            })}

            {activityLoading && <TimelineSkeleton />}
            {activityError && (
              <div className={`py-2 text-[12px] ${activityStale ? 'text-warn' : 'text-danger'}`}>
                {activityStale
                  ? i18nT('apps.issueRadar.components.prDetail.showing_the_last_successful_read', { error: activityError.message })
                  : i18nT('apps.issueRadar.components.prDetail.couldnt_load_activity', { error: activityError.message })}
              </div>
            )}
            {!activityLoading && !activityError && activityDesc.length === 0 && (
              <div className="py-2 text-[12px] text-muted">{i18nT('apps.issueRadar.components.prDetail.no_activity_yet')}</div>
            )}
          </main>

          {/* Sidebar — the most useful PR metadata. */}
          <aside className="w-[236px] flex-shrink-0 overflow-y-auto scrollbar-none text-[12.5px]" style={{ scrollbarWidth: 'none' }}>
            {/* Auto review first — failing/running checks are the most
                actionable thing on a PR. */}
            <AutoReviewChecks
              checks={checks}
              loading={detailQuery.isLoading}
              failed={Boolean(detailQuery.error)}
            >
              {/* Cancel / re-run controls live INSIDE the Auto review block: they
                  act on the same CI the rows above report, so splitting them into
                  their own section would separate the status from its remedy. */}
              <PrRunActions
                repoRef={active}
                number={pull.number}
                headSha={detail?.head_sha ?? null}
                canWrite={canWrite}
                live={!lifecycleMergedAt && lifecycleState !== 'closed'}
              />
            </AutoReviewChecks>

            <Section title={i18nT('apps.issueRadar.components.prDetail.reviewers')} icon={<Users size={12} />}>
              {reviewers.length > 0 ? (
                <div className="flex flex-col gap-1">
                  {reviewers.map((a) => (
                    <a key={a} href={userUrlFor(active, a)} target="_blank" rel="noreferrer" className="text-text hover:text-accent hover:underline truncate">
                      {a}
                    </a>
                  ))}
                </div>
              ) : <span className="text-muted">{i18nT('apps.issueRadar.components.prDetail.no_reviewers_requested')}</span>}
            </Section>

            <Section title={i18nT('apps.issueRadar.components.prDetail.assignees')} icon={<Users size={12} />}>
              {assignees.length > 0 ? (
                <div className="flex flex-col gap-1">
                  {assignees.map((a) => (
                    <a key={a} href={userUrlFor(active, a)} target="_blank" rel="noreferrer" className="text-text hover:text-accent hover:underline truncate">
                      {a}
                    </a>
                  ))}
                </div>
              ) : <span className="text-muted">{i18nT('apps.issueRadar.components.prDetail.no_one_assigned')}</span>}
            </Section>

            <Section title={i18nT('apps.issueRadar.components.prDetail.labels')} icon={<Tag size={12} />}>
              {labelObjs.length > 0 ? (
                <div className="flex items-center gap-1.5 flex-wrap">
                  {labelObjs.map((l) => <LabelChip key={l.name} name={l.name} color={l.color} />)}
                </div>
              ) : <span className="text-muted">{i18nT('apps.issueRadar.components.prDetail.none_yet')}</span>}
            </Section>

            <Section title={i18nT('apps.issueRadar.components.prDetail.milestone_2')} icon={<MilestoneIcon size={12} />}>
              {milestone ? (
                <span className="inline-flex items-center gap-1.5 text-text">
                  {milestone.title}
                  <span className="text-[10.5px] text-muted">({milestone.state})</span>
                </span>
              ) : <span className="text-muted">{i18nT('apps.issueRadar.components.prDetail.no_milestone')}</span>}
            </Section>

            <Section title={i18nT('apps.issueRadar.components.prDetail.branches')} icon={<GitBranch size={12} />}>
              {base || head ? (
                <div className="flex flex-col gap-1 font-mono text-[11.5px]">
                  <div className="flex items-baseline gap-1.5">
                    <span className="text-muted font-body flex-shrink-0">{i18nT('apps.issueRadar.components.prDetail.into')}</span>
                    <span className="text-text break-all">{base ?? '—'}</span>
                  </div>
                  <div className="flex items-baseline gap-1.5">
                    <span className="text-muted font-body flex-shrink-0">{i18nT('apps.issueRadar.components.prDetail.from')}</span>
                    <span className="text-text break-all">{head ?? '—'}</span>
                  </div>
                </div>
              ) : <span className="text-muted">{i18nT('apps.issueRadar.components.prDetail.unknown_branches')}</span>}
            </Section>

            <Section title={i18nT('apps.issueRadar.components.prDetail.changes')} icon={<FileDiff size={12} />}>
              <dl className="space-y-1">
                <div className="flex justify-between gap-2">
                  <dt className="text-muted">{i18nT('apps.issueRadar.components.prDetail.commits')}</dt>
                  <dd className="text-text tabular-nums">{commits ?? unknownValue}</dd>
                </div>
                <div className="flex justify-between gap-2">
                  <dt className="text-muted">{i18nT('apps.issueRadar.components.prDetail.files')}</dt>
                  <dd className="text-text tabular-nums">{changedFiles ?? unknownValue}</dd>
                </div>
                <div className="flex justify-between gap-2">
                  <dt className="text-muted">{i18nT('apps.issueRadar.components.prDetail.diff')}</dt>
                  <dd className="tabular-nums">
                    {additions === null && deletions === null
                      ? unknownValue
                      : (
                        <>
                          <span className="text-ok">+{additions ?? 0}</span>{' '}
                          <span className="text-danger">−{deletions ?? 0}</span>
                        </>
                      )}
                  </dd>
                </div>
                {mergeableState && !merged && state !== 'closed' && (
                  <div className="flex justify-between gap-2">
                    <dt className="text-muted">{i18nT('apps.issueRadar.components.prDetail.mergeable')}</dt>
                    <dd className="text-text capitalize">{mergeableState.replace(/_/g, ' ')}</dd>
                  </div>
                )}
              </dl>
            </Section>

            <Section title={i18nT('apps.issueRadar.components.prDetail.dates')} icon={<CalendarDays size={12} />}>
              <dl className="space-y-1">
                <div className="flex justify-between gap-2">
                  <dt className="text-muted">{i18nT('apps.issueRadar.components.prDetail.opened_2')}</dt>
                  <dd className="text-text">{createdAt ? <RelTime iso={createdAt} /> : '—'}</dd>
                </div>
                <div className="flex justify-between gap-2">
                  <dt className="text-muted">{i18nT('apps.issueRadar.components.prDetail.updated')}</dt>
                  <dd className="text-text">{updatedAt ? <RelTime iso={updatedAt} /> : '—'}</dd>
                </div>
                {detail?.merged_at && (
                  <div className="flex justify-between gap-2">
                    <dt className="text-muted">{i18nT('apps.issueRadar.components.prDetail.merged')}</dt>
                    <dd className="text-text">
                      <RelTime iso={detail.merged_at} />{detail.merged_by ? ` · ${detail.merged_by}` : ''}
                    </dd>
                  </div>
                )}
                {detail?.closed_at && !detail?.merged_at && (
                  <div className="flex justify-between gap-2">
                    <dt className="text-muted">{i18nT('apps.issueRadar.components.prDetail.closed')}</dt>
                    <dd className="text-text"><RelTime iso={detail.closed_at} /></dd>
                  </div>
                )}
              </dl>
            </Section>
          </aside>
        </div>
      </div>
    </article>
  )
}
