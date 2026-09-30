import { useCallback, useEffect, useMemo, useState, useSyncExternalStore } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useAppSelector } from '../../store'
import { api } from '../../api/client'
import { cronJobsQuery } from '../../api/cronJobsQuery'
import { appNotificationBadges, mergeAppBadges } from '../../appNotificationBadges'
import { appRunStates, nextSuccessExpiryMs } from '../../appRunState'
// Static on purpose, and the tradeoff is real: the sidebar updates badge
// needs `registryQueryFn` (its own fetch boundary — a badge that only lights
// after a store-page visit does not do its job), and importing it pulls the
// store data layer into the eager App chunk. Accepted: the bundle-size gate
// still passes, and a second raw fetcher under the same query key would win
// React Query's one-queryFn-per-key registration and poison the cache shape.
import { countUpdatables, registryQueryFn, type UpdatableInstalledRow } from '../../pages/apps/useAppsData'

/** Pending task-gate approvals, for the Projects row's badge. */
export function useGlobalApprovalCount(): number {
  // Global approvals (project task-gates) — sourced from React Query, not
  // Redux, so it can't go through `selectAllSurfacesAttention` directly.
  // Routed through `appBadges` (the existing dynamic-app channel) so the
  // Projects nav item picks it up via `NavBadge`'s app-badge fallback path.
  const { data: pendingApprovals = [] } = useQuery({
    queryKey: ['global-approvals'],
    queryFn: () => api.approvals(),
    staleTime: 0,
    refetchInterval: 30_000,
  })
  const approvalCount = pendingApprovals.filter((a: { id?: string }) => a.id?.startsWith('task-gate-')).length
  return approvalCount
}

/**
 * Every count and state mark the rail's rows carry: app badges pushed through
 * `mc:app:badge`, the global-approvals count on Projects, the pending app-update
 * count on Discover, notification-derived badges for installed apps, and the
 * run state of an app's own cron jobs.
 */
export function useRailBadges(approvalCount: number) {
  const queryClient = useQueryClient()
  // App badge counts — apps call useNavBadge() to push counts
  const [appBadges, setAppBadges] = useState<Record<string, number>>({})
  useEffect(() => {
    const handler = (e: Event) => {
      const { appName, count } = (e as CustomEvent).detail || {}
      if (appName) setAppBadges(prev => ({ ...prev, [appName]: count || 0 }))
    }
    window.addEventListener('mc:app:badge', handler)
    return () => window.removeEventListener('mc:app:badge', handler)
  }, [])
  // Surface the global-approvals count on the Projects nav item via the same
  // `appBadges` channel external apps use. The `projects` surface declares no
  // slotMode/unreadSelector, so `NavBadge` falls back to `appBadges['projects']`.
  useEffect(() => {
    setAppBadges(prev => prev.projects === approvalCount ? prev : { ...prev, projects: approvalCount })
  }, [approvalCount])

  // Pending app-update count for the sidebar Discover badge — the SAME count
  // the Discover Updates sub-tab shows, via the shared `countUpdatables`
  // derivation. The registry read is an ACTIVE query on the shared
  // `registryQueryFn` boundary (one normalize path, so either observer may
  // fetch and both see the same shape): a passive cache read only ever fires
  // after a store page has populated the cache, which is the one place the
  // count is already visible — a badge that cannot appear in a fresh session
  // does not do its job. `mc:apps-changed` invalidation (`App.tsx`) refetches it.
  // `['apps']` stays a passive read: refreshAppNav in this shell already
  // writes it on every fetch.
  const { data: registryBadgeData } = useQuery({
    queryKey: ['registry'],
    queryFn: registryQueryFn,
    staleTime: 5 * 60_000,
    refetchOnWindowFocus: false,
  })
  const subscribeQueryCache = useCallback(
    (onStoreChange: () => void) => queryClient.getQueryCache().subscribe(onStoreChange),
    [queryClient],
  )
  const installedSnapshot = useSyncExternalStore(
    subscribeQueryCache,
    () => queryClient.getQueryData<UpdatableInstalledRow[]>(['apps']),
  )
  const appUpdatesCount = useMemo(
    () => countUpdatables(registryBadgeData?.apps, installedSnapshot),
    [registryBadgeData, installedSnapshot],
  )
  // Merged only into the badge map the two Discover rows read — NOT into the
  // `appBadges` state: that map feeds the tab-title `totalAttention` sum, and
  // a pending app update is not an attention item the way an approval or an
  // unread message is. `NavBadge` hides at count 0 (BadgeIndicator renders
  // null), so an empty count leaves the row badge-free.
  const discoverBadges = useMemo(
    () => (appUpdatesCount > 0 ? { ...appBadges, apps: appUpdatesCount } : appBadges),
    [appBadges, appUpdatesCount],
  )

  // Rail badges for INSTALLED apps, derived from notifications the app already
  // published: the host stamps `source: "app:<name>"` on every pushed record
  // and owns the `acked` flag, so the count needs no new manifest field and no
  // new app-implemented route -- see `appNotificationBadges`.
  //
  // Merged only into the badge map the rail rows read -- NOT into the
  // `appBadges` state, for the same reason `discoverBadges` above stays out of
  // it: that map feeds the tab-title `totalAttention` sum, and the
  // notifications bell ALREADY badges these same records, so adding them there
  // would count one notification twice in the tab title.
  //
  // An app that pushes its own count through `useNavBadge()` still wins on its
  // own key, so an app using the SDK hook today sees no change at all; the
  // derivation only fills rows that had no badge.
  //
  // Handed ONLY to rows that pass `isAppNavId` (see the call site). `NavBadge`
  // keys its fallback on the BARE navId for an unprefixed row, so a host row
  // such as `schedule` indexes the same map an app name would -- an app could
  // otherwise put attention on host chrome by choosing its own name.
  const notificationItems = useAppSelector(s => s.notifications.items)
  const railAppBadges = useMemo(
    () => mergeAppBadges(appBadges, appNotificationBadges(notificationItems)),
    [appBadges, notificationItems],
  )

  // Rail RUN STATE for installed apps, derived from the app's own cron jobs.
  // Separate from the badge above and deliberately not merged into it: the badge
  // is a count meaning "something is waiting for you", while this is the state of
  // scheduled work and is addressed to nobody -- see `appRunState`.
  //
  // Reads the SHARED ['cron-jobs'] query rather than adding a poll. That key is
  // invalidated on every server `refresh` frame (`useWebSocket`), which is where
  // its freshness comes from, and several surfaces already observe it, so the
  // rail costs no extra request. `enabled` is not gated on having installed apps:
  // the query is shared, so gating it here would only change WHICH observer
  // happens to fetch first.
  const { data: cronJobsForRail } = useQuery({
    ...cronJobsQuery,
    refetchOnWindowFocus: false,
  })
  // The derivation is a pure function of the job list AND an instant, so it needs
  // a clock of its own: without one a `success` mark stays on screen past its
  // window whenever no refresh follows, and the window would be a claim the code
  // does not keep. The clock is state rather than a `Date.now()` read inside the
  // memo so it is a real dependency, and it advances on exactly two occasions --
  // the job list changed, or the earliest showing `success` just expired. That is
  // why there is no ticking interval: a per-second clock would re-render the rail
  // continuously to show the same thing in every second but one.
  const [runStateClockMs, setRunStateClockMs] = useState(() => Date.now())
  const railAppRunStates = useMemo(
    () => appRunStates(cronJobsForRail ?? [], runStateClockMs),
    [cronJobsForRail, runStateClockMs],
  )
  // Fresh data is judged against now, not against the previous tick.
  useEffect(() => { setRunStateClockMs(Date.now()) }, [cronJobsForRail])
  useEffect(() => {
    const due = nextSuccessExpiryMs(cronJobsForRail ?? [], runStateClockMs)
    // Null means nothing is showing `success`, so the common case arms no timer.
    // Re-arming on each clock change walks a batch of successes in expiry order
    // and terminates when the last one is gone, rather than looping.
    if (due === null) return
    const timer = setTimeout(() => setRunStateClockMs(Date.now()), due)
    return () => clearTimeout(timer)
  }, [cronJobsForRail, runStateClockMs])
  return { appBadges, discoverBadges, railAppBadges, railAppRunStates }
}
