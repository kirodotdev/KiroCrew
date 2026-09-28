import type { ChatSlot } from '../types'

export type SlotApprovalMode = 'yolo' | 'trust' | 'trust_reads' | 'normal'

/**
 * The approval mode a chat header shows for one slot.
 *
 * A slot is trusted by either the person's own "trust this session" flag or a
 * live app-armed scoped grant (`trust_scope`, blank once the grant lapses). Both
 * auto-approve the slot's tools, so both read as trust here.
 */
export function slotApprovalMode(
  approvalMode: string | undefined,
  slot: Pick<ChatSlot, 'trust' | 'trust_scope' | 'trust_reads'> | undefined,
): SlotApprovalMode {
  if (approvalMode === 'yolo') return 'yolo'
  if (slot?.trust || slot?.trust_scope) return 'trust'
  if (slot?.trust_reads) return 'trust_reads'
  return 'normal'
}

/**
 * True when an app-armed scoped grant, not the person's own flag, is what makes
 * the slot trusted. The picker names the grant in that state.
 */
export function slotTrustIsScoped(
  slot: Pick<ChatSlot, 'trust' | 'trust_scope'> | undefined,
): boolean {
  return !slot?.trust && !!slot?.trust_scope
}
