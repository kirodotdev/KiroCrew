/**
 * A crewmate's Sessions tab: the sessions it dispatched, at any depth, in the Crew
 * panel's groups (approval, working, waiting on the mate, waiting, done).
 *
 * The rows come from the same two sources the Crew tab reads (`CrewWorkerList`):
 * the open slots the store carries (`liveMateWorkers`) and the mate's
 * `GET .../crew-workers` answer, which adds closed sessions, the ledger line and
 * the server's own group. A 404 from an older gateway is no data, not a failure.
 *
 * A click expands the row IN PLACE and shows its recap: no navigation, no side
 * panel, no drawer (rfc-mate-sessions-folder-or-lineage.md, "Owner decision: a
 * click shows the recap in place"). The recap is read from records only, with no
 * model call (`GET .../crew-workers/{worker}/recap`).
 */
import { useId, useMemo, useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery } from '@tanstack/react-query'
import { ChevronDown, ChevronRight } from 'lucide-react'
import { api } from '../../api/client'
import { apiErrorCode, isNotFoundError } from '../../api/apiError'
import ErrorNotice from '../../components/ErrorNotice'
import type { ChatSlot, CrewWorker, CrewWorkerRecap } from '../../types'
import {
  CREW_FINISHED_GROUP,
  CREW_GROUP_LABEL_KEY,
  CREW_GROUP_TONE,
  crewRouteNotServed,
  groupCrewWorkers,
  liveMateWorkers,
  mergeCrewWorkers,
  type CrewWorkerGroup,
} from '../../lib/crewWorkers'
import { crewWorkersQueryKey } from '../chat/command-center/CrewWorkerList'
import { timeAgo } from '../../utils/timeAgo'
import { sessionDisplayTitle } from '../../utils/sessionDisplayTitle'
import { parseSourceLinkUrl } from '../../utils/pullRequestLinks'

/** How often the open tab re-reads the mate's list and an open recap. */
export const MATE_SESSIONS_POLL_MS = 10_000

/** The route is not served (an older gateway): stop asking. A 404 that names the
 *  mate as no root yet (its thread still restoring after a restart) keeps polling. */
function listNotServed(error: unknown): boolean {
  return crewRouteNotServed(error) && apiErrorCode(error) !== 'not_crew_mode_root'
}

/** The recap's session left this mate's tree (a 404 that is not the mate's own
 *  thread still restoring): stop asking. */
function recapGone(error: unknown): boolean {
  return isNotFoundError(error) && apiErrorCode(error) !== 'not_crew_mode_root'
}

/** `#N` for a GitHub pull-request link, so a label never names another PR. Parsed
 *  by the shared source-link parser (`parseSourceLinkUrl`, the one the sidebar
 *  chips use), and only when its canonical url IS the link, the shape the gateway
 *  sends; anything else is shown as the link itself. */
function prLabel(url: string): string {
  const link = parseSourceLinkUrl(url)
  return link && link.provider === 'github' && link.kind === 'change' && link.url === url ? `#${link.number}` : url
}

export const mateRecapQueryKey = (root: string, worker: string) => ['crew-worker-recap', root, worker] as const

const STATUS_KEY: Record<string, string> = {
  progress: 'pages.membersPage.recap_status_progress',
  done: 'pages.membersPage.recap_status_done',
  blocked: 'pages.membersPage.recap_status_blocked',
  question: 'pages.membersPage.recap_status_question',
}
const REASON_KEY: Record<string, string> = {
  approval: 'pages.membersPage.recap_reason_approval',
  needs_human: 'pages.membersPage.recap_reason_needs_human',
}
const STATE_KEY: Record<string, string> = {
  accepted: 'pages.membersPage.recap_state_accepted',
  rejected: 'pages.membersPage.recap_state_rejected',
  abandoned: 'pages.membersPage.recap_state_abandoned',
}

export interface MateSessionsListProps {
  /** The mate thread's slot key: the root its sessions are stamped with. */
  root: string
  /** The mate's display name, for "With <name>". */
  mateName: string
  /** The mate's open sessions from the store. */
  liveSlots: readonly ChatSlot[]
  slotsLoaded: boolean
  /** What the tab shows when the mate has no session at all. */
  empty: ReactNode
}

export default function MateSessionsList({ root, mateName, liveSlots, slotsLoaded, empty }: MateSessionsListProps) {
  const { t } = useTranslation()
  const listed = useQuery({
    queryKey: crewWorkersQueryKey(root),
    queryFn: () => api.crewWorkers(root),
    enabled: !!root,
    refetchInterval: q => (listNotServed(q.state.error) ? false : MATE_SESSIONS_POLL_MS),
  })
  const live = useMemo(() => liveMateWorkers(liveSlots, root), [liveSlots, root])
  const workers = useMemo(() => mergeCrewWorkers(listed.data?.workers, live), [listed.data?.workers, live])
  const groups = useMemo(() => groupCrewWorkers(workers), [workers])
  const [expanded, setExpanded] = useState<string | null>(null)
  const [finishedOpen, setFinishedOpen] = useState(false)
  const failed = listed.isError && !crewRouteNotServed(listed.error)

  // No root (the thread slot is still resolving) reads nothing, so it is never pending.
  if (root && workers.length === 0 && (!slotsLoaded || listed.isPending) && !failed && !crewRouteNotServed(listed.error)) {
    return (
      <div className="space-y-2" aria-hidden data-testid="mate-sessions-loading">
        <div className="h-12 rounded-2xl bg-bg-hover animate-pulse" />
        <div className="h-12 rounded-2xl bg-bg-hover animate-pulse" />
      </div>
    )
  }
  if (workers.length === 0) {
    return (
      <>
        {/* No hand-off: the Members page holds unsaved Profile, schedule and
            crew-editor drafts, and Ask the agent navigates away from them. */}
        {failed && <ErrorNotice variant="inline" message={t('pages.membersPage.sessions_read_failed')} />}
        {empty}
      </>
    )
  }
  const toggle = (key: string) => setExpanded(prev => (prev === key ? null : key))
  const label = (group: CrewWorkerGroup) =>
    group === 'with_conductor' ? t('pages.membersPage.sessions_group_waiting_on_mate', { name: mateName }) : t(CREW_GROUP_LABEL_KEY[group])
  return (
    <div className="flex flex-col gap-3" data-testid="mate-sessions">
      {/* No hand-off: the Members page holds unsaved Profile, schedule and
          crew-editor drafts, and Ask the agent navigates away from them. */}
      {failed && <ErrorNotice variant="inline" message={t('pages.membersPage.sessions_read_failed')} />}
      {groups.map(({ group, workers: rows }) => {
        const finished = group === CREW_FINISHED_GROUP
        const list = (
          <ul className="list-none m-0 p-0 rounded-2xl border border-border bg-bg overflow-hidden">
            {rows.map(w => (
              <MateSessionRow key={w.key} root={root} worker={w} open={expanded === w.key} onToggle={() => toggle(w.key)} />
            ))}
          </ul>
        )
        if (finished) {
          return (
            <details key={group} className="group" open={finishedOpen} data-testid="mate-sessions-done"
              onToggle={e => setFinishedOpen((e.currentTarget as HTMLDetailsElement).open)}>
              <summary className="flex items-center gap-1 cursor-pointer list-none text-[12px] text-muted hover:text-text py-1">
                <ChevronRight size={14} className="transition-transform group-open:rotate-90" aria-hidden="true" />
                {`${label(group)} · ${rows.length}`}
              </summary>
              <div className="mt-1">{list}</div>
            </details>
          )
        }
        return (
          <section key={group} data-testid={`mate-sessions-group-${group}`} aria-label={label(group)}>
            <h3 className="m-0 mb-1.5 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-[1px] text-muted">
              <span className={`inline-block h-1.5 w-1.5 rounded-full bg-current ${CREW_GROUP_TONE[group]}`} aria-hidden="true" />
              {`${label(group)} · ${rows.length}`}
            </h3>
            {list}
          </section>
        )
      })}
    </div>
  )
}

function MateSessionRow({ root, worker, open, onToggle }: { root: string; worker: CrewWorker; open: boolean; onToggle: () => void }) {
  const { t } = useTranslation()
  const regionId = useId()
  const title = sessionDisplayTitle({ key: worker.key, title: worker.title })
  const ts = worker.last_activity ? Date.parse(worker.last_activity) : 0
  return (
    <li className="border-t border-border first:border-t-0" data-testid="mate-session-row" data-group={worker.group}>
      <button type="button" onClick={onToggle} aria-expanded={open} aria-controls={regionId}
        className="w-full flex items-center gap-3 px-3.5 py-3 text-left hover:bg-bg-hover transition-colors cursor-pointer"
        data-testid="mate-session-toggle">
        <span className="flex-1 min-w-0 leading-tight">
          <span className="block text-[13px] font-semibold truncate">{title}</span>
          {(worker.summary || ts > 0) && (
            <span className="block text-[11.5px] text-muted truncate">
              {[worker.summary, ts > 0 ? timeAgo(ts) : ''].filter(Boolean).join(' · ')}
            </span>
          )}
        </span>
        <span className="shrink-0 flex items-center gap-1 text-[11.5px] text-muted">
          {open ? t('pages.membersPage.recap_hide') : t('pages.membersPage.recap_show')}
          <ChevronDown size={15} className={`transition-transform ${open ? 'rotate-180' : ''}`} aria-hidden="true" />
        </span>
      </button>
      {open && (
        <div id={regionId} role="region" aria-label={title} className="px-3.5 pb-3">
          <MateSessionRecap root={root} worker={worker.key} rowTitle={title} />
        </div>
      )}
    </li>
  )
}

function MateSessionRecap({ root, worker, rowTitle }: { root: string; worker: string; rowTitle: string }) {
  const { t } = useTranslation()
  const recap = useQuery({
    queryKey: mateRecapQueryKey(root, worker),
    queryFn: () => api.crewWorkerRecap(root, worker),
    // A 404 is final (the session left this mate's tree): stop asking. A 404 that
    // names the mate as no root yet (its thread still restoring) keeps polling.
    refetchInterval: q => (recapGone(q.state.error) ? false : MATE_SESSIONS_POLL_MS),
  })
  if (recap.isPending) return <p className="m-0 text-[12px] text-muted" data-testid="mate-recap-loading">{t('pages.membersPage.recap_loading')}</p>
  // No hand-off: the Members page holds unsaved Profile, schedule and
  // crew-editor drafts, and Ask the agent navigates away from them.
  const notice = <ErrorNotice variant="inline" message={t('pages.membersPage.recap_failed')} />
  if (!recap.data) return notice
  // A failed refetch keeps the recap already read on screen, under the notice.
  return <>{recap.isError && notice}<RecapBody recap={recap.data} rowTitle={rowTitle} /></>
}

/** The recap's lines, exactly the RFC table's: title, status, last report, PR link,
 *  conductor's decision, last update, open approvals and questions. */
export function RecapBody({ recap, rowTitle }: { recap: CrewWorkerRecap; rowTitle: string }) {
  const { t } = useTranslation()
  const status = [
    STATUS_KEY[recap.status] ? t(STATUS_KEY[recap.status]) : '',
    REASON_KEY[recap.reason] ? t(REASON_KEY[recap.reason]) : '',
    STATE_KEY[recap.state] ? t(STATE_KEY[recap.state]) : '',
  ].filter(Boolean).join(' · ')
  const updated = recap.last_update ? Date.parse(recap.last_update) : 0
  const hasReport = !!(recap.summary || recap.status)
  const lines: [string, ReactNode][] = []
  if (status) lines.push([t('pages.membersPage.recap_status'), status])
  if (recap.summary) lines.push([t('pages.membersPage.recap_last_report'), recap.summary])
  if (recap.pr_url || recap.pr) {
    const label = recap.pr_url ? prLabel(recap.pr_url) : `#${recap.pr}`
    lines.push([t('pages.membersPage.recap_pr'), recap.pr_url
      ? <a href={recap.pr_url} target="_blank" rel="noopener noreferrer" className="text-accent hover:underline">{label}</a>
      : label])
  }
  if (recap.decision) lines.push([t('pages.membersPage.recap_decision'), recap.decision])
  if (updated > 0) lines.push([t('pages.membersPage.recap_last_update'), timeAgo(updated)])
  const where = t('pages.membersPage.recap_answer_where')
  if (recap.open_approvals > 0) lines.push([t('pages.membersPage.recap_open_approvals'), `${recap.open_approvals} · ${where}`])
  if (recap.open_questions > 0) lines.push([t('pages.membersPage.recap_open_questions'), `${recap.open_questions} · ${where}`])
  return (
    <div className="rounded-xl bg-bg-hover px-3 py-2.5 text-[12.5px] leading-relaxed" data-testid="mate-recap">
      {recap.title && recap.title !== rowTitle && <p className="m-0 mb-1 font-semibold" data-testid="mate-recap-title">{recap.title}</p>}
      {!hasReport && <p className="m-0 text-muted" data-testid="mate-recap-empty">{t('pages.membersPage.recap_no_report')}</p>}
      {lines.length > 0 && (
        <dl className="m-0 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
          {lines.map(([k, v]) => (
            <div key={k} className="contents">
              <dt className="text-muted">{k}</dt>
              <dd className="m-0 min-w-0 break-words whitespace-pre-wrap">{v}</dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  )
}
