/** The one agent-switch call path shared by every surface (#5120).
 *
 *  App.tsx (keyboard cycle), ChatPage.tsx and ChatPane.tsx all switch a
 *  slot's agent the same way: fire the endpoint through the adjudicated
 *  switch protocol and mirror EXACTLY what the response names — the stored
 *  agent plus the re-resolved workspace binding, as one pair — into the
 *  store. Hoisted here so the named follow-up (carrying `project` in the
 *  response and adjudicating it as a third member of the pair) edits one
 *  site, not three. Error handling stays at the call sites: each surface
 *  owns its own failure notice.
 */

import { api } from '../api/client'
import { updateSlot } from '../store/dashboardSlice'
import type { AppDispatch } from '../store'
import { performSlotSwitch } from './slotSwitch'

export function performAgentSlotSwitch(
  slot: string,
  agent: string,
  dispatch: AppDispatch,
  kind?: 'member' | 'template',
): Promise<void> {
  // The ticket identity stays the bare name: burst stepping (the cycle
  // shortcuts) advances by name, and two kinds of one name are never both in
  // flight from a single control.
  return performSlotSwitch('agent', slot, agent,
    async () => {
      // Two-arg form when no kind was picked: the legacy request shape, byte
      // for byte, so a name-only control sends exactly what it always sent.
      const r = kind ? await api.chatSlotAgent(slot, agent, kind) : await api.chatSlotAgent(slot, agent)
      return { agent: r?.agent ?? agent, agentKind: r?.agent_kind, workspace: r?.workspace }
    },
    (value) => dispatch(updateSlot({
      key: slot, agent: value.agent,
      // Absent means the response did not name it (an older gateway); the
      // write then leaves the slot's stored value alone rather than clobber.
      ...(value.agentKind !== undefined ? { agent_kind: value.agentKind } : {}),
      // An absent workspace means the response did not name one; the write
      // must then leave the slot's workspace untouched rather than clobber.
      ...(value.workspace !== undefined ? { workspace: value.workspace } : {}),
    })))
}
