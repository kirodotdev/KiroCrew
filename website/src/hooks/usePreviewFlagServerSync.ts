import { useEffect, useSyncExternalStore } from 'react'
import { useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query'

import { api } from '../api/client'
import { setConfigPathValue } from '../pages/settings/useOptimisticConfigPaths'
import {
  PREVIEW_FLAGS, PREVIEW_INSTANCE_SESSIONS, cachePreviewFlag,
  previewFlagConfigPath, previewFlagName, readPreviewFlag, setPreviewFlagPersister,
} from '../utils/previewFlags'
import { safeRemoveItem } from '../utils/safeStorage'

/** The slice of the `['kirocrewConfig']` body this sync reads. */
export type PreviewFlagsConfigBody = { dashboard?: { feature_previews?: unknown } }

const CONFIG_KEY = ['kirocrewConfig'] as const

/**
 * Toggles whose PATCH has not settled yet, counted per flag. While a flag has
 * one in flight, a config body read before the write landed still carries the
 * old value, and applying it would flip the switch back under the user's hand.
 */
const pending = new Map<string, number>()

/**
 * The last queued PATCH per flag. Each write for a flag starts only after the
 * one before it settled, so the gateway applies a flag's writes in click order
 * and the last click is the value it keeps.
 */
const tails = new Map<string, Promise<void>>()

/** Per flag with toggles queued: the value a refusal of the last one puts back. */
const baselines = new Map<string, boolean>()

/**
 * Browser keys an earlier build kept while it carried switches set before the
 * gateway kept them up once. Nothing reads them now; mounting clears them.
 */
const RETIRED_SYNC_KEYS = ['kc-feature-previews-synced', 'kc-feature-previews-server-backed']

/**
 * The last toggle the gateway refused, for the Feature previews section to say
 * so. Without it a refused write would only flip the switch back, which reads
 * as the switch not working. `denied` is a 403: the dashboard session is not
 * the gateway owner, who alone may write config.
 */
export type PreviewFlagWriteError = {
  flag: string
  denied: boolean
}

let writeError: PreviewFlagWriteError | null = null
const writeErrorListeners = new Set<() => void>()

function setWriteError(next: PreviewFlagWriteError | null): void {
  if (writeError === next) return
  writeError = next
  for (const listener of writeErrorListeners) listener()
}

/** Forget the last refused toggle (the notice's dismiss). */
export function clearPreviewFlagWriteError(): void {
  setWriteError(null)
}

function subscribeWriteError(listener: () => void): () => void {
  writeErrorListeners.add(listener)
  return () => { writeErrorListeners.delete(listener) }
}

/** The last toggle the gateway refused, or `null`; cleared by the next accepted toggle. */
export function usePreviewFlagWriteError(): PreviewFlagWriteError | null {
  return useSyncExternalStore(subscribeWriteError, () => writeError, () => null)
}

/** Reset the module state between tests. */
export function __resetPreviewFlagServerSyncForTests(): void {
  setWriteError(null)
  pending.clear()
  tails.clear()
  baselines.clear()
}

/** The server's `{name: bool}` map, or `null` when the body has none to trust. */
function serverFlags(body: PreviewFlagsConfigBody | undefined): Record<string, unknown> | null {
  const fp = body?.dashboard?.feature_previews
  return fp !== null && typeof fp === 'object' && !Array.isArray(fp) ? fp as Record<string, unknown> : null
}

function settle(qc: QueryClient, flag: string): void {
  const left = (pending.get(flag) ?? 1) - 1
  if (left > 0) pending.set(flag, left)
  else {
    pending.delete(flag)
    baselines.delete(flag)
  }
  // Reconcile against the refetched body directly: one equal to the body already
  // cached keeps its identity (structural sharing), so the hook's effect would not
  // run again, and a refused write would leave the cache disagreeing with it.
  void qc.invalidateQueries({ queryKey: CONFIG_KEY })
    .then(() => reconcilePreviewFlags(qc.getQueryData<PreviewFlagsConfigBody>(CONFIG_KEY)))
}

/**
 * Queue one write of *flag* to the gateway, after any earlier write of it. A
 * refused write is put right by the refetch it ends with: the cache then takes
 * the server's value. A refused toggle is also kept for
 * {@link usePreviewFlagWriteError}, so the switch moving back comes with a
 * reason, and is put back to the value it had before the click.
 */
function persist(qc: QueryClient, flag: string, on: boolean): void {
  // The value a refusal puts back: what the switch showed before the first of
  // the clicks now queued, moved on by each one the gateway accepts. Reverting
  // to `!on` instead would land on the previous click, not on a saved value.
  if (!baselines.has(flag)) baselines.set(flag, !on)
  pending.set(flag, (pending.get(flag) ?? 0) + 1)
  const write = (tails.get(flag) ?? Promise.resolve())
    .then(() => api.patchConfig(previewFlagConfigPath(flag), on))
    .then(() => {
      // Store the accepted value in the cached config body, so readers of it agree with the write.
      qc.setQueryData<PreviewFlagsConfigBody>(CONFIG_KEY, prev =>
        prev && setConfigPathValue(prev, previewFlagConfigPath(flag), on))
      baselines.set(flag, on)
      // Clears only this switch's notice: another switch's refused write is
      // still true after this one saved.
      if (writeError?.flag === flag) setWriteError(null)
    }, (e: unknown) => {
      // A 403 is owner-only unless the session itself no longer authenticates,
      // which is a different cause to name.
      const status = (e as { status?: unknown } | null)?.status
      const authRequired = (e as { authRequired?: unknown } | null)?.authRequired === true
      const denied = status === 403 && !authRequired
      // Only the last queued write reports and reverts: a later click still on
      // its way decides where the switch ends, and a notice saying "switched
      // back" under it would be wrong. The revert happens here rather than in
      // the refetch: with no config body to read, reconcile keeps the cache.
      if (pending.get(flag) === 1) {
        setWriteError({ flag, denied })
        cachePreviewFlag(flag, baselines.get(flag) ?? !on)
      }
    })
  const tail = write.then(() => settle(qc, flag))
  tails.set(flag, tail)
  void tail.then(() => { if (tails.get(flag) === tail) tails.delete(flag) })
}

/**
 * Bring the browser cache in line with one config body: the gateway value wins.
 *
 * - Each flag takes the value the server holds, OFF when it holds none, unless
 *   a toggle of that flag is still in flight.
 * - A body with no `feature_previews` object (a failed read has none) leaves the
 *   cache as it is.
 */
export function reconcilePreviewFlags(body: PreviewFlagsConfigBody | undefined): void {
  const fp = serverFlags(body)
  if (fp === null) return
  for (const flag of PREVIEW_FLAGS) {
    if (pending.has(flag)) continue
    const server = fp[previewFlagName(flag)] === true
    if (readPreviewFlag(flag) !== server) cachePreviewFlag(flag, server)
  }
}

/**
 * Keep the Feature previews switches on the gateway, with the browser copy as a
 * cache (see `utils/previewFlags.ts`). Mount once, in the app shell.
 *
 * Reads the shared `['kirocrewConfig']` query, which a config `refresh` frame
 * already invalidates, so a switch flipped on another surface reaches this one
 * without a reload. Installs the persister `setPreviewFlag` hands each toggle to.
 */
export function usePreviewFlagServerSync(): void {
  const qc = useQueryClient()
  const { data } = useQuery<PreviewFlagsConfigBody>({
    queryKey: CONFIG_KEY,
    queryFn: () => api.kirocrewConfig(),
  })
  useEffect(() => {
    for (const key of RETIRED_SYNC_KEYS) safeRemoveItem(key)
    // The retired "Remote crew sessions" key has no server copy, so left on it
    // would keep "Chat on a crew" on behind a switch reading off.
    if (readPreviewFlag(PREVIEW_INSTANCE_SESSIONS)) cachePreviewFlag(PREVIEW_INSTANCE_SESSIONS, false)
  }, [])
  useEffect(() => {
    setPreviewFlagPersister((flag, on) => {
      // The retired key has no server copy; `setRemoteCrewChat` clears it in the
      // cache only.
      if (PREVIEW_FLAGS.includes(flag)) persist(qc, flag, on)
    })
    return () => setPreviewFlagPersister(null)
  }, [qc])
  useEffect(() => {
    reconcilePreviewFlags(data)
  }, [data])
}
