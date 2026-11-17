import { memo, useState } from 'react'
import { RotateCcw } from 'lucide-react'

import ErrorNotice from '../../components/ErrorNotice'
import { guideTrustRoot } from '../../guide/trustRoot'
import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import { useAppDispatch, useAppSelector } from '../../store'
import { changeApprovalMode } from '../../store/dashboardSlice'
import { slotApprovalMode } from '../../utils/slotApprovalMode'

export type LostMode = 'trust' | 'trust_reads'

/** Whether the chat's live mode already auto-approves what *lost* did. */
function covers(live: string, lost: LostMode): boolean {
  return live === 'yolo' || live === 'trust' || live === lost
}

/**
 * The chat row a restart leaves when it ended this chat's Trust or Reads
 * (`meta.kind === 'trust_reset_notice'`, written by
 * `src/kiro_crew/dashboard/chat_trust_restart.py`). The row's English `content`
 * is the fallback for readers that cannot localize; here the copy comes from the
 * catalog. The one button is the person picking the mode again: it dispatches the
 * same `changeApprovalMode` the composer's picker does, so the gateway's
 * `apply_approval_mode` re-raises this chat's verified workers exactly as a picker
 * pick would. Nothing here turns a mode on without that click.
 */
export default memo(function TrustResetNoticeCard({ slot: slotProp, mode }: {
  /** The row's slot; omitted on the single-chat surface, which reads the active one. */
  slot?: string
  mode: LostMode
}) {
  // This memo() boundary renders i18nT() strings, so a language switch must
  // invalidate it.
  useLanguageGeneration()
  const dispatch = useAppDispatch()
  const activeSlot = useAppSelector(s => s.chat.activeSlot)
  const slot = slotProp ?? activeSlot ?? ''
  const live = useAppSelector(s => slotApprovalMode(
    s.dashboard.approvalMode,
    s.dashboard.slots?.find(x => x.key === slot),
  ))
  const [busy, setBusy] = useState(false)
  // The rejection's own message: ErrorNotice looks the failed request's
  // structured context up in the error journal by it.
  const [failure, setFailure] = useState('')
  // The policy the composer picker hides denied modes by: offering a pick it
  // will refuse would only loop the person through the same 403.
  const blocked = useAppSelector(s => s.dashboard.status?.disabled_approval_modes?.includes(mode) ?? false)
  const [policyRefused, setPolicyRefused] = useState(false)
  const reads = mode === 'trust_reads'
  const on = covers(live, mode)
  const denied = blocked || policyRefused

  const pickAgain = () => {
    // No slot means no chat to scope the pick to; an unscoped pick would trust
    // every chat, so the button does nothing rather than that.
    if (!slot) return
    setBusy(true)
    setFailure('')
    void dispatch(changeApprovalMode({ mode, slot })).then(r => {
      setBusy(false)
      if (r.meta.requestStatus === 'rejected') {
        const payload = r.payload as { code?: string; message?: string } | undefined
        // A policy refusal never succeeds on retry: say so, as the picker does.
        if (payload?.code === 'mode_disabled_by_policy') setPolicyRefused(true)
        else setFailure(payload?.message || i18nT('pages.chat.trustResetNotice.failed'))
      }
    })
  }

  return (
    // The button raises this chat's own approval ceiling, so the guide must
    // never point at it (guide/trustRoot): only the person finds and presses it.
    <div
      {...guideTrustRoot}
      className="self-center w-full max-w-full min-w-0 rounded-md ring-1 ring-inset forced-colors:border ring-border bg-card text-muted animate-scale-in"
      data-testid="trust-reset-notice-card"
    >
      <div className="flex items-start gap-2 px-3 py-2 min-w-0 text-[13px] leading-5">
        <RotateCcw
          size={13}
          className="lucide-inline shrink-0 mt-[calc((1.25rem-1em)/2)]"
          aria-hidden="true"
        />
        <div className="min-w-0 flex flex-col gap-1.5">
          <span className="min-w-0 break-words" data-testid="trust-reset-notice-text">
            {on
              ? i18nT(reads ? 'pages.chat.trustResetNotice.reads_on' : 'pages.chat.trustResetNotice.trust_on')
              : i18nT(reads ? 'pages.chat.trustResetNotice.reads_off' : 'pages.chat.trustResetNotice.trust_off')}
          </span>
          {!on && blocked && !policyRefused && (
            // Known before any request: a status line, not a failure.
            <span className="min-w-0 break-words" data-testid="trust-reset-notice-policy">
              {i18nT('components.approvalModePicker.mode_disabled_by_policy')}
            </span>
          )}
          {!on && policyRefused && (
            // A refused request is a failure, so it goes through ErrorNotice,
            // as the composer picker renders the same 403.
            <ErrorNotice
              variant="inline"
              message={i18nT('components.approvalModePicker.mode_disabled_by_policy')}
              askAgent
              testId="trust-reset-notice-policy"
            />
          )}
          {!on && !denied && (
            // A transcript row holds no draft, so the hand-off loses nothing.
            <ErrorNotice
              variant="inline"
              title={i18nT('pages.chat.trustResetNotice.failed')}
              message={failure}
              askAgent
            />
          )}
          {!on && !denied && (
            <div>
              <button
                type="button"
                onClick={pickAgain}
                disabled={busy}
                data-testid="trust-reset-notice-again"
                className="pi-morph inline-flex items-center gap-1 text-[13px] leading-5 text-accent hover:text-accent-hover bg-transparent border-none cursor-pointer px-1.5 py-1 rounded hover:bg-accent/10 transition-colors disabled:opacity-50 disabled:cursor-default"
              >
                {i18nT(reads ? 'pages.chat.trustResetNotice.reads_again' : 'pages.chat.trustResetNotice.trust_again')}
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  )
})
