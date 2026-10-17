/**
 * The panel of a stubbed server the gateway refused to launch, shown in a
 * full-width sub-row by `McpManagement`. It names why the launch needs approval
 * and offers the Approve button; it does not list the launch command. The page
 * no longer shows command text anywhere -- the operator approves by name, and the
 * exact-launch compare-and-set on the write is what pins the approval to the
 * command the gateway resolved.
 */
import type { McpManagedServer } from '../../api/client'
import { Btn } from '../../components/ui'
import { i18nT } from '../../i18n/t'
import { type LaunchRefusal, launchIsApprovable, REFUSAL_REASON_KEY } from './mcpManagementModel'

/**
 * A stubbed server the gateway will not launch until its command is approved.
 *
 * One reason line and one Approve button. The command itself is not rendered:
 * the lists were long, clipped the card, and went unread, and the write still
 * carries `expected_launch` so approval covers the exact launch the gateway
 * resolved, whether or not it was ever on screen.
 */
export function RefusalPanel({
  server,
  refusal,
  busy,
  onApprove,
  approveRef,
}: {
  server: McpManagedServer
  refusal: LaunchRefusal
  busy: boolean
  onApprove: () => void
  /** The row's STUB switch hands the keyboard here, so the decision the operator
   *  came to make is the next thing they reach. */
  approveRef?: (node: HTMLButtonElement | null) => void
}) {
  const approvable = launchIsApprovable(refusal)
  // The "added outside the dashboard" reason uses the wording that does not point
  // at a command below it: no command is shown now, so the other spelling ("Check
  // the command below, then approve it") would send the operator to something that
  // is not there. The Approve button is the call to action instead.
  const reasonKey =
    refusal.reason === 'added_outside_dashboard'
      ? 'pages.mcpManagement.reapproval_reason_added_outside_dashboard_unapprovable'
      : REFUSAL_REASON_KEY[refusal.reason] ?? REFUSAL_REASON_KEY.changed_needs_reapproval
  return (
    <div className="space-y-1.5">
      <p className="text-[12.5px] leading-snug text-[var(--muted)]">{i18nT(reasonKey)}</p>
      {approvable ? (
        <div className="pt-1">
          <Btn
            type="button"
            ref={approveRef}
            disabled={busy}
            aria-label={i18nT('pages.mcpManagement.reapprove_aria', { name: server.name })}
            onClick={onApprove}
            className="px-2 py-0.5 text-[12px]"
          >
            {i18nT('pages.mcpManagement.reapprove_action', { name: server.name })}
          </Btn>
        </div>
      ) : (
        // No approvable identity means no button: without this line a changed-command
        // row is a dead end -- a reason with nothing to act on and no word of why.
        <p className="text-[12.5px] leading-snug text-[var(--muted)]">
          {i18nT('pages.mcpManagement.approval_not_approvable_here')}
        </p>
      )}
    </div>
  )
}
