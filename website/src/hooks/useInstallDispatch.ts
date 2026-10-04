import { useEffect, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import type { UpdateState } from './useUpdateSubscription'

/**
 * The desktop "Install update & restart" action, shared by every surface that
 * offers it.
 *
 * Install is a ONE-WAY door, so `dispatched` stays true once the IPC resolves:
 * `update:install` resolves as soon as the install is DISPATCHED, and the
 * platform installer then works for several more seconds before the app quits.
 * Keying the control on `isPending` alone lets it re-arm in that window, so the
 * user sees a clickable install action followed by an unexplained quit — which
 * reads as a crash.
 *
 * EXCEPT when the main process refuses or aborts the handoff and the app keeps
 * running: the freshness gate refuses a stage the feed has moved past, and an
 * abort invalidates the stage mid-dispatch. A stale `isSuccess` would then
 * leave the next version's control disabled for good. The dispatch is "still
 * live" only while the state is 'installing', or 'downloaded' FOR THE VERSION
 * that was clicked — keyed on the version because the IPC resolution can land
 * after the supersede states were already pushed, when a bare state check would
 * read the NEW version's 'downloaded' as the old dispatch still running. A
 * rejected dispatch is reset by the same rule, so its error does not follow the
 * user onto the next version's prompt.
 */
export function useInstallDispatch(update: UpdateState | null | undefined) {
  const installMutation = useMutation({ mutationFn: () => window.updateAPI!.install() })
  const [installFor, setInstallFor] = useState<string | undefined>(undefined)
  const state = update?.state
  const stateVersion = update?.version
  useEffect(() => {
    if (!installMutation.isSuccess && !installMutation.isError) return
    const dispatchStillLive = state === 'installing'
      || (state === 'downloaded' && stateVersion === installFor)
    if (!dispatchStillLive) installMutation.reset()
    // eslint-disable-next-line react-hooks/exhaustive-deps -- reset identity is stable; keying on state transition
  }, [state, stateVersion, installFor, installMutation.isSuccess, installMutation.isError])
  const dispatchInstall = () => {
    setInstallFor(stateVersion)
    installMutation.mutate()
  }
  return {
    installMutation,
    dispatched: installMutation.isPending || installMutation.isSuccess,
    dispatchInstall,
  }
}
