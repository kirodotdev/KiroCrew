// Right column: the full-width issue detail pane.
//
// Layout fills the whole column: a non-scrolling header (large title over a
// meta row carrying a copy-link button + the #number linking out to GitHub +
// state + author, plus close/reopen + refresh actions) over a scroll area
// split into a main column (an AI triage summary card, then the issue's
// original DESCRIPTION pinned at the top, an optional "Linked pull requests &
// issues" section lifted out of the timeline, and finally the activity TIMELINE
// — comments + events on a single dot-and-rail activity
// timeline, ordered NEWEST-FIRST with the latest node pulsing) and a right
// sidebar of the most triage-useful GitHub metadata (assignees, editable
// labels + AI suggestions, milestone, reactions, dates).
//
// Instant paint comes from the list `issue` (title / state / author / body /
// labels / assignees); the richer fields (reactions, milestone, association,
// closed_by, and the whole timeline) stream in from GET /issue and are cached.
// The AI summary + suggested labels stream in from GET /issue-ai (one model
// call, cache-first). Label edits and close/reopen are the write half of the
// suggest→confirm loop and are gated on the repo's triage/push access
// (`canWrite`); a read-only repo degrades to suggest-only.
import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { motion, useReducedMotion } from 'framer-motion'
import {
  Copy, Check, RefreshCw, CircleDot, CircleCheck, CircleSlash, MessageSquare,
  Tag, UserPlus, UserMinus, Pencil, Milestone as MilestoneIcon, GitPullRequest,
  GitCommitHorizontal, Link2, Users, CalendarDays, Lock, Sparkles,
  Plus, ChevronDown, Loader2,
  ThumbsUp, ThumbsDown, Laugh, PartyPopper, Frown, Heart, Rocket, Eye,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import RefMarkdown from './RefMarkdown'
import { parseRepoRef } from '../lib/refLinks'
import { safeHttpUrl } from '../../../lib/safeUrl'
import { CommentCardSkeleton, HeaderSkeleton, TimelineSkeleton } from './DetailSkeleton'
import AiSummaryCard from './AiSummaryCard'
import Clickable from '../../../components/Clickable'
import LabelChip from './LabelChip'
import LabelPicker from './LabelPicker'
import MemberBadge from './MemberBadge'
import InvestigateButton from './InvestigateButton'
import { useIssueRadar } from '../context'
import { relativeTimeOrDate, hexToRgba, asArray, detailPollMs } from '../lib/format'
import {
  issueRadarApi,
  type Issue, type Reactions, type TimelineEvent, type DetailLabel,
  type SuggestedLabel, type IssueDetailResponse, type IssuesResponse,
  type RepoRef,
} from '../api'
import { commitUrlFor, userUrlFor, repoScopeKey } from '../lib/links'
import { providerTerms } from '../lib/links'

import { i18nT } from '../../../i18n/t'
import { fmtDateTime, fmtDateTimeNumeric } from '../../../i18n/format'
/** A relative timestamp that flips to the absolute local date-time when
 * clicked (and always shows it on hover). Within the last 24h it reads
 * "just now / 12m ago / 3h ago"; older it reads "Yesterday / 5 days ago / …".
 * Renders nothing for a missing or unparseable timestamp. */
function RelTime({ iso, className = '' }: { iso?: string | null; className?: string }) {
  const [abs, setAbs] = useState(false)
  if (!iso) return null
  const d = new Date(iso)
  if (isNaN(d.getTime())) return null
  const absolute = fmtDateTime(d)
  const toggle = () => setAbs((v) => !v)
  return (
    <span
      role="button"
      tabIndex={0}
      title={absolute}
      onClick={(e) => { e.stopPropagation(); toggle() }}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle() } }}
      className={`cursor-pointer rounded-sm hover:text-accent transition-colors focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 ${className}`}
    >
      {abs ? absolute : relativeTimeOrDate(iso)}
    </span>
  )
}

const REACTION_ICONS: [keyof Reactions, LucideIcon][] = [
  ['plus1', ThumbsUp], ['minus1', ThumbsDown], ['laugh', Laugh], ['hooray', PartyPopper],
  ['confused', Frown], ['heart', Heart], ['rocket', Rocket], ['eyes', Eye],
]

/** A compact strip of the reactions that actually have a count (>0). */
function ReactionStrip({ reactions }: { reactions: Reactions }) {
  const items = REACTION_ICONS.filter(([k]) => (reactions[k] as number) > 0)
  if (items.length === 0) return null
  return (
    <div className="flex items-center gap-1.5 flex-wrap mt-2.5">
      {items.map(([k, Icon]) => (
        <span key={k} className="inline-flex items-center gap-1 text-[12px] px-1.5 py-0.5 rounded-full border border-border bg-bg-elevated text-muted">
          <Icon size={12} className="lucide-inline" /> {reactions[k] as number}
        </span>
      ))}
    </div>
  )
}

/** The open/closed pill. Closed splits on state_reason: completed (accent-ish
 * purple) vs "not planned" (muted). */
function StatePill({ state, reason }: { state?: string; reason?: string | null }) {
  if (state !== 'closed') {
    return (
      <span className="inline-flex items-center gap-1 text-[12px] font-medium px-2 py-0.5 rounded-full bg-accent-subtle text-accent">
        <CircleDot size={12} /> {i18nT('apps.issueRadar.components.issueDetail.open')}
      </span>
    )
  }
  if (reason === 'not_planned') {
    return (
      <span className="inline-flex items-center gap-1 text-[12px] font-medium px-2 py-0.5 rounded-full bg-bg-elevated text-muted border border-border">
        <CircleSlash size={12} /> {i18nT('apps.issueRadar.components.issueDetail.closed_as_not_planned')}
      </span>
    )
  }
  return (
    <span className="inline-flex items-center gap-1 text-[12px] font-medium px-2 py-0.5 rounded-full bg-aim-subtle text-aim">
      <CircleCheck size={12} /> {i18nT('apps.issueRadar.components.issueDetail.closed')}
    </span>
  )
}

/** Header close/reopen control. Only rendered when the user can write. When the
 * issue is open, "Close" opens a tiny menu to pick the close reason (completed
 * vs not planned, matching GitHub); when closed, a single "Reopen" button. */
function StateActions({
  state, pending, onClose, onReopen,
}: {
  state: string
  pending: boolean
  onClose: (reason: 'completed' | 'not_planned') => void
  onReopen: () => void
}) {
  const [menu, setMenu] = useState(false)
  const btn =
    'inline-flex items-center gap-1 text-[12px] px-2 py-1 rounded-md border border-border ' +
    'text-muted hover:text-text hover:border-accent/50 disabled:opacity-40 disabled:cursor-default ' +
    'cursor-pointer bg-transparent whitespace-nowrap'

  if (state === 'closed') {
    return (
      <button onClick={onReopen} disabled={pending} title={i18nT('apps.issueRadar.components.issueDetail.reopen_this_issue')} className={btn}>
        {pending ? <Loader2 size={13} className="animate-spin" /> : <CircleDot size={13} className="text-ok" />} {i18nT('apps.issueRadar.components.issueDetail.reopen')}
      </button>
    )
  }
  return (
    <div className="relative">
      <button onClick={() => setMenu((v) => !v)} disabled={pending} title={i18nT('apps.issueRadar.components.issueDetail.close_this_issue')} className={btn}>
        {pending ? <Loader2 size={13} className="animate-spin" /> : <CircleCheck size={13} />} {i18nT('apps.issueRadar.components.issueDetail.close')} <ChevronDown size={12} />
      </button>
      {menu && (
        <>
          <Clickable className="fixed inset-0 z-10" aria-label={i18nT('apps.issueRadar.components.issueDetail.dismiss_menu')} onClick={() => setMenu(false)} />
          <div className="absolute right-0 mt-1 z-20 w-48 rounded-md border border-border bg-card shadow-lg py-1 text-[12.5px]">
            <button
              onClick={() => { setMenu(false); onClose('completed') }}
              className="w-full text-left px-3 py-1.5 hover:bg-bg-elevated flex items-center gap-2 cursor-pointer bg-transparent text-text"
            >
              <CircleCheck size={13} className="text-aim" /> {i18nT('apps.issueRadar.components.issueDetail.close_as_completed')}
            </button>
            <button
              onClick={() => { setMenu(false); onClose('not_planned') }}
              className="w-full text-left px-3 py-1.5 hover:bg-bg-elevated flex items-center gap-2 cursor-pointer bg-transparent text-text"
            >
              <CircleSlash size={13} className="text-muted" /> {i18nT('apps.issueRadar.components.issueDetail.close_as_not_planned')}
            </button>
          </div>
        </>
      )}
    </div>
  )
}

/** AI-suggested labels (those not already applied), rendered in the Labels
 * sidebar as dashed sparkle chips. Each has a one-click accept (＋) when the
 * user can write; on a read-only repo they show as suggestions only. */
function AiSuggestions({
  suggestions, loading, canWrite, pending, onAccept, colorByName,
}: {
  suggestions: SuggestedLabel[]
  loading: boolean
  canWrite: boolean
  pending: boolean
  onAccept: (name: string) => void
  colorByName: Map<string, string>
}) {
  const reduce = useReducedMotion()
  if (loading) {
    return (
      <div className="mt-3 text-[11px] text-muted flex items-center gap-1.5">
        <Sparkles size={11} className="text-accent flex-shrink-0" />
        <Loader2 size={11} className="animate-spin flex-shrink-0" /> {i18nT('apps.issueRadar.components.issueDetail.finding_suggestions')}
      </div>
    )
  }
  if (suggestions.length === 0) return null
  return (
    <div className="mt-3">
      <div className="flex items-center gap-1 text-[10.5px] uppercase tracking-wider text-accent mb-1.5 font-medium">
        <Sparkles size={11} className="animate-pulse" /> {i18nT('apps.issueRadar.components.issueDetail.suggested')}
      </div>
      <div className="flex flex-wrap gap-1.5">
        {suggestions.map((s, i) => {
          const color = colorByName.get(s.name) ?? '888888'
          const tip = canWrite
            ? (s.reason
              ? i18nT('apps.issueRadar.components.issueDetail.add_with_reason', { name: s.name, reason: s.reason })
              : i18nT('apps.issueRadar.components.issueDetail.add', { name: s.name }))
            : (s.reason || i18nT('apps.issueRadar.components.issueDetail.read_only_connect_with_triage_push_access_to_app_2'))
          return (
            <motion.button
              key={s.name}
              initial={reduce ? false : { opacity: 0, y: 4 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.25, ease: 'easeOut', delay: reduce ? 0 : i * 0.05 }}
              whileHover={canWrite && !reduce ? { y: -1 } : undefined}
              onClick={() => onAccept(s.name)}
              disabled={!canWrite || pending}
              title={tip}
              style={{
                backgroundColor: hexToRgba(color, 0.14),
                borderColor: hexToRgba(color, 0.5),
                color: 'var(--text)',
              }}
              className="group relative inline-flex items-center gap-1 max-w-full rounded-full px-2 py-0.5 text-[12px] border border-dashed cursor-pointer overflow-hidden disabled:cursor-default"
            >
              {/* A gentle drift in the label's OWN colour — subtle, not flashy. */}
              {!reduce && (
                <span
                  aria-hidden
                  className="pointer-events-none absolute inset-0 animate-shimmer"
                  style={{
                    backgroundImage: `linear-gradient(90deg, transparent, ${hexToRgba(color, 0.22)}, transparent)`,
                    backgroundSize: '200% 100%',
                  }}
                />
              )}
              {canWrite
                ? <Plus size={11} className="relative flex-shrink-0" style={{ color: `#${color}` }} />
                : <Sparkles size={10} className="relative flex-shrink-0" style={{ color: `#${color}` }} />}
              <span className="relative truncate">{s.name}</span>
            </motion.button>
          )
        })}
      </div>
      {!canWrite && (
        <div className="text-[10.5px] text-muted mt-1.5">{i18nT('apps.issueRadar.components.issueDetail.read_only_connect_with_triage_push_access_to_app')}</div>
      )}
    </div>
  )
}

/** One row of the timeline rail: [relative time | dot + connector | content].
 * `pulse` marks the newest row — its dot gets an accent border and an
 * animate-ping halo (suppressed under prefers-reduced-motion) so, with the
 * timeline ordered newest-first, the eye lands on the most recent activity. */
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
          {pulse && (
            <span
              aria-hidden="true"
              className="absolute inset-0 rounded-full bg-accent/40 animate-ping motion-reduce:hidden"
            />
          )}
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

/** The opening post and every comment share this card (header + markdown). */
function CommentCard({
  author, when, assoc, role, body, reactions, opening,
}: {
  author: string | null; when?: string; assoc?: string | null; role?: string | null; body?: string
  reactions?: Reactions | null; opening?: boolean
}) {
  return (
    <div className="rounded-lg border border-border bg-card overflow-hidden">
      <div className="flex items-center gap-2 px-3.5 py-2 border-b border-border bg-bg-elevated/60 text-[12.5px] flex-wrap">
        <span className="font-semibold text-text-strong">{author ?? 'ghost'}</span>
        <MemberBadge role={role} assoc={assoc} />
        <span className="text-muted">{opening ? i18nT('apps.issueRadar.components.issueDetail.opened_this_issue') : i18nT('apps.issueRadar.components.issueDetail.commented')}</span>
        <span className="text-muted">· {when ? <RelTime iso={when} /> : ''}</span>
      </div>
      <div className="px-3.5 py-3">
        {body?.trim()
          ? <RefMarkdown content={body} />
          : <div className="text-[13px] text-muted italic">{i18nT('apps.issueRadar.components.issueDetail.no_description_provided')}</div>}
        {reactions && <ReactionStrip reactions={reactions} />}
      </div>
    </div>
  )
}

/** Icon + colour + inline sentence for a non-comment timeline event. */
function eventVisual(
  ev: TimelineEvent, repoRef: RepoRef, colorByName: Map<string, string>,
): { Icon: LucideIcon; color: string; body: React.ReactNode } {
  const who = <span className="font-medium text-text">{ev.actor ?? 'someone'}</span>
  const person = (login?: string | null) => <span className="font-medium text-text">{login ?? 'someone'}</span>
  const commitUrl = ev.commit_id ? commitUrlFor(repoRef, ev.commit_id) : undefined
  const labelChip = ev.label
    ? <LabelChip name={ev.label.name} color={ev.label.color || colorByName.get(ev.label.name) || '888888'} small />
    : null

  switch (ev.kind) {
    case 'labeled':
      return { Icon: Tag, color: 'text-accent', body: <>{who} {i18nT('apps.issueRadar.components.issueDetail.added_the')} {labelChip} {i18nT('apps.issueRadar.components.issueDetail.label')}</> }
    case 'unlabeled':
      return { Icon: Tag, color: 'text-muted', body: <>{who} {i18nT('apps.issueRadar.components.issueDetail.removed_the')} {labelChip} {i18nT('apps.issueRadar.components.issueDetail.label')}</> }
    case 'assigned':
      return {
        Icon: UserPlus, color: 'text-muted',
        body: ev.actor && ev.actor === ev.assignee
          ? <>{who} {i18nT('apps.issueRadar.components.issueDetail.self_assigned_this')}</>
          : <>{who} {i18nT('apps.issueRadar.components.issueDetail.assigned')} {person(ev.assignee)}</>,
      }
    case 'unassigned':
      return { Icon: UserMinus, color: 'text-muted', body: <>{who} {i18nT('apps.issueRadar.components.issueDetail.unassigned')} {person(ev.assignee)}</> }
    case 'closed': {
      const notPlanned = ev.state_reason === 'not_planned'
      return {
        Icon: notPlanned ? CircleSlash : CircleCheck,
        color: notPlanned ? 'text-muted' : 'text-aim',
        body: (
          <>
            {who} {i18nT('apps.issueRadar.components.issueDetail.closed_this')} {notPlanned ? i18nT('apps.issueRadar.components.issueDetail.as_not_planned') : i18nT('apps.issueRadar.components.issueDetail.as_completed')}
            {commitUrl && <> {i18nT('apps.issueRadar.components.issueDetail.in')} <a href={commitUrl} target="_blank" rel="noreferrer" className="font-mono text-accent hover:underline">{ev.commit_id!.slice(0, 7)}</a></>}
          </>
        ),
      }
    }
    case 'reopened':
      return { Icon: CircleDot, color: 'text-ok', body: <>{who} {i18nT('apps.issueRadar.components.issueDetail.reopened_this')}</> }
    case 'renamed':
      return {
        Icon: Pencil, color: 'text-muted',
        body: <>{who} {i18nT('apps.issueRadar.components.issueDetail.changed_the_title')} <span className="line-through">{ev.rename?.from}</span> → <span className="text-text">{ev.rename?.to}</span></>,
      }
    case 'milestoned':
      return { Icon: MilestoneIcon, color: 'text-muted', body: <>{who} {i18nT('apps.issueRadar.components.issueDetail.added_this_to_the')} <span className="font-medium text-text">{ev.milestone}</span> {i18nT('apps.issueRadar.components.issueDetail.milestone')}</> }
    case 'demilestoned':
      return { Icon: MilestoneIcon, color: 'text-muted', body: <>{who} {i18nT('apps.issueRadar.components.issueDetail.removed_this_from_the')} <span className="font-medium text-text">{ev.milestone}</span> {i18nT('apps.issueRadar.components.issueDetail.milestone')}</> }
    case 'cross-referenced':
      return {
        Icon: ev.source?.is_pr ? GitPullRequest : Link2, color: 'text-accent',
        body: (
          <>
            {who} {i18nT('apps.issueRadar.components.issueDetail.referenced_this_in')}{' '}
            <a href={safeHttpUrl(ev.source?.url ?? '') ?? undefined} target="_blank" rel="noreferrer" className="text-accent hover:underline">
              {ev.source?.is_pr ? providerTerms(repoRef).changeRequestShort : 'issue'}
              {providerTerms(repoRef).sigil}{ev.source?.number}
            </a>{' '}
            <span className="text-muted">{ev.source?.title}</span>
          </>
        ),
      }
    case 'referenced':
      return {
        Icon: GitCommitHorizontal, color: 'text-muted',
        body: <>{who} {i18nT('apps.issueRadar.components.issueDetail.referenced_this_in_commit')}{' '}
          {commitUrl
            ? <a href={commitUrl} target="_blank" rel="noreferrer" className="font-mono text-accent hover:underline">{ev.commit_id!.slice(0, 7)}</a>
            : <span className="font-mono">{ev.commit_id?.slice(0, 7)}</span>}
        </>,
      }
    default:
      return { Icon: CircleDot, color: 'text-muted', body: <>{who} {ev.kind}</> }
  }
}

/** One linked issue/PR reference (a GitHub cross-reference), lifted out of the
 * timeline into the dedicated "Linked" section. */
interface RelatedRef {
  number: number
  title: string
  url: string
  state: string
  is_pr: boolean
  actor: string | null
  created_at: string
}

/** Icon tint for a linked ref by state — an open target reads live (accent),
 * anything closed / merged reads muted. */
function refStateTint(state: string): string {
  return state === 'open' ? 'text-accent' : 'text-muted'
}

/** The "Linked pull requests & issues" section: every OTHER issue/PR that
 * cross-references this one, pulled off the timeline into its own block so
 * related work is obvious at a glance.
 *
 * A row into the ACTIVE repo opens in the in-app reference sheet, exactly like a
 * reference clicked in the body (see RefLink). A cross-reference from a DIFFERENT
 * repo — which the timeline does surface — keeps opening on its own provider,
 * because the detail panes are bound to the active repo's labels, roster and
 * permissions. */
function RelatedLinks({ items }: { items: RelatedRef[] }) {
  const { active, openRef } = useIssueRadar()
  const terms = providerTerms(active)
  return (
    <section className="mb-6">
      <div className="flex items-center gap-1.5 text-[11px] uppercase tracking-wider text-muted mb-3 font-medium">
        <Link2 size={12} /> {i18nT('apps.issueRadar.components.issueDetail.linked')} {terms.changeRequestPlural} {i18nT('apps.issueRadar.components.issueDetail.issues')}
        <span className="text-muted normal-case tracking-normal opacity-70">· {items.length}</span>
      </div>
      <div className="flex flex-col gap-1.5">
        {items.map((r) => {
          const kind = r.is_pr ? terms.changeRequestShort : 'issue'
          const sigil = r.is_pr ? terms.sigil : '#'
          const target = parseRepoRef(r.url, active)
          return (
            <a
              key={r.url}
              href={safeHttpUrl(r.url) ?? undefined}
              target="_blank"
              rel="noreferrer"
              title={target
                ? `Open ${kind} ${sigil}${r.number} here`
                : `Open ${kind} ${sigil}${r.number} on ${terms.providerName}`}
              onClick={target
                ? (e) => {
                  // Modified clicks stay the browser's: open-in-new-tab must work.
                  if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
                  e.preventDefault()
                  openRef(target)
                }
                : undefined}
              className="group flex items-start gap-2.5 rounded-lg border border-border bg-card px-3.5 py-2.5 no-underline hover:border-accent/50 transition-colors"
            >
              <span className={`mt-0.5 flex-shrink-0 ${refStateTint(r.state)}`}>
                {r.is_pr ? <GitPullRequest size={15} /> : <CircleDot size={15} />}
              </span>
              <span className="min-w-0 flex-1">
                <span className="block text-[13px] text-text group-hover:text-accent leading-snug line-clamp-2 break-words">
                  {r.title || `${r.is_pr ? terms.changeRequestTitle : i18nT('apps.issueRadar.components.issueDetail.issue')} ${sigil}${r.number}`}
                </span>
                <span className="mt-0.5 flex items-center gap-1.5 flex-wrap text-[11.5px] text-muted">
                  <span className="font-mono">
                    {r.is_pr ? terms.changeRequestShort : i18nT('apps.issueRadar.components.issueDetail.issue')}{sigil}{r.number}
                  </span>
                  {r.state && <span>· {r.state}</span>}
                  {r.actor && <span>{i18nT('apps.issueRadar.components.issueDetail.by')} {r.actor}</span>}
                  {r.created_at && (
                    <span title={fmtDateTimeNumeric(r.created_at)}>· {relativeTimeOrDate(r.created_at)}</span>
                  )}
                </span>
              </span>
            </a>
          )
        })}
      </div>
    </section>
  )
}

/** A titled sidebar block, divider below (except the last). Optional `action`
 * is rendered right-aligned in the header row (e.g. the labels "Edit" toggle). */
function Section({
  title, icon, action, children,
}: {
  title: string; icon?: React.ReactNode; action?: React.ReactNode; children: React.ReactNode
}) {
  return (
    <div className="pb-3.5 mb-3.5 border-b border-border last:border-b-0 last:mb-0 last:pb-0">
      <div className="flex items-center justify-between gap-2 mb-2">
        <div className="flex items-center gap-1.5 text-[11px] uppercase tracking-wider text-muted font-medium">
          {icon}{title}
        </div>
        {action}
      </div>
      {children}
    </div>
  )
}

export default function IssueDetail({ issue }: { issue: Issue }) {
  const {
    active, colorByName, memberRoleByLogin, repoLabels, countByLabel, canWrite, stateFilter,
    refreshPrefs,
  } = useIssueRadar()
  const { owner, repo } = active
  const scopeKey = repoScopeKey(active)
  // GitLab calls a change request a merge request, and its CLI is `glab` — the
  // pane's own copy follows the active repo's provider.
  const terms = providerTerms(active)
  const queryClient = useQueryClient()

  // Reset transient per-issue UI (label edit mode, close menu) when the pane
  // switches to a different issue (the component instance is reused).
  const [editingLabels, setEditingLabels] = useState(false)
  useEffect(() => { setEditingLabels(false) }, [issue.number])

  // Copy-link affordance (the #number links out to GitHub; this copies the URL
  // to the clipboard). Brief check-mark feedback, then reverts. Reads the URL off
  // the live detail when it has arrived: a pane opened from a cross-reference
  // starts from a PLACEHOLDER row whose url is synthesized, and GitHub's own url
  // is the one worth putting on the clipboard.
  const [copied, setCopied] = useState(false)
  const copyLink = async () => {
    try {
      await navigator.clipboard.writeText(detail?.url ?? issue.url)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      /* clipboard unavailable (blocked / insecure context) — no-op */
    }
  }

  // refreshRef lets the header refresh button force a server re-fetch
  // (?refresh=1) through react-query's normal refetch(), without a second
  // query key. Cleared inside queryFn so only that one refetch bypasses cache.
  const refreshRef = useRef(false)
  // False until this pane has fetched the CURRENT item once; see queryFn.
  const fetchedOnceRef = useRef(false)
  useEffect(() => { fetchedOnceRef.current = false }, [owner, repo, issue.number])
  const detailKey = ['issue-radar', 'issue', scopeKey, issue.number]
  // The lifecycle the POLL rate is derived from: whatever the pane last read,
  // falling back to the list row. A pane opened from a cross-reference starts
  // from a placeholder row with no real state, and any list row can be minutes
  // stale by the time it is opened.
  const cachedState = queryClient.getQueryData<IssueDetailResponse>(detailKey)?.detail?.state
  const detailQuery = useQuery({
    queryKey: detailKey,
    queryFn: () => {
      // Cache-first on the FIRST fetch after opening (instant paint from the
      // server's cache); every later fetch — the poll below, a window-focus
      // refetch, or the header button — forces a real GitHub read, because the
      // backend detail cache has no TTL and would otherwise never move.
      const useRefresh = refreshRef.current || fetchedOnceRef.current
      refreshRef.current = false
      fetchedOnceRef.current = true
      return issueRadarApi.issueDetail(active, issue.number, { refresh: useRefresh })
    },
    // A CLOSED issue backs off by an order of magnitude: only late commentary can
    // still arrive, and each poll costs a fully-paginated timeline read.
    refetchInterval: detailPollMs(
      (cachedState ?? issue.state) !== 'closed', refreshPrefs.detailPollMs,
    ),
    refetchIntervalInBackground: refreshPrefs.pollInBackground,
  })
  const refreshDetail = () => { refreshRef.current = true; detailQuery.refetch() }

  // AI triage (summary + suggested labels): fires on open, cache-first
  // server-side (one model call per issue, served instantly on re-open). The
  // regenerate button forces a recompute via ?refresh=1 (same ref trick).
  const aiRefreshRef = useRef(false)
  const aiQuery = useQuery({
    queryKey: ['issue-radar', 'issue-ai', scopeKey, issue.number],
    queryFn: () => {
      const useRefresh = aiRefreshRef.current
      aiRefreshRef.current = false
      return issueRadarApi.issueAi(active, issue.number, { refresh: useRefresh })
    },
    // Wait for the detail read to land first (mirrors PrDetail's aiQuery). The AI
    // route derives its summary from the issue detail, and on a COLD open firing
    // both at once made the server fetch the detail TWICE — once for /issue and
    // again inside /issue-ai, which missed the not-yet-written detail cache. Gating
    // on the detail lets /issue-ai read the warm cache, saving one gh round-trip per
    // first-open. Warm re-opens hit the AI cache and never reach this path.
    enabled: Boolean(detailQuery.data?.detail),
    staleTime: Infinity,  // server owns freshness; don't refetch on window focus
  })
  const regenerateAi = () => { aiRefreshRef.current = true; aiQuery.refetch() }

  const detail = detailQuery.data?.detail
  const timeline = asArray<TimelineEvent>(detailQuery.data?.timeline)

  // Prefer live detail, fall back to the instant list row for first paint.
  const state = detail?.state ?? issue.state ?? 'open'
  const body = detail?.body ?? issue.body ?? ''
  const author = detail?.author ?? issue.author ?? null
  const createdAt = detail?.created_at ?? issue.created_at
  const assignees = asArray<string>(detail?.assignees ?? issue.assignees)
  // Authoritative label objects (full colour/description) — from live detail if
  // present, else synthesized from the list row's names + the repo colour map.
  const currentLabelObjs: DetailLabel[] = asArray<DetailLabel>(detail?.labels).length
    ? asArray<DetailLabel>(detail?.labels)
    : asArray<string>(issue.labels).map((n) => ({ name: n, color: colorByName.get(n) ?? '888888', description: '' }))
  const currentNames = currentLabelObjs.map((l) => l.name)
  const labelList = currentLabelObjs.map((l) => ({ name: l.name, color: l.color }))
  // Detail-only fields hoisted to locals so TS narrows them cleanly (accessing
  // `detail.x` inside a `detail?.x && …` guard otherwise trips strictNullChecks).
  const stateReason = detail?.state_reason ?? null
  // Author's repo role, from the authoritative member roster — the badge shows
  // this (Admin/Maintainer/…) when the author is a member. It resolves
  // instantly from the cached roster, independent of the per-issue fetch.
  const authorRole = author ? memberRoleByLogin.get(author) ?? null : null
  // Per-issue author_association is the FALLBACK signal used when the author
  // isn't in the roster — notably first-timer / contributor, which the roster
  // does not carry. Prefer live detail, then the instant list row.
  const association = detail?.author_association ?? issue.author_association ?? null
  const reactions = detail?.reactions ?? null
  const milestone = detail?.milestone ?? null
  const closedAt = detail?.closed_at ?? null
  const closedBy = detail?.closed_by ?? null
  const updatedAt = detail?.updated_at ?? issue.updated_at
  const locked = detail?.locked ?? false

  const activityLoading = detailQuery.isLoading
  // No row to paint from and no detail yet — i.e. opened from a cross-reference,
  // where every field would otherwise render a fabricated value ("someone
  // opened", "No description provided") for the length of one fetch. Show the
  // SHAPE of the content instead. A pane opened from the list has its row and
  // never takes this path.
  const awaitingFirstPaint = !detail && !issue.title
  // The close/reopen control acts on `state`, which falls back to 'open' when
  // nothing authoritative has arrived. A pane opened from a cross-reference to a
  // CLOSED issue would therefore offer "Close as completed" and let that write
  // overwrite the issue's existing state_reason. Withhold the control until the
  // state is known (from the detail, or from the reference summary that seeded the
  // placeholder row).
  const stateKnown = !!(detail?.state ?? issue.state)
  const activityError = !detailQuery.data ? (detailQuery.error as Error | null) : null

  // The original issue description is pinned to the top (below), so the timeline
  // rail carries ACTIVITY ONLY. Cross-references (links from other issues/PRs)
  // are lifted into their own "Linked" section, deduped by target URL. Whatever
  // remains renders on the rail NEWEST-FIRST (reversed), latest node pulsing.
  const relatedRefs: RelatedRef[] = (() => {
    const seen = new Set<string>()
    const out: RelatedRef[] = []
    for (const ev of timeline) {
      if (ev.kind !== 'cross-referenced' || !ev.source) continue
      const s = ev.source
      if (!s.url || s.number == null || seen.has(s.url)) continue
      seen.add(s.url)
      out.push({
        number: s.number, title: s.title ?? '', url: s.url,
        state: s.state ?? '', is_pr: !!s.is_pr,
        actor: ev.actor, created_at: ev.created_at,
      })
    }
    return out
  })()
  const activityDesc = timeline
    .filter((ev) => ev.kind !== 'cross-referenced')
    .reverse()

  // ── writes: label apply + close/reopen ──
  // Both patch the detail cache (so the pane is instantly correct) and the
  // issues-list cache (so the middle column + dashboards stay in step) without
  // forcing a slow full re-fetch; the backend patches its own on-disk caches to
  // match, so the change survives a reload / repo switch.
  const labelMutation = useMutation({
    mutationFn: (vars: { add: string[]; remove: string[] }) =>
      issueRadarApi.applyLabels(active, issue.number, vars.add, vars.remove),
    onSuccess: (res) => {
      queryClient.setQueryData<IssueDetailResponse>(detailKey, (old) =>
        old ? { ...old, detail: { ...old.detail, labels: res.labels } } : old)
      const names = res.labels.map((l) => l.name)
      queryClient.setQueryData<IssuesResponse>(
        ['issue-radar', 'issues', scopeKey, stateFilter],
        (old) => old
          ? { ...old, issues: old.issues.map((i) => i.number === issue.number ? { ...i, labels: names } : i) }
          : old,
      )
    },
  })
  const toggleLabel = (name: string) => {
    if (!canWrite || labelMutation.isPending) return
    if (currentNames.includes(name)) labelMutation.mutate({ add: [], remove: [name] })
    else labelMutation.mutate({ add: [name], remove: [] })
  }
  const acceptSuggestion = (name: string) => {
    if (!canWrite || labelMutation.isPending) return
    labelMutation.mutate({ add: [name], remove: [] })
  }

  const stateMutation = useMutation({
    mutationFn: (vars: { state: 'open' | 'closed'; reason?: 'completed' | 'not_planned' }) =>
      issueRadarApi.setIssueState(active, issue.number, vars.state, vars.reason),
    onSuccess: (res) => {
      queryClient.setQueryData<IssueDetailResponse>(detailKey, (old) =>
        old ? { ...old, detail: { ...old.detail, state: res.state, state_reason: res.state_reason } } : old)
      // Patch the row's state in the open/closed list caches rather than
      // invalidating: an invalidate would drop the just-closed issue from the
      // open list, blanking this detail pane (Workspace renders detail only
      // while activeIssue resolves from the list). Keeping the row visible
      // (with its new state) until the next manual refresh matches GitHub's own
      // behaviour; the backend has already dropped it from the list it left, so
      // a refresh reconciles cleanly.
      for (const sf of ['open', 'closed'] as const) {
        queryClient.setQueryData<IssuesResponse>(
          ['issue-radar', 'issues', scopeKey, sf],
          (old) => old
            ? { ...old, issues: old.issues.map((i) => i.number === issue.number ? { ...i, state: res.state } : i) }
            : old,
        )
      }
    },
  })

  // The row handed to the child ACTIONS (investigate) — the live title/body when
  // they have arrived. A pane opened from a cross-reference starts from a
  // placeholder row, and the seed prompt names the issue it is about.
  const actionIssue: Issue = { ...issue, title: detail?.title ?? issue.title, body }

  // Suggested labels not already on the issue (accepted ones drop out as the
  // detail cache updates). Kept to labels that still exist on the repo.
  const suggestions = asArray<SuggestedLabel>(aiQuery.data?.suggested_labels).filter((s) => !currentNames.includes(s.name))

  return (
    <article className="h-full flex flex-col">
      {/* ── Header (does not scroll) ── */}
      <header className="px-6 pt-5 pb-4 border-b border-border">
        <div className="flex items-start gap-3">
          <div className="flex-1 min-w-0">
            {awaitingFirstPaint ? <HeaderSkeleton /> : (<>
            <h1 className="text-[27px] font-bold leading-tight text-text-strong break-words">
              {detail?.title ?? issue.title}
            </h1>
            <div className="flex items-center gap-2 mt-3 flex-wrap text-[12.5px] text-muted">
              <StatePill state={state} reason={stateReason} />
              {/* Copy-link + issue number, sitting right after the state pill.
                  The copy button writes the URL to the clipboard; the #number
                  itself links out to the provider. */}
              <span className="inline-flex items-center gap-1">
                <button
                  onClick={copyLink}
                  title={copied ? i18nT('apps.issueRadar.components.issueDetail.link_copied') : i18nT('apps.issueRadar.components.issueDetail.copy_link_to_this_issue')}
                  aria-label={i18nT('apps.issueRadar.components.issueDetail.copy_link_to_this_issue')}
                  className="inline-flex items-center -ml-0.5 p-0.5 cursor-pointer bg-transparent text-muted hover:text-accent"
                >
                  {copied ? <Check size={13} className="text-ok" /> : <Copy size={13} />}
                </button>
                <a
                  href={safeHttpUrl(detail?.url ?? issue.url ?? '') ?? undefined}
                  target="_blank"
                  rel="noreferrer"
                  title={`Open on ${terms.providerName}`}
                  className="font-mono text-muted hover:text-accent hover:underline"
                >
                  #{issue.number}
                </a>
              </span>
              <MemberBadge role={authorRole} assoc={association} />
              <span>
                {author ? <span className="text-text font-medium">{author}</span> : 'someone'} {i18nT('apps.issueRadar.components.issueDetail.opened')}{' '}
                {createdAt ? <RelTime iso={createdAt} /> : ''}
              </span>
              {locked && <span className="inline-flex items-center gap-1 text-warn"><Lock size={12} /> {i18nT('apps.issueRadar.components.issueDetail.locked')}</span>}
            </div>
            </>)}
          </div>
          <div className="flex-shrink-0 flex items-center gap-1.5">
            {/* Withheld until the pane has something real to describe: the seed
                prompt names the issue by title, so firing it from a placeholder
                row would persist a session titled "#0" with a malformed context
                line. */}
            {!awaitingFirstPaint && <InvestigateButton repoRef={active} issue={actionIssue} />}
            {canWrite && stateKnown && (
              <StateActions
                state={state}
                pending={stateMutation.isPending}
                onClose={(reason) => stateMutation.mutate({ state: 'closed', reason })}
                onReopen={() => stateMutation.mutate({ state: 'open' })}
              />
            )}
            <button
              onClick={refreshDetail}
              disabled={detailQuery.isFetching}
              aria-label={i18nT('apps.issueRadar.components.issueDetail.refresh_issue_details')}
              title={i18nT('apps.issueRadar.components.issueDetail.re_fetch_issue_and_timeline_from', { provider: terms.providerName })}
              className="inline-flex items-center text-muted hover:text-text disabled:opacity-30 cursor-pointer bg-transparent p-1"
            >
              <RefreshCw size={14} className={detailQuery.isFetching ? 'animate-spin' : ''} />
            </button>
          </div>
        </div>
        {stateMutation.isError && (
          <div className="mt-2 text-[12px] text-danger">
            {(stateMutation.error as Error).message}
          </div>
        )}
      </header>

      {/* ── Scroll area: timeline + sidebar ── */}
      <div className="flex-1 min-h-0 overflow-hidden">
        <div className="flex gap-6 px-6 py-5 h-full items-stretch">
          {/* Main column — AI summary, the pinned description, linked refs,
              then the activity timeline (newest-first). */}
          <main className="flex-1 min-w-0 overflow-y-auto scrollbar-none" style={{ scrollbarWidth: 'none' }}>
            <AiSummaryCard
              summary={aiQuery.data?.summary ?? ''}
              fromCache={aiQuery.data?.from_cache ?? false}
              loading={aiQuery.isLoading}
              fetching={aiQuery.isFetching}
              error={!aiQuery.data ? (aiQuery.error as Error | null) : null}
              onRegenerate={regenerateAi}
              generatedAt={aiQuery.data?.generated_at ?? null}
            />

            {/* Original description — pinned to the top, NOT on the timeline. */}
            <div className="mb-6">
              {awaitingFirstPaint ? <CommentCardSkeleton /> : <CommentCard
                opening
                author={author}
                when={createdAt}
                assoc={association}
                role={authorRole}
                body={body}
                reactions={reactions}
              />}
            </div>

            {/* Linked PRs / issues — their own section, lifted off the rail. */}
            {relatedRefs.length > 0 && <RelatedLinks items={relatedRefs} />}

            {/* Activity timeline — newest first, latest node pulsing. */}
            <div className="flex items-center gap-1.5 text-[11px] uppercase tracking-wider text-muted mb-3 font-medium">
              <CircleDot size={12} /> {i18nT('apps.issueRadar.components.issueDetail.timeline')}
              <span className="text-muted normal-case tracking-normal opacity-70">{i18nT('apps.issueRadar.components.issueDetail.newest_first')}</span>
            </div>

            {activityDesc.map((ev, i) => {
              const isOldest = i === activityDesc.length - 1
              const isNewest = i === 0
              if (ev.kind === 'comment') {
                return (
                  <TimelineRow key={i} time={ev.created_at} icon={<MessageSquare size={13} />} iconColor="text-muted" connector={!isOldest} pulse={isNewest}>
                    <CommentCard author={ev.actor} when={ev.created_at} assoc={ev.author_association} role={ev.actor ? memberRoleByLogin.get(ev.actor) ?? null : null} body={ev.body} reactions={ev.reactions ?? null} />
                  </TimelineRow>
                )
              }
              const { Icon, color, body: evBody } = eventVisual(ev, active, colorByName)
              return (
                <TimelineRow key={i} time={ev.created_at} icon={<Icon size={13} />} iconColor={color} connector={!isOldest} pulse={isNewest}>
                  <div className="text-[12.5px] text-muted leading-snug pt-1">{evBody}</div>
                </TimelineRow>
              )
            })}

            {activityLoading && <TimelineSkeleton />}
            {activityError && (
              <div className="py-2 text-[12px] text-danger">{i18nT('apps.issueRadar.components.issueDetail.couldn_t_load_activity')} {activityError.message}</div>
            )}
            {!activityLoading && !activityError && activityDesc.length === 0 && (
              <div className="py-2 text-[12px] text-muted">{i18nT('apps.issueRadar.components.issueDetail.no_activity_yet')}</div>
            )}
          </main>

          {/* Sidebar — most triage-useful GitHub metadata. */}
          <aside className="w-[236px] flex-shrink-0 overflow-y-auto scrollbar-none text-[12.5px]" style={{ scrollbarWidth: 'none' }}>
            <Section title={i18nT('apps.issueRadar.components.issueDetail.assignees')} icon={<Users size={12} />}>
              {assignees.length > 0 ? (
                <div className="flex flex-col gap-1">
                  {assignees.map((a) => (
                    <a key={a} href={userUrlFor(active, a)} target="_blank" rel="noreferrer" className="text-text hover:text-accent hover:underline truncate">
                      {a}
                    </a>
                  ))}
                </div>
              ) : (
                <span className="text-muted">{i18nT('apps.issueRadar.components.issueDetail.no_one_assigned')}</span>
              )}
            </Section>

            <Section
              title={i18nT('apps.issueRadar.components.issueDetail.labels')}
              icon={<Tag size={12} />}
              action={canWrite && repoLabels.length > 0 ? (
                <button
                  onClick={() => setEditingLabels((v) => !v)}
                  className="inline-flex items-center gap-1 text-[11px] text-muted hover:text-accent cursor-pointer bg-transparent"
                >
                  {editingLabels ? <>{i18nT('apps.issueRadar.components.issueDetail.done')}</> : <><Pencil size={11} /> {i18nT('apps.issueRadar.components.issueDetail.edit')}</>}
                </button>
              ) : undefined}
            >
              {editingLabels && canWrite ? (
                <div className={labelMutation.isPending ? 'opacity-60 pointer-events-none' : ''}>
                  <LabelPicker
                    labels={repoLabels}
                    selected={currentNames}
                    onToggle={toggleLabel}
                    countByLabel={countByLabel}
                  />
                </div>
              ) : labelList.length > 0 ? (
                <div className="flex items-center gap-1.5 flex-wrap">
                  {labelList.map((l) => <LabelChip key={l.name} name={l.name} color={l.color} />)}
                </div>
              ) : (
                <span className="text-muted">{i18nT('apps.issueRadar.components.issueDetail.none_yet')}</span>
              )}

              <AiSuggestions
                suggestions={suggestions}
                loading={aiQuery.isLoading}
                canWrite={canWrite}
                pending={labelMutation.isPending}
                onAccept={acceptSuggestion}
                colorByName={colorByName}
              />

              {labelMutation.isError && (
                <div className="mt-2 text-[11px] text-danger">{(labelMutation.error as Error).message}</div>
              )}
            </Section>

            <Section title={i18nT('apps.issueRadar.components.issueDetail.milestone_2')} icon={<MilestoneIcon size={12} />}>
              {milestone ? (
                <span className="inline-flex items-center gap-1.5 text-text">
                  {milestone.title}
                  <span className="text-[10.5px] text-muted">({milestone.state})</span>
                </span>
              ) : (
                <span className="text-muted">{i18nT('apps.issueRadar.components.issueDetail.no_milestone')}</span>
              )}
            </Section>

            <Section title={i18nT('apps.issueRadar.components.issueDetail.dates')} icon={<CalendarDays size={12} />}>
              <dl className="space-y-1">
                <div className="flex justify-between gap-2">
                  <dt className="text-muted">{i18nT('apps.issueRadar.components.issueDetail.opened_2')}</dt>
                  <dd className="text-text">{createdAt ? <RelTime iso={createdAt} /> : '—'}</dd>
                </div>
                <div className="flex justify-between gap-2">
                  <dt className="text-muted">{i18nT('apps.issueRadar.components.issueDetail.updated')}</dt>
                  <dd className="text-text">{updatedAt ? <RelTime iso={updatedAt} /> : '—'}</dd>
                </div>
                {closedAt && (
                  <div className="flex justify-between gap-2">
                    <dt className="text-muted">{i18nT('apps.issueRadar.components.issueDetail.closed')}</dt>
                    <dd className="text-text">
                      <RelTime iso={closedAt} />{closedBy ? ` · ${closedBy}` : ''}
                    </dd>
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
