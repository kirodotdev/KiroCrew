import { useEffect } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { useAppDispatch } from '../store'
import { setDesktopUpdateAvailable } from '../store/dashboardSlice'

/**
 * The update lifecycle payload pushed by the Electron main process.
 *
 * The single shared declaration: AboutPanel and UpdateModal import it rather
 * than re-declaring, so a field added for one consumer cannot be silently
 * absent at another — a missing optional field here reads as "never sent" at
 * the consumer that nobody remembered to edit.
 */
export type UpdateState = {
  state: 'checking' | 'found' | 'available' | 'downloading' | 'downloaded' | 'installing' | 'not-available' | 'error'
  version?: string
  notes?: string
  pubDate?: string
  channel?: string
  message?: string
  /** Which stage failed. Absent on builds older than the phase-aware emit. */
  phase?: 'check' | 'download' | 'install'
  /** Stable failure class; the user-facing copy is chosen from this, not from `message`. */
  code?: string
  httpStatus?: number
  /** Download progress, 0-100. Absent until the first progress event arrives. */
  percent?: number
  bytesPerSecond?: number
  /**
   * True when this payload was replayed from getInfo() on a fresh mount rather
   * than pushed live. Restoration surfaces (the About card) render it like any
   * other state; interruption surfaces (the UpdateModal) ignore it, so a
   * dismissed modal is not resurrected by every renderer reload.
   */
  replayed?: boolean
}

type UpdateAPI = {
  onState: (cb: (payload: UpdateState) => void) => (() => void)
  /**
   * Optional: the preload bridge exposes it, but an older bundle paired with a
   * newer renderer may not — replay then degrades to the pre-replay behaviour.
   */
  getInfo?: () => Promise<{ lastState?: UpdateState | null } | undefined>
}

/**
 * Subscribes to the Electron main process's update lifecycle events and mirrors
 * each one into the shared ['update-state'] React Query cache.
 *
 * Must be mounted exactly once at the app root (App.tsx) so update events are
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
export function useUpdateSubscription() {
  const queryClient = useQueryClient()
  const dispatch = useAppDispatch()
  useEffect(() => {
    const api = (window as unknown as { updateAPI?: UpdateAPI }).updateAPI
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
    api.getInfo?.().then(info => {
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
