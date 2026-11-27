import { useCallback } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'

import { api, type KiroPrerequisiteStatus } from '../api/client'

/** The one cache entry every reader of `GET /api/kiro-prerequisite` shares. */
export const KIRO_PREREQUISITE_KEY = ['kiro-prerequisite'] as const

export function kiroPrerequisiteRefetchInterval(
  status: KiroPrerequisiteStatus | undefined,
  kiroGated = false,
): number | false {
  // A closed LTS login gate keeps the fast poll even with `ready`: the
  // probe can latch ready a few seconds before the gate itself reopens.
  if (status?.ready && status.lts_login_required !== true) return 30_000
  if (status && status.setup_allowed === false) return 3_000
  if (kiroPrerequisiteIsBlocking(status, kiroGated)) return 5_000
  return 30_000
}

// True while the full-screen first-run gate is the only thing the user can see.
// Two behaviors key off it: the faster poll above, and forcing that poll to probe
// the HOST rather than read the boot-time latch.
//
// Forcing matters because Kiro Crew no longer performs setup — the user installs
// Kiro CLI from kiro.dev and may sign in from a terminal. Neither of those
// touches the gateway, and the latched status is refreshed only at boot or on an
// explicit request, so a latch-reading poll can never observe them and the gate
// would hold forever behind a Check again button. Bounded deliberately: it costs
// two short `kiro-cli` spawns per interval, runs ONLY on this blocking screen,
// and stops the moment `ready` flips. A returning user reaches it only through
// the out-of-date card below.
export function kiroPrerequisiteIsBlocking(
  status: KiroPrerequisiteStatus | undefined,
  kiroGated = false,
): boolean {
  // An LTS gateway whose login gate is closed serves nothing else, so the
  // sign-in screen blocks and its poll must probe to notice the new sign-in.
  if (status?.lts_login_required === true) return true
  if (!status || status.ready) return false
  // A non-owner cannot probe and is shown the "owner must finish setup" screen.
  if (status.setup_allowed === false) return false
  // The out-of-date card blocks an established install too, and its copy says
  // the page continues once the CLI is updated: a terminal update touches no
  // gateway, so only a probing poll can see it (the server clears a spawn-proven
  // too-old latch only when the probe finds a different binary). Only when a
  // loaded config runs a Kiro harness (*kiroGated*): another harness's user sees
  // the dashboard, not this card, and must not drive host probes behind it.
  if (status.acp_supported === false && kiroGated) return true
  return !status.initial_setup_complete
}

/**
 * The Kiro CLI status the welcome screen and the chat's kiro-cli sign-in row
 * both read: one query, one cache entry, one "Check again".
 *
 * *kiroGated* reports whether a loaded config runs a Kiro harness (the gate
 * knows; a reader that does not passes nothing). *poll* is off for a reader that
 * only shows the status: the gate drives the cadence.
 *
 * `checkAgain` is the user's own re-read: it always probes the host
 * (`refresh=explicit`) and replaces any read in flight, the way the gate's
 * button always did. It is a fetch on the shared entry rather than a refetch of
 * one observer, so a press from either reader asks for the same probe.
 */
export function useKiroPrerequisite({
  kiroGated = () => false,
  poll = true,
}: { kiroGated?: () => boolean; poll?: boolean } = {}) {
  const queryClient = useQueryClient()
  const statusQuery = useQuery({
    queryKey: KIRO_PREREQUISITE_KEY,
    queryFn: () => {
      // Probe the host while the blocking first-run gate is up — see
      // kiroPrerequisiteIsBlocking for why a latch-reading poll cannot lift that
      // gate on its own. The automatic poll is coalesced server-side so several
      // open tabs do not multiply the gateway's kiro-cli spawns.
      const refresh = kiroPrerequisiteIsBlocking(
        queryClient.getQueryData<KiroPrerequisiteStatus>(KIRO_PREREQUISITE_KEY),
        kiroGated(),
      )
        ? 'auto' as const
        : false
      return api.kiroPrerequisite(refresh)
    },
    refetchInterval: poll
      ? (query) => kiroPrerequisiteRefetchInterval(query.state.data, kiroGated())
      : false,
  })
  const checkAgain = useCallback(async () => {
    await queryClient.cancelQueries({ queryKey: KIRO_PREREQUISITE_KEY })
    try {
      await queryClient.fetchQuery({
        queryKey: KIRO_PREREQUISITE_KEY,
        queryFn: () => api.kiroPrerequisite('explicit'),
        staleTime: 0,
      })
    } catch {
      // The failure is on the shared entry: its readers render it.
    }
  }, [queryClient])
  return { statusQuery, checkAgain, checking: statusQuery.isFetching }
}
