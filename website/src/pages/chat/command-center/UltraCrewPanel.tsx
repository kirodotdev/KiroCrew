/** The Ultra Mode chat's Crew tab: a Status | List switch. Status = Overview
 *  (the goal and "Done when" in one sentence each), Needs your input (the main
 *  chat's question as one row pointing at its card, the approval cards compact), Milestones (one row per
 *  work-ledger item) and a one-line Recap. List = the crew as one indented tree,
 *  each lead foldable, trees finished all through folded away; a click opens the member in the main pane. Every
 *  number comes from reads the dashboard already makes: the crew list route,
 *  the work-ledger board, the session summary (GET only), the live slot rows
 *  and the pending question cards. No model call. */
import { useCallback, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, ArrowDown, Check, ChevronDown, ChevronLeft, ChevronRight, Loader, Loader2, MessageCircleQuestionMark, MessageSquare, Pause } from 'lucide-react'
import { safeGetItem, safeSetItem } from '../../../utils/safeStorage'
import { useCrewBack, type CrewBack } from '../../../components/crew/useCrewBack'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '../../../components/ui/dropdown-menu'
import { api } from '../../../api/client'
import type { CrewWorker } from '../../../types'
import type { SessionSummary } from '../../../types/sessionSummary'
import { crewBoardQueryKey, CREW_BOARD_POLL_MS, type WorkBoardItem, type WorkBoardResponse } from '../../../api/crewBoard'
import { artifactEntries } from '../../crewBoardRows'
import { fmtList, fmtRelative } from '../../../i18n/format'
import { fmtMinutes } from '../../../lib/crewWork'
import ErrorNotice from '../../../components/ErrorNotice'
import { isNotFoundError } from '../../../api/apiError'
import { errMessage } from '../../../utils/thunkError'
import { closeActivityPanel, pendingQuestionFor } from '../../../store/chatSlice'
import { useAppDispatch, useAppSelector, useAppStore } from '../../../store'
import { useIsMobile } from '../../../hooks/useIsMobile'
import { i18nT } from '../../../i18n/t'
import { CrewStatusDot } from '../../../components/crew/CrewModeMarks'
import CrewApprovalCards, { crewApprovalRequests } from '../../../components/crew/CrewApprovalCards'
import { crewRouteNotServed, crewSubDispatcher, crewWorkerGroup, mergeCrewWorkers, searchCrewWorkers } from '../../../lib/crewWorkers'
import { fmtRelativeTime } from '../sessionOrder'
import { CREW_FINISHED_OPEN_KEY, CREW_WORKER_SEARCH_MIN, CrewPanelBack, CrewWorkerSearch, openCrewSession, reopenCrewWorker, useCrewWorkerQuery, useCrewWorkers, useLiveCrewWorkers } from './CrewWorkerList'
import { usePersistedBool } from '../../../hooks/usePersistedBool'
import { RadioChipGroup, radioChip } from '../../../components/RadioChipGroup'
import { useCrewMode } from '../../../hooks/usePreviewFlag'
import { CrewMemberContinue, CrewPauseRow } from './CrewPauseControl'

export interface UltraCounts { total: number; done: number; running: number; approvals: number; questions: number }

export function ultraCounts(workers: readonly CrewWorker[], approvals: number, questions: number): UltraCounts {
  const g = workers.map(crewWorkerGroup)
  return {
    total: workers.length,
    done: g.filter(x => x === 'done').length,
    running: g.filter(x => x === 'working').length,
    approvals,
    questions,
  }
}

/** Names joined for a sentence, in the active locale ("A, B and C"). */
function joinNames(names: string[]): string {
  return fmtList(names)
}

/** The recap: ONE plain sentence from the counts and the names that wait. */
export function ultraRecap(c: UltraCounts, waitingNames: string[], hasQuestion: boolean): string {
  if (c.total === 0) return i18nT('components.crewMode.ultra_recap_empty')
  const head = i18nT('components.crewMode.ultra_recap_head', { done: c.done, total: c.total })
  const names = [...waitingNames]
  if (hasQuestion) names.push(i18nT('components.crewMode.ultra_recap_main_chat'))
  if (names.length > 0) {
    return i18nT(names.length === 1 ? 'components.crewMode.ultra_recap_waiting_one' : 'components.crewMode.ultra_recap_waiting_many', { head, names: joinNames(names) })
  }
  if (c.running > 0) return i18nT('components.crewMode.ultra_recap_running', { head, n: c.running })
  return i18nT('components.crewMode.ultra_recap_quiet', { head })
}

/** The Recap from the session summary (GET /api/chat/slots/{slot}/summary, a free
 *  read that never generates): the first goal's latest status
 *  line, else its title. '' when the summary is off or has no goal yet. */
export function summaryRecap(summary: SessionSummary | undefined, milestoneTitles: readonly string[]): string {
  // A stale summary may name a state the milestones have moved past.
  if (!summary?.enabled || summary.stale || !summary.intents?.length) return ''
  // The FIRST goal, in the summary's own order (the panel's lead goal).
  const goal = summary.intents[0]
  const line = firstSentence((goal.progress?.[goal.progress.length - 1] || '').trim())
  // Only a line about the goal's progress: one that names a milestone, and none
  // of the crew's own bookkeeping (limits, caps, ledger, sessions, retries).
  if (!line || BOOKKEEPING.test(line)) return ''
  const lower = line.toLowerCase()
  return milestoneTitles.some(t => t && lower.includes(t.toLowerCase())) ? line : ''
}

const BOOKKEEPING = /\b(task limit|open-task|tasks used|cap|ledger|item id|session|dispatch|worker|round|retry|try|timed out|approval|auto-nudge|monitor|patrol)\b/i

/** The milestone sentence: "4 of 8 done; A and B wait on you; C is stuck; E has gone quiet; D is running." */
export function milestoneSentence(done: number, total: number, waiting: string[], running: string[], stuck: string[] = [], quiet: string[] = []): string {
  if (total === 0) return i18nT('components.crewMode.ultra_recap_empty')
  const parts = [i18nT('components.crewMode.ultra_recap_head', { done, total })]
  if (waiting.length) parts.push(i18nT(waiting.length === 1 ? 'components.crewMode.ultra_recap_wait_one' : 'components.crewMode.ultra_recap_wait', { names: joinNames(waiting) }))
  if (stuck.length) parts.push(i18nT(stuck.length === 1 ? 'components.crewMode.ultra_recap_stuck_one' : 'components.crewMode.ultra_recap_stuck_many', { names: joinNames(stuck) }))
  if (quiet.length) parts.push(i18nT(quiet.length === 1 ? 'components.crewMode.ultra_recap_gone_quiet_one' : 'components.crewMode.ultra_recap_gone_quiet_many', { names: joinNames(quiet) }))
  if (running.length) parts.push(i18nT(running.length === 1 ? 'components.crewMode.ultra_recap_run_one' : 'components.crewMode.ultra_recap_run_many', { names: joinNames(running) }))
  return `${parts.join('; ')}.`
}

/** One member in the Workers tree, with what its subtree holds: the members
 *  waiting on an approval (`needsYou`) and running (`running`) below it, its
 *  size counting itself, and whether all of it is finished. */
export interface CrewTreeNode {
  w: CrewWorker
  depth: number
  kids: CrewTreeNode[]
  needsYou: number
  running: number
  size: number
  allDone: boolean
}

/** The crew as ONE tree, finished and unfinished members together, each lead
 *  followed by its own crew in the given order. A member whose lead is not in
 *  the list is a top-level row, never indented under nothing. Cycle safe: a
 *  member only reachable through a cycle starts a tree of its own. */
export function crewTree(workers: readonly CrewWorker[], root: string): CrewTreeNode[] {
  const keys = new Set(workers.map(w => w.key))
  const kids = new Map<string, CrewWorker[]>()
  const tops: CrewWorker[] = []
  for (const w of workers) {
    const by = crewSubDispatcher(w, root)
    if (by && keys.has(by)) {
      const list = kids.get(by)
      if (list) list.push(w)
      else kids.set(by, [w])
    } else tops.push(w)
  }
  const seen = new Set<string>()
  const build = (w: CrewWorker, depth: number): CrewTreeNode => {
    seen.add(w.key)
    const below: CrewTreeNode[] = []
    for (const k of kids.get(w.key) ?? []) if (!seen.has(k.key)) below.push(build(k, depth + 1))
    let needsYou = 0, running = 0, size = 1, allDone = crewWorkerGroup(w) === 'done'
    for (const c of below) {
      const g = crewWorkerGroup(c.w)
      needsYou += c.needsYou + (g === 'approval' ? 1 : 0)
      running += c.running + (g === 'working' ? 1 : 0)
      size += c.size
      allDone = allDone && c.allDone
    }
    return { w, depth, kids: below, needsYou, running, size, allDone }
  }
  const out: CrewTreeNode[] = []
  for (const w of tops) if (!seen.has(w.key)) out.push(build(w, 0))
  for (const w of workers) if (!seen.has(w.key)) out.push(build(w, 0))
  return out
}

/** The rows on screen: every node whose ancestors are all open, in tree order. */
export function visibleCrewRows(nodes: readonly CrewTreeNode[], isOpen: (n: CrewTreeNode) => boolean): CrewTreeNode[] {
  const out: CrewTreeNode[] = []
  const walk = (n: CrewTreeNode) => { out.push(n); if (n.kids.length && isOpen(n)) n.kids.forEach(walk) }
  nodes.forEach(walk)
  return out
}

/** The keys on the path down to `key` (its ancestors), so the row on screen
 *  is never folded out of sight. Empty when `key` is not in the tree. */
export function crewAncestors(nodes: readonly CrewTreeNode[], key: string | null): Set<string> {
  const path: string[] = []
  const find = (n: CrewTreeNode): boolean => {
    if (n.w.key === key) return true
    path.push(n.w.key)
    if (n.kids.some(find)) return true
    path.pop()
    return false
  }
  return key && nodes.some(find) ? new Set(path) : new Set()
}

/** Where the Workers tree's fold choices are kept: `{ key: open }` for the rows
 *  the person toggled. A row absent from it uses the default (top-level rows
 *  open, deeper ones folded). Its own key, never shared with the sidebar's. */
export const CREW_BOARD_EXPANDED_KEY = 'mc-crew-board-expanded'

/** Past this depth the rows stop stepping right and carry the level as `·N`, as the sidebar does. */
export const CREW_MAX_INDENT_DEPTH = 6

/** The stored fold choices, or none when the value is unusable. */
export function readCrewBoardExpanded(): Record<string, boolean> {
  try {
    const raw = safeGetItem(CREW_BOARD_EXPANDED_KEY)
    if (!raw) return {}
    const parsed: unknown = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {}
    return Object.fromEntries(Object.entries(parsed).filter(([k, v]) => k !== '' && typeof v === 'boolean')) as Record<string, boolean>
  } catch {
    return {}
  }
}

/** A row's open state: the person's choice, else open at the top level or on
 *  the path to the member on screen (`onPath`). */
export function crewRowOpen(choices: Readonly<Record<string, boolean>>, n: CrewTreeNode, onPath = false): boolean {
  return choices[n.w.key] ?? (n.depth === 0 || onPath)
}

/** First sentence of a long text, for one-line headers. */
export function firstSentence(text: string): string {
  const t = (text || '').replace(/\s+/g, ' ').trim()
  const m = t.match(/^(.+?[.!?。！？])(\s|$)/)
  return (m ? m[1] : t)
}

/** "Done when": ONE checkable sentence from the items' acceptance. A single
 *  machine-checkable kind (PRs merged, PR checks, files) names it, with the
 *  person's approval for the rest; anything else is the approval sentence. */
export function doneWhen(items: readonly WorkBoardItem[]): string {
  const count = new Map<string, number>()
  for (const i of items) {
    const k = String(i.acceptance?.kind || '')
    if (k) count.set(k, (count.get(k) ?? 0) + 1)
  }
  const checkable = [...count.keys()].filter(k => k === 'pr_merged' || k === 'pr_checks' || k === 'file')
  if (checkable.length !== 1) return i18nT('components.crewMode.ultra_done_line_approval')
  const kind = checkable[0]
  const n = count.get(kind) ?? 0
  // "and you approve the rest" only while such an item is still open.
  const rest = items.some(i => String(i.acceptance?.kind || '') !== kind && i.state !== 'accepted' && !i.terminal)
  return i18nT(DONE_LINE_KEY[kind as keyof typeof DONE_LINE_KEY][rest ? 1 : 0], { count: n })
}

/** Full literal keys per acceptance kind: [alone, with "you approve the rest"]. */
const DONE_LINE_KEY = {
  pr_merged: ['components.crewMode.ultra_done_line_merged', 'components.crewMode.ultra_done_line_merged_rest'],
  pr_checks: ['components.crewMode.ultra_done_line_green', 'components.crewMode.ultra_done_line_green_rest'],
  file: ['components.crewMode.ultra_done_line_files', 'components.crewMode.ultra_done_line_files_rest'],
} as const

/** The goal in the user's own words: the ledger goal's first sentence with
 *  parentheticals removed, cut on a word boundary when long. */
export function goalSentence(text: string, max = 90): string {
  const one = firstSentence(text.replace(/\s*\([^)]*\)/g, '')).replace(/\s+([:,.;])/g, '$1').trim()
  if (one.length <= max) return one
  const cut = one.slice(0, max)
  const at = cut.lastIndexOf(' ')
  return `${(at > 30 ? cut.slice(0, at) : cut).replace(/[\s,:;]+$/, '')}…`
}

/** Scope: a limits line, only when the goal states one ("Scope: ..." / "Limits: ..."). */
export function goalScope(text: string): string {
  const m = text.match(/\b(?:scope|limits?|constraints?)\s*:\s*([^.。\n]+)/i)
  return m ? m[1].trim() : ''
}

type MilestoneState = 'done' | 'needs_you' | 'running' | 'waiting' | 'quiet' | 'stuck' | 'next'

/** A report a worker said only a person can move (`reason` approval / needs_human). */
const PERSON_REASONS = new Set(['approval', 'needs_human'])

/** The item's last blocked / question report has its answer: for a question the
 *  board's own `outstanding` (a newer decision or person's steer), for a blocked
 *  report the conductor's `decide`, or a decision / steer after the newest report
 *  on the event tail, as `answered_since_report` reads it on the server. */
export function reportAnswered(item: WorkBoardItem): boolean {
  if (item.status === 'question') return !item.outstanding
  // Stamps may carry different offsets (a remote peer keeps its own), so they
  // are compared as instants; one that does not parse answers nothing.
  const reported = item.last_report_at ? Date.parse(item.last_report_at) : NaN
  const atOrAfterReport = (ts: string | null | undefined) => {
    const t = ts ? Date.parse(ts) : NaN
    return Number.isFinite(t) && (!item.last_report_at || (Number.isFinite(reported) && t >= reported))
  }
  if (atOrAfterReport(item.decided_at)) return true
  const events = item.events || []
  let last = -1
  events.forEach((e, n) => { if (e.kind === 'report') last = n })
  if (last >= 0) return events.slice(last + 1).some(e => e.kind === 'decision' || e.kind === 'steer')
  return events.some(e => (e.kind === 'decision' || e.kind === 'steer') && atOrAfterReport(e.ts))
}

export function milestoneState(item: WorkBoardItem, waiting: boolean): MilestoneState {
  // A closed item is finished whatever its last report said. Then the person;
  // then a session working again reads as running even after an earlier
  // "done" report. A report counts only until the conductor decides on it
  // (`decided_at` at or after the report hands it back).
  if (item.state === 'accepted' || item.terminal) return 'done'
  const open = item.status === 'progress' || item.status === 'blocked' || item.status === 'question'
  // Needs you only when the person must act: a pending approval of the item's
  // member (`waiting`), or an unanswered blocked / question report whose reason
  // names a person. An answer -- the conductor's or the person's own steer -- ends it.
  if (waiting || ((item.status === 'blocked' || item.status === 'question') && PERSON_REASONS.has(item.reason || '') && !reportAnswered(item))) return 'needs_you'
  if (item.alive === 'running') return 'running'
  // Stuck only when nothing is left to move it: its session closed on an open
  // report, or the board is orphaned (no main chat reads its reports). A done
  // report or an unstarted item keeps its own state even on an orphaned board.
  if (open && (item.alive === 'closed' || item.orphaned)) return 'stuck'
  // Quiet: the board's `stale` flag with the chat still there. Its worker is
  // idle with no loop to wake it and the next move is its own, so it stays
  // visible, but calmer than stuck: the chat can still be resumed. An item
  // with no report yet stays Not started (it may have no worker bound at all),
  // and raises only the quiet pulse below.
  if (open && item.stale) return 'quiet'
  // An idle session after progress, or a question / blocked report the
  // conductor still owns, is waiting, as the List shows it.
  if (open) return 'waiting'
  if (item.status === 'done') return 'done'
  return 'next'
}

/** The conductor still owes this item an answer: an open question, or a blocked report it has not decided on. */
function onConductor(item: WorkBoardItem): boolean {
  return (item.status === 'blocked' || item.status === 'question') && !reportAnswered(item)
}

function minutesBetween(a: string | null | undefined, b: string | number | null | undefined): number {
  const ta = a ? Date.parse(a) : NaN
  const tb = typeof b === 'number' ? b : b ? Date.parse(b) : NaN
  return Number.isFinite(ta) && Number.isFinite(tb) ? Math.max(0, Math.round((tb - ta) / 60000)) : NaN
}


const STATE_ICON: Record<MilestoneState, JSX.Element> = {
  done: <span className="w-5 h-5 rounded-full grid place-items-center bg-[color-mix(in_srgb,var(--ok)_18%,transparent)] crew-tone-ok"><Check size={12} strokeWidth={3} /></span>,
  needs_you: <span className="w-5 h-5 rounded-full grid place-items-center bg-[color-mix(in_srgb,var(--warn)_20%,transparent)] crew-tone-warn"><AlertTriangle size={11} strokeWidth={2.5} /></span>,
  running: <span className="w-5 h-5 rounded-full grid place-items-center bg-[color-mix(in_srgb,var(--aim)_18%,transparent)] ultra-accent"><Loader size={12} className="motion-safe:animate-spin" /></span>,
  waiting: <span className="block w-5 h-5 rounded-full border-[1.5px] border-[color-mix(in_srgb,var(--muted)_70%,transparent)]" />,
  quiet: <span className="w-5 h-5 rounded-full grid place-items-center bg-[color-mix(in_srgb,var(--warn)_20%,transparent)] crew-tone-warn"><Pause size={11} strokeWidth={2.5} /></span>,
  stuck: <span className="w-5 h-5 rounded-full grid place-items-center bg-[color-mix(in_srgb,var(--danger)_18%,transparent)] text-[var(--danger)]"><AlertTriangle size={11} strokeWidth={2.5} /></span>,
  next: <span className="block w-5 h-5 rounded-full border-[1.5px] border-dashed border-[color-mix(in_srgb,var(--muted)_70%,transparent)]" />,
}

function MilestoneRow({ item, state, waitingOn, now, onOpen }: { item: WorkBoardItem; state: MilestoneState; waitingOn: string; now: number; onOpen?: () => void }) {
  let sub: JSX.Element | string = ''
  if (state === 'done') {
    const link = artifactEntries(item).find(([, v]) => /^https?:\/\//.test(v))
    sub = <>
      <span className="truncate">{firstSentence(item.summary)}</span>
      {item.pr != null && <span className="shrink-0 ultra-accent">{i18nT('components.crewMode.ultra_ms_pr', { n: item.pr })}</span>}
      {link && <a className="shrink-0 ultra-accent hover:underline" href={link[1]} target="_blank" rel="noreferrer">· {link[0]}</a>}
    </>
  } else if (state === 'stuck' || (state === 'waiting' && onConductor(item))) {
    // Who acts next, not the worker's own words: a stuck row says why and how to
    // restart it, and a row the conductor owns says the main chat is on it.
    const said = firstSentence(item.summary)
    const text = i18nT(state === 'waiting' ? 'components.crewMode.ultra_ms_main_chat_answering'
      : item.alive === 'closed' ? 'components.crewMode.ultra_ms_stuck_chat_closed' : 'components.crewMode.ultra_ms_stuck_unread')
    sub = <span className="truncate" title={said || text}>{text}</span>
  } else if (state === 'quiet') {
    // Why it is quiet and the PR it named on one line, the worker's own last
    // words on the next, so a narrow panel keeps both: the PR number never
    // shrinks, the reason and the words truncate.
    const said = firstSentence(item.summary)
    const why = i18nT('components.crewMode.ultra_ms_quiet')
    sub = <span className="flex flex-col min-w-0 w-full">
      <span className="flex items-center gap-1 min-w-0">
        <span className="min-w-0 truncate" title={why} data-testid="ultra-quiet-reason">{why}</span>
        {item.pr != null && <span className="shrink-0">{i18nT('components.crewMode.ultra_ms_pr', { n: item.pr })}</span>}
      </span>
      {said && <span className="truncate" title={said} data-testid="ultra-quiet-said">{said}</span>}
    </span>
  } else if (state === 'waiting') {
    // What it waits on, only as far as the board knows: the PR it reported, then
    // the worker's own words; with neither, a calm "no reason given".
    const said = firstSentence(item.summary)
    const head = item.pr != null ? i18nT('components.crewMode.ultra_ms_waiting_pr', { n: item.pr })
      : said ? i18nT('commandCenter.worker_group_waiting') : i18nT('components.crewMode.ultra_ms_waiting_no_reason')
    sub = <>
      <span className="shrink-0">{head}</span>
      {said && <span className="truncate" title={said}>· {said}</span>}
    </>
  } else {
    const text = state === 'needs_you' ? waitingOn
      : state === 'running' ? (firstSentence(item.summary) || i18nT('components.crewMode.ultra_ms_running'))
        : i18nT('components.crewMode.ultra_ms_not_started')
    sub = <span className="truncate" title={text}>{text}</span>
  }
  const right = state === 'running' ? i18nT('components.crewMode.ultra_ms_now')
    : state === 'next' ? i18nT('components.crewMode.ultra_ms_next')
      : state === 'done' ? fmtMinutes(minutesBetween(item.created_at, item.closed_at || item.last_report_at))
        : fmtMinutes(minutesBetween(item.last_report_at || item.created_at, now))
  return (
    <li className="flex items-start gap-2.5 py-1.5" data-testid="ultra-milestone" data-state={state}>
      <span className="mt-0.5 shrink-0">{STATE_ICON[state]}</span>
      <span className="flex-1 min-w-0">
        {/* A milestone with a crew member opens it, so a blocked one is one click from where it is decided. */}
        {onOpen
          ? <button type="button" onClick={onOpen} data-testid="ultra-milestone-open" className={`block max-w-full text-left text-[13px] font-medium truncate hover:underline ${state === 'next' ? 'text-muted' : 'text-text'}`}>{item.title}</button>
          : <span className={`block text-[13px] font-medium truncate ${state === 'next' ? 'text-muted' : 'text-text'}`}>{item.title}</span>}
        <span className={`flex items-center gap-1 min-w-0 text-[12px] ${state === 'needs_you' ? 'crew-tone-warn' : 'text-muted'}`}>{sub}</span>
      </span>
      <span className={`shrink-0 text-[11px] tabular-nums mt-0.5 ${state === 'running' ? 'ultra-accent font-medium' : 'text-muted'}`}>{right}</span>
    </li>
  )
}

/** One member card. A member with a crew of its own gets a chevron (`onToggle`;
 *  absent while a search unfolds everything); folded, the card carries its child
 *  count and its subtree's needs-you and running counts, with the sidebar's glyphs.
 *  `gutter` keeps childless rows aligned beside rows that have a chevron.
 *  `pausedIn` (the paused root) marks a member Pause all stopped: its card shows
 *  "Paused" and its own Continue instead of the status dot. */
function MemberRow({ node, open, gutter, onToggle, onOpen, current, pausedIn }: {
  node: CrewTreeNode; open: boolean; gutter: boolean; onToggle?: () => void; onOpen: () => void; current: boolean; pausedIn?: string
}) {
  const { w, depth } = node
  const title = w.title || i18nT('commandCenter.worker_untitled')
  const indent = Math.min(depth, CREW_MAX_INDENT_DEPTH) * 16
  const done = crewWorkerGroup(w) === 'done'
  const kids = node.kids.length
  const folded = kids > 0 && !open
  const toggleLabel = i18nT(open ? 'pages.chatSidebar.collapse_sessions_this_one_opened' : 'pages.chatSidebar.expand_sessions_this_one_opened')
  return (
    <li data-testid="ultra-member" data-depth={depth} data-done={done ? 'true' : undefined} data-paused={pausedIn ? 'true' : undefined} style={{ paddingLeft: indent }} className="relative flex items-start gap-0.5">
      {depth > 0 && <span aria-hidden="true" className="absolute top-0 bottom-0 border-l border-border" style={{ left: indent - 9 }} />}
      {kids > 0 && onToggle ? (
        <button type="button" onClick={onToggle} aria-expanded={open} aria-label={toggleLabel} title={toggleLabel} data-testid="ultra-member-chevron"
          className="mt-2.5 w-4 h-4 shrink-0 rounded flex items-center justify-center text-muted hover:text-text">
          <ChevronRight size={12} aria-hidden="true" className={`shrink-0 transition-transform duration-200 ${open ? 'rotate-90' : ''}`} />
        </button>
      ) : gutter && <span aria-hidden="true" className="mt-2.5 w-4 h-4 shrink-0" />}
      <button type="button" onClick={onOpen} aria-current={current ? 'true' : undefined}
        className={`flex-1 min-w-0 text-left rounded-lg border px-3 py-2 text-sm transition-colors ${current ? 'crew-card' : 'border-border bg-bg-elevated/40 hover:bg-bg-hover'}`}>
        <span className="flex items-center gap-2 min-w-0">
          <span className={`font-medium truncate ${done ? 'text-muted' : 'text-text'}`} data-testid="ultra-member-title">{title}</span>
          <span className="ml-auto flex items-center gap-1.5 shrink-0 text-[11px]">
            {depth > CREW_MAX_INDENT_DEPTH && (
              <span className="text-muted tabular-nums" title={i18nT('pages.chatSidebar.nesting_depth', { depth })} data-testid="ultra-member-depth">&middot;{depth}</span>
            )}
            {kids > 0 && (
              <span className="text-muted tabular-nums" role="img" aria-label={i18nT('pages.chatSidebar.opened_sessions_count', { count: kids })}
                title={i18nT('pages.chatSidebar.opened_sessions_count', { count: kids })} data-testid="ultra-member-kids">{kids}</span>
            )}
            {folded && node.needsYou > 0 && (
              <span className="inline-flex items-center gap-0.5 px-1 rounded bg-accent-subtle text-accent tabular-nums"
                title={i18nT('components.crewMode.ultra_needs_you')} data-testid="ultra-member-needs-you">
                <MessageCircleQuestionMark size={10} className="lucide-inline shrink-0" aria-hidden />{node.needsYou}
              </span>
            )}
            {folded && node.running > 0 && (
              <span className="inline-flex items-center gap-0.5 px-1 rounded bg-bg-hover text-muted tabular-nums"
                title={i18nT('pages.chatSidebar.running_session', { count: node.running })} data-testid="ultra-member-running">
                <Loader2 size={10} className="lucide-inline shrink-0 motion-safe:animate-spin" aria-hidden />{node.running}
              </span>
            )}
            {!pausedIn && <CrewStatusDot group={crewWorkerGroup(w)} />}
          </span>
        </span>
        {w.summary && <span className="block text-[12px] text-text/80 truncate mt-0.5">{w.summary}</span>}
        {w.last_activity && <span className="block text-[11px] text-muted truncate mt-0.5">{fmtRelativeTime(w.last_activity)}</span>}
      </button>
      {pausedIn && <span className="pt-2 pl-1 shrink-0"><CrewMemberContinue root={pausedIn} worker={w.key} /></span>}
    </li>
  )
}

/** The open ledger items that wait on the PERSON: the board read's own
 *  `awaits_person` (goal-board's "needs you" rule, minus a report the conductor or
 *  the person has answered since, the same test as the sidebar's count), less an
 *  item whose tool approval already has its own card (`covered`). Exported for tests. */
export function personAsks(items: readonly WorkBoardItem[], covered: ReadonlySet<string> = new Set()): WorkBoardItem[] {
  return items.filter(item => !!item.awaits_person && !covered.has(item.item_id))
}

function clip(text: string, n: number): string {
  return text.length > n ? `${text.slice(0, n - 1)}…` : text
}

/** One item that waits on the person: which task, what it asks, and goal-board's
 *  Reply: one Write a reply button and, when the conductor offered answers, one
 *  Suggested answers menu holding them (two controls in the row). Each only FILLS
 *  the chat box (`onAct`); the person still presses send, and the line under the
 *  buttons says so. With a crew member on screen (`onAct` absent) the row offers
 *  the way back to the main chat (`back`: "Back to main chat", or "Open main chat"
 *  reopening a closed one), the same as the panel's Back row. */
function PersonAskRow({ item, options, onAct, back }: { item: WorkBoardItem; options: readonly string[]; onAct?: (text: string) => void; back: CrewBack }) {
  const draft = (answer: string) => i18nT('components.crewMode.ultra_reply_draft', { t: clip(item.title, 70), id: item.item_id, answer })
  const small = 'inline-flex items-center gap-1 h-7 px-2 rounded-md text-[12px] font-medium border border-border hover:bg-bg-hover max-w-full'
  return (
    <article className="rounded-lg border border-warn bg-card px-2.5 py-2 space-y-1 text-text" data-testid="ultra-person-ask" data-item={item.item_id}>
      <div className="flex items-center gap-2 min-w-0">
        <MessageSquare size={14} aria-hidden="true" className="shrink-0 text-warn" />
        <h4 className="flex-1 min-w-0 text-[13px] font-semibold truncate" title={item.title}>{item.title}</h4>
      </div>
      {item.summary && <p className="pl-[22px] text-[12px] text-text/85 break-words line-clamp-3" title={item.summary} data-testid="ultra-person-ask-text">{item.summary}</p>}
      {onAct ? (
        <div className="pl-[22px] space-y-1">
          <div className="flex items-center gap-1.5" data-testid="ultra-person-ask-actions">
            <button type="button" className={`${small} ultra-accent`} onClick={() => onAct(draft(''))} data-testid="ultra-person-ask-reply">{i18nT('components.crewMode.ultra_reply')}</button>
            {options.length > 0 && (
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <button type="button" className={small} data-testid="ultra-person-ask-answers">{i18nT('components.crewMode.ultra_answers')}<ChevronDown size={12} aria-hidden="true" /></button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="start" className="max-w-[280px]">
                  {options.map(o => (
                    <DropdownMenuItem key={o} onSelect={() => onAct(draft(clip(o, 200)))} data-testid="ultra-person-ask-option">
                      <span className="break-words">{clip(o, 80)}</span>
                    </DropdownMenuItem>
                  ))}
                </DropdownMenuContent>
              </DropdownMenu>
            )}
          </div>
          <p className="text-[11px] text-muted" data-testid="ultra-person-ask-hint">{i18nT('components.crewMode.ultra_option_title')}</p>
        </div>
      ) : (
        <div className="pl-[22px] space-y-1">
          <button type="button" onClick={back.back} data-testid="ultra-person-ask-open-main"
            className="inline-flex items-center gap-1 h-7 px-2 rounded-md text-[12px] font-medium ultra-accent hover:bg-bg-hover">
            <ChevronLeft size={12} aria-hidden="true" />{back.label}
          </button>
          {back.reopenFailed && <ErrorNotice variant="inline" message={i18nT('commandCenter.worker_reopen_failed')} askAgent />}
        </div>
      )}
    </article>
  )
}

export default function UltraCrewPanel({ root, viewing, active, onAct }: {
  root: string
  viewing: string | null
  active: boolean
  /** Put a reply into the main chat's composer (the side panel's composer insert). */
  onAct?: (text: string) => void
}) {
  const dispatch = useAppDispatch()
  const store = useAppStore()
  const isMobile = useIsMobile()
  const listed = useCrewWorkers(root, active)
  const live = useLiveCrewWorkers(root)
  const workers = useMemo(() => mergeCrewWorkers(listed.data?.workers, live), [listed.data, live])
  // Pause all ships behind the Ultra Mode preview, like the rest of this panel's crew surfaces.
  const crewModeOn = useCrewMode()
  const pause = crewModeOn ? listed.data?.paused : undefined
  const slots = useAppSelector(s => s.dashboard.slots)
  const approvals = crewApprovalRequests(slots, root, new Map())
  // The same pending-question record the chat's own card reads.
  const pendingQuestion = useAppSelector(st => pendingQuestionFor(st.chat.pendingQuestions, root))
  const questionCount = pendingQuestion?.questions?.length ? 1 : 0
  const c = ultraCounts(workers, approvals.length, questionCount)
  const waitingNames = [...new Set(approvals.map(a => a.title))]
  // Same query key and read as the side panel's Summary tab, so one cache serves both.
  const summary = useQuery({ queryKey: ['session-summary', root], queryFn: () => api.sessionSummary(root), enabled: !!root, staleTime: 30_000, retry: false })
  const [reopenFailed, setReopenFailed] = useState(false)
  // The main chat's own question is answered on its card in the chat: bring
  // the main chat on screen if a crew member is, then scroll to that card.
  const answerInChat = useCallback(() => {
    if (store.getState().chat.activeSlot !== root) openCrewSession(dispatch, root, !isMobile)
    // On a phone the panel is a sheet over the chat: close it so the card shows.
    else if (isMobile) dispatch(closeActivityPanel())
    let tries = 0
    const reach = () => {
      // The main chat's own card: the single chat's composer or its Split View pane.
      const card = document.querySelector<HTMLElement>(`[data-question-slot="${CSS.escape(root)}"]`)
      if (card) {
        card.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
        card.querySelector<HTMLElement>('button, input, textarea')?.focus({ preventScroll: true })
      } else if (++tries < 20) window.setTimeout(reach, 50)
    }
    reach()
  }, [dispatch, store, root, isMobile])
  const open = useCallback((w: CrewWorker) => {
    setReopenFailed(false)
    if (w.status !== 'closed') { openCrewSession(dispatch, w.key, !isMobile); return }
    void reopenCrewWorker(dispatch, store.getState, w.key, w.title, !isMobile).then(ok => { if (!ok) setReopenFailed(true) })
  }, [dispatch, store, isMobile])
  // The List's search (from CREW_WORKER_SEARCH_MIN members up) filters live and
  // finished members alike; while it is on, finished matches show unfolded.
  const [query, setQuery] = useCrewWorkerQuery(root)
  const searching = query.trim() !== ''
  const showSearch = searching || workers.length >= CREW_WORKER_SEARCH_MIN
  const [finishedOpen, setFinishedOpen] = usePersistedBool(CREW_FINISHED_OPEN_KEY, false)
  const shown = searching ? searchCrewWorkers(workers, query) : workers
  // ONE tree: a finished member keeps its place under its lead and is only
  // styled. A top-level tree that is finished all through folds into Finished.
  const tree = useMemo(() => crewTree(shown, root), [shown, root])
  const unfinishedTops = tree.filter(n => !n.allDone)
  const finishedTops = tree.filter(n => n.allDone)
  const finishedCount = finishedTops.reduce((s, n) => s + n.size, 0)
  // Only a top-level row can have a crew when any row has one.
  const gutter = tree.some(n => n.kids.length > 0)
  const [foldChoices, setFoldChoices] = useState(readCrewBoardExpanded)
  // The row on screen starts visible: its leads default to open (a stored choice still wins).
  const pathToViewing = useMemo(() => crewAncestors(tree, viewing), [tree, viewing])
  // A search unfolds every match's lead; the stored choices come back after.
  const isOpen = (n: CrewTreeNode) => searching || crewRowOpen(foldChoices, n, pathToViewing.has(n.w.key))
  const toggleRow = useCallback((n: CrewTreeNode, onPath: boolean) => setFoldChoices(prev => {
    // shortcut: choices are never pruned (keys of long-closed sessions stay), a few
    // bytes per toggle; prune to the listed keys if the value ever grows large.
    const next = { ...prev, [n.w.key]: !crewRowOpen(prev, n, onPath) }
    safeSetItem(CREW_BOARD_EXPANDED_KEY, JSON.stringify(next))
    return next
  }), [])
  const unfinished = visibleCrewRows(unfinishedTops, isOpen)
  const finished = visibleCrewRows(finishedTops, isOpen)
  const memberRow = (n: CrewTreeNode) => (
    <MemberRow key={n.w.key} node={n} open={isOpen(n)} gutter={gutter} current={n.w.key === viewing}
      onToggle={searching ? undefined : () => toggleRow(n, pathToViewing.has(n.w.key))} onOpen={() => open(n.w)}
      pausedIn={pause?.stopped.includes(n.w.key) ? root : undefined} />
  )
  const [view, setView] = useState<'status' | 'list'>('status')
  const [doneOpen, setDoneOpen] = useState(false)
  // The work ledger through the Crew page's own read (GET /api/crew-board), same key and cadence.
  const board = useQuery({ queryKey: crewBoardQueryKey(root), queryFn: () => api.crewBoard(root) as Promise<WorkBoardResponse>, enabled: !!root && active, refetchInterval: CREW_BOARD_POLL_MS, retry: false })
  // ISO timestamps from the board: a byte compare orders them, no locale involved.
  const items = useMemo(() => [...(board.data?.items ?? [])].sort((a, b) => { const x = a.created_at || '', y = b.created_at || ''; return x < y ? -1 : x > y ? 1 : 0 }), [board.data])
  const goalText = (board.data?.conductor?.goal || '').replace(/\s+/g, ' ').trim()
  // A goal that says its own "Done when ..." wins over the acceptance kinds.
  const doneMatch = goalText.match(/\bdone when:?\s*([^.。]+)/i)
  const goalFull = firstSentence((doneMatch ? goalText.slice(0, doneMatch.index) : goalText).replace(/\s*\([^)]*\)/g, '')).trim()
  const goal = goalSentence(goalText.slice(0, doneMatch ? doneMatch.index : undefined) || goalText)
  const doneText = doneMatch ? i18nT('components.crewMode.ultra_done_when', { what: doneMatch[1].trim() }) : doneWhen(items)
  const scope = goalScope(goalText)
  const now = Date.now()
  // Which ledger item each pending approval belongs to: the crew list's own join.
  const approvalByItem = new Map<string, string>()
  for (const a of approvals) {
    const w = workers.find(x => x.key === a.worker)
    if (w?.ledger_item) approvalByItem.set(w.ledger_item, i18nT('components.crewMode.ultra_ms_waiting_approval'))
  }
  // The ledger items a worker said wait on the person, with the conductor's offered answers.
  const forYou = personAsks(items, new Set(approvalByItem.keys()))
  // Its milestone says it waits on the person too, not on the main chat.
  for (const item of forYou) approvalByItem.set(item.item_id, i18nT('components.crewMode.ultra_ms_waiting_answer'))
  // Only while the main chat is on screen: the side panel's insert fills the
  // composer on screen, and a reply belongs in the main chat's.
  const replyInto = !viewing || viewing === root ? onAct : undefined
  const crewBack = useCrewBack(root)
  const milestones = items.map(item => {
    const state = milestoneState(item, approvalByItem.has(item.item_id))
    const waitingOn = approvalByItem.get(item.item_id) || firstSentence(item.summary) || i18nT('components.crewMode.ultra_needs_you')
    return { item, state, waitingOn }
  })
  const doneMs = milestones.filter(m => m.state === 'done')
  // Needs-you milestones with no approval card of their own count too (an
  // approval row's card is already in `c.approvals`), so the header and the
  // rows agree on whether the person must act.
  const needsYouRows = milestones.filter(m => m.state === 'needs_you' && !approvalByItem.has(m.item.item_id)).map(m => m.item.title)
  const needYou = c.approvals + c.questions + needsYouRows.length
  // Plus the rows below that wait on the person (kept out of `needsYouRows`).
  const needCount = needYou + forYou.length
  const openMs = milestones.filter(m => m.state !== 'done')
  const msDone = doneMs.length
  const lastReport = items.map(i => i.last_report_at).filter(Boolean).sort().pop() || null
  // Stuck (red): a milestone with nothing left to move it. Quiet (amber): one
  // idle with nothing scheduled to wake it, whose chat can still be resumed.
  const stuck = openMs.some(m => m.state === 'stuck')
  // Any stale open item raises it, a not-started one included, as main's pulse did.
  const quiet = openMs.some(m => m.state === 'quiet' || (m.state === 'next' && m.item.stale))
  // "moving" only while a milestone runs; with nothing running, stuck or quiet
  // (waiting or next only) there is no pulse.
  const moving = openMs.some(m => m.state === 'running')
  const openItem = (item: WorkBoardItem) => {
    const w = workers.find(x => x.ledger_item === item.item_id)
    return w ? () => open(w) : undefined
  }
  const recap = summaryRecap(summary.data, items.map(i => i.title))
    || (items.length
      ? milestoneSentence(msDone, items.length, milestones.filter(m => m.state === 'needs_you').map(m => m.item.title), milestones.filter(m => m.state === 'running').map(m => m.item.title), milestones.filter(m => m.state === 'stuck').map(m => m.item.title), milestones.filter(m => m.state === 'quiet').map(m => m.item.title))
      : ultraRecap(c, waitingNames, questionCount > 0))

  return (
    <div className="h-full flex flex-col min-h-0" data-testid="ultra-crew-panel" data-view={view}>
      {/* A crew member on screen: one click back to its root. */}
      {viewing && viewing !== root && <CrewPanelBack root={root} />}
      {/* A failed crew read, shown in both views; a gateway that does not serve
          the route is not a failure (the live rows stand in). */}
      {listed.isError && !crewRouteNotServed(listed.error) && <div className="shrink-0 px-3 pt-3"><ErrorNotice variant="inline" message={i18nT(live.length ? 'commandCenter.workers_error_live' : 'commandCenter.workers_error')} askAgent /></div>}
      <div className="shrink-0 px-3 pt-3">
        <RadioChipGroup ariaLabel={i18nT('commandCenter.workers_view_label')} className="flex gap-1 rounded-lg border border-border bg-bg-hover/40 p-0.5" data-testid="ultra-view-switch">
          {(['status', 'list'] as const).map(v => (
            <button key={v} type="button" {...radioChip(view === v)} data-testid={`ultra-view-${v}`} onClick={() => setView(v)}
              className={`flex-1 rounded-md px-2 py-1 text-[12px] font-medium transition-colors ${view === v ? 'bg-bg-elevated text-text shadow-sm' : 'text-muted hover:text-text'}`}>
              {i18nT(v === 'status' ? 'components.crewMode.ultra_view_status' : 'components.crewMode.ultra_view_list')}
            </button>
          ))}
        </RadioChipGroup>
      </div>
      {view === 'status' ? (
        <div className="flex-1 min-h-0 overflow-y-auto p-3 space-y-4" data-testid="ultra-status">
          {/* (1) Overview: the ledger's goal in one line, and what "done" means. */}
          <section data-testid="ultra-overview" className="space-y-0.5">
            <h3 className="text-[11px] font-semibold uppercase tracking-[1px] text-muted">{i18nT('components.crewMode.ultra_overview')}</h3>
            <p className="text-[14px] font-semibold text-text line-clamp-1 break-words" title={goalFull}>{goal || i18nT('components.crewMode.ultra_goal_none')}</p>
            {/* "Done when" needs something to finish: no goal and no milestone, no line. */}
            {(goalText || items.length > 0) && <p className="text-[12px] text-text/85 line-clamp-1" title={doneText}>{doneText}</p>}
            {scope && <p className="text-[11px] text-muted line-clamp-1" title={scope}>{scope}</p>}
          </section>
          {/* (2) Needs your input: the chat's own ask_question card and the crew's
              approval cards, both compact. */}
          <section data-testid="ultra-needs-you" className="space-y-1.5">
            <h3 className="text-[11px] font-semibold uppercase tracking-[1px] text-muted">{`${i18nT('components.crewMode.ultra_needs_input')} · ${needCount}`}</h3>
            {/* A milestone that needs the person without an approval card, or one
                that is stuck or quiet, is not counted here, but the line does not claim
                nothing is wrong: it points at the rows below. */}
            {(needCount === 0 || needsYouRows.length > 0) && <p className="text-[12px] text-muted" data-testid="ultra-needs-you-none">{needsYouRows.length
              ? i18nT(needsYouRows.length === 1 ? 'components.crewMode.ultra_needs_you_rows_one' : 'components.crewMode.ultra_needs_you_rows_many', { names: joinNames(needsYouRows) })
              : i18nT(stuck || quiet ? 'components.crewMode.ultra_needs_you_none_stuck' : 'components.crewMode.ultra_needs_you_none')}</p>}
            {pendingQuestion?.questions?.length ? (
              <div className="flex items-center gap-2 rounded-lg border border-border bg-bg-elevated/40 px-2.5 py-2" data-testid="ultra-main-question">
                <span className="flex-1 min-w-0 text-[13px] text-text truncate" title={pendingQuestion.questions[0].question}>{pendingQuestion.questions[0].question}</span>
                <button type="button" onClick={answerInChat} data-testid="ultra-answer-in-chat"
                  className="shrink-0 inline-flex items-center gap-1 h-7 px-2 rounded-md text-[12px] font-medium ultra-accent hover:bg-bg-hover">
                  {i18nT('components.crewMode.ultra_answer_in_chat')}<ArrowDown size={12} aria-hidden="true" />
                </button>
              </div>
            ) : null}
            {forYou.map(item => <PersonAskRow key={item.item_id} item={item} options={board.data?.asks?.[item.item_id] ?? []} onAct={replyInto} back={crewBack} />)}
            <CrewApprovalCards root={root} compact />
          </section>
          {/* (3) Milestones: one row per ledger item, as outcomes. */}
          <section data-testid="ultra-milestones">
            <div className="flex items-center gap-2">
              <h3 className="text-[11px] font-semibold uppercase tracking-[1px] text-muted">{i18nT('components.crewMode.ultra_milestones', { done: msDone, total: items.length })}</h3>
              {(stuck || quiet || (moving && lastReport)) && (
                <span className={`ml-auto text-[11px] inline-flex items-center gap-1 ${stuck ? 'text-[var(--danger)]' : quiet ? 'crew-tone-warn' : 'crew-tone-ok'}`} data-testid="ultra-pulse">
                  <span aria-hidden="true" className="w-1.5 h-1.5 rounded-full bg-current" />
                  {stuck ? i18nT('components.crewMode.ultra_stuck') : quiet ? i18nT('components.crewMode.ultra_quiet') : i18nT('components.crewMode.ultra_moving', { when: fmtRelative(lastReport || '') })}
                </span>
              )}
            </div>
            {/* A ledger the root never opened (404) is the empty state; any
                other failure says so and offers the agent hand-off. */}
            {board.isError && !isNotFoundError(board.error) && <div className="mt-1"><ErrorNotice variant="inline" message={errMessage(board.error) || i18nT('pages.crewBoard.error_title')} askAgent /></div>}
            {board.isError && !isNotFoundError(board.error) ? null : items.length === 0
              ? <p className="text-[12px] text-muted mt-1">{i18nT('components.crewMode.ultra_milestones_none')}</p>
              : <>
                {/* Finished milestones fold into one row, so what is open stays on screen. */}
                {doneMs.length > 0 && (
                  <details className="group mt-0.5 border-b border-border" data-testid="ultra-milestones-done" open={doneOpen}
                    onToggle={e => setDoneOpen((e.currentTarget as HTMLDetailsElement).open)}>
                    <summary className="flex items-center gap-2.5 py-1.5 cursor-pointer list-none">
                      <span className="shrink-0">{STATE_ICON.done}</span>
                      <span className="flex-1 min-w-0 text-[13px] text-text truncate">{i18nT('components.crewMode.ultra_ms_done_fold', { n: doneMs.length })}{!doneOpen && <span className="text-muted"> · {doneMs.map(m => m.item.title).join(', ')}</span>}</span>
                      <ChevronRight size={14} aria-hidden="true" className="shrink-0 text-muted transition-transform group-open:rotate-90" />
                    </summary>
                    <ol className="divide-y divide-border pl-2">{doneMs.map(m => <MilestoneRow key={m.item.item_id} {...m} now={now} onOpen={openItem(m.item)} />)}</ol>
                  </details>
                )}
                <ol className="divide-y divide-border">
                  {openMs.map(m => <MilestoneRow key={m.item.item_id} {...m} now={now} onOpen={openItem(m.item)} />)}
                </ol>
              </>}
          </section>
          {/* (4) Recap: one line from the session summary. */}
          <section data-testid="ultra-recap-section" className="space-y-0.5">
            <h3 className="text-[11px] font-semibold uppercase tracking-[1px] text-muted">{i18nT('components.crewMode.ultra_recap_title')}</h3>
            <p className="text-[13px] leading-snug text-text line-clamp-2 break-words" title={recap} data-testid="ultra-recap">{recap}</p>
            {summary.isError && !isNotFoundError(summary.error) && <ErrorNotice variant="inline" message={errMessage(summary.error) || i18nT('components.errorBoundary.something_went_wrong')} askAgent />}
          </section>
        </div>
      ) : (
        <div className="relative flex-1 min-h-0 flex flex-col">
        {showSearch && <CrewWorkerSearch query={query} onChange={setQuery} />}
        {/* A paused crew can always be resumed, its root's tab closed or not; only Pause all needs a live root. */}
        {crewModeOn && (pause || !listed.data?.root_closed) && <CrewPauseRow root={root} paused={pause} running={c.running} leadRunning={!!slots.find(s => s.key === root)?.running} needsYou={workers.filter(w => crewWorkerGroup(w) === 'approval').length} />}
        <div className="flex-1 min-h-0 overflow-y-auto p-3 space-y-1.5" data-testid="ultra-list">
          {reopenFailed && <ErrorNotice variant="inline" message={i18nT('commandCenter.worker_reopen_failed')} askAgent />}
          {searching && shown.length === 0 && <p role="status" className="text-sm text-muted" data-testid="crew-search-empty">{i18nT('commandCenter.workers_search_empty', { query: query.trim() })}</p>}
          <h3 className="text-[11px] font-semibold uppercase tracking-[1px] text-muted">{`${i18nT('components.crewMode.ultra_crew')} · ${i18nT('components.crewMode.ultra_row_sessions', { count: c.total, running: c.running })}`}</h3>
          <ul className="space-y-1.5">
            {unfinished.map(memberRow)}
          </ul>
          {finishedTops.length > 0 && (
            <details className="group" data-testid="ultra-finished" open={searching || finishedOpen}
              onToggle={e => { if (!searching) setFinishedOpen((e.currentTarget as HTMLDetailsElement).open) }}>
              <summary className="flex items-center gap-1 cursor-pointer list-none text-[12px] text-muted hover:text-text py-1">
                <ChevronRight size={14} className="transition-transform group-open:rotate-90" aria-hidden="true" />
                {i18nT('components.crewMode.ultra_finished', { n: finishedCount })}
              </summary>
              <ul className="space-y-1.5 mt-1">
                {finished.map(memberRow)}
              </ul>
            </details>
          )}
        </div>
        </div>
      )}
    </div>
  )
}
