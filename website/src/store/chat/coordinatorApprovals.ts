/** Permission rows raised by the coordinator approval registry.
 *
 *  A sub-agent's (or another background source's) tool approval is registered
 *  by `interaction_coordinator.ApprovalCoordinator.request` and reaches this tab
 *  as an `approval` frame, or through the reconnect reconcile of
 *  `GET /api/approvals`. Both write a permission row stamped
 *  `meta.registry: 'coordinator'` into the owning chat (`useWebSocket`). The
 *  server never writes that row into the slot's transcript, so the tab's copy is
 *  the only one: a transcript read cannot vouch for it either way, and the row
 *  stays pending until the registry retires it (`approval_resolved`, or a
 *  reconcile that no longer lists the id). */
import type { ChatMessage } from '../../types'

/** Rows that must stay below a kept card: the live or finished reply a chat
 *  switch re-attaches by reading the last row, and the queued bubbles that
 *  always sit last. */
const TRAILING_ROLES = new Set<ChatMessage['role']>(['assistant', 'streaming', 'queued'])

/** A coordinator-registry permission row nobody has decided yet. */
export function isPendingCoordinatorApproval(m: ChatMessage): boolean {
  const aid = m.meta?.approval_id
  return m.role === 'permission'
    && m.meta?.registry === 'coordinator'
    && !m.meta?.resolved
    && typeof aid === 'string' && aid !== ''
}

/** *next* with every pending coordinator row of *prior* that it does not carry.
 *
 *  For a reducer that rebuilds a chat from a server page: the page cannot hold
 *  these rows, so rebuilding from it alone drops a card that is still waiting
 *  on the person. A row is kept only when *next* has no coordinator row with its
 *  approval id, so a card is never seated twice. Kept rows go ahead of the
 *  trailing reply and queued bubbles: `switchSlot` reads the last row as the
 *  live reply it re-attaches, so a card there would hide that reply from it. */
export function keepPendingCoordinatorApprovals(prior: readonly ChatMessage[], next: ChatMessage[]): ChatMessage[] {
  // Only another coordinator row holds the id: a chat-runner request id is
  // connection-scoped and can match a coordinator id, yet it is a different
  // request, so it must not stand in for this card.
  const held = new Set<string>()
  for (const m of next) {
    const aid = m.meta?.approval_id
    if (m.role === 'permission' && m.meta?.registry === 'coordinator' && typeof aid === 'string') held.add(aid)
  }
  const kept: ChatMessage[] = []
  for (const m of prior) {
    if (!isPendingCoordinatorApproval(m)) continue
    const aid = m.meta!.approval_id as string
    if (held.has(aid)) continue
    held.add(aid)
    kept.push(m)
  }
  if (!kept.length) return next
  let at = next.length
  while (at > 0 && TRAILING_ROLES.has(next[at - 1].role)) at -= 1
  return [...next.slice(0, at), ...kept, ...next.slice(at)]
}
