/**
 * The Bots page's section for the bots on the user's other crews (#39808): below
 * the local list and never mixed into it, one group per remote crew (Settings >
 * Remote Crew) the switcher has a tab for, under that crew's name. A user who remembers a bot but not which crew
 * hosts it finds it here without opening each crew.
 *
 * The rows come from `GET /api/instances/bots`, which the gateway reads from each
 * connected crew through the same closed roster read the crew window's header
 * uses; nothing reaches a crew through the proxy. They are the crew's roster up
 * to the row cap, so the page's search box narrows them too, and the bots past
 * the cap are counted under the group. A click opens that crew's window, as its
 * switcher tab does; landing on the bot's own page inside it is #41490.
 * A crew that could not be read, or whose tunnel failed, gets a notice, and one
 * that is otherwise not connected its tab's state, instead of being left out;
 * the local read failing is one notice for the whole section.
 *
 * Renders nothing in an embedded crew pane (its gateway's crews are not this
 * window's) and nothing with no crew tab, so a single-machine Bots page is
 * unchanged. The query key sits under `['instances']`, so the invalidation every
 * connect, disconnect and edit issues re-reads it.
 */
import { Fragment, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ArrowUpRight, Bot, Server } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { api } from '../../api/client'
import { cn } from '../../lib/utils'
import { isEmbeddedPane } from '../../lib/embedded'
import { useAppSelector } from '../../store'
import type { WarmConn } from '../../store/instancesSlice'
import { useSelectInstance } from '../../hooks/useSelectInstance'
import { visibleInstanceTabs } from '../../components/InstanceTabBar'
import { ROW_BOX_CLS, ROW_IDLE_CLS, ROW_STATUS_CLS } from '../../components/listShell'
import ErrorNotice from '../../components/ErrorNotice'
import { crewBadge } from '../../hooks/useInstanceSessions'
import { matchesSearch, rowLabel } from './rosterFilter'

const OTHER_CREWS_BOTS_KEY = ['instances', 'bots'] as const

/** How long a read stays fresh: each crew's read walks its config and usage
 *  history, and a roster changes far less often than the page is opened. */
const BOTS_STALE_MS = 60_000

/** Stable empty fallback for partial (test) stores. */
const EMPTY_WARM: Record<string, WarmConn> = {}

export default function OtherCrewsBots({ search }: { search: string }) {
  const { t } = useTranslation()
  const embedded = isEmbeddedPane()
  // The shared ['instances'] cache the switcher already reads on every page.
  const instancesQuery = useQuery({ queryKey: ['instances'], queryFn: () => api.listInstances(), enabled: !embedded })
  const warm = useAppSelector(s => s.instances?.warm ?? EMPTY_WARM)
  const instancesData = instancesQuery.data?.instances
  const instances = useMemo(() => instancesData ?? [], [instancesData])
  const crews = useMemo(() => (embedded ? [] : visibleInstanceTabs(instances, warm)), [embedded, instances, warm])
  // Keyed on the connected crews too, so a crew that connects is read without
  // waiting for the read to go stale.
  const connected = crews.filter(c => c.status?.state === 'connected').map(c => c.id).join(',')
  const botsQuery = useQuery({
    queryKey: [...OTHER_CREWS_BOTS_KEY, connected],
    queryFn: () => api.instancesBots(),
    enabled: crews.length > 0,
    staleTime: BOTS_STALE_MS,
    retry: false,
  })
  const { selectInstance } = useSelectInstance(instances)
  const needle = search.trim().toLowerCase()

  if (crews.length === 0) return null
  // The local read failing is one notice for the section, never a fault laid on
  // each crew.
  const groups = botsQuery.isError ? [] : crews.flatMap((crew) => {
    const read = botsQuery.data?.crews.find(c => c.id === crew.id)
    const bots = (read?.bots ?? []).filter(b => !needle || matchesSearch(b, needle))
    // A crew the reply leaves out is not connected: its tab's own state says how,
    // in the words the Sessions list's crew groups use (the same catalog keys).
    // One the tab shows as connected is the read for its new key still on its
    // way, and a failed tunnel is its notice, so only these two words remain.
    const badge = read ? null : crewBadge(crew.status?.state)
    const tunnelError = !botsQuery.isPending && badge === 'error'
    let status = ''
    if (botsQuery.isPending || badge === 'online') status = t('pages.membersPage.other_crews_loading')
    else if (badge && !tunnelError) status = badge === 'reconnecting' ? t('pages.chatSidebar.crew_status_reconnecting') : t('pages.chatSidebar.crew_status_offline')
    else if (read && !read.error && read.bots.length === 0 && !needle) status = t('pages.membersPage.other_crews_no_bots')
    // With a search typed, a crew with no match says nothing; a crew that could
    // not be read, is not connected, or has bots past the cap still says so:
    // the bot may be there.
    if (!read?.error && !tunnelError && !status && !read?.omitted && bots.length === 0) return []
    return [{ crew, read, bots, status, tunnelError }]
  })
  if (!botsQuery.isError && groups.length === 0) return null
  return (
    <>
      <li
        className="mt-4 px-3.5 pt-3 border-t border-border text-[11.5px] font-semibold text-muted"
        data-testid="other-crews-bots"
      >
        {t('pages.membersPage.other_crews_heading')}
      </li>
      {botsQuery.isError && (
        <li className="px-3.5 py-1">
          <ErrorNotice
            variant="inline"
            impact="partial"
            message={t('pages.membersPage.other_crews_load_failed')}
            askAgent
            testId="other-crews-error"
          />
        </li>
      )}
      {groups.map(({ crew, read, bots, status, tunnelError }) => {
        const open = t('pages.membersPage.other_crews_open', { crew: crew.name })
        const unreachable = t('pages.membersPage.other_crews_unreachable')
        const why = read?.error || (tunnelError && crew.status?.error) || ''
        return (
          <Fragment key={crew.id}>
            <li className="mt-1 flex items-center gap-2 px-3.5 py-1.5 min-w-0" data-testid="other-crew-group" data-crew={crew.id}>
              <Server size={13} className="lucide-inline shrink-0 text-muted" aria-hidden="true" />
              <span className="text-[11.5px] font-semibold text-muted truncate min-w-0">{crew.name}</span>
              <span className="flex-1 min-w-0 h-px bg-border" />
            </li>
            {/* A failed read or tunnel: its cause is the message, as in the
                Sessions list's crew groups, so the hand-off carries it. A list
                read holds no draft, so the hand-off is on. */}
            {(read?.error || tunnelError) && (
              <li className="pl-8 pr-3 py-1">
                <ErrorNotice
                  variant="inline"
                  impact="partial"
                  title={why ? unreachable : undefined}
                  message={why || unreachable}
                  messagePlacement="below"
                  askAgent
                  testId="other-crew-error"
                />
              </li>
            )}
            {status && (
              <li className={cn('pl-8 pr-3 py-1 text-muted', ROW_STATUS_CLS)} data-testid="other-crew-status">
                {status}
              </li>
            )}
            {bots.map(b => (
              <li key={b.name}>
                <button
                  type="button"
                  onClick={() => selectInstance(crew.id)}
                  className={cn('group w-full flex items-center gap-2.5 text-sm text-left select-none transition-all', ROW_BOX_CLS, 'pl-8', ROW_IDLE_CLS)}
                  data-testid="other-crew-bot"
                >
                  <Bot size={14} className="lucide-inline shrink-0" aria-hidden="true" />
                  <span className="truncate min-w-0">{rowLabel(b)}</span>
                  {/* The click leaves this page for the crew's window, so the row says so.
                      The words take only the room the bot's name leaves, down to the arrow. */}
                  <span
                    className="flex-1 min-w-4 inline-flex justify-end items-center gap-1 text-[11px] text-muted opacity-0 group-hover:opacity-100 [@media(hover:none)]:opacity-100 group-focus-visible:opacity-100 transition-opacity"
                    data-testid="other-crew-bot-open"
                  >
                    <span className="truncate">{open}</span>
                    <ArrowUpRight size={12} className="lucide-inline shrink-0" aria-hidden="true" />
                  </span>
                </button>
              </li>
            ))}
            {read && read.omitted > 0 && (
              <li className={cn('pl-8 pr-3 py-1 text-muted', ROW_STATUS_CLS)} data-testid="other-crew-omitted">
                {t('pages.membersPage.hidden_more_count', { count: read.omitted })}
              </li>
            )}
          </Fragment>
        )
      })}
    </>
  )
}
