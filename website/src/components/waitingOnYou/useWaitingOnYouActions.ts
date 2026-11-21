/** Answer and dismiss for pinned asks.
 *
 *  An answer is an ordinary chat send of the chosen label or the typed note, with
 *  a claim naming its ask. The gateway checks that claim when the message is
 *  written, so the server's slot push removes the card. A queued receipt only
 *  clears this surface's in-flight state; every surface derives queued-answer
 *  state from the shared slot queue until that entry drains or is cancelled. */
import { useCallback, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../../api/client'
import { ApiError } from '../../api/apiError'
import { sendTurn, mintSendId } from '../../chat-core/transport/sendTurn'
import type { WaitingOnYouAsk } from '../../types'
import { deleteWaitingDraftIfUnchanged, waitingDraftRevision } from './drafts'

export type WaitingChoice = { label: string } | { text: string }

export interface WaitingAskState {
  busy?: boolean
  /** Catalog key of the sentence saying why the last request did not land. */
  errorKey?: string
  /** The gateway's own refusal sentence, shown under `errorKey` when it gave one. */
  errorDetail?: string
  /** The answer was sent or queued, or the ask dismissed. */
  settled?: boolean
}

/** The message an answer sends: an action card answered with no note says
 *  what its button said. */
function choiceText(choice: WaitingChoice, emptyActionAnswer: string): string {
  return 'label' in choice ? choice.label : (choice.text.trim() || emptyActionAnswer)
}

export function useWaitingOnYouActions(slot: string | null) {
  const { t } = useTranslation()
  const emptyActionAnswer = t('components.waitingOnYou.done')
  const [states, setStates] = useState<Record<string, WaitingAskState>>({})
  // The one-request-at-a-time guard, read synchronously at the click: a double
  // click must not send twice before React has rendered the first.
  const live = useRef(new Map<string, WaitingAskState>())
  const patch = useCallback((askId: string, next: WaitingAskState) => {
    live.current.set(askId, { ...live.current.get(askId), ...next })
    setStates(Object.fromEntries(live.current))
  }, [])
  const claim = useCallback((ask: WaitingOnYouAsk): boolean => {
    const current = live.current.get(ask.ask_id)
    if (current?.busy || current?.settled) return false
    patch(ask.ask_id, { busy: true, errorKey: undefined, errorDetail: undefined })
    return true
  }, [patch])

  const answer = useCallback((ask: WaitingOnYouAsk, choice: WaitingChoice) => {
    if (!slot || !claim(ask)) return
    const submittedDraft = waitingDraftRevision(slot, ask.ask_id)
    // No `steer`: a busy agent queues the answer, so the turn it opens is the one
    // told which ask it answers.
    void sendTurn({
      message: choiceText(choice, emptyActionAnswer),
      slot,
      meta: { sendId: mintSendId(), waitingOnYouAnswer: { key: ask.key, ask_id: ask.ask_id } },
    }).then(receipt => {
      if (receipt.status === 'queued') {
        deleteWaitingDraftIfUnchanged(slot, ask.ask_id, submittedDraft)
        patch(ask.ask_id, { busy: false })
        return
      }
      if (receipt.status === 'dispatched') {
        deleteWaitingDraftIfUnchanged(slot, ask.ask_id, submittedDraft)
        patch(ask.ask_id, { busy: false, settled: true })
        return
      }
      if (receipt.status === 'refused') {
        patch(ask.ask_id, { busy: false, errorKey: 'components.waitingOnYou.answer_refused', errorDetail: receipt.reason || undefined })
        return
      }
      patch(ask.ask_id, { busy: false, errorKey: 'components.waitingOnYou.answer_failed' })
    })
  }, [slot, patch, claim, emptyActionAnswer])

  const dismiss = useCallback((ask: WaitingOnYouAsk) => {
    if (!slot || !claim(ask)) return
    const submittedDraft = waitingDraftRevision(slot, ask.ask_id)
    api.dismissWaitingOnYou(slot, ask.key, ask.ask_id).then(() => {
      deleteWaitingDraftIfUnchanged(slot, ask.ask_id, submittedDraft)
      patch(ask.ask_id, { busy: false, settled: true })
    }).catch((err: unknown) => {
      // 409: the agent already replaced or withdrew it, so there is nothing left to dismiss.
      if (err instanceof ApiError && err.status === 409) {
        patch(ask.ask_id, { busy: false, settled: true })
        return
      }
      patch(ask.ask_id, { busy: false, errorKey: 'components.waitingOnYou.dismiss_failed' })
    })
  }, [slot, patch, claim])

  return { states, answer, dismiss }
}
