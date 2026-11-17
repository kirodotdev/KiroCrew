import { useState, type ReactNode } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Loader2, Timer, X } from 'lucide-react'

import { api } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import { fmtDateTime } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { slotRowByKey } from '../../store/slotIndex'
import { useAppSelector } from '../../store'

const ACTION_BTN =
  'inline-flex items-center gap-2 text-[12px] leading-5 font-medium text-balance px-3 py-1 rounded-md border-none cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed transition-colors'

/**
 * The action row of a usage-limit card that names when the limit resets: the
 * card's own Resume (`children`, when it has one) beside one button that asks the
 * gateway to continue this chat once just after the reset
 * (`POST /api/chat/slots/{slot}/limit-continue`). While that wait is armed the
 * button becomes Cancel and a line says so. At most two buttons share the row.
 *
 * Whether a wait is armed is the gateway's to say: its slot summary carries
 * `limit_continue_at` while one is held, so a reloaded card and a second tab agree.
 * A press is shown at once and yields to the summary the moment it changes. The
 * offer names the reset time; once armed, the line names the instant the gateway
 * reports (the reset plus its margin), from the summary or from the press's answer.
 * A message typed in the chat makes this card no longer the newest error, which ends
 * the wait and removes the row.
 *
 * A Cancel the gateway answers with `started` came after the continue was queued:
 * the row then says the continue already started and offers nothing.
 *
 * With `offer` false (the reset has passed, or the chat cannot be resumed right
 * now) the row offers no new wait: it renders only while the gateway still holds
 * one, so a wait that exists can always be cancelled, and nothing otherwise.
 */
export function LimitContinueRow({ slot, resetAt, time, offer = true, children }: {
  slot: string
  /** The row's own `reset_at` string, sent back unchanged so no precision is lost. */
  resetAt: string
  /** The reset instant in the reader's locale and zone. */
  time: string
  offer?: boolean
  children?: ReactNode
}) {
  const heldAt = useAppSelector((s) => slotRowByKey(s.dashboard.slots, slot)?.limit_continue_at)
  const heldByGateway = Boolean(heldAt)
  const [pressed, setPressed] = useState<{ on: boolean; seen: boolean; at?: string } | null>(null)
  // Cancel came too late: the gateway had already queued the continue.
  const [started, setStarted] = useState(false)
  // The gateway spoke after the press: its word replaces the press for good.
  if (pressed && pressed.seen !== heldByGateway) setPressed(null)
  const armed = pressed ? pressed.on : heldByGateway
  const armedIso = pressed ? pressed.at : heldAt
  const armedMs = armedIso ? Date.parse(armedIso) : NaN
  const armedTime = Number.isFinite(armedMs) ? fmtDateTime(armedMs) : time
  const toggle = useMutation({
    mutationFn: async (on: boolean) => {
      if (!on) return { started: Boolean((await api.cancelLimitContinue(slot)).started) }
      const result = await api.armLimitContinue(slot, resetAt)
      if (!result.ok) throw new Error(result.warning ?? 'not armed')
      return { started: false, at: result.continue_at }
    },
    onSuccess: (answer, on) => {
      setStarted(answer.started)
      setPressed({ on, seen: heldByGateway, at: answer.at })
    },
  })
  if (started) {
    return (
      <div className="text-[12px] leading-5 text-muted" role="status" data-testid="error-card-limit-continue-started">
        {i18nT('pages.chat.errorCard.limit_continue_started')}
      </div>
    )
  }
  if (!offer && !armed) return null
  const controls = (
    <>
      {armed && (
        <div className="text-[12px] leading-5 text-muted" data-testid="error-card-limit-continue-armed">
          {i18nT('pages.chat.errorCard.limit_continue_armed', { time: armedTime })}
        </div>
      )}
      <div className="flex flex-wrap items-center gap-2">
        {children}
        <button
          type="button"
          onClick={() => toggle.mutate(!armed)}
          disabled={toggle.isPending}
          aria-pressed={armed}
          className={`${ACTION_BTN} bg-transparent text-text ring-1 ring-inset ring-border hover:bg-bg-elevated`}
          data-testid="error-card-limit-continue"
        >
          {toggle.isPending
            ? <Loader2 size={12} className="lucide-inline shrink-0 animate-spin" aria-hidden="true" />
            : armed
              ? <X size={12} className="lucide-inline shrink-0" aria-hidden="true" />
              : <Timer size={12} className="lucide-inline shrink-0" aria-hidden="true" />}
          {armed ? i18nT('pages.chat.errorCard.limit_continue_cancel') : i18nT('pages.chat.errorCard.limit_continue', { time })}
        </button>
      </div>
      {toggle.isError && (
        <ErrorNotice
          message={i18nT('pages.chat.errorCard.limit_continue_failed')}
          variant="inline"
          noHandoffReason="This card exists because the chat's agent has no allowance left until the limit resets; an Ask-the-agent chat would be refused the same way."
          testId="error-card-limit-continue-failed"
        />
      )}
    </>
  )
  return offer ? controls : <div className="mt-2 flex flex-col gap-2">{controls}</div>
}
