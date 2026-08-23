import { useEffect, useRef, useState } from 'react'
import { CheckCircle, Handshake, Ban, Package, Wrench } from 'lucide-react'
import ToolInputPreview from './ToolInputPreview'
import TrustDropdown from './TrustDropdown'
import ErrorNotice from './ErrorNotice'
import { ApiError } from '../api/client'

import { i18nT } from '../i18n/t'
export default function ApprovalCard({ title, toolInput, showButtons, showTrust = true, hasCommand = true, trustAllLabelKey, onApprove }: {
  title: string; toolInput: string; showButtons: boolean; showTrust?: boolean
  /** False when the approval has no tool command behind it (for example, an
      agent-role channel approval): forwarded to TrustDropdown so command-
      scoped tiers are not offered for that card. */
  hasCommand?: boolean
  // Passed through to TrustDropdown: a surface whose `trust` decision grants
  // more than the session (e.g. channel-wide, persisted) labels the real grant.
  trustAllLabelKey?: string
  /** MUST return the request's promise: it feeds the optimistic-state rollback
   *  below (rejection restores the buttons). No `void` arm, so a fire-and-forget
   *  handler — the shape behind #5524 — cannot compile here. */
  onApprove: (decision: string, pattern?: string) => Promise<unknown>
}) {
  const [decided, setDecided] = useState<string | null>(null)
  // null = no failure. `terminal` marks a refusal that retrying can never
  // clear; `message` is the server's own refusal text ('' = a response-less
  // transport failure, which renders the generic hedged copy — raw fetch
  // internals like "Failed to fetch" are not user vocabulary).
  const [failure, setFailure] = useState<{ terminal: boolean; message: string; attempted: string } | null>(null)
  const buttonsRef = useRef<HTMLDivElement | null>(null)
  // The decided state flips optimistically so the buttons collapse on click,
  // but a rejected decision request rolls it back: a card must never read
  // "Trusted"/"Approved" on a failed POST. An ApiError carries the server's
  // own verdict, so its copy asserts the decision was not recorded; a
  // response-less transport failure proves only that no response arrived, so
  // it hedges with "may not have been recorded". A refusal is TERMINAL only
  // when the approval itself is gone: 404 (channel/agent gone) or the
  // endpoint's own "no pending approval" 400 (expired / already decided) —
  // matched exactly because the dashboard ships with its gateway, and a copy
  // drift merely degrades to the retryable path. Other 400s (e.g. an action
  // the endpoint rejects) leave a LIVE approval the user can still decide
  // another way, and auth expiry (403) / transport failures are retryable,
  // so those roll back to the buttons.
  const handle = (d: string, pattern?: string) => {
    setFailure(null)
    setDecided(d)
    Promise.resolve(onApprove(d, pattern)).catch((err: unknown) => {
      setDecided(null)
      const refusal = err instanceof ApiError ? err : null
      const gone = !!refusal && !refusal.authRequired
        && (refusal.status === 404 || (refusal.status === 400 && refusal.message === 'no pending approval'))
      setFailure({
        terminal: gone,
        message: refusal && refusal.message ? refusal.message : '',
        attempted: d,
      })
    })
  }
  // The optimistic unmount dropped keyboard focus to <body>; when the buttons
  // return for a retryable failure, put focus back on the button matching the
  // decision that failed — never a different one, or a keyboard user retrying
  // a failed Reject with Enter would silently APPROVE the command instead.
  // Positional: Approve renders first and Reject last; a trust attempt lands
  // on the dropdown trigger between them.
  useEffect(() => {
    if (!failure || failure.terminal) return
    const buttons = Array.from(buttonsRef.current?.querySelectorAll('button') ?? [])
    if (!buttons.length) return
    const target = failure.attempted === 'approved' ? buttons[0]
      : failure.attempted === 'rejected' ? buttons[buttons.length - 1]
        : buttons.length > 2 ? buttons[1] : buttons[0]
    target.focus()
  }, [failure])
  const borderColor = decided === 'approved' || decided === 'trust' || decided === 'trust_command' || decided === 'trust_base' ? 'border-l-ok' : decided === 'rejected' || failure?.terminal ? 'border-l-danger' : 'border-l-warn'

  const isShell = title.startsWith('Running: ')
  const normalized = title.replace(/^(Running: |Reading )/, '')
  // Approvals without a command (including agent-role channel approvals) pass
  // `hasCommand=false`; do not derive dead command/base authority from a title.
  const baseCmd = hasCommand ? (normalized.split(/\s+/)[0] || normalized) : ''
  // The showButtons branch renders its own i18n "Running:" label, so a shell
  // title (which carries the "Running: " prefix) must be de-prefixed there to
  // avoid "Running: Running: …". The wrench branch renders no label, so the
  // raw title keeps its verb ("Running: " / "Reading ") for context.
  const displayTitle = showButtons && isShell ? normalized : title
  const btnClass = 'px-2.5 py-1 rounded-md border border-border bg-transparent text-muted text-[13px] cursor-pointer font-body hover:text-text hover:border-border-strong hover:bg-bg-hover transition-all'

  return (
    <div className={`bg-card border border-border border-l-[3px] ${borderColor} rounded-md px-3.5 py-2.5 text-sm animate-scale-in`}>
      {toolInput
        ? <><strong>{i18nT('components.approvalCard.tool_approval_requested')}</strong></>
        : <>{showButtons ? <><Package className="lucide-inline" /> {i18nT('components.approvalCard.running')} </> : <><Wrench className="lucide-inline" /> </>}<strong>{displayTitle}</strong>{showButtons ? ' wants to run' : ''}</>
      }
      {toolInput && <ToolInputPreview toolInput={toolInput} threshold={200} />}
      {showButtons && !decided && !failure?.terminal && (
        <div ref={buttonsRef} className="mt-1.5 flex gap-1.5 flex-wrap">
          <button className={btnClass} onClick={() => handle('approved')}><CheckCircle className="lucide-inline" /> {i18nT('components.approvalCard.approve')}</button>
          {showTrust && <TrustDropdown fullCommand={hasCommand ? normalized : ''} baseCommand={baseCmd} isShell={hasCommand && isShell} hasCommand={hasCommand} trustAllLabelKey={trustAllLabelKey} className={btnClass} onAction={(action, pattern) => handle(action, pattern)} />}
          <button className={btnClass + ' hover:!text-danger hover:!border-danger'} onClick={() => handle('rejected')}><Ban className="lucide-inline" /> {i18nT('components.approvalCard.reject')}</button>
        </div>
      )}
      {failure !== null && (
        <ErrorNotice variant="inline" className="mt-1.5" message={failure.terminal
          ? i18nT('components.approvalCard.approval_no_longer_pending')
          : failure.message
            ? i18nT('components.approvalCard.decision_not_recorded_error', { error: failure.message })
            : i18nT('components.approvalCard.decision_failed')} />
      )}
      {decided && (
        <div className="mt-1.5 text-[13px] text-muted">
          {decided === 'approved' && <><CheckCircle className="lucide-inline" /> {i18nT('components.approvalCard.approved')}</>}
          {(decided === 'trust' || decided === 'trust_command' || decided === 'trust_base') && <><Handshake className="lucide-inline" /> {i18nT('components.approvalCard.trusted_auto_approving_future_calls')}</>}
          {decided === 'rejected' && <><Ban className="lucide-inline" /> {i18nT('components.approvalCard.rejected')}</>}
        </div>
      )}
    </div>
  )
}
