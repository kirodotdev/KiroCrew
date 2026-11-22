/** The session list's Shared Crew view: whether the sidebar shows who started
 *  each session (the bylines and the "Sort & filter" menu's "Started by"
 *  section), the remembered "Started by" selection, and the read failures that
 *  view depends on.
 *
 *  The "Started by" filter runs on the client, over the rows the list already
 *  holds, like the tag and model filters (`./creators`). Filtering is discovery,
 *  not access: every session is still listed with nothing ticked.
 *
 *  The selection is remembered per Crew (keyed by the mode route's `crew_id`).
 *  The section and creator bylines are part of the Shared Crew preview: they
 *  show only with the preview on and someone besides the owner joined
 *  (`showsTeamAttribution`), never on a personal Crew or one that cannot
 *  attribute sessions. A Crew whose mode cannot be read shows the error at the
 *  top of the list, and no bylines. */
import { isRelayHostOffline } from '../../utils/relayHostOffline'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn } from '../../components/ui'
import { capabilityUsable, isUnknownPrincipal, sharedAccessConfigured, sharedCrewPreviewOn, someoneJoined, type PeopleRow } from '../../api/sharedCrewUi'
import { isPermissionRefusal } from '../../api/apiError'
import { effectiveMode, loadCreatorFilter, saveCreatorFilter, showsTeamAttribution, type CrewMode } from '../../api/sharedCrewMode'
import { useSharedCrewCapabilities, useSharedCrewMode, useSharedCrewPeople } from '../../hooks/sharedCrew/useSharedCrew'
import { i18nT } from '../../i18n/t'
import type { SharedCrewBylineView } from './SessionCreatorByline'

export interface SharedCrewListView {
  /** Whether the read-failure notices may render: on a team Crew, or when a
   *  read that decides it failed. */
  noticesAvailable: boolean
  /** Whether the "Started by" section and its chip show (`showsTeamAttribution`). */
  creatorFilterShown: boolean
  /** The ticked "Started by" keys (`./creators`); empty while the section is hidden. */
  creatorKeys: ReadonlySet<string>
  toggleCreator: (key: string) => void
  clearCreators: () => void
  /** The People rows the section names people from. */
  people: readonly PeopleRow[] | undefined
  /** Every id that names the viewer, for "Started by You" (the mode's `viewer_ids`). */
  viewerIds: ReadonlySet<string>
  bylines: SharedCrewBylineView
}

const NO_KEYS: ReadonlySet<string> = new Set()

/** The session list's Shared Crew view: whether to filter and label creators,
 *  the remembered selection for this Crew, and the verified viewer. */
export function useSharedCrewListView(): SharedCrewListView {
  const caps = useSharedCrewCapabilities()
  const { capabilities } = caps
  // Everything here is the Shared Crew preview: with it off the Crew reads no
  // mode or People and the sidebar shows neither the filter nor its errors.
  const previewOn = sharedCrewPreviewOn(capabilities)
  const attributes = previewOn && capabilityUsable(capabilities.attribution)
  const modeQuery = useSharedCrewMode(attributes)
  const people = useSharedCrewPeople(attributes)
  // An initial unreadable mode has no guessed replacement. A cached mode keeps
  // the filter and bylines in place while the notice reports the failed refresh.
  const modeReadFailed = useQueryReadFailed(modeQuery)
  const capsReadFailed = useQueryReadFailed(caps)
  const peopleReadFailed = useQueryReadFailed(people)
  const modeFailed = attributes && modeReadFailed
  const team = showsTeamAttribution({ previewOn, attributes, mode: listMode(modeQuery.data, capabilities, people.data), joined: someoneJoined(people.data?.people) })
  const crewId = modeQuery.data?.crew_id ?? ''
  const viewer = modeQuery.data?.viewer
  const viewerId = viewer && !isUnknownPrincipal(viewer) ? viewer.id : ''
  const viewerIdList = modeQuery.data?.viewer_ids
  const viewerIds = useMemo<ReadonlySet<string>>(
    () => new Set([viewerId, ...(viewerIdList ?? [])].filter(Boolean)),
    [viewerId, viewerIdList],
  )
  const [keys, setKeys] = useState<ReadonlySet<string>>(NO_KEYS)
  useEffect(() => { setKeys(new Set(loadCreatorFilter(crewId))) }, [crewId])
  const toggleCreator = useCallback((key: string) => {
    setKeys(prev => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      saveCreatorFilter(crewId, next)
      return next
    })
  }, [crewId])
  const clearCreators = useCallback(() => {
    setKeys(NO_KEYS)
    saveCreatorFilter(crewId, [])
  }, [crewId])
  const bylines = useMemo(() => ({ hidden: !team, viewerId }), [team, viewerId])
  return {
    noticesAvailable: team || modeFailed || capsReadFailed || (attributes && peopleReadFailed),
    creatorFilterShown: team,
    creatorKeys: team ? keys : NO_KEYS,
    toggleCreator,
    clearCreators,
    people: people.data?.people,
    viewerIds,
    bylines,
  }
}

/** The mode the list reads, or `null` while the mode route has not answered. */
function listMode(
  data: ReturnType<typeof useSharedCrewMode>['data'],
  capabilities: ReturnType<typeof useSharedCrewCapabilities>['capabilities'],
  people: ReturnType<typeof useSharedCrewPeople>['data'],
): CrewMode | null {
  if (data === undefined) return null
  return effectiveMode(data, {
    teamPath: sharedAccessConfigured(capabilities),
    othersSeen: (people?.people ?? []).some(row => !row.is_viewer),
  })
}

type ReadQuery = { isError: boolean; isSuccess: boolean; isPending: boolean; errorUpdateCount: number; error: unknown }

/** A read failed and has not succeeded since. A retry without cached data
 *  stays visibly failed while pending, rather than blinking out the notice.
 *  A permission refusal is not a failure: the mode and People reads answer 403
 *  `owner_only` to a caller who is not the owner (a relay teammate), and a
 *  "Try again" there can never succeed, so no notice shows for it (#32031).
 *  That holds while the refused read refetches too: with no
 *  cached data a refetch clears `error` while pending, so the last settled
 *  error is remembered here rather than read from the query.
 *  shortcut: the memory is per mounted consumer, so one that mounts mid-refetch
 *  has not seen the refusal; key it by query if that case ever shows. */
function useQueryReadFailed(q: ReadQuery): boolean {
  const refused = useRef(false)
  if (q.isError) refused.current = isPermissionRefusal(q.error)
  else if (q.isSuccess) refused.current = false
  if (refused.current) return false
  return q.isError || (q.isPending && q.errorUpdateCount > 0)
}

/** The capability, mode and People read failures, at the top of the session
 *  list, each with Try again. They own those failures even with no open chat. */
export function SharedCrewReadNotices() {
  // The list view owns fetching; mounting its failure surface must not retry it.
  const caps = useSharedCrewCapabilities({ fetchOnMount: false })
  const previewOn = sharedCrewPreviewOn(caps.capabilities)
  const attributes = previewOn && capabilityUsable(caps.capabilities.attribution)
  const modeQuery = useSharedCrewMode(attributes, { fetchOnMount: false })
  const people = useSharedCrewPeople(attributes, { fetchOnMount: false })
  const failed = [useQueryReadFailed(caps), useQueryReadFailed(modeQuery), useQueryReadFailed(people)]
  const failures = [
    // A failed capabilities read cannot say whether the preview is on, so it still shows.
    { query: caps, testId: 'creator-filter-capabilities-error', fallback: i18nT('sharedCrew.session_state_failed'), enabled: true },
    { query: modeQuery, testId: 'creator-filter-mode-error-notice', fallback: i18nT('sharedCrew.mode_failed'), enabled: attributes },
    { query: people, testId: 'creator-filter-people-error', fallback: i18nT('sharedCrew.people_failed'), enabled: attributes },
  // The relay's host-offline answer is the offline cover's to say; the read runs again on reconnect.
  ].filter(({ enabled, query }, i) => enabled && failed[i] && !isRelayHostOffline(query.error))
  return (
    <>
      {failures.map(({ query, testId, fallback }) => (
        <div className="mx-2 mb-1 min-w-0" key={testId} data-testid={testId === 'creator-filter-mode-error-notice' ? 'creator-filter-mode-error' : undefined}>
          <ErrorNotice message={query.error instanceof Error ? query.error.message : fallback} variant="inline" askAgent testId={testId} />
          <Btn onClick={() => { void query.refetch() }}>{i18nT('components.mobileConnect.try_again')}</Btn>
        </div>
      ))}
    </>
  )
}
