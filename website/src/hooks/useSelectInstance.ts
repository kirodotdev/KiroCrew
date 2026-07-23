/**
 * useSelectInstance — THE single owner of "switch to a pane" semantics, shared
 * by every top-level switching surface (InstanceTabBar clicks and the
 * useInstanceShortcuts keyboard chord), so the two paths can never drift:
 * a future fix to how selection/reconnect works lands in both automatically.
 *
 * Semantics (hard-won — see the inline comments):
 *  - Always activate the target so its pane shows immediately — the warm
 *    iframe if connected, otherwise the in-pane connecting/error panel.
 *  - (Re)connect when the pane has no warm iframe yet OR its live tunnel is no
 *    longer connected. The status check matters: a mid-session tunnel drop
 *    flips status to error/disconnected but does NOT clear the stale `warm`
 *    entry, so gating only on `!warm[id]` would skip the reconnect AND hide
 *    the error panel — selecting the (red) tab would do nothing visible.
 *  - A failed connect never removes the tab; the in-pane error panel shows
 *    (see InstancesViewport).
 *
 * Takes the caller's `instances` list (both consumers already subscribe to the
 * shared ['instances'] React Query cache) rather than running its own query.
 */
import { useCallback } from 'react'
import { useMutation } from '@tanstack/react-query'
import { api, type InstanceView } from '../api/client'
import { useAppDispatch, useAppSelector } from '../store'
import { setWarm, setActiveId } from '../store/instancesSlice'

export function useSelectInstance(instances: InstanceView[]) {
  const dispatch = useAppDispatch()
  const warm = useAppSelector(s => s.instances.warm)

  const connectMutation = useMutation({
    mutationFn: (id: string) => api.connectInstance(id),
    onSuccess: (st, id) => {
      if (st.state === 'connected' && st.local_port && st.token) {
        dispatch(setWarm({ id, conn: { port: st.local_port, token: st.token } }))
      }
      // The pane was already activated on select; on failure the active pane
      // shows the in-pane error/reconnect panel (see InstancesViewport).
    },
  })

  /** Switch to instance `id`, or to the Local dashboard when `id` is null. */
  const selectInstance = useCallback(
    (id: string | null) => {
      dispatch(setActiveId(id))
      if (id === null) return
      const inst = instances.find(i => i.id === id)
      const live = !inst || inst.status?.state === 'connected'
      if (!warm[id] || !live) connectMutation.mutate(id)
    },
    [warm, instances, dispatch, connectMutation],
  )

  return { selectInstance, connectMutation }
}
