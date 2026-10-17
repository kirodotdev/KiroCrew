/**
 * setCrewEnabled — the one owner-only switch that turns a crew off or back on.
 *
 * Off: the gateway tears the tunnel down and refuses every connect until the
 * crew is enabled again. The warm entry is dropped only after the gateway
 * accepts, so a refused disable leaves the open pane (and any draft in it) alone.
 * On: the flag is cleared, then the crew reconnects through the same step a tab
 * click takes, so its pane comes back warm.
 */
import { api } from '../api/client'
import { removeWarm } from '../store/instancesSlice'
import type { AppDispatch } from '../store'
import { connectInstanceInto } from './connectInstance'

export async function setCrewEnabled(dispatch: AppDispatch, id: string, enabled: boolean) {
  await api.updateInstance(id, { disabled: !enabled })
  if (enabled) await connectInstanceInto(dispatch, id)
  else dispatch(removeWarm(id))
}
