import { useEffect } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { useAppDispatch } from '../store'
import { setDesktopUpdateAvailable } from '../store/dashboardSlice'

export type UpdateState = {
  state: 'checking' | 'found' | 'available' | 'downloading' | 'downloaded' | 'not-available' | 'error'
  version?: string
  notes?: string
  channel?: string
  message?: string
  mandatory?: boolean
  minimumSupportedVersion?: string
}

type UpdateAPI = {
  onState: (cb: (payload: UpdateState) => void) => (() => void)
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
 * No-ops in the browser (window.updateAPI is only defined by the Electron preload).
 */
export function useUpdateSubscription() {
  const queryClient = useQueryClient()
  const dispatch = useAppDispatch()
  useEffect(() => {
    const api = (window as unknown as { updateAPI?: UpdateAPI }).updateAPI
    if (!api?.onState) return
    return api.onState((payload) => {
      queryClient.setQueryData(['update-state'], payload)
      // Mirror availability into Redux so nav dots (Settings item, About tab)
      // can use the surface-registry badge pipeline. found/downloading/
      // downloaded all mean "an update exists"; not-available clears it.
      // checking/error deliberately leave the flag unchanged.
      if (payload.state === 'found' || payload.state === 'available' || payload.state === 'downloading' || payload.state === 'downloaded') {
        dispatch(setDesktopUpdateAvailable(true))
      } else if (payload.state === 'not-available') {
        dispatch(setDesktopUpdateAvailable(false))
      }
    })
  }, [queryClient, dispatch])
}
