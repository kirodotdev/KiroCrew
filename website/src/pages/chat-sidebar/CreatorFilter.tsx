/** The sidebar's creator filter (All / Mine / a person / Unattributed) and the
 *  server-filtered session list it shows while a filter other than All is on.
 *
 *  The filter runs on the server before paging (`GET /api/shared-crew/sessions`),
 *  so the counts are the server's: `total_matching` sessions match across every
 *  page, and `hidden_by_filter` is how many the filter left out. The client never
 *  hides a row it was sent. Filtering is discovery, not access: every session is
 *  still reachable with All.
 *
 *  The selection is remembered per Crew (keyed by the mode route's `crew_id`). A
 *  personal Crew shows neither the control nor creator bylines, and neither
 *  does a Crew that cannot attribute sessions. A Crew whose mode cannot be read
 *  shows the error where the control would be, and no bylines. */
import { useCallback, useContext, useEffect, useMemo, useState } from 'react'
import { Users } from 'lucide-react'
import SimpleSelect from '../../components/SimpleSelect'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn, Skeleton } from '../../components/ui'
import { capabilityUsable, isUnknownPrincipal, principalName, sharedAccessConfigured } from '../../api/sharedCrewUi'
import type { CreatorFilter as CreatorFilterValue } from '../../api/sharedCrew'
import { creatorDisplay, effectiveMode, hidesTeamAttribution, loadCreatorFilter, normalizeCreatorFilter, saveCreatorFilter } from '../../api/sharedCrewMode'
import { useSharedCrewCapabilities, useSharedCrewMode, useSharedCrewPeople, useSharedCrewSessionsPage } from '../../hooks/sharedCrew/useSharedCrew'
import { i18nT } from '../../i18n/t'
import { fmtNumber } from '../../i18n/format'
import { fmtRelativeTime } from '../chat/sessionOrder'
import { SharedCrewBylineContext, type SharedCrewBylineView } from './SessionCreatorByline'

export interface SharedCrewListView {
  /** Whether the creator filter bar renders: the control on a team Crew, or the
   *  mode-read error in its place. The filter stays All while the mode is unread. */
  filterAvailable: boolean
  filter: CreatorFilterValue
  setFilter: (v: CreatorFilterValue) => void
  bylines: SharedCrewBylineView
}

/** The session list's Shared Crew view: whether to filter and label creators,
 *  the remembered selection for this Crew, and the verified viewer. */
export function useSharedCrewListView(): SharedCrewListView {
  const caps = useSharedCrewCapabilities()
  const { capabilities } = caps
  const attributes = capabilityUsable(capabilities.attribution)
  const modeQuery = useSharedCrewMode(attributes)
  const people = useSharedCrewPeople(attributes)
  // An initial unreadable mode has no guessed replacement. A cached mode keeps
  // the filter and bylines in place while the bar reports the failed refresh.
  const modeFailed = attributes && queryReadFailed(modeQuery)
  const mode = effectiveMode(modeQuery.data, {
    teamPath: sharedAccessConfigured(capabilities),
    othersSeen: (people.data?.people ?? []).some(row => !row.is_viewer),
  })
  const team = attributes && modeQuery.data !== undefined && !hidesTeamAttribution(mode)
  const crewId = modeQuery.data?.crew_id ?? ''
  const viewer = modeQuery.data?.viewer
  const viewerId = viewer && !isUnknownPrincipal(viewer) ? viewer.id : ''
  const [filter, setFilterState] = useState<CreatorFilterValue>('all')
  useEffect(() => { setFilterState(loadCreatorFilter(crewId)) }, [crewId])
  const setFilter = useCallback((v: CreatorFilterValue) => {
    const next = normalizeCreatorFilter(v)
    setFilterState(next)
    saveCreatorFilter(crewId, next)
  }, [crewId])
  const bylines = useMemo(() => ({ hidden: !team, viewerId }), [team, viewerId])
  return { filterAvailable: team || modeFailed || queryReadFailed(caps) || (attributes && queryReadFailed(people)), filter: team ? filter : 'all', setFilter, bylines }
}

/** A read failed and has not succeeded since. A retry without cached data
 *  stays visibly failed while pending, rather than blinking out the notice. */
function queryReadFailed(q: { isError: boolean; isPending: boolean; errorUpdateCount: number }): boolean {
  return q.isError || (q.isPending && q.errorUpdateCount > 0)
}

export function CreatorFilterBar({ value, onChange }: { value: CreatorFilterValue; onChange: (v: CreatorFilterValue) => void }) {
  // The list view owns fetching; mounting its failure surface must not retry it.
  // This bar owns capability, mode and People failures even with no open chat.
  const caps = useSharedCrewCapabilities({ fetchOnMount: false })
  const attributes = capabilityUsable(caps.capabilities.attribution)
  const modeQuery = useSharedCrewMode(attributes, { fetchOnMount: false })
  const people = useSharedCrewPeople(attributes, { fetchOnMount: false })
  const failures = [
    { query: caps, testId: 'creator-filter-capabilities-error', fallback: i18nT('sharedCrew.session_state_failed'), enabled: true },
    { query: modeQuery, testId: 'creator-filter-mode-error-notice', fallback: i18nT('sharedCrew.mode_failed'), enabled: attributes },
    { query: people, testId: 'creator-filter-people-error', fallback: i18nT('sharedCrew.people_failed'), enabled: attributes },
  ].filter(({ query, enabled }) => enabled && queryReadFailed(query))
  const mode = effectiveMode(modeQuery.data, {
    teamPath: sharedAccessConfigured(caps.capabilities),
    othersSeen: (people.data?.people ?? []).some(row => !row.is_viewer),
  })
  return (
    <>
      {failures.map(({ query, testId, fallback }) => (
        <div className="mx-2 mb-1 min-w-0" key={testId} data-testid={testId === 'creator-filter-mode-error-notice' ? 'creator-filter-mode-error' : undefined}>
          <ErrorNotice message={query.error instanceof Error ? query.error.message : fallback} variant="inline" askAgent testId={testId} />
          <Btn onClick={() => { void query.refetch() }}>{i18nT('components.mobileConnect.try_again')}</Btn>
        </div>
      ))}
      {attributes && modeQuery.data !== undefined && !hidesTeamAttribution(mode) && <CreatorFilterSelect value={value} onChange={onChange} people={people.data} />}
    </>
  )
}

function CreatorFilterSelect({ value, onChange, people }: {
  value: CreatorFilterValue
  onChange: (v: CreatorFilterValue) => void
  people: ReturnType<typeof useSharedCrewPeople>['data']
}) {
  const current = normalizeCreatorFilter(value)
  const { options, labels } = useMemo(() => {
    const opts: string[] = ['all', 'mine']
    const labs: string[] = [i18nT('sharedCrew.filter_all'), i18nT('sharedCrew.filter_mine')]
    for (const row of people?.people ?? []) {
      if (row.is_viewer) continue
      const name = principalName(row.principal)
      if (!name) continue
      opts.push(row.principal.id)
      labs.push(name)
    }
    opts.push('unattributed')
    labs.push(i18nT('sharedCrew.unattributed'))
    // A creator chosen from an earlier People read stays selectable even if the
    // person is no longer listed, so the control never shows an empty value.
    if (!opts.includes(current)) {
      opts.push(current)
      labs.push(i18nT('sharedCrew.filter_other_person'))
    }
    return { options: opts, labels: labs }
  }, [people, current])
  return (
    <div className="mx-2 mb-1 flex flex-wrap items-center gap-1.5 min-w-0" data-testid="creator-filter">
      <Users size={12} className="shrink-0 text-muted" aria-hidden />
      <SimpleSelect
        options={options}
        optionLabels={labels}
        value={current}
        onChange={v => onChange(v as CreatorFilterValue)}
        aria-label={i18nT('sharedCrew.filter_label')}
        className="h-7 min-w-0 flex-1"
      />
    </div>
  )
}

export interface FilteredSessionsProps {
  creator: CreatorFilterValue
  q: string
  connected: boolean
  onOpen: (key: string, title: string) => void
  onClear: () => void
}

export function CreatorFilteredSessions({ creator, q, connected, onOpen, onClear }: FilteredSessionsProps) {
  // The cursor trail of the pages already shown; Back pops it, More pushes.
  const [cursors, setCursors] = useState<string[]>([''])
  const cursor = cursors[cursors.length - 1]
  const { viewerId } = useContext(SharedCrewBylineContext)
  const page = useSharedCrewSessionsPage(normalizeCreatorFilter(creator), q.trim(), cursor, true)
  const data = page.data
  const unavailable = page.isSuccess && data === null
  return (
    <div className="flex-1 min-h-0 overflow-y-auto px-2 pb-2 pt-[var(--list-dock-h,0.5rem)]" data-testid="creator-filtered-sessions" aria-busy={page.isFetching}>
      {page.isLoading && (
        <div className="flex flex-col gap-2 pt-1" aria-label={i18nT('sharedCrew.loading')}>
          <Skeleton className="h-10" /><Skeleton className="h-10" /><Skeleton className="h-10" />
        </div>
      )}
      {page.isError && (
        <ErrorNotice
          message={page.error instanceof Error ? page.error.message : i18nT('sharedCrew.sessions_failed')}
          variant="inline"
          askAgent
          testId="creator-filter-error"
        />
      )}
      {unavailable && (
        <p className="text-muted text-[12px] px-1 py-2" data-testid="creator-filter-unavailable">
          {i18nT('sharedCrew.filter_unavailable')}{' '}
          <Btn className="ml-1" onClick={onClear}>{i18nT('sharedCrew.show_all_sessions')}</Btn>
        </p>
      )}
      {data && (
        <>
          <p className="text-muted text-[12px] px-1 pb-1" role="status" data-testid="creator-filter-counts">
            {i18nT('sharedCrew.filter_counts', {
              shown: fmtNumber(data.total_matching),
              hidden: fmtNumber(data.hidden_by_filter),
            })}
          </p>
          {data.items.length === 0 ? (
            <p className="text-muted text-[12px] px-1 py-2">{i18nT('sharedCrew.filter_empty')}</p>
          ) : (
            <ul className="flex flex-col" aria-label={i18nT('sharedCrew.filtered_sessions')}>
              {data.items.map(item => {
                const title = item.title || item.key
                const by = item.attribution ? creatorDisplay(item.attribution, viewerId) : i18nT('sharedCrew.unattributed')
                const when = item.modified ?? ''
                return (
                  <li key={item.key}>
                    <button
                      type="button"
                      disabled={!connected}
                      onClick={() => onOpen(item.key, title)}
                      className="w-full text-left rounded-md px-2 py-1.5 bg-transparent border-none cursor-pointer hover:bg-bg-hover disabled:cursor-not-allowed disabled:opacity-50 min-w-0"
                      data-testid={`creator-filter-row-${item.key}`}
                    >
                      <span className="block text-[11px] leading-4 text-muted truncate">
                        {item.agent && <>{item.agent}<span aria-hidden>{'\u00A0·\u00A0'}</span></>}
                        {by}
                        {when !== '' && <><span aria-hidden>{'\u00A0·\u00A0'}</span>{fmtRelativeTime(when)}</>}
                      </span>
                      <span className="block text-[13px] leading-5 text-text truncate" title={title}>{title}</span>
                    </button>
                  </li>
                )
              })}
            </ul>
          )}
          <div className="flex gap-2 pt-1">
            {cursors.length > 1 && (
              <Btn onClick={() => setCursors(c => c.slice(0, -1))}>{i18nT('sharedCrew.page_previous')}</Btn>
            )}
            {data.next_cursor && (
              <Btn onClick={() => setCursors(c => [...c, data.next_cursor])}>{i18nT('sharedCrew.page_next')}</Btn>
            )}
          </div>
        </>
      )}
    </div>
  )
}
