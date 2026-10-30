import { useCallback, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { ChevronLeft, ChevronRight } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../../../api/client'
import type { CrewWorker } from '../../../types'
import ErrorNotice from '../../../components/ErrorNotice'
import { SearchFilterBar } from '../../../components/SearchFilterBar'
import { safeGetSessionItem, safeSetSessionItem } from '../../../utils/safeStorage'
import { fmtRelativeTime } from '../sessionOrder'
import { useAppDispatch, useAppSelector, useAppStore, type RootState } from '../../../store'
import { closeActivityPanel, openActivityPanel, resumeFromHistory, switchSlot } from '../../../store/chatSlice'
import { useIsMobile } from '../../../hooks/useIsMobile'
import { usePersistedBool } from '../../../hooks/usePersistedBool'
import { openPanelView } from '../../../hooks/usePanelTabs'
import { CrewGhostBadge, CrewStatusDot } from '../../../components/crew/CrewModeMarks'
import { useCrewBack } from '../../../components/crew/useCrewBack'
import { CREW_FINISHED_GROUP, CREW_GROUP_LABEL_KEY, crewRouteNotServed, crewSubDispatcher, crewWorkerGroup, groupCrewWorkers, liveCrewWorkers, mergeCrewWorkers, orderUnderDispatcher, searchCrewWorkers } from '../../../lib/crewWorkers'
import CrewTimeline from './CrewTimeline'

/** The status labels, keyed by the wire value `GET .../crew-workers` sends. */
export const CREW_WORKER_STATUS_KEY: Record<CrewWorker['status'], string> = {
  working: 'commandCenter.worker_status_working',
  idle: 'commandCenter.worker_status_idle',
  needs_approval: 'commandCenter.worker_status_needs_approval',
  asking: 'commandCenter.worker_status_asking',
  closed: 'commandCenter.worker_status_closed',
}

/** The query key of one root's worker list, for anything that must re-read it. */
export const crewWorkersQueryKey = (root: string) => ['crew-workers', root] as const

/** Where the Done group's open/closed choice is kept (closed by default). */
export const CREW_FINISHED_OPEN_KEY = 'mc-crew-workers-finished-open'

/** The search box shows from this many workers up: a shorter list is read at a glance. */
export const CREW_WORKER_SEARCH_MIN = 6

/** Where a root's search query is kept for this tab: opening a match switches
 *  the slot and remounts the list, and the person keeps their search. */
export const crewWorkerQueryKey = (root: string) => `mc-crew-workers-search:${root}`

function useCrewWorkerQuery(root: string): [string, (query: string) => void] {
  const [query, setState] = useState(() => safeGetSessionItem(crewWorkerQueryKey(root)) || '')
  const set = useCallback((next: string) => {
    setState(next)
    // Storage off: the query lives as long as the list.
    safeSetSessionItem(crewWorkerQueryKey(root), next)
  }, [root])
  return [query, set]
}

/** `crewRouteNotServed`, for the worker list route. */
const routeNotServed = crewRouteNotServed

/** One root's worker list, re-read every 5s while shown. Shared by the side panel
 *  and the worker view's breadcrumb, so both name a worker the same way. */
export function useCrewWorkers(root: string, active: boolean) {
  return useQuery({
    queryKey: crewWorkersQueryKey(root),
    queryFn: () => api.crewWorkers(root),
    enabled: active && !!root,
    refetchInterval: query => (active && !routeNotServed(query.state.error) ? 5000 : false),
  })
}

/** A root's OPEN workers from the slot store, shaped as worker-list rows
 *  (`liveCrewWorkers`): the part of the list that needs no route. */
export function useLiveCrewWorkers(root: string): CrewWorker[] {
  const slots = useAppSelector(s => s.dashboard.slots)
  return useMemo(() => liveCrewWorkers(slots, root), [slots, root])
}

/** One root's workers for its session-list summary and the breadcrumb: the same
 *  query (and cache) the panel reads, re-read every 15s when the panel is not.
 *  `failed` is true while the latest read failed: the caller says so rather than
 *  showing counts from an older read, or nothing at all. */
export function useCrewWorkerSummary(root: string): {
  workers: CrewWorker[] | undefined; failed: boolean; savedTitle: string
  /** The last list a read gave, kept through a failed poll (the cache's data),
   *  with the open live rows the store carries on its own. */
  lastWorkers: CrewWorker[] | undefined
} {
  const query = useQuery({
    queryKey: crewWorkersQueryKey(root),
    queryFn: () => api.crewWorkers(root),
    enabled: !!root,
    refetchInterval: query => (routeNotServed(query.state.error) ? false : 15_000),
  })
  const live = useLiveCrewWorkers(root)
  // A closed root's list is read from saved data and names the root's saved title.
  const savedTitle = query.data?.root_closed ? query.data.root_title || '' : ''
  // The live rows are this gateway's own and need no read: they are always in.
  const lastWorkers = query.data?.workers || live.length ? mergeCrewWorkers(query.data?.workers, live) : undefined
  // A route the gateway does not serve is no data, not a failed read: the row
  // shows what the live rows say and raises no notice.
  const failed = query.isError && !routeNotServed(query.error)
  return { workers: failed ? (live.length ? live : undefined) : lastWorkers, failed, savedTitle, lastWorkers }
}

/** Open one of a root's sessions -- a worker, or the root itself -- in the main
 *  pane, with the Workers tab in front in its side panel so switching is one click.
 *  A plain `switchSlot`, like a tab click: the composer keeps each session's draft,
 *  so leaving one needs no guard.
 *
 *  Each session remembers its own panel open state, so on its own the switch would
 *  restore the target's (usually closed) panel. With `keepPanel` -- every desktop
 *  open -- the panel is opened on the target too, so the Workers list stays beside
 *  the worker. A phone passes false: there the panel is a sheet over the chat. */
export function openCrewSession(dispatch: ReturnType<typeof useAppDispatch>, key: string, keepPanel = false) {
  openPanelView(key, 'workers')
  dispatch(switchSlot({ key, announceOnMissing: true, gesture: true }))
  // `switchSlot.pending` has already made `key` the active slot and restored ITS
  // remembered panel state. Desktop keeps the panel beside the chat; a phone
  // always lands on the chat, even when the target last had its sheet open.
  dispatch(keepPanel ? openActivityPanel() : closeActivityPanel())
}

/** Reopen a CLOSED worker from its saved transcript and open it like a live one.
 *
 *  History leaves workers out, so the conductor's panel is their way back: the
 *  reopened tab keeps its root (its saved metadata names its creator), so it is
 *  listed under this root again and can be steered. Resolves false when the
 *  transcript could not be reopened. */
export async function reopenCrewWorker(
  dispatch: ReturnType<typeof useAppDispatch>, getState: () => RootState, key: string, title: string, keepPanel = false,
): Promise<boolean> {
  return (await reopenCrewSession(dispatch, getState, key, title, keepPanel)).ok
}

/** `reopenCrewWorker`, saying also whether the reopened session took the view
 *  (`activated`) and under which key. A caller that moves the person somewhere
 *  after the reopen moves them only when `activated` is true: false means they
 *  picked another session while it was pending, and that newer pick wins. */
export async function reopenCrewSession(
  dispatch: ReturnType<typeof useAppDispatch>, getState: () => RootState, key: string, title: string, keepPanel = false,
): Promise<{ ok: boolean; activated: boolean; key: string }> {
  try {
    const resuming = dispatch(resumeFromHistory({ key: `dashboard_${key}`, title }))
    const resumed = await resuming.unwrap()
    if (!resumed.ok) return { ok: false, activated: false, key }
    const opened = resumed.key || key
    // The resume thunk decides whether its answer may take the view: a newer
    // resume, or a session the person opened while this one was pending, wins
    // (`resumeFromHistory.fulfilled`). When it declined, the worker is reopened
    // as a tab but the person stays where they went; switching here anyway
    // would undo their newer pick.
    if (getState().chat.lastResumeActivation?.requestId !== resuming.requestId) return { ok: true, activated: false, key: opened }
    openCrewSession(dispatch, opened, keepPanel)
    return { ok: true, activated: true, key: opened }
  } catch {
    return { ok: false, activated: false, key }
  }
}

/**
 * A Crew Mode root's workers in the side panel: cards grouped Approval needed, Working,
 * Waiting, Done, each heading with its count, what needs the person first. Done
 * folds under its heading, closed by default (`CREW_FINISHED_OPEN_KEY`). From
 * `CREW_WORKER_SEARCH_MIN` workers up, a search box over the cards filters them
 * by title or task line, finished ones included (`searchCrewWorkers`). A
 * card opens that worker in the MAIN pane like any session; the card for the
 * worker on screen is marked current, so moving between workers is one click.
 *
 * Props:
 * - `root` -- the Crew Mode root's slot key (`crew_mode_root` on its row, or the
 *   `crew_root` a worker row names).
 * - `viewing` -- the slot the main pane shows: the root or one of its workers.
 * - `active` -- the panel is on screen; the list is re-read only while it is.
 *
 * A **List | Timeline** switch on top swaps the cards for the root's timeline
 * (`CrewTimeline`); List is the default. A phone has no switch: its panel is a
 * sheet too narrow for lanes, so it always shows the list.
 */
export default function CrewWorkerList({ root, viewing, active }: {
  root: string
  viewing: string | null
  active: boolean
}) {
  const { t } = useTranslation()
  const dispatch = useAppDispatch()
  const store = useAppStore()
  // On a phone the panel is a sheet over the chat: opening a worker closes it, so
  // the worker fills the screen and its breadcrumb's Back is the way out.
  const isMobile = useIsMobile()
  const [reopenFailed, setReopenFailed] = useState(false)
  // Stable across renders, so the timeline's memoised lanes keep their props. A
  // closed worker (its tab is gone) is reopened from its saved transcript first.
  const open = useCallback((key: string, title = '', closed = false) => {
    if (!closed) {
      openCrewSession(dispatch, key, !isMobile)
      return
    }
    setReopenFailed(false)
    void reopenCrewWorker(dispatch, store.getState, key, title, !isMobile).then(ok => { if (!ok) setReopenFailed(true) })
  }, [dispatch, store, isMobile])
  const [view, setView] = useState<'list' | 'timeline'>('list')
  const [finishedOpen, setFinishedOpen] = usePersistedBool(CREW_FINISHED_OPEN_KEY, false)
  const showTimeline = view === 'timeline' && !isMobile
  // Read only while the cards are on screen: the timeline has its own read. The
  // root's open workers come from the slot store whatever the read does, so a
  // worker left out of the person's lists is always listed here.
  const workers = useCrewWorkers(root, active && !showTimeline)
  const live = useLiveCrewWorkers(root)
  const all = mergeCrewWorkers(workers.data?.workers, live)
  // The search filters the cards in place, live and finished alike. While it is
  // on, the Done group lists its matches open; the person's fold choice is left
  // as it was, so clearing the search folds it back.
  const [query, setQuery] = useCrewWorkerQuery(root)
  const searching = query.trim() !== ''
  const showSearch = searching || all.length >= CREW_WORKER_SEARCH_MIN
  const groups = groupCrewWorkers(searching ? searchCrewWorkers(all, query) : all)
  // A sub-conductor's crew: one indent step and a "from <dispatcher>" note,
  // whatever the depth. The note names it for readers too, not the indent alone.
  const titleOf = (key: string) => all.find(w => w.key === key)?.title || t('commandCenter.worker_untitled')
  return <div className="h-full flex flex-col min-w-0" data-testid="crew-worker-list">
    {/* A worker on screen: one click back to its root, above both views. */}
    {viewing && viewing !== root && <CrewPanelBack root={root} />}
    {!isMobile && <CrewViewSwitch view={view} onChange={setView} />}
    {/* A failed reopen, above both views so the Timeline's lanes report it too.
        Nothing to lose: the composer's draft is saved per chat (utils/chatDrafts),
        so the hand-off may navigate. */}
    {reopenFailed && <div className="px-3 pt-3"><ErrorNotice message={t('commandCenter.worker_reopen_failed')} askAgent /></div>}
    {!showTimeline && showSearch && <CrewWorkerSearch query={query} onChange={setQuery} />}
    {showTimeline
      ? <div className="flex-1 min-h-0 overflow-y-auto"><CrewTimeline root={root} viewing={viewing} active={active} onOpen={open} /></div>
      : <div className="flex-1 min-h-0 overflow-y-auto p-3 space-y-4">
      {/* A read failure with nothing to lose: the composer's draft is saved per
          chat (utils/chatDrafts), so the hand-off may navigate. */}
      {workers.isError && !routeNotServed(workers.error) && <ErrorNotice message={t(live.length ? 'commandCenter.workers_error_live' : 'commandCenter.workers_error')} askAgent />}
      {/* The first read is in flight and the store names no worker yet: say so,
          so a slow read is not an empty panel. */}
      {workers.isPending && live.length === 0 && <p role="status" aria-busy="true" className="text-sm text-muted" data-testid="crew-workers-loading">{t('commandCenter.workers_loading')}</p>}
      {!searching && (workers.isSuccess || routeNotServed(workers.error)) && groups.length === 0 && <p className="text-sm text-muted">{t('commandCenter.workers_empty')}</p>}
      {/* "No match" only once the full list is read: while the read is pending or
          failed, only the live rows are here, and a finished match may be missing. */}
      {searching && (workers.isSuccess || routeNotServed(workers.error)) && groups.length === 0 && <p role="status" className="text-sm text-muted" data-testid="crew-search-empty">{t('commandCenter.workers_search_empty', { query: query.trim() })}</p>}
      {groups.map(({ group, workers: rows }) => {
        const label = `${t(CREW_GROUP_LABEL_KEY[group])} · ${rows.length}`
        // Finished workers fold under one toggle, closed by default, so the live
        // ones stay on top. The choice is kept per browser like other view choices.
        // A search shows its finished matches open, under a plain heading.
        const folds = group === CREW_FINISHED_GROUP && !searching
        // Folded, it still shows the worker on screen, so its card stays marked current.
        const shownRows = !folds || finishedOpen ? rows : rows.filter(w => w.key === viewing)
        return <section key={group} aria-labelledby={`crew-group-${group}`} data-testid="crew-worker-group" data-group={group}>
          <h3 id={`crew-group-${group}`} className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted">
            {folds
              ? <button
                  type="button"
                  aria-expanded={finishedOpen}
                  data-testid="crew-finished-toggle"
                  onClick={() => setFinishedOpen(!finishedOpen)}
                  className="-mx-1 inline-flex min-h-6 items-center gap-1 rounded px-1 uppercase tracking-wide hover:text-text focus-ring"
                >
                  <ChevronRight size={12} aria-hidden="true" className={`shrink-0 transition-transform motion-reduce:transition-none ${finishedOpen ? 'rotate-90' : ''}`} />
                  {label}
                </button>
              : label}
          </h3>
          {shownRows.length > 0 && <ul className="space-y-2">
            {orderUnderDispatcher(shownRows, root).map(w => {
              const by = crewSubDispatcher(w, root)
              return <CrewWorkerCard key={w.key} worker={w} current={w.key === viewing} from={by ? titleOf(by) : ''} onOpen={() => open(w.key, w.title, w.status === 'closed')} />
            })}
          </ul>}
        </section>
      })}
    </div>}
    <p className="shrink-0 border-t border-border px-3 py-2 text-[12px] text-muted" data-testid="crew-projects-soon">{t('commandCenter.projects_coming_soon')}</p>
  </div>
}

/** The panel's way back to the root while one of its workers is on screen: the
 *  same Back the worker view's breadcrumb carries, naming the root it returns to.
 *  Desktop keeps the panel open on the root. */
function CrewPanelBack({ root }: { root: string }) {
  const { t } = useTranslation()
  const { back, label, rootClosed, rootTitle, reopenFailed } = useCrewBack(root)
  return <div className="shrink-0 px-3 pt-3">
    <button type="button" onClick={back} data-testid="crew-panel-back" data-root-closed={rootClosed ? 'true' : undefined}
      aria-label={`${label} · ${rootTitle}`}
      className="w-full min-h-11 flex items-center gap-2 rounded-lg border border-border bg-bg-elevated px-2.5 py-2 text-left text-[13px] text-text hover:bg-bg-hover cursor-pointer">
      <ChevronLeft size={14} aria-hidden="true" className="shrink-0 text-muted" />
      <span className="font-medium shrink-0">{label}</span>
      <span className="ml-auto flex items-center gap-1.5 min-w-0" title={rootTitle}>
        <CrewGhostBadge size={16} />
        <span className="crew-grad-text truncate">{rootTitle}</span>
      </span>
    </button>
    {reopenFailed && <div className="pt-2"><ErrorNotice variant="inline" message={t('commandCenter.worker_reopen_failed')} askAgent /></div>}
  </div>
}

/** The panel's List | Timeline switch: two radio-like buttons in one group. */
function CrewViewSwitch({ view, onChange }: { view: 'list' | 'timeline'; onChange: (view: 'list' | 'timeline') => void }) {
  const { t } = useTranslation()
  const option = (value: 'list' | 'timeline', label: string) => (
    <button
      type="button"
      aria-pressed={view === value}
      data-testid={`crew-view-${value}`}
      onClick={() => onChange(value)}
      className={`flex-1 rounded-md px-2 py-1 text-[12px] font-medium transition-colors ${view === value ? 'bg-bg-elevated text-text shadow-sm' : 'text-muted hover:text-text'}`}
    >{label}</button>
  )
  return <div className="shrink-0 px-3 pt-3">
    <div role="group" aria-label={t('commandCenter.workers_view_label')} className="flex gap-1 rounded-lg border border-border bg-bg-hover/40 p-0.5" data-testid="crew-view-switch">
      {option('list', t('commandCenter.workers_view_list'))}
      {option('timeline', t('commandCenter.workers_view_timeline'))}
    </div>
  </div>
}

/** The list's search box, the shared list search row: filters as the person types; Esc or the clear button empties it. */
function CrewWorkerSearch({ query, onChange }: { query: string; onChange: (query: string) => void }) {
  const { t } = useTranslation()
  return <div className="shrink-0" role="search">
    <SearchFilterBar
      value={query}
      onChange={onChange}
      placeholder={t('commandCenter.workers_search_placeholder')}
      clearLabel={t('commandCenter.workers_search_clear')}
      aria-label={t('commandCenter.workers_search_label')}
      inputTestId="crew-search"
      className="px-3 pt-3"
      onKeyDown={e => {
        // Esc clears a typed query; on an empty box it is left to the panel.
        if (e.key === 'Escape' && query) {
          e.preventDefault()
          e.stopPropagation()
          onChange('')
        }
      }}
    />
  </div>
}

function CrewWorkerCard({ worker, current, from, onOpen }: { worker: CrewWorker; current: boolean; from: string; onOpen: () => void }) {
  const { t } = useTranslation()
  const closed = worker.status === 'closed'
  const title = worker.title || t('commandCenter.worker_untitled')
  const meta = [
    worker.last_activity ? fmtRelativeTime(worker.last_activity) : '',
  ].filter(Boolean).join(' · ')
  const body = <>
    <span className="flex items-center gap-2 min-w-0">
      <span className="font-medium text-text truncate">{title}</span>
      <span className="ml-auto"><CrewStatusDot group={crewWorkerGroup(worker)} /></span>
    </span>
    {from && <span className="block text-[11px] text-muted truncate" data-testid="crew-worker-from">{t('commandCenter.worker_dispatched_by', { name: from })}</span>}
    {worker.summary && <span className="block text-[12px] text-text/80 truncate mt-0.5" data-testid="crew-worker-summary">{worker.summary}</span>}
    {meta && <span className="block text-[12px] text-muted truncate mt-0.5">{meta}</span>}
  </>
  const cls = `block w-full text-left rounded-lg border px-3 py-2.5 text-sm transition-colors ${current ? 'crew-card' : 'border-border bg-bg-elevated/40 hover:bg-bg-hover'}`
  // A closed worker opens too: from its saved transcript, under this root.
  return <li data-testid="crew-worker-row" data-worker={worker.key} data-indent={from ? '1' : undefined} className={from ? 'ml-4' : undefined}>
    {closed
      ? <button type="button" className={`${cls} cursor-pointer min-h-11`} aria-label={from ? `${t('commandCenter.worker_reopen', { title })} · ${t('commandCenter.worker_dispatched_by', { name: from })}` : t('commandCenter.worker_reopen', { title })} data-testid="crew-worker-reopen" onClick={onOpen}>{body}</button>
      : <button type="button" className={`${cls} cursor-pointer min-h-11`} aria-current={current ? 'true' : undefined} onClick={onOpen}>{body}</button>}
  </li>
}

/** The side panel's Workers tab for `slot`: its root's worker list when the slot
 *  is a Crew Mode root or one of its workers, and one line saying so otherwise. */
export function CrewWorkersTab({ slot, active }: { slot: string | null; active: boolean }) {
  const { t } = useTranslation()
  const row = useAppSelector(s => (slot ? s.dashboard.slots.find(item => item.key === slot) : undefined))
  const root = row?.crew_mode_root ? row.key : row?.crew_root || ''
  if (!root) return <p className="p-3 text-sm text-muted" data-testid="crew-workers-none">{t('commandCenter.workers_not_crew')}</p>
  return <CrewWorkerList root={root} viewing={slot} active={active} />
}
