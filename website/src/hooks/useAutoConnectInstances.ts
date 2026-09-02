/**
 * useAutoConnectInstances — proactively brings remote-crew tunnels UP so a crew
 * is live the moment you open the web app, instead of sitting Offline until you
 * click it. This is the "when I use the web app, it tries to connect all my
 * crews" behavior; the backend's own startup revive
 * (_revive_intended_instances) only fires on GATEWAY restart, which never
 * happens on a long-lived Mac after the SSH forwarder dies on sleep — so the
 * frontend has to be the one to notice and re-establish.
 *
 * WHAT IT DOES
 *  - After the shared ['instances'] query resolves, and again whenever the tab
 *    regains focus/visibility, it fans `connectInstanceInto` at every crew that
 *    is not already live, bounded by concurrency and the warm-set cap.
 *  - Reuses the SAME connect-and-store unit as manual select
 *    (connectInstanceInto), so an auto-connected pane goes warm through the
 *    exact path a click would take. It NEVER touches `activeId`: tunnels come
 *    up in the background; the user is not yanked to a crew they didn't pick.
 *
 * WHY EACH GUARD EXISTS
 *  - Default-on setting (mc-auto-connect): each connect is a real SSH session +
 *    a remote token mint, so a many-crew user can turn the whole behavior off.
 *  - Per-instance opt-out (mc-auto-connect-exclude): skip a specific crew (a
 *    flaky or rarely-used host) without disabling the rest.
 *  - Warm-set cap: connecting past the cap would only make the viewport evict
 *    an equal number, so we target at most `warmCap` crews and skip any already
 *    connected — no thrash.
 *  - Per-instance cooldown: focus/visibility can fire in bursts (window manager
 *    churn, tab thrashing); a short cooldown keeps a burst from spamming mints
 *    at the same host. A genuinely-dropped tunnel is retried once the cooldown
 *    elapses.
 *  - Embedded panes never run this: an embedded pane shows no switcher and must
 *    not connect onward (see isEmbeddedPane).
 *
 * Registered ONCE from App.tsx (like useInstanceShortcuts), never inside a
 * component that can mount more than once.
 */
import { useCallback, useEffect, useRef } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type InstanceView } from '../api/client'
import { useAppDispatch, useAppSelector } from '../store'
import { type WarmConn } from '../store/instancesSlice'
import { isEmbeddedPane } from '../lib/embedded'
import { connectInstanceInto } from '../lib/connectInstance'

/** Global default-on switch. Absent key ⇒ on; '0' ⇒ off. */
export const AUTO_CONNECT_KEY = 'mc-auto-connect'
/** JSON array of instance ids the user opted OUT of auto-connect for. */
export const AUTO_CONNECT_EXCLUDE_KEY = 'mc-auto-connect-exclude'

/** How many connects run at once — each is an SSH session + a remote mint. */
const CONCURRENCY = 3
/** Per-instance cooldown (ms): a focus burst must not re-hit the same host. */
const COOLDOWN_MS = 20_000

export function autoConnectEnabled(): boolean {
  try {
    return localStorage.getItem(AUTO_CONNECT_KEY) !== '0'
  } catch {
    return true
  }
}

/** Read the opt-out id set, tolerant of a missing/corrupt value. */
export function readAutoConnectExcludes(): Set<string> {
  try {
    const raw = localStorage.getItem(AUTO_CONNECT_EXCLUDE_KEY)
    if (!raw) return new Set()
    const arr = JSON.parse(raw)
    return Array.isArray(arr) ? new Set(arr.filter(x => typeof x === 'string')) : new Set()
  } catch {
    return new Set()
  }
}

/**
 * Pure target picker (unit-tested): the crews auto-connect should raise right
 * now. A crew is a target when it is NOT excluded and NOT already live — "live"
 * means it has a warm entry AND its polled status is `connected`, mirroring the
 * (re)connect gate in useSelectInstance so a dropped tunnel (warm-but-not-
 * connected) is retried. The cap is a budget of NEW connects against the warm
 * set: already-live panes (warm AND connected) are subtracted from `warmCap`
 * first, so auto-connect fills only the remaining slots and never drives the
 * viewport's LRU eviction (InstancesViewport.removeWarm) by overshooting it.
 */
export function selectAutoConnectTargets(
  instances: InstanceView[],
  warm: Record<string, WarmConn>,
  excluded: Set<string>,
  warmCap: number,
): string[] {
  const isLive = (inst: InstanceView) => !!warm[inst.id] && inst.status?.state === 'connected'
  // Already-live panes occupy warm slots; only the remainder is free to fill.
  const liveCount = instances.filter(isLive).length
  const budget = Math.max(0, warmCap - liveCount)
  const out: string[] = []
  for (const inst of instances) {
    if (out.length >= budget) break
    if (excluded.has(inst.id)) continue
    if (isLive(inst)) continue
    out.push(inst.id)
  }
  return out
}

/** Run `task` over `ids` with a fixed-size worker pool. */
async function runBounded(ids: string[], limit: number, task: (id: string) => Promise<void>) {
  let i = 0
  const worker = async () => {
    while (i < ids.length) {
      const id = ids[i++]
      await task(id)
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, ids.length) }, worker))
}

const EMPTY_WARM: Record<string, WarmConn> = {}

export function useAutoConnectInstances() {
  const dispatch = useAppDispatch()
  const queryClient = useQueryClient()
  const warm = useAppSelector(s => s.instances?.warm ?? EMPTY_WARM)
  const embedded = isEmbeddedPane()

  // Share the SAME ['instances'] cache the tab bar / viewport already poll —
  // react-query dedupes by key, so this adds no extra network traffic.
  const instancesQuery = useQuery({
    queryKey: ['instances'],
    queryFn: () => api.listInstances(),
    enabled: !embedded,
  })

  // Last attempt time per instance id, for the per-instance cooldown. A ref so
  // it survives re-renders without re-triggering the effect.
  const lastAttempt = useRef<Record<string, number>>({})
  // Guards against a second fan-out starting while one is still in flight.
  const running = useRef(false)
  // Live mirror of `warm` for the async fan-out, so a connect that lands mid-run
  // updates what later iterations see as already-live without a stale closure.
  const warmRef = useRef(warm)
  warmRef.current = warm

  const fanOut = useCallback(async () => {
    if (embedded || running.current || !autoConnectEnabled()) return
    const data = instancesQuery.data
    if (!data?.active || !data.instances?.length) return

    const warmCap = data.warm_set_cap || 5
    const excluded = readAutoConnectExcludes()
    const now = Date.now()
    const targets = selectAutoConnectTargets(data.instances, warmRef.current, excluded, warmCap)
      // Drop anything attempted within the cooldown so a focus burst can't spam
      // the same host's SSH + mint.
      .filter(id => now - (lastAttempt.current[id] ?? 0) >= COOLDOWN_MS)

    if (!targets.length) return
    running.current = true
    try {
      await runBounded(targets, CONCURRENCY, async id => {
        lastAttempt.current[id] = Date.now()
        try {
          await connectInstanceInto(dispatch, id)
        } catch {
          // A failed/unreachable host settles into the switcher's terminal error
          // dot via the status poll; auto-connect stays silent and lets the
          // cooldown gate the next attempt. Never surface an error here.
        }
      })
    } finally {
      running.current = false
      // Refresh statuses after the fan-out so the poll reflects what actually
      // connected (and what silently failed) rather than the pre-connect view.
      void queryClient.invalidateQueries({ queryKey: ['instances'] })
    }
  }, [embedded, instancesQuery.data, dispatch, queryClient])

  // Trigger 1: the first (and every subsequent) successful instances load.
  useEffect(() => {
    if (instancesQuery.isSuccess) void fanOut()
  }, [instancesQuery.isSuccess, instancesQuery.dataUpdatedAt, fanOut])

  // Trigger 2: tab regains focus / becomes visible — the Mac-sleep case where
  // the gateway lived but the local SSH forwarder died. A tunnel that dropped
  // since the last poll still reads `connected` in the cached status, so fanning
  // out on the STALE cache would skip exactly the dead tunnel we want back.
  // Invalidate first: the refetch resolves, Trigger 1's dataUpdatedAt effect
  // then fans out on fresh status. (No refetch in flight? invalidate is a cheap
  // no-op beyond one request.)
  useEffect(() => {
    if (embedded) return
    const refresh = () => { void queryClient.invalidateQueries({ queryKey: ['instances'] }) }
    const onFocus = () => refresh()
    const onVisible = () => { if (document.visibilityState === 'visible') refresh() }
    window.addEventListener('focus', onFocus)
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      window.removeEventListener('focus', onFocus)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [embedded, queryClient])
}
