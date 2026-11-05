/** The composer's pending tool-approval bar: the request's label (or, while its
 *  inline pill is out of view, a ghost mirror with the tool's details), Allow
 *  once / Trust / Reject (or a security-rule override's scopes), and the hint
 *  that points at the approval-mode picker.
 *
 *  State and decisions are `useToolApproval`'s; this module only draws them, so
 *  every surface that answers a slot's tool approval (the composer, and an
 *  app-drawn session pane, which has no composer) shows the same controls and
 *  submits through the same route. */
import { motion, AnimatePresence } from 'framer-motion'
import { CheckCircle, Clock, Lock, Target } from 'lucide-react'
import RejectDropdown from '../RejectDropdown'
import DenyOverrideDropdown, { DenyOverrideWarning } from '../DenyOverrideDropdown'
import TrustDropdown from '../TrustDropdown'
import { APPROVAL_MODE_ADJUSTED_LS_KEY } from '../ApprovalModePicker'
import { ToolDetails } from '../../pages/chat/ToolDetails'
import { safeSetItem } from '../../utils/safeStorage'
import { fmtDateFields } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { approvalBtnClass, type useToolApproval } from './approval'
import { RaisedBy } from './TypingLockBanner'
import { toolRailColor } from '../../utils/toolRailColor'

/** The ghost's detail rail: the colour a tool row awaiting approval wears. */
const PENDING_RAIL = { isRejected: false, isAutoDenied: false, isDone: false, hasPendingPerm: true }

/** Everything `useToolApproval` returns: the bar's whole input. */
export type ToolApprovalState = ReturnType<typeof useToolApproval>

export function ToolApprovalBar({ approval, approvalMode }: {
  approval: ToolApprovalState
  /** The slot's approval mode. Absent where no mode picker is mounted, which
   *  withholds the hint that opens it. */
  approvalMode?: string
}) {
  const {
    pendingApproval, approvalId, approvalSubmitting, setApprovalPickerSignal,
    approvalModeAdjusted, approvalNudgeActive,
    approvalToolInput, approvalIsReadOnly, approvalFullCommand, approvalBaseCommand, approvalIsShell,
    approvalTrustCommandGrantable, approvalTrustBaseGrantable, approvalTrustAllGrantable, approvalIsUnattended, approvalTrustGrantable,
    approvalDenyRule, approvalDenyDetail, approvalDenyFamily, approvalDenyCommand,
    approvalLabelRaw, approvalToolCallId, approvalPurpose, approvalTs, approvalLabel, approvalRaisedBy, showGhost, showInChat, handleApprovalAction,
  } = approval
  return (
    <AnimatePresence>
      {pendingApproval && approvalId && (
        <motion.div
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: 8 }}
          transition={{ type: 'spring', damping: 25, stiffness: 300, mass: 0.8 }}
        >
        <div className={`bg-[color-mix(in_srgb,var(--warn)_12%,transparent)] ${showGhost ? 'rounded-2xl' : 'rounded-t-2xl border-b border-[color:var(--glass-edge)]'} transition-[border-radius,border-color,border-width] duration-300 ease-[cubic-bezier(0.4,0,0.2,1)]`}>
            <AnimatePresence initial={false}>
                {showGhost && (
                    <motion.div
                        key="ghost"
                        initial={{ height: 0, opacity: 0, y: -6 }}
                        animate={{ height: 'auto', opacity: 1, y: 0 }}
                        exit={{ height: 0, opacity: 0, y: -6 }}
                        transition={{ type: 'spring', damping: 24, stiffness: 280, mass: 0.7 }}
                        style={{ overflow: 'hidden' }}
                    >
                        <div className="px-3.5 pt-2.5 pb-1">
                            <div className="inline-flex items-start gap-1 text-[13px] font-mono px-2 py-0.5">
                                <Lock size={12} className="text-warn shrink-0" style={{ marginTop: '3px' }} />
                                <span className="text-muted break-words min-w-0 line-clamp-2">{approvalLabel}</span><RaisedBy name={approvalRaisedBy} />
                            </div>
                            <ToolDetails
                                purpose={approvalPurpose}
                                pillLabel={approvalLabel}
                                toolName={approvalLabelRaw}
                                input={approvalToolInput}
                                output=""
                                auto={false}
                                pending={true}
                                ts={approvalTs}
                                hasEntry={!!approvalToolInput}
                                fmtTime={t => t ? fmtDateFields(t, { hour: '2-digit', minute: '2-digit' }) : ''}
                                barColor={toolRailColor(PENDING_RAIL)}
                                layoutId={`ghost-tool-detail-${approvalToolCallId || approvalId}`}
                                compact
                            />
                        </div>
                        <div className="mx-3.5 h-px bg-[color-mix(in_srgb,var(--warn)_25%,transparent)]" />
                    </motion.div>
                )}
            </AnimatePresence>
            <div className="flex items-center gap-1.5 px-3.5 py-2.5 select-none flex-wrap">
                {!showGhost && <>
                    <Lock size={12} className="text-warn shrink-0" />
                    <span className="text-[13px] font-mono text-muted truncate flex-1 min-w-0">{approvalLabel}</span><RaisedBy name={approvalRaisedBy} />
                </>}
                {showGhost && <div className="flex-1 min-w-0" />}
                {showGhost && approvalToolCallId && (
                    <button
                        type="button"
                        onClick={showInChat}
                        title={i18nT('components.chatInput.show_pending_tool_call_in_chat')}
                        aria-label={i18nT('components.chatInput.show_pending_tool_call_in_chat')}
                        className="inline-flex items-center gap-1 px-2 py-1 rounded-md bg-transparent border border-border text-muted text-[11px] cursor-pointer hover:text-text hover:border-border-strong hover:bg-bg-hover transition-colors"
                    >
                        <Target size={11} className="shrink-0" />
                        {i18nT('components.chatInput.show_in_chat')}
                    </button>
                )}
                {/* `data-approval-actions` is the probe `queryPendingApprovalAction`
                    resolves — a keyboard chord lands on this row's first enabled
                    control. A `data-` hook rather than button text because every
                    catalog translates the labels. */}
                <div data-approval-actions className="flex gap-1.5 flex-wrap items-center">
                    {approvalDenyRule ? <DenyOverrideDropdown disabled={approvalSubmitting} className={approvalBtnClass} family={approvalDenyFamily} onAction={(action) => { handleApprovalAction(action) }} /> : <>
                    <button disabled={approvalSubmitting} className={approvalBtnClass} onClick={() => handleApprovalAction('approved')}><CheckCircle size={12} className="shrink-0" />{i18nT('components.chatInput.allow_once')}</button>
                    {/* One dropdown carries every standing grant (trust-reads is a tier in it):
                        `max-two-buttons-per-row` grandfathers Allow once + Trust + Reject; no fourth. */}
                    {approvalTrustGrantable && (approvalTrustCommandGrantable || approvalTrustAllGrantable || approvalIsReadOnly) && (
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
                          onAction={(action, pattern, ttlSeconds) => { handleApprovalAction(action, pattern, ttlSeconds) }}
                      />
                    )}
                    </>}
                    <RejectDropdown
                        disabled={approvalSubmitting}
                        className={`${approvalBtnClass} hover:!text-danger hover:!bg-[color-mix(in_srgb,var(--danger)_10%,transparent)]`}
                        onAction={(action) => { handleApprovalAction(action) }}
                    />
                </div>
            </div>
            {approvalDenyRule && <DenyOverrideWarning rule={approvalDenyRule} family={approvalDenyFamily} detail={approvalDenyDetail} command={approvalDenyCommand} />}
            {/* A1 hint: points at the footer mode picker. Withheld for unattended
                sources (the picker governs THIS slot), the ghost state (picker
                unmounted), an override card, the B2 nudge, and once the user has
                found the picker (this link or a mode change). */}
            {!showGhost && !approvalIsUnattended && !approvalDenyRule && !approvalModeAdjusted && !approvalNudgeActive && approvalMode && (
              <div className="flex items-center gap-1.5 flex-wrap px-3.5 pb-2 -mt-1 text-[12px] text-muted select-none">
                <span>{i18nT('components.chatInput.approval_hint_question')}</span>
                <button
                  type="button"
                  className="inline-flex items-center gap-0.5 p-0 bg-transparent border-none text-accent text-[12px] cursor-pointer hover:underline"
                  onClick={() => {
                    // Discovery achieved: the picker is about to open under a
                    // spotlight, so the hint has done its job for good.
                    safeSetItem(APPROVAL_MODE_ADJUSTED_LS_KEY, '1')
                    setApprovalPickerSignal(n => n + 1)
                  }}
                >
                  {i18nT('components.chatInput.approval_hint_adjust')}
                </button>
              </div>
            )}
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}

/** How the last decision ended when nothing failed: the request expired, someone
 *  else settled it, or a timed grant landed (what it covers and until when). A
 *  failed submit is an error and is drawn by the host's own `ErrorNotice`. */
export function ToolApprovalStatusNotice({ approval }: { approval: ToolApprovalState }) {
  const { approvalNotice, approvalNoticeKind } = approval
  return (
    <>
      {approvalNotice && (approvalNoticeKind === 'status' || approvalNoticeKind === 'settled') && (
        <div
          role="status"
          className="flex items-center gap-2 px-4 py-2 mb-1 bg-[color-mix(in_srgb,var(--warn)_12%,transparent)] rounded-lg"
        >
          {approvalNoticeKind === 'settled' ? <CheckCircle size={12} className="text-ok shrink-0" /> : <Lock size={12} className="text-warn shrink-0" />}
          <span className="text-muted text-[13px]">{approvalNotice}</span>
        </div>
      )}
      {approvalNotice && approvalNoticeKind === 'granted' && (
        // A grant that landed, not a block: its own clock icon and ok tint,
        // so the confirmation never wears the warning lock above.
        <div role="status" className="flex items-center gap-2 px-4 py-2 mb-1 bg-ok-subtle rounded-lg">
          <Clock size={12} className="text-ok shrink-0" />
          <span className="text-muted text-[13px]">{approvalNotice}</span>
        </div>
      )}
    </>
  )
}
