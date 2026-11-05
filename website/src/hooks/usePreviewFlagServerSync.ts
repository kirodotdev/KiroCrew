import { useEffect, useSyncExternalStore } from 'react'
import { useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query'

import { api } from '../api/client'
import { setConfigPathValue } from '../pages/settings/useOptimisticConfigPaths'
import {
  PREVIEW_FLAGS, PREVIEW_INSTANCE_SESSIONS, PREVIEW_REMOTE_CREW_CHAT, cachePreviewFlag,
  previewFlagConfigPath, previewFlagName, readPreviewFlag, setPreviewFlagPersister,
} from '../utils/previewFlags'
import { safeGetItem, safeSetItem } from '../utils/safeStorage'

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
 * Flags this document has already tried to carry up from the cache. A failed
 * carry is not retried on every config refetch; the next document load tries
 * again.
 */
const carried = new Set<string>()

/** Carries this document started that have not settled, and whether any failed. */
let carriesInFlight = 0
let carryFailed = false

/**
 * Set in this browser once its cache has been brought in line with a server
 * copy, so every cache-only ON from then on is a value the server gave and
 * later dropped, not one set before the server kept the switches. Outside the
 * `mc-preview-` prefix, so it is never read as a flag.
 */
export const PREVIEW_FLAGS_SYNCED_KEY = 'kc-feature-previews-synced'

/**
 * The flags this browser has seen the server hold, or has saved to it, kept per
 * flag as a JSON array of keys. A flag listed here that the server no longer
 * holds was dropped there, so it reads as OFF even while the browser as a whole
 * is not yet marked synced (a carry of another switch may have failed). Outside
 * the `mc-preview-` prefix, so it is never read as a flag.
 */
export const PREVIEW_FLAGS_SERVER_BACKED_KEY = 'kc-feature-previews-server-backed'

/**
 * The last toggle the gateway refused, for the Feature previews section to say
 * so. Without it a refused write would only flip the switch back, which reads
 * as the switch not working. `denied` is a 403: the dashboard session is not
 * the gateway owner, who alone may write config.
 */
export type PreviewFlagWriteError = {
  flag: string
  denied: boolean
  /**
   * The one-time carry-up of a switch set in this browser before the gateway
   * kept it was refused (anything but the 409 that means the gateway already
   * holds a value). The switch stays on here, so the notice says it is on in
   * this browser only rather than that it was switched back.
   */
  carry?: true
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
  carried.clear()
  documentBacked.clear()
  carriesInFlight = 0
  carryFailed = false
}

function markSynced(): void {
  safeSetItem(PREVIEW_FLAGS_SYNCED_KEY, '1')
}

/**
 * The flags this document has seen the server hold, kept beside the stored
 * record so a storage write that fails still counts for the rest of the page.
 */
const documentBacked = new Set<string>()

/** The flags this browser knows the server has held; an unreadable entry reads as none. */
function serverBacked(): Set<string> {
  let stored: string[] = []
  try {
    const raw: unknown = JSON.parse(safeGetItem(PREVIEW_FLAGS_SERVER_BACKED_KEY) ?? '[]')
    if (Array.isArray(raw)) stored = raw.filter((v): v is string => typeof v === 'string')
  } catch {
    // Read as none; the document's own record still applies.
  }
  return new Set([...stored, ...documentBacked])
}

function markServerBacked(flag: string): void {
  documentBacked.add(flag)
  safeSetItem(PREVIEW_FLAGS_SERVER_BACKED_KEY, JSON.stringify([...serverBacked()]))
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
    .then(() => reconcilePreviewFlags(qc, qc.getQueryData<PreviewFlagsConfigBody>(CONFIG_KEY)))
}

/**
 * Queue one write of *flag* to the gateway, after any earlier write of it. A
 * refused write is put right by the refetch it ends with: the server's own
 * value, when it has one, is what the cache then takes. A refused toggle is
 * also kept for {@link usePreviewFlagWriteError}, so the switch moving back
 * comes with a reason, and is put back to the value it had before the click.
 * `ifUnset` makes the gateway refuse (409) when the key is already set.
 * Resolves to whether the gateway accepted it (for a carry, a 409 counts: the
 * gateway already holds a value for the flag).
 */
function persist(qc: QueryClient, flag: string, on: boolean, ifUnset = false): Promise<boolean> {
  // The value a refusal puts back: what the switch showed before the first of
  // the clicks now queued, moved on by each one the gateway accepts. Reverting
  // to `!on` instead would land on the previous click, not on a saved value.
  // A carry still in flight does not count as a queued click, so the first
  // click behind it records the value the switch showed before it.
  if (!ifUnset && !baselines.has(flag)) baselines.set(flag, !on)
  pending.set(flag, (pending.get(flag) ?? 0) + 1)
  const write = (tails.get(flag) ?? Promise.resolve())
    .then(() => api.patchConfig(previewFlagConfigPath(flag), on, ifUnset ? { ifUnset: true } : undefined))
    .then(() => {
      // Store the accepted value in the cached config body, so readers of it agree with the write.
      qc.setQueryData<PreviewFlagsConfigBody>(CONFIG_KEY, prev =>
        prev && setConfigPathValue(prev, previewFlagConfigPath(flag), on))
      markServerBacked(flag)
      // An accepted carry is a saved value too, so a click queued behind it
      // that is refused goes back to it.
      if (baselines.has(flag)) baselines.set(flag, on)
      // Clears only this switch's notice: another switch's refused write is
      // still true after this one saved.
      if (!ifUnset && writeError?.flag === flag) setWriteError(null)
      return true
    }, (e: unknown) => {
      const status = (e as { status?: unknown } | null)?.status
      // A carry the gateway answered 409 found the key already set: the server
      // holds a choice for it, which is what the carry was there to make sure of.
      // A 403 is owner-only unless the session itself no longer authenticates,
      // which is a different cause to name.
      const authRequired = (e as { authRequired?: unknown } | null)?.authRequired === true
      const denied = status === 403 && !authRequired
      if (ifUnset) {
        if (status === 409) {
          markServerBacked(flag)
          return true
        }
        // A click queued behind the carry decides where the switch ends, so the
        // carry's outcome is not reported over it.
        if (pending.get(flag) === 1) setWriteError({ flag, denied, carry: true })
        return false
      }
      // In a browser that has not synced yet, a refused toggle leaves this
      // browser's own value unsaved anywhere else, so it must not be marked
      // synced: the next reconcile would then read the switch as off.
      if (safeGetItem(PREVIEW_FLAGS_SYNCED_KEY) !== '1') carryFailed = true
      // Only the last queued write reports and reverts: a later click still on
      // its way decides where the switch ends, and a notice saying "switched
      // back" under it would be wrong. The revert happens here rather than in
      // the refetch: with no server value for the flag (or no body at all),
      // reconcile would keep the cache, or carry it up. `carried` keeps
      // reconcile from carrying the refused value up afterwards.
      if (pending.get(flag) === 1) {
        setWriteError({ flag, denied })
        carried.add(flag)
        cachePreviewFlag(flag, baselines.get(flag) ?? !on)
      }
      return false
    })
  const tail = write.then(() => settle(qc, flag))
  tails.set(flag, tail)
  void tail.then(() => { if (tails.get(flag) === tail) tails.delete(flag) })
  return write
}

/**
 * Bring the browser cache in line with one config body.
 *
 * - A flag the server holds as `true`/`false` is written into the cache when the
 *   cache differs, unless a toggle of that flag is still in flight.
 * - A flag the server does not hold reads as OFF, except in a browser that has
 *   never synced and has never seen the server hold that flag (tracked per
 *   flag, so one switch's failed carry cannot keep another dropped switch on):
 *   there a cache-only ON is carried up once with `if_unset`, so
 *   a value another surface wrote first is never replaced. This is the one-time
 *   migration of a switch set before the server kept it.
 * - The retired "Remote crew sessions" key is never carried as itself: an ON is
 *   carried as "Chat on a crew", and the key is cleared once the server holds
 *   that switch, so it cannot keep the feature on behind a switch reading off.
 * - A body with no `feature_previews` object (a failed read has none) leaves the
 *   cache as it is.
 */
export function reconcilePreviewFlags(qc: QueryClient, body: PreviewFlagsConfigBody | undefined): void {
  const fp = serverFlags(body)
  if (fp === null) return
  const synced = safeGetItem(PREVIEW_FLAGS_SYNCED_KEY) === '1'
  const backed = serverBacked()
  const remote = fp[previewFlagName(PREVIEW_REMOTE_CREW_CHAT)]
  if (readPreviewFlag(PREVIEW_INSTANCE_SESSIONS)
    && (synced || backed.has(PREVIEW_REMOTE_CREW_CHAT) || typeof remote === 'boolean')) {
    cachePreviewFlag(PREVIEW_INSTANCE_SESSIONS, false)
  }
  // A flag with a toggle in flight is not settled yet, so this pass cannot
  // conclude the migration: its value may still have to be carried up.
  let deferred = false
  for (const flag of PREVIEW_FLAGS) {
    if (pending.has(flag)) {
      deferred = true
      continue
    }
    const server = fp[previewFlagName(flag)]
    if (typeof server === 'boolean') {
      if (readPreviewFlag(flag) !== server) cachePreviewFlag(flag, server)
      if (!backed.has(flag)) markServerBacked(flag)
      // A refused carry said this switch was on in this browser only; now that
      // the gateway holds a value for it, that notice no longer holds.
      if (writeError?.flag === flag && writeError.carry) setWriteError(null)
      continue
    }
    const cacheOn = readPreviewFlag(flag)
      || (flag === PREVIEW_REMOTE_CREW_CHAT && readPreviewFlag(PREVIEW_INSTANCE_SESSIONS))
    if (!cacheOn) continue
    // A flag the server held before and no longer does was dropped there,
    // whatever became of another flag's carry.
    if (synced || backed.has(flag)) {
      cachePreviewFlag(flag, false)
      continue
    }
    if (carried.has(flag)) continue
    carried.add(flag)
    carriesInFlight += 1
    void persist(qc, flag, true, true).then(ok => {
      carriesInFlight -= 1
      if (!ok) carryFailed = true
      if (carriesInFlight === 0 && !carryFailed && pending.size === 0) markSynced()
    })
  }
  if (!synced && !deferred && carriesInFlight === 0 && !carryFailed) markSynced()
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
    setPreviewFlagPersister((flag, on) => {
      // The retired key has no server copy; `setRemoteCrewChat` clears it in the
      // cache only.
      if (PREVIEW_FLAGS.includes(flag)) void persist(qc, flag, on)
    })
    return () => setPreviewFlagPersister(null)
  }, [qc])
  useEffect(() => {
    reconcilePreviewFlags(qc, data)
  }, [qc, data])
}
