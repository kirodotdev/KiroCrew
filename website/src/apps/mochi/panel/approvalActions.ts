/**
 * Approval decisions → the two DIFFERENT gateway endpoints they need.
 *
 * This existed as a silent bug: the app POSTed "approved" / "rejected" / "trust" /
 * "yolo" to `/api/approvals/{id}/{action}`, but that route validates
 * `action in ("approve", "reject")` and 400s on anything else. So every approval
 * from the pet failed, and the failure was swallowed twice (no res.ok check plus a
 * bare catch) — the dialog closed, the pet said "approved", the agent stayed
 * blocked. Matching the dashboard's own mapping (website/src/components/ChatInput.tsx):
 *
 *   approve / reject          → POST /api/approvals/{id}/{approve|reject}
 *   trust, trust_reads, …     → POST /api/chat/slots/{slot}/approve
 *                               body: { action, request_id, pattern? }
 *
 * Trust is not an approval verb: it answers this request AND widens the slot's
 * standing policy, which only the slot endpoint can do.
 */
export type ApprovalUiAction = 'approve' | 'reject' | 'trust' | 'trust_reads' | string

/** Which endpoint a decision has to go to. */
export type ApprovalRoute =
  | { kind: 'approval'; action: 'approve' | 'reject' }
  | { kind: 'slot'; action: string }

const TRUST_ACTIONS = new Set(['trust', 'trust_reads', 'trust_command', 'trust_base'])

export function approvalRoute(uiAction: ApprovalUiAction): ApprovalRoute {
  if (TRUST_ACTIONS.has(uiAction)) return { kind: 'slot', action: uiAction }
  return { kind: 'approval', action: uiAction === 'reject' ? 'reject' : 'approve' }
}

/** True when the decision lets the tool run (everything except an explicit reject). */
export function isGrant(uiAction: ApprovalUiAction): boolean {
  return uiAction !== 'reject'
}

/** One-line label for the pending tool, matching the dashboard's truncation. */
export function approvalLabel(tool: string, toolInput?: string, max = 72): string {
  const parts = [tool, toolInput].map(v => String(v || '').replace(/\s+/g, ' ').trim()).filter(Boolean)
  const line = parts.join(' ')
  return line.length > max ? line.slice(0, max) + '…' : line
}
