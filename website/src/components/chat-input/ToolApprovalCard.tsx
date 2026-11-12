import { useId, useMemo, useRef, type ReactNode } from 'react'
import { motion, useReducedMotion } from 'framer-motion'
import { Plug, Target, Terminal } from 'lucide-react'
import DenyOverrideDropdown, { DenyOverrideWarning } from '../DenyOverrideDropdown'
import RejectDropdown from '../RejectDropdown'
import TrustDropdown, { TrustUnavailable } from '../TrustDropdown'
import { APPROVAL_MODE_ADJUSTED_LS_KEY } from '../ApprovalModePicker'
import { FormattedPayloadCard } from '../../pages/chat/ToolDetails'
import { fmtDateFields } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { safeSetItem } from '../../utils/safeStorage'
import { approvalBtnClass, approvalPrimaryBtnClass, type useToolApproval } from './approval'
import { RaisedBy } from './TypingLockBanner'

/* The pending tool approval as a card above the composer, and the one-line
   receipt that replaces it once a decision made here lands. `useToolApproval`
   (./approval) owns the state and the decision; this file only draws it. */

type ApprovalState = 'waiting' | 'approved' | 'rejected'

const STATUS_LABEL_KEY: Record<ApprovalState, string> = {
  // The same words, for the same state, as the transcript's own tool row.
  waiting: 'pages.chat.toolDetails.waiting_for_approval',
  approved: 'components.chatInput.approval_status_approved',
  rejected: 'components.chatInput.approval_status_rejected',
}

const STATUS_CLASS: Record<ApprovalState, string> = {
  waiting: 'bg-[color-mix(in_srgb,var(--warn)_22%,transparent)] text-warn-text',
  approved: 'bg-ok-subtle text-ok-text',
  rejected: 'bg-[color-mix(in_srgb,var(--text)_10%,transparent)] text-muted',
}

/** Where the request stands, in words. The dot breathes only while a decision
 *  is still owed, and holds still for a reader who asked for less motion. */
export function ApprovalStatusPill({ state }: { state: ApprovalState }) {
  const still = useReducedMotion() || state !== 'waiting'
  return (
    <span className={`inline-flex shrink-0 items-center gap-1.5 rounded-full px-2.5 py-0.5 text-[12px] leading-4 font-body font-medium whitespace-nowrap ${STATUS_CLASS[state]}`}>
      <motion.span
        aria-hidden
        className="size-1.5 rounded-full bg-current"
        animate={still ? { opacity: 1 } : { opacity: [1, 0.3, 1] }}
        transition={still ? { duration: 0 } : { duration: 1.6, repeat: Infinity, ease: 'easeInOut' }}
      />
      {i18nT(STATUS_LABEL_KEY[state])}
    </span>
  )
}

/** What a decision made in this tab did: the verdict as the card's own pill,
 *  then one line on what was granted and when it ends. */
export function ApprovalReceipt({ kind, children }: { kind: 'granted' | 'rejected'; children: ReactNode }) {
  return (
    <div role="status" data-testid="approval-receipt" className="flex items-center gap-2 flex-wrap px-4 py-2 mb-1 rounded-lg bg-[color-mix(in_srgb,var(--text)_5%,transparent)]">
      <ApprovalStatusPill state={kind === 'granted' ? 'approved' : 'rejected'} />
      <span className="min-w-0 text-muted text-[13px]">{children}</span>
    </div>
  )
}

const CHIP_CLASS = 'inline-flex items-center gap-1 px-2 py-0.5 rounded-md border border-border bg-bg-elevated text-[11px] leading-4'

export function ToolApprovalCard({ approval, activeSlot, approvalMode }: {
  approval: ReturnType<typeof useToolApproval>
  activeSlot: string | null
  approvalMode?: string
}) {
  const {
    approvalId, approvalMid, approvalSubmitting, setApprovalPickerSignal, approvalModeAdjusted, approvalNudgeActive,
    approvalToolInput, approvalIsReadOnly, approvalFullCommand, approvalBaseCommand, approvalIsShell, approvalTakesReason,
    approvalTrustCommandGrantable, approvalTrustBaseGrantable, approvalTrustAllGrantable, approvalIsUnattended, approvalTrustGrantable,
    approvalDenyRule, approvalDenyDetail, approvalDenyFamily, approvalDenyCommand,
    approvalToolCallId, approvalTs, approvalTitle, approvalMcpServer, approvalRaisedBy, showGhost, showInChat, handleApprovalAction, approvalActionsRef,
  } = approval
  const commandRef = useRef<HTMLPreElement>(null)
  const commandId = useId()

  const kindLabel = approvalIsShell
    ? i18nT('components.chatInput.approval_kind_shell')
    : approvalMcpServer ? i18nT('components.chatInput.approval_kind_mcp', { server: approvalMcpServer }) : ''
  const KindIcon = approvalIsShell ? Terminal : Plug
  const canShowInChat = showGhost && !!approvalToolCallId
  // An override card states the refused command in its own warning below.
  const command = approvalDenyRule ? '' : approvalFullCommand
  const commandKey = i18nT(approvalIsShell ? 'components.chatInput.approval_command_key' : 'components.chatInput.approval_call_key')
  // A shell call's command is already in the command box, so it is not printed
  // again; whatever else the call carries (a working directory, a timeout) is
  // still shown, since it changes what the command does. Any other input (a
  // tool's arguments, a command the gateway could not give a grantable form)
  // is shown as the transcript shows it.
  const payload = useMemo(() => {
    if (!approvalIsShell || !command) return approvalToolInput
    try {
      const parsed: unknown = JSON.parse(approvalToolInput)
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return ''
      // Only the entry that IS the command is dropped; a string that differs
      // from it in any way stays on the card.
      const rest = Object.entries(parsed).filter(([, value]) => !(typeof value === 'string' && value.trim() === command.trim()))
      return rest.length > 0 ? JSON.stringify(Object.fromEntries(rest)) : ''
    } catch {
      // Not an object the card can split: the command box stands alone.
      return ''
    }
  }, [approvalIsShell, command, approvalToolInput])

  return (
    <div className={`bg-[color-mix(in_srgb,var(--warn)_12%,transparent)] ${showGhost ? 'rounded-2xl' : 'rounded-t-2xl border-b border-[color:var(--glass-edge)]'} transition-[border-radius,border-color,border-width] duration-300 ease-[cubic-bezier(0.4,0,0.2,1)]`}>
      <div className="flex items-start gap-2 px-3.5 pt-2.5">
        <ApprovalStatusPill state="waiting" />
        {/* Prose, so the body face: the command itself is below, in the one
            place it is set in mono. */}
        <span data-testid="approval-title" className="flex-1 min-w-0 pt-px text-[13px] leading-5 font-body font-medium text-text break-words line-clamp-2">{approvalTitle}</span>
        <RaisedBy name={approvalRaisedBy} />
      </div>
      <div className="px-3.5">
        <div className="mt-2 mb-2.5 ml-1.5 border-l-2 pl-3 flex flex-col gap-2" style={{ borderLeftColor: 'color-mix(in srgb, var(--warn) 70%, transparent)' }}>
          {(kindLabel || approvalIsReadOnly || approvalTs > 0 || canShowInChat) && (
            <div className="flex items-center gap-2 flex-wrap select-none">
              {kindLabel && (
                <span data-testid="approval-kind" className={`${CHIP_CLASS} font-body font-medium text-text`}>
                  <KindIcon size={11} className="shrink-0 text-muted" />{kindLabel}
                </span>
              )}
              {approvalIsReadOnly && (
                <span data-testid="approval-read-only" className="inline-flex items-center px-2 py-0.5 rounded-md border border-[color-mix(in_srgb,var(--ok)_30%,transparent)] bg-ok-subtle text-ok-text text-[11px] leading-4 font-body font-medium">
                  {i18nT('components.chatInput.approval_read_only')}
                </span>
              )}
              {approvalTs > 0 && <span className={`${CHIP_CLASS} font-mono text-muted`}>{fmtDateFields(approvalTs, { hour: '2-digit', minute: '2-digit' })}</span>}
              {/* Only while the transcript's own row for this call is out of
                  view. Here and not in the action row, which holds its three. */}
              {canShowInChat && (
                <button
                  type="button"
                  onClick={showInChat}
                  title={i18nT('components.chatInput.show_pending_tool_call_in_chat')}
                  aria-label={i18nT('components.chatInput.show_pending_tool_call_in_chat')}
                  className="ml-auto inline-flex items-center gap-1 px-2 py-0.5 rounded-md bg-transparent border border-border text-muted text-[11px] leading-4 cursor-pointer hover:text-text hover:border-border-strong hover:bg-bg-hover transition-colors"
                >
                  <Target size={11} className="shrink-0" />
                  {i18nT('components.chatInput.show_in_chat')}
                </button>
              )}
            </div>
          )}
          {command && (
            <div className="flex gap-3 rounded-md border border-border bg-bg-elevated px-2.5 py-2 text-[12px] leading-5 font-mono">
              <span className="shrink-0 select-none" style={{ color: 'var(--json-key)' }}>{commandKey}</span>
              {/* THE WHOLE COMMAND, AS SENT. The Trust menu's exact-command row
                  grants an exact-STRING match and names no command itself, so
                  this is where the string being consented to is read, and the
                  menu lifts itself clear of this element (`keepClear`). Hence
                  no shortening and no highlighter between the string and the
                  reader; `whitespace-pre-wrap` because collapsing a run of
                  spaces shows a different command than the one granted, and
                  `break-all` so an unspaced run (a sha, a base64 argument)
                  wraps rather than widening the card. Bounded and scrollable,
                  as the override card's command is: a pathological command must
                  not push the buttons out of the pane. */}
              {/* eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- keyboard users scroll the exact command. */}
              <pre ref={commandRef} id={commandId} tabIndex={0} role="region" aria-label={commandKey} data-testid="approval-command" className="m-0 flex-1 min-w-0 max-h-[30vh] overflow-y-auto whitespace-pre-wrap break-all font-mono text-text">{command}</pre>
            </div>
          )}
          {payload && (
            <div className="max-h-[160px] overflow-y-auto [&>div]:my-0" data-testid="approval-input">
              <FormattedPayloadCard text={payload}>
                <pre className="m-0 px-2 py-2 rounded-md border border-border bg-bg-elevated text-[12px] leading-5 font-mono whitespace-pre-wrap break-all">{payload}</pre>
              </FormattedPayloadCard>
            </div>
          )}
        </div>
      </div>
      {/* `data-approval-actions` is the probe `queryPendingApprovalAction`
          resolves, and the reject chord's event arrives on the row. A `data-`
          hook rather than button text because every catalog translates the labels. */}
      <div ref={approvalActionsRef} data-approval-actions className="flex gap-1.5 flex-wrap items-center justify-end px-3.5 pb-2.5 select-none">
        {approvalDenyRule ? (
          <DenyOverrideDropdown disabled={approvalSubmitting} className={approvalBtnClass} family={approvalDenyFamily} onAction={(action) => { handleApprovalAction(action) }} />
        ) : null}
        {/* Keyed per card row: a typed reason never reaches another card. */}
        <RejectDropdown
          key={`${activeSlot}:${approvalId}:${approvalMid ?? ''}`}
          disabled={approvalSubmitting} takesReason={approvalTakesReason}
          className={`${approvalBtnClass} hover:!text-danger hover:!bg-[color-mix(in_srgb,var(--danger)_10%,transparent)]`}
          onAction={(action, reason) => { handleApprovalAction(action, undefined, undefined, reason) }}
        />
        {!approvalDenyRule && <>
          {/* One dropdown carries every standing grant (trust-reads is a tier in it), and a card with none
              keeps its place disabled: `max-two-buttons-per-row` grandfathers Reject + Trust + Allow once; no fourth. */}
          {approvalTrustGrantable && (approvalTrustCommandGrantable || approvalTrustAllGrantable || approvalIsReadOnly) ? (
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
              commandNoun={approvalIsShell ? 'command' : 'call'}
              keepClear={() => commandRef.current}
              commandDescribedBy={commandId}
              onAction={(action, pattern, ttlSeconds) => { handleApprovalAction(action, pattern, ttlSeconds) }}
            />
          ) : <TrustUnavailable className={approvalBtnClass} />}
          {/* The one filled control, last in the row and so nearest the
              composer. `data-approval-primary` is where the focus-approval
              chord lands; a trust grant is never one keystroke from it. */}
          <button data-approval-primary disabled={approvalSubmitting} className={approvalPrimaryBtnClass} onClick={() => handleApprovalAction('approved')}>{i18nT('components.chatInput.allow_once')}</button>
        </>}
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
  )
}
