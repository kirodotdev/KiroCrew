import { CheckCircle } from 'lucide-react'
import DenyOverrideDropdown from '../DenyOverrideDropdown'
import RejectDropdown from '../RejectDropdown'
import TrustDropdown, { TrustUnavailable } from '../TrustDropdown'
import { i18nT } from '../../i18n/t'
import { approvalBtnClass, type useToolApproval } from './approval'

/** The pending card's actions share one row and one keyboard target. */
export function ApprovalActions({ approval, slotId }: { approval: ReturnType<typeof useToolApproval>; slotId: string | null }) {
  const {
    approvalId, approvalMid, approvalSubmitting, approvalDenyRule, approvalDenyFamily,
    approvalTrustGrantable, approvalTrustCommandGrantable, approvalTrustAllGrantable,
    approvalIsReadOnly, approvalAlwaysAllowRef, approvalAlwaysAllowRefusal,
    approvalFullCommand, approvalBaseCommand, approvalIsShell, approvalTrustBaseGrantable,
    approvalTakesReason, handleApprovalAction, approvalActionsRef,
  } = approval
  return (
    // A keyboard chord targets this row's first enabled control; Reject arrives on the row.
    <div ref={approvalActionsRef} data-approval-actions className="flex gap-1.5 flex-wrap items-center">
      {approvalDenyRule ? <DenyOverrideDropdown disabled={approvalSubmitting} className={approvalBtnClass} family={approvalDenyFamily} onAction={action => { handleApprovalAction(action) }} /> : <>
        <button disabled={approvalSubmitting} className={approvalBtnClass} onClick={() => handleApprovalAction('approved')}><CheckCircle size={12} className="shrink-0" />{i18nT('components.chatInput.allow_once')}</button>
        {/* All standing grants stay in Trust: Allow once + Trust + Reject, never a fourth button. */}
        {approvalTrustGrantable && (approvalTrustCommandGrantable || approvalTrustAllGrantable || approvalIsReadOnly || !!approvalAlwaysAllowRef || !!approvalAlwaysAllowRefusal) ? (
          <TrustDropdown
            fullCommand={approvalFullCommand}
            baseCommand={approvalBaseCommand}
            isShell={approvalIsShell && approvalTrustBaseGrantable}
            hasCommand={approvalTrustCommandGrantable}
            trustReadsLabelKey={approvalIsReadOnly ? 'components.chatInput.trust_reads' : undefined}
            showTrustAll={approvalTrustAllGrantable || approvalTrustCommandGrantable}
            disabled={approvalSubmitting}
            className={approvalBtnClass}
            timedGrants
            alwaysAllowTool={approvalAlwaysAllowRef}
            alwaysAllowRefusal={approvalAlwaysAllowRefusal}
            onAction={(action, pattern, ttlSeconds) => { handleApprovalAction(action, pattern, ttlSeconds) }}
          />
        ) : <TrustUnavailable className={approvalBtnClass} />}
      </>}
      {/* Keyed per card row: a typed reason never reaches another card. */}
      <RejectDropdown
        key={`${slotId}:${approvalId}:${approvalMid ?? ''}`}
        disabled={approvalSubmitting} takesReason={approvalTakesReason}
        className={`${approvalBtnClass} hover:!text-danger hover:!bg-[color-mix(in_srgb,var(--danger)_10%,transparent)]`}
        onAction={(action, reason) => { handleApprovalAction(action, undefined, undefined, reason) }}
      />
    </div>
  )
}
