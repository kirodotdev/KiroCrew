/** Where the rendered rows come from: the caller's local tabs, live peer rows from
 *  connected crews, and the federated Older Sessions search. */
import { useQuery } from '@tanstack/react-query'
import { useMemo } from 'react'
import { useAppSelector } from '../../store'
import { usePreviewFlag } from '../../hooks/usePreviewFlag'
import { PREVIEW_INSTANCE_SESSIONS } from '../../utils/previewFlags'
import { api } from '../../api/client'
import { useInstanceSessions } from '../../hooks/useInstanceSessions'
import { i18nT } from '../../i18n/t'
import { useSelectInstance } from '../../hooks/useSelectInstance'
import { useDebouncedSessionSearch } from './search'
import type { Slot } from './types'
import { sessionRowIdentity } from './rowIdentity'

/** Every row this sidebar may show, and the federated Older Sessions search. */
export function useSessionSources({ historyFilter, slotTitleDigest, localSlots }: {
  historyFilter: string
  slotTitleDigest: string
  localSlots: Slot[]
}) {
  // The Older Sessions pane renders `history`, so `slotTitleDigest` (slots-derived) is a
  // proxy: it moves for every rename reachable today, all of which start on a live row.
  // Federated when any remote instance holds a live connection: the endpoint
  // then also covers every connected instance's sessions (rows tagged with
  // instance_id/_name render a badge and activate that instance's pane).
  // Guarded read: ChatSidebar is rendered by dozens of test harnesses whose
  // partial stores omit the instances slice entirely (unlike the instances-own
  // components, which only ever mount with it).
  const hasWarmInstances = useAppSelector(s => Object.keys(s.instances?.warm ?? {}).length > 0)
  // Live sessions on connected remote instances, merged into the list below.
  // The flag is read HERE and passed in, so a user who has not opted in issues no
  // per-instance request at all — this component mounts for every dashboard user,
  // so gating the render would gate the rows but not the wire.
  const instanceSessionsEnabled = usePreviewFlag(PREVIEW_INSTANCE_SESSIONS)
  const historySearchResults = useDebouncedSessionSearch(
    historyFilter, s => s, slotTitleDigest, hasWarmInstances,
  )
  // Shared ['instances'] cache + shared select-and-maybe-reconnect semantics for
  // activating a remote row. THE ONLY `['instances']` observer in this component:
  // the merged-sessions hook takes this list as a parameter rather than opening a
  // second observer on the same key, because two observers notify the sidebar
  // twice for one cache write and a spurious render landing mid-rename cancels
  // the edit. Enabled for a warm connection OR for the merged-sessions preview,
  // since that preview needs the list even before anything is warm.
  const instancesQuery = useQuery({
    queryKey: ['instances'],
    queryFn: () => api.listInstances(),
    enabled: hasWarmInstances || instanceSessionsEnabled,
  })
  const instancesData = instancesQuery.data?.instances
  const instancesList = useMemo(() => instancesData ?? [], [instancesData])
  // "The peer list has not answered yet", derived from state this component
  // ALREADY reads. Deliberately NOT `isLoading` / `isFetching`: react-query
  // tracks which result properties a consumer touches, so reading either one
  // subscribes the whole sidebar to fetch-status transitions — including every
  // background refetch — and one such render landing mid-rename blurs the rename
  // textarea and cancels the edit. That is the same failure the hook's deleted
  // `notifyOnChangeProps` workaround was suppressing, and reading `isLoading`
  // here reintroduced it from the other side (caught by
  // `integration/ChatSidebarRenameFocus.integration.test.tsx`, which is why that
  // spec belongs in the local gate for this hook and not only in CI).
  // `data` is already tracked for the list itself and `isError` flips at most
  // once per query lifecycle, so neither adds a new churn source. The error arm
  // matters: without it a permanently failing request would leave the sidebar
  // claiming "checking" forever.
  const instancesUnanswered = instancesData === undefined && !instancesQuery.isError
  const instanceSessions = useInstanceSessions(
    instanceSessionsEnabled, instancesList, instancesUnanswered,
  )
  // BOTH reads the merged-sessions preview depends on, collapsed into one error
  // banner. Either can fail, and each used to vanish differently: a failing
  // `['instances']` only flipped `instancesUnanswered` false, so the "checking"
  // line disappeared and the list silently claimed a completeness it did not
  // have; a failing peer read was reduced to a hand-written warn box that threw
  // the peer's own message away. Both are read failures, so both belong in
  // `ErrorNotice` with the hand-off on.
  //
  // `message` is the JOURNAL LOOKUP KEY, so it carries the underlying error text
  // whenever there is one — that is what recovers the failed endpoint, HTTP
  // status and backend `code` (`peer_slots_refused`, `proxy_not_connected`, a 403
  // the user can fix by reconnecting) for the agent. The localized sentence rides
  // as `title` beside it, and stands in AS the message only when nothing came
  // back to look up, so the banner is never empty.
  //
  // ONE banner, not two: a failing instances list leaves `instancesList` empty,
  // so no peer query is ever created and `failed` cannot be non-empty at the same
  // time. Reading `instancesQuery.error` adds no render churn beyond the `isError`
  // already read above — both settle at most once per query lifecycle.
  //
  // Gated on the preview flag because this describes the preview's own rows. With
  // the flag off the sidebar makes no claim about peer sessions, and a new
  // sidebar-wide banner for the pre-existing warm-instance read would be a
  // different feature's decision to make.
  const remoteSessionsError = useMemo((): { title?: string, message: string } | null => {
    if (!instanceSessionsEnabled) return null
    const say =(sentence: string, detail?: string) => (detail
      ? { title: sentence, message: detail }
      : { message: sentence })
    if (instancesQuery.isError) {
      return say(
        i18nT('pages.chatSidebar.remote_instances_list_unavailable'),
        instancesQuery.error instanceof Error ? instancesQuery.error.message : undefined,
      )
    }
    if (instanceSessions.failed.length > 0) {
      return say(
        i18nT('pages.chatSidebar.sessions_from_instance_unavailable', {
          names: instanceSessions.failed.join(', '),
        }),
        instanceSessions.failure,
      )
    }
    return null
  }, [
    instanceSessionsEnabled, instancesQuery.isError, instancesQuery.error,
    instanceSessions.failed, instanceSessions.failure,
  ])
  // THE RENDERED COLLECTION: every row this sidebar may show, local tabs plus
  // live peer rows. Hoisted to a named memo rather than built inside
  // `filteredSlots` because a collection that exists only inside one memo cannot
  // be reached by anything else, and every other consumer that needed it reached
  // for `localSlots` instead — which is how a filter badge ended up counting a
  // different set than the filter renders. Anything describing what is ON SCREEN
  // reads this; anything that is genuinely about the user's own tabs reads
  // `localSlots`.
  //
  // Row ORDER here is deliberately the raw concatenation: `filteredSlots` owns
  // narrowing and sorting, so duplicating either would give two answers.
  const allRows = useMemo(
    () => {
      if (instanceSessions.rows.length === 0) return localSlots
      // DEDUPE BY IDENTITY, local wins. An adopted session resolves to the same
      // identity as the peer row it was bound from, which is what makes the row
      // transform in place — but it also means both can name the same row for as
      // long as the peer listing is stale, and rendering both would be a duplicate
      // React key on top of a duplicate row. The local slot is the survivor: it is
      // the one with a transcript and a `slot_key`, and it is the authority on a
      // session this machine now drives.
      //
      // The server drops a driven row from the listing too, so this is the second
      // of two guards rather than the only one; it exists because the client's copy
      // of that listing can be older than the binding.
      const local = localSlots as Slot[]
      const seen = new Set(local.map(sessionRowIdentity))
      const peers = (instanceSessions.rows as unknown as Slot[])
        .filter(row => !seen.has(sessionRowIdentity(row)))
      return peers.length === 0 ? local : [...local, ...peers]
    },
    [localSlots, instanceSessions.rows],
  )
  // The UNFILTERED live slot list, every surface. `localSlots` is what the chat page
  // shows; this is what the gateway has. Read for one purpose -- `creatorAnchors`,
  // the conductor lane's missing creators -- and nowhere else: anything describing
  // what is ON SCREEN reads `allRows`.
  const allLiveSlots = useAppSelector(st => st.dashboard.slots)
  // `selectInstance` stays for the FEDERATED OLDER-SESSIONS rows the pane renders,
  // which genuinely have nowhere local to go: a history row names a closed
  // session on the peer, with no live peer slot to bind, so switching to that
  // crew's pane IS its outcome. The LIVE peer rows above no longer use it — they
  // adopt (see `adoptPeerSession` in ChatSidebar).
  const { selectInstance } = useSelectInstance(instancesList)
  return {
    historySearchResults, instancesList, instanceSessions, remoteSessionsError, allRows, allLiveSlots,
    selectInstance,
  }
}
