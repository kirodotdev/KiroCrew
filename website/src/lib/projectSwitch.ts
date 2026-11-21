/** The one project-switch call path shared by the main chat (sessionPicks.ts)
 *  and a split-view pane (ChatPaneProject.tsx): fire the endpoint through the
 *  adjudicated switch protocol and mirror the directory the response names
 *  into the store. The server realpath-normalizes the directory, so the
 *  response's spelling is what gets written. Error handling stays at the call
 *  sites: each surface owns its own failure notice.
 */

import { api } from '../api/client'
import { updateSlot } from '../store/dashboardSlice'
import type { AppDispatch } from '../store'
import { performSlotSwitch } from './slotSwitch'

export function performProjectSlotSwitch(slot: string, path: string, dispatch: AppDispatch): Promise<void> {
  return performSlotSwitch('project', slot, path,
    async () => {
      const r = await api.chatSlotProject(slot, path)
      return r?.project ?? path
    },
    (value) => dispatch(updateSlot({ key: slot, project: value })))
}
