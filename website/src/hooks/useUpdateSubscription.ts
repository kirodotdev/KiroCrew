import { useEffect } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { useAppDispatch, useAppSelector } from '../store'
import { setDesktopUpdateAvailable, selectPolicyGoverned } from '../store/dashboardSlice'
import { updateInfoQuery } from '../api/updateInfoQuery'

export type UpdateState = globalThis.UpdateState

/**
 * The stop command an install refused with `gateway-still-running` names, or
 * null for any other state. Both refusal surfaces (UpdateModal and the About
 * card) render it verbatim after the catalog sentence that points at it, and
 * fall back to their generic install-failure copy when no command came with
 * the refusal.
 */
export function gatewayStopCommand(st: UpdateState | null | undefined): string | null {
  if (st?.state !== 'error' || st.code !== 'gateway-still-running') return null
  return typeof st.stopCommand === 'string' && st.stopCommand.trim() ? st.stopCommand : null
}

/**
 * The update lifecycle payload pushed by the Electron main process.
 *
 * The single shared declaration: AboutPanel and UpdateModal import it rather
 * than re-declaring, so a field added for one consumer cannot be silently
 * absent at another — a missing optional field here reads as "never sent" at
 * the consumer that nobody remembered to edit.
 */
/**
 * Subscribes to the Electron main process's update lifecycle events and mirrors
 * each one into the shared ['update-state'] React Query cache.
 *
 * Must be mounted exactly once at the app root (shell/appShell.tsx) so update events are
 * captured regardless of which page is visible. Both UpdateModal and the
 * Settings > About panel read from this cache — if the subscription lived only
 * inside About, the modal would never fire unless the user opened About first.
 *
 * On mount it also REPLAYS the last state the main process emitted, pulled from
 * the same info payload the panel already requests. Pushed state dies with the
 * renderer: the post-install-failure recovery path reloads the window, and
 * without the replay the failure card (and its Retry) vanish — the user sees a
 * loading screen and then the old version with no error surfaced. A live event
 * always wins over the replay: the listener is registered first, and the seed
 * only lands while the cache is still empty.
 *
 * Only two states are worth restoring: a staged build awaiting install
 * ('downloaded') and an install failure — the states a reload strands the user
 * without, because nothing re-emits them (the 4h poll is gated off while a
 * build is staged). Everything else is either transient (a check/download in
 * flight keeps emitting live events) or goes stale in a way that misleads — an
 * offline error from hours ago replayed as current reads as "the update lane
 * is broken now".
 *
 * No-ops in the browser (window.updateAPI is only defined by the Electron preload).
 */
/**
 * Keeps the Electron main process informed when the gateway's security policy
 * provider governs this install's updates, so the feed lane stands down and
 * the Electron auto-updater does not compete with the provider.
 *
 * No-ops in the browser (window.updateAPI is only defined by the Electron
 * preload). Also no-ops when the shell is too old to expose
 * `notifyPolicyGoverned` (the field is optional; absent on older shells).
 */
export function usePolicyGovernedNotification() {
  const governed = useAppSelector(selectPolicyGoverned)
  // Do not send `false` before the gateway status has loaded: on a window
  // reload Redux resets to null and selectPolicyGoverned returns false before
  // the status arrives, which would transiently clear governance and let the
  // Electron feed lane fire while the policy provider still owns the install.
  const statusLoaded = useAppSelector(s => s.dashboard.status !== null)
  useEffect(() => {
    if (!statusLoaded && !governed) return
    window.updateAPI?.notifyPolicyGoverned?.(governed)
  }, [governed, statusLoaded])
}

export function useUpdateSubscription() {
  const queryClient = useQueryClient()
  const dispatch = useAppDispatch()
  useEffect(() => {
    const api = window.updateAPI
    if (!api?.onState) return
    const apply = (payload: UpdateState) => {
      queryClient.setQueryData(['update-state'], payload)
      // Mirror availability into Redux so nav dots (Settings item, About tab)
      // can use the surface-registry badge pipeline. found/downloading/
      // downloaded all mean "an update exists"; not-available clears it.
      // checking/installing/error deliberately leave the flag unchanged.
      if (payload.state === 'found' || payload.state === 'available' || payload.state === 'downloading' || payload.state === 'downloaded') {
        dispatch(setDesktopUpdateAvailable(true))
      } else if (payload.state === 'not-available') {
        dispatch(setDesktopUpdateAvailable(false))
      }
    }
    const unsubscribe = api.onState(apply)
    let disposed = false
    // Best-effort replay: a failure here (old preload, IPC teardown) just
    // leaves the pre-replay behaviour, so it must never throw out of the hook.
    // Through the shared query, so this one IPC also serves every later
    // ['update-info'] reader (About, the What's-new modal, settings search).
    queryClient.fetchQuery({ ...updateInfoQuery, retry: false }).then(info => {
      if (disposed) return
      const last = info?.lastState
      if (!last) return
      const worthRestoring = last.state === 'downloaded'
        || (last.state === 'error' && last.phase === 'install')
      if (!worthRestoring) return
      // A live event that arrived while the round-trip was in flight is newer
      // than the snapshot it returned — never overwrite it.
      if (queryClient.getQueryData(['update-state']) != null) return
      apply({ ...last, replayed: true })
    }).catch(() => { /* replay is best-effort */ })
    return () => {
      disposed = true
      unsubscribe()
    }
  }, [queryClient, dispatch])
}
