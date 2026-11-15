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
 *
 * Install now only ARMS: the update installs when the user quits, and the main
 * process re-emits 'downloaded' with `armed: true` for the same version. That
 * is the finished state, not a dispatch still in flight, so it ends the latch
 * (the control must not sit on a disabled "Restarting..." while nothing
 * restarts) and the surface shows `armed` copy plus a Restart now action
 * (`restartNow`, the plain relaunch whose quit applies the armed update).
 */
export function useInstallDispatch(update: UpdateState | null | undefined) {
  const installMutation = useMutation({ mutationFn: () => window.updateAPI!.install() })
  const [installFor, setInstallFor] = useState<string | undefined>(undefined)
  // Restart now: a plain quit and relaunch. Absent on a shell that predates it.
  const relaunchMutation = useMutation({
    mutationFn: async () => {
      const relaunch = window.updateAPI?.relaunch
      if (typeof relaunch !== 'function') throw new Error('relaunch is unavailable')
      const result = await relaunch()
      if (result && result.ok === false) throw new Error(result.error || 'relaunch refused')
      return result
    },
  })
  const state = update?.state
  const stateVersion = update?.version
  const armed = state === 'downloaded' && update?.armed === true
  useEffect(() => {
    if (!installMutation.isSuccess && !installMutation.isError) return
    const dispatchStillLive = state === 'installing'
      || (state === 'downloaded' && !armed && stateVersion === installFor)
    if (!dispatchStillLive) installMutation.reset()
    // eslint-disable-next-line react-hooks/exhaustive-deps -- reset identity is stable; keying on state transition
  }, [state, stateVersion, installFor, armed, installMutation.isSuccess, installMutation.isError])
  const dispatchInstall = () => {
    setInstallFor(stateVersion)
    installMutation.mutate()
  }
  return {
    installMutation,
    // An armed update is finished arming, not dispatching: the latch is off.
    dispatched: !armed && (installMutation.isPending || installMutation.isSuccess),
    dispatchInstall,
    /** The update is armed: it installs when the user quits. */
    armed,
    /**
     * The shell's install ARMS the update instead of applying it at once. An older
     * shell's `update:install` stops the gateway and installs immediately, so the
     * "Install when I quit" action must not reach it: it is offered only when the
     * bridge advertises `armsOnQuit`.
     */
    canArmOnQuit: typeof window !== 'undefined' && window.updateAPI?.armsOnQuit === true,
    /** Restart now is offered only when the shell exposes `relaunch`. */
    canRestartNow: typeof window !== 'undefined' && typeof window.updateAPI?.relaunch === 'function',
    restartNow: () => relaunchMutation.mutate(),
    restartNowPending: relaunchMutation.isPending || relaunchMutation.isSuccess,
    restartNowFailed: relaunchMutation.isError,
  }
}
