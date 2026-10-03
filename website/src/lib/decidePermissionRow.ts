/** Decide the request a chat permission row names.
 *
 *  The transcript's collapsed tool groups decide their pending row through
 *  here. The decide is bound to the row's own target (types/approvalTarget):
 *  the id recurs and a chat runner's id can collide with a coordinator one, so
 *  a bare id could decide a request the card never showed. A row that names no
 *  request has nothing to decide and is refused the way a retired request is,
 *  with nothing sent.
 *
 *  It goes to the ONE-SHOT decide, which has no trust verb: the shared
 *  `toApiDecision` (utils/approvalDecision.ts) is fail-closed and is the only
 *  place that mapping is spelled, so a Trust affordance on this path would claim
 *  a standing grant the backend never records (#5400, #5434). */
import { api } from '../api/client'
import { noPendingApprovalError } from '../api/apiError'
import { resolveApprovalRow } from '../store/chatSlice'
import { approvalRowsFor, settleDecidedApproval } from '../store/notificationsSlice'
import type { RootState } from '../store'
import { permissionRowTarget } from '../types/approvalTarget'
import { toApiDecision } from '../utils/approvalDecision'

type Dispatch = (action: unknown) => unknown

export const decidePermissionRow = (meta: Record<string, unknown> | undefined, slot: string | null | undefined, action: string) =>
  async (dispatch: Dispatch, getState: () => unknown): Promise<void> => {
    const target = permissionRowTarget(meta, slot)
    if (!target) throw noPendingApprovalError()
    const a = toApiDecision(action)
    await api.decideApproval(target, a)
    // The decision that was sent: without it the reducer records `approved`,
    // which would overwrite a rejection the backend's frame already wrote.
    const decision = a === 'approve' ? 'approved' : a === 'reject_once' ? 'rejected_once' : 'rejected'
    dispatch(resolveApprovalRow({ target, decision }))
    if (target.origin !== 'coordinator') return
    // The decision landed: retire the request's feed row with its outcome, then
    // remove it once the server confirms (a failure keeps it listed and says so
    // there). Matched on the target whatever the row's retirement: the frame
    // for this very decision can retire the row before this response lands,
    // and the row must still be removed.
    const notifications = (getState() as RootState).notifications
    const n = approvalRowsFor(notifications, target).at(-1)
    if (n) void dispatch(settleDecidedApproval(n.ts, a === 'approve' ? 'approve' : 'reject'))
  }
