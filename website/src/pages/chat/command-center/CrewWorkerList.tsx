import { useCallback, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery } from '@tanstack/react-query'
import { api } from '../../../api/client'
import type { CrewWorker } from '../../../types'
import ErrorNotice from '../../../components/ErrorNotice'
import { SearchFilterBar } from '../../../components/SearchFilterBar'
import { safeGetSessionItem, safeSetSessionItem } from '../../../utils/safeStorage'
import { useAppDispatch, useAppSelector, type RootState } from '../../../store'
import { closeActivityPanel, openActivityPanel, resumeFromHistory, switchSlot } from '../../../store/chatSlice'
import { openPanelView } from '../../../hooks/usePanelTabs'
import { ChevronLeft } from 'lucide-react'
import { useCrewBack } from '../../../components/crew/useCrewBack'
import { crewRouteNotServed, liveCrewWorkers, mergeCrewWorkers } from '../../../lib/crewWorkers'
import { crewModeRoot, crewRootOf } from '../../../lib/crewMode'
import UltraCrewPanel from './UltraCrewPanel'

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

export function useCrewWorkerQuery(root: string): [string, (query: string) => void] {
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
  // Held: the target's history loads after the switch, and a PR link in it
  // must not move the panel off Workers, where the way back lives.
  openPanelView(key, 'workers', { hold: true })
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

/** The List's search box (from `CREW_WORKER_SEARCH_MIN` crew members up). */
export function CrewWorkerSearch({ query, onChange }: { query: string; onChange: (query: string) => void }) {
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

/** The one way back to the root while one of its workers is on screen: "Back to
 *  main chat" ("Open main chat" while the root is closed). The worker view's
 *  breadcrumb names the sessions and has no control of its own. Desktop keeps
 *  the panel open on the root. */
export function CrewPanelBack({ root }: { root: string }) {
  const { t } = useTranslation()
  const { back, label, rootClosed, reopenFailed } = useCrewBack(root)
  return <div className="shrink-0 px-3 pt-3">
    <button type="button" onClick={back} data-testid="crew-panel-back" data-root-closed={rootClosed ? 'true' : undefined}
      className="w-full min-h-11 flex items-center gap-2 rounded-lg border border-border bg-bg-elevated px-2.5 py-2 text-left text-[13px] text-text hover:bg-bg-hover cursor-pointer">
      <ChevronLeft size={14} aria-hidden="true" className="shrink-0 text-muted" />
      <span className="font-medium truncate">{label}</span>
    </button>
    {reopenFailed && <div className="pt-2"><ErrorNotice variant="inline" message={t('commandCenter.worker_reopen_failed')} askAgent /></div>}
  </div>
}

/** The side panel's Crew tab for `slot`: its root's Ultra Mode panel when the slot
 *  is an Ultra Mode root or one of its crew members, and one line saying so otherwise. */
export function CrewWorkersTab({ slot, active }: { slot: string | null; active: boolean }) {
  const { t } = useTranslation()
  const row = useAppSelector(s => (slot ? s.dashboard.slots.find(item => item.key === slot) : undefined))
  const root = crewModeRoot(row) && row ? row.key : crewRootOf(row)
  // A brand-new chat whose Ultra Mode switch is landing has no root
  // stamp yet; show its (empty) Ultra panel instead of "not an Ultra Mode chat".
  if (!root && row && !row.messages && !row.crew_root) return <UltraCrewPanel root={row.key} viewing={slot} active={active} />
  if (!root) return <p className="p-3 text-sm text-muted" data-testid="crew-workers-none">{t('commandCenter.workers_not_crew')}</p>
  return <UltraCrewPanel root={root} viewing={slot} active={active} />
}
