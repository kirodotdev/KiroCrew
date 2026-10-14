/**
 * setCrewEnabled — the one owner-only switch that turns a crew off or back on.
 *
 * Off: the gateway tears the tunnel down and refuses every connect until the
 * crew is enabled again. The warm entry is dropped only after the gateway
 * accepts, so a refused disable leaves the open pane (and any draft in it) alone.
 * On: the flag is cleared, then the crew reconnects through the same step a tab
 * click takes, so its pane comes back warm -- unless the record the gateway
 * returns names a transport this build does not map. The gateway dials any
 * method it does not recognise over SSH, so enabling such a crew only clears
 * the flag; its pane shows the "update Kiro Crew" hint, like every other path.
 */
import { api } from '../api/client'
import { removeWarm } from '../store/instancesSlice'
import type { AppDispatch } from '../store'
import { transportPresentation } from '../utils/remoteCrew'
import { connectInstanceInto } from './connectInstance'

export async function setCrewEnabled(dispatch: AppDispatch, id: string, enabled: boolean) {
  // The gateway answers with the updated record, so the method checked here is
  // the one it would dial, not a possibly stale copy from the caller's list.
  const view = await api.updateInstance(id, { disabled: !enabled })
  if (!enabled) {
    dispatch(removeWarm(id))
    return
  }
  if (!transportPresentation(view ?? {}).mapped) return
  await connectInstanceInto(dispatch, id)
}
