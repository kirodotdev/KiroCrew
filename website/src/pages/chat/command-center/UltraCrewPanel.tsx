/** The Ultra Mode chat's Crew tab: a Status | List switch. Status = Overview
 *  (the goal and "Done when" in one sentence each), Needs your input (the main
 *  chat's question as one row pointing at its card, the approval cards compact), Milestones (one row per
 *  work-ledger item) and a one-line Recap. List = the crew as an indented tree,
 *  finished members folded away; a click opens the member in the main pane. Every
 *  number comes from reads the dashboard already makes: the crew list route,
 *  the work-ledger board, the session summary (GET only), the live slot rows
 *  and the pending question cards. No model call. */
import { useCallback, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, ArrowDown, Check, ChevronRight, Loader } from 'lucide-react'
import { api } from '../../../api/client'
import type { CrewWorker } from '../../../types'
import type { SessionSummary } from '../../../types/sessionSummary'
import { crewBoardQueryKey, CREW_BOARD_POLL_MS, type WorkBoardItem, type WorkBoardResponse } from '../../../api/crewBoard'
import { artifactEntries } from '../../crewBoardRows'
import { fmtDuration, fmtList, fmtRelative } from '../../../i18n/format'
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

/** The milestone sentence: "4 of 8 done; A and B wait on you; C is running." */
export function milestoneSentence(done: number, total: number, waiting: string[], running: string[], blocked: string[] = []): string {
  if (total === 0) return i18nT('components.crewMode.ultra_recap_empty')
  const parts = [i18nT('components.crewMode.ultra_recap_head', { done, total })]
  if (waiting.length) parts.push(i18nT(waiting.length === 1 ? 'components.crewMode.ultra_recap_wait_one' : 'components.crewMode.ultra_recap_wait', { names: joinNames(waiting) }))
  if (blocked.length) parts.push(i18nT(blocked.length === 1 ? 'components.crewMode.ultra_recap_blocked_one' : 'components.crewMode.ultra_recap_blocked_many', { names: joinNames(blocked) }))
  if (running.length) parts.push(i18nT(running.length === 1 ? 'components.crewMode.ultra_recap_run_one' : 'components.crewMode.ultra_recap_run_many', { names: joinNames(running) }))
  return `${parts.join('; ')}.`
}

/** Depth of each member under the members IN THIS LIST: a member whose lead is
 *  not in the same list is drawn at the top level, never indented under nothing. */
export function depthWithin(workers: readonly CrewWorker[], root: string): Map<string, number> {
  const byKey = new Map(workers.map(w => [w.key, w]))
  const out = new Map<string, number>()
  const depth = (w: CrewWorker, seen: Set<string>): number => {
    if (out.has(w.key)) return out.get(w.key)!
    const by = crewSubDispatcher(w, root)
    const parent = by ? byKey.get(by) : undefined
    const d = parent && !seen.has(parent.key) ? depth(parent, new Set(seen).add(w.key)) + 1 : 0
    out.set(w.key, d)
    return d
  }
  workers.forEach(w => depth(w, new Set()))
  return out
}

/** Tree order: each lead followed by its own crew, recursively. */
function treeOrder(workers: readonly CrewWorker[], root: string): CrewWorker[] {
  const keys = new Set(workers.map(w => w.key))
  const kids = new Map<string, CrewWorker[]>()
  const tops: CrewWorker[] = []
  for (const w of workers) {
    const by = crewSubDispatcher(w, root)
    if (by && keys.has(by)) kids.set(by, [...(kids.get(by) ?? []), w])
    else tops.push(w)
  }
  const out: CrewWorker[] = []
  const seen = new Set<string>()
  const visit = (w: CrewWorker) => { if (seen.has(w.key)) return; seen.add(w.key); out.push(w); (kids.get(w.key) ?? []).forEach(visit) }
  tops.forEach(visit)
  workers.forEach(visit)
  return out
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

type MilestoneState = 'done' | 'blocked' | 'running' | 'next'

export function milestoneState(item: WorkBoardItem, waiting: boolean): MilestoneState {
  // A closed item is finished whatever its last report said. Then waiting on
  // the person; then a session working again reads as running even after an
  // earlier "done" report. A blocked report counts only until the conductor
  // decides on it (`decided_at` at or after the report hands it back).
  if (item.state === 'accepted' || item.terminal) return 'done'
  // `waiting` is a pending approval of the item's member (on the person); a
  // worker's unanswered question (`outstanding`) waits on the main chat.
  if (waiting || item.outstanding) return 'blocked'
  if (item.alive === 'running') return 'running'
  // A report left open by a worker whose session closed is not running: it
  // stopped there, so it reads as blocked (the item itself is not done).
  if (item.alive === 'closed' && (item.status === 'progress' || item.status === 'blocked' || item.status === 'question')) return 'blocked'
  const decided = !!item.decided_at && (!item.last_report_at || item.decided_at >= item.last_report_at)
  if (item.status === 'blocked' && !decided) return 'blocked'
  if (item.status === 'progress' || item.status === 'blocked' || item.status === 'question') return 'running'
  if (item.status === 'done') return 'done'
  return 'next'
}

function minutesBetween(a: string | null | undefined, b: string | number | null | undefined): number {
  const ta = a ? Date.parse(a) : NaN
  const tb = typeof b === 'number' ? b : b ? Date.parse(b) : NaN
  return Number.isFinite(ta) && Number.isFinite(tb) ? Math.max(0, Math.round((tb - ta) / 60000)) : NaN
}

function fmtMinutes(mins: number): string {
  if (!Number.isFinite(mins)) return ''
  if (mins < 60) return fmtDuration([[Math.max(1, mins), 'minute']], { unitDisplay: 'narrow' } as never)
  return fmtDuration([[Math.floor(mins / 60), 'hour'], [mins % 60, 'minute']], { dropZero: true, unitDisplay: 'narrow' } as never)
}

const STATE_ICON: Record<MilestoneState, JSX.Element> = {
  done: <span className="w-5 h-5 rounded-full grid place-items-center bg-[color-mix(in_srgb,var(--ok)_18%,transparent)] crew-tone-ok"><Check size={12} strokeWidth={3} /></span>,
  blocked: <span className="w-5 h-5 rounded-full grid place-items-center bg-[color-mix(in_srgb,var(--warn)_20%,transparent)] crew-tone-warn"><AlertTriangle size={11} strokeWidth={2.5} /></span>,
  running: <span className="w-5 h-5 rounded-full grid place-items-center bg-[color-mix(in_srgb,var(--aim)_18%,transparent)] ultra-accent"><Loader size={12} className="motion-safe:animate-spin" /></span>,
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
  } else {
    const text = state === 'blocked' ? waitingOn
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
        <span className={`flex items-center gap-1 min-w-0 text-[12px] ${state === 'blocked' ? 'crew-tone-warn' : 'text-muted'}`}>{sub}</span>
      </span>
      <span className={`shrink-0 text-[11px] tabular-nums mt-0.5 ${state === 'running' ? 'ultra-accent font-medium' : 'text-muted'}`}>{right}</span>
    </li>
  )
}

function MemberRow({ w, depth, onOpen, current }: { w: CrewWorker; depth: number; onOpen: () => void; current: boolean }) {
  const title = w.title || i18nT('commandCenter.worker_untitled')
  return (
    <li data-testid="ultra-member" data-depth={depth} style={{ paddingLeft: depth * 16 }} className="relative">
      {depth > 0 && <span aria-hidden="true" className="absolute top-0 bottom-0 border-l border-border" style={{ left: depth * 16 - 9 }} />}
      <button type="button" onClick={onOpen} aria-current={current ? 'true' : undefined}
        className={`w-full text-left rounded-lg border px-3 py-2 text-sm transition-colors ${current ? 'crew-card' : 'border-border bg-bg-elevated/40 hover:bg-bg-hover'}`}>
        <span className="flex items-center gap-2 min-w-0">
          <span className="font-medium text-text truncate">{title}</span>
          <span className="ml-auto"><CrewStatusDot group={crewWorkerGroup(w)} /></span>
        </span>
        {w.summary && <span className="block text-[12px] text-text/80 truncate mt-0.5">{w.summary}</span>}
        {w.last_activity && <span className="block text-[11px] text-muted truncate mt-0.5">{fmtRelativeTime(w.last_activity)}</span>}
      </button>
    </li>
  )
}

export default function UltraCrewPanel({ root, viewing, active }: { root: string; viewing: string | null; active: boolean }) {
  const dispatch = useAppDispatch()
  const store = useAppStore()
  const isMobile = useIsMobile()
  const listed = useCrewWorkers(root, active)
  const live = useLiveCrewWorkers(root)
  const workers = useMemo(() => mergeCrewWorkers(listed.data?.workers, live), [listed.data, live])
  const slots = useAppSelector(s => s.dashboard.slots)
  const approvals = crewApprovalRequests(slots, root, new Map())
  // The same pending-question record the chat's own card reads.
  const pendingQuestion = useAppSelector(st => pendingQuestionFor(st.chat.pendingQuestions, root))
  const questionCount = pendingQuestion?.questions?.length ? 1 : 0
  const c = ultraCounts(workers, approvals.length, questionCount)
  const needYou = c.approvals + c.questions
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
  const unfinishedRows = shown.filter(w => crewWorkerGroup(w) !== 'done')
  const finishedRows = shown.filter(w => crewWorkerGroup(w) === 'done')
  const unfinished = treeOrder(unfinishedRows, root)
  const finished = treeOrder(finishedRows, root)
  const depthOpen = depthWithin(unfinishedRows, root)
  const depthDone = depthWithin(finishedRows, root)
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
  const milestones = items.map(item => {
    const state = milestoneState(item, approvalByItem.has(item.item_id))
    const waitingOn = approvalByItem.get(item.item_id) || (item.outstanding ? i18nT('commandCenter.worker_group_with_conductor') : (firstSentence(item.summary) || i18nT('components.crewMode.ultra_ms_blocked')))
    return { item, state, waitingOn }
  })
  const doneMs = milestones.filter(m => m.state === 'done')
  const openMs = milestones.filter(m => m.state !== 'done')
  const msDone = doneMs.length
  const lastReport = items.map(i => i.last_report_at).filter(Boolean).sort().pop() || null
  // Stalled is the board's own `stale` flag on open work (quiet, not running,
  // the move with the worker); a finished crew shows no pulse at all.
  const stalled = openMs.some(m => m.item.stale)
  // "moving" only while a milestone runs; with nothing running and nothing
  // stale (blocked or next only) there is no pulse.
  const moving = openMs.some(m => m.state === 'running')
  const openItem = (item: WorkBoardItem) => {
    const w = workers.find(x => x.ledger_item === item.item_id)
    return w ? () => open(w) : undefined
  }
  const asksPerson = (m: typeof milestones[number]) => approvalByItem.has(m.item.item_id)
  const recap = summaryRecap(summary.data, items.map(i => i.title))
    || (items.length
      ? milestoneSentence(msDone, items.length, milestones.filter(m => m.state === 'blocked' && asksPerson(m)).map(m => m.item.title), milestones.filter(m => m.state === 'running').map(m => m.item.title), milestones.filter(m => m.state === 'blocked' && !asksPerson(m)).map(m => m.item.title))
      : ultraRecap(c, waitingNames, questionCount > 0))

  return (
    <div className="h-full flex flex-col min-h-0" data-testid="ultra-crew-panel" data-view={view}>
      {/* A crew member on screen: one click back to its root. */}
      {viewing && viewing !== root && <CrewPanelBack root={root} />}
      {/* A failed crew read, shown in both views; a gateway that does not serve
          the route is not a failure (the live rows stand in). */}
      {listed.isError && !crewRouteNotServed(listed.error) && <div className="shrink-0 px-3 pt-3"><ErrorNotice variant="inline" message={i18nT(live.length ? 'commandCenter.workers_error_live' : 'commandCenter.workers_error')} askAgent /></div>}
      <div className="shrink-0 px-3 pt-3">
        <div role="group" aria-label={i18nT('commandCenter.workers_view_label')} className="flex gap-1 rounded-lg border border-border bg-bg-hover/40 p-0.5" data-testid="ultra-view-switch">
          {(['status', 'list'] as const).map(v => (
            <button key={v} type="button" aria-pressed={view === v} data-testid={`ultra-view-${v}`} onClick={() => setView(v)}
              className={`flex-1 rounded-md px-2 py-1 text-[12px] font-medium transition-colors ${view === v ? 'bg-bg-elevated text-text shadow-sm' : 'text-muted hover:text-text'}`}>
              {i18nT(v === 'status' ? 'components.crewMode.ultra_view_status' : 'components.crewMode.ultra_view_list')}
            </button>
          ))}
        </div>
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
            <h3 className="text-[11px] font-semibold uppercase tracking-[1px] text-muted">{`${i18nT('components.crewMode.ultra_needs_input')} · ${needYou}`}</h3>
            {/* Blocked work that needs no approval is not counted here, but the
                line does not claim nothing is wrong: it points at the rows below. */}
            {needYou === 0 && <p className="text-[12px] text-muted" data-testid="ultra-needs-you-none">{i18nT(milestones.some(m => m.state === 'blocked') ? 'components.crewMode.ultra_needs_you_none_blocked' : 'components.crewMode.ultra_needs_you_none')}</p>}
            {pendingQuestion?.questions?.length ? (
              <div className="flex items-center gap-2 rounded-lg border border-border bg-bg-elevated/40 px-2.5 py-2" data-testid="ultra-main-question">
                <span className="flex-1 min-w-0 text-[13px] text-text truncate" title={pendingQuestion.questions[0].question}>{pendingQuestion.questions[0].question}</span>
                <button type="button" onClick={answerInChat} data-testid="ultra-answer-in-chat"
                  className="shrink-0 inline-flex items-center gap-1 h-7 px-2 rounded-md text-[12px] font-medium ultra-accent hover:bg-bg-hover">
                  {i18nT('components.crewMode.ultra_answer_in_chat')}<ArrowDown size={12} aria-hidden="true" />
                </button>
              </div>
            ) : null}
            <CrewApprovalCards root={root} compact />
          </section>
          {/* (3) Milestones: one row per ledger item, as outcomes. */}
          <section data-testid="ultra-milestones">
            <div className="flex items-center gap-2">
              <h3 className="text-[11px] font-semibold uppercase tracking-[1px] text-muted">{i18nT('components.crewMode.ultra_milestones', { done: msDone, total: items.length })}</h3>
              {lastReport && (stalled || moving) && (
                <span className={`ml-auto text-[11px] inline-flex items-center gap-1 ${stalled ? 'crew-tone-warn' : 'crew-tone-ok'}`} data-testid="ultra-pulse">
                  <span aria-hidden="true" className="w-1.5 h-1.5 rounded-full bg-current" />
                  {stalled ? i18nT('components.crewMode.ultra_stalled') : i18nT('components.crewMode.ultra_moving', { when: fmtRelative(lastReport) })}
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
        <div className="flex-1 min-h-0 overflow-y-auto p-3 space-y-1.5" data-testid="ultra-list">
          {reopenFailed && <ErrorNotice variant="inline" message={i18nT('commandCenter.worker_reopen_failed')} askAgent />}
          {searching && shown.length === 0 && <p role="status" className="text-sm text-muted" data-testid="crew-search-empty">{i18nT('commandCenter.workers_search_empty', { query: query.trim() })}</p>}
          <h3 className="text-[11px] font-semibold uppercase tracking-[1px] text-muted">{`${i18nT('components.crewMode.ultra_crew')} · ${i18nT('components.crewMode.ultra_row_sessions', { count: c.total, running: c.running })}`}</h3>
          <ul className="space-y-1.5">
            {unfinished.map(w => <MemberRow key={w.key} w={w} depth={depthOpen.get(w.key) ?? 0} current={w.key === viewing} onOpen={() => open(w)} />)}
          </ul>
          {finished.length > 0 && (
            <details className="group" data-testid="ultra-finished" open={searching || finishedOpen}
              onToggle={e => { if (!searching) setFinishedOpen((e.currentTarget as HTMLDetailsElement).open) }}>
              <summary className="flex items-center gap-1 cursor-pointer list-none text-[12px] text-muted hover:text-text py-1">
                <ChevronRight size={14} className="transition-transform group-open:rotate-90" aria-hidden="true" />
                {i18nT('components.crewMode.ultra_finished', { n: finished.length })}
              </summary>
              <ul className="space-y-1.5 mt-1">
                {finished.map(w => <MemberRow key={w.key} w={w} depth={depthDone.get(w.key) ?? 0} current={w.key === viewing} onOpen={() => open(w)} />)}
              </ul>
            </details>
          )}
        </div>
        </div>
      )}
    </div>
  )
}
