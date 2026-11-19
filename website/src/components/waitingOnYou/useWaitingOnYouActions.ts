/** Answer and dismiss for pinned asks, with what each refusal means on screen.
 *
 *  The card is removed by the slot push, never here: the gateway retires an ask
 *  only once its answer's send is accepted, so a local removal on a click could
 *  hide an ask the gateway still holds. Until that push lands, a settled ask is
 *  hidden by its `ask_id`, which a re-declare replaces, so the replacement shows
 *  at once. */
import { useCallback, useRef, useState } from 'react'
import { api } from '../../api/client'
import { waitingRefusal, type WaitingStaleCode } from '../../lib/waitingOnYou'
import type { WaitingOnYouAsk } from '../../types'
import { answerIdFor, forgetAnswerIds } from './answerIds'
import { deleteWaitingDraft } from './drafts'

export type WaitingChoice = { label: string } | { text: string }

export interface WaitingAskState {
  busy?: boolean
  /** Catalog key of the sentence saying why the last request did not land. */
  errorKey?: string
  /** The gateway's own refusal sentence, shown under `errorKey` when it gave one. */
  errorDetail?: string
  /** An answer to this ask may already be in the chat (409 `uncertain`). */
  uncertain?: boolean
  /** The request settled: the ask is answered or dismissed here. */
  settled?: boolean
  /** The ask was already gone when the request landed, and why. */
  stale?: WaitingStaleCode
  /** The chosen label or typed note went back to the composer. */
  returned?: boolean
  /** The ask as it was, for the notice that outlives it. */
  ask?: WaitingOnYouAsk
}

/** The text an answer sends; what goes back to the composer when it cannot. */
function choiceText(choice: WaitingChoice): string {
  return 'label' in choice ? choice.label : choice.text
}

export function useWaitingOnYouActions(slot: string | null, onReturnToComposer?: (text: string) => void) {
  const [states, setStates] = useState<Record<string, WaitingAskState>>({})
  // The one-request-at-a-time guard, read synchronously at the click: a double
  // click must not mint two requests before React has rendered the first.
  const live = useRef(new Map<string, WaitingAskState>())
  const patch = useCallback((askId: string, next: WaitingAskState | null) => {
    if (next) live.current.set(askId, { ...live.current.get(askId), ...next })
    else live.current.delete(askId)
    setStates(Object.fromEntries(live.current))
  }, [])
  const claim = useCallback((ask: WaitingOnYouAsk): boolean => {
    const current = live.current.get(ask.ask_id)
    if (current?.busy || current?.settled) return false
    patch(ask.ask_id, { busy: true, errorKey: undefined, errorDetail: undefined, ask })
    return true
  }, [patch])

  const answer = useCallback((ask: WaitingOnYouAsk, choice: WaitingChoice) => {
    if (!slot) return
    const choiceKey = 'label' in choice ? `label:${choice.label}` : `text:${choice.text}`
    const answerId = answerIdFor(ask.ask_id, choiceKey)
    if (!claim(ask)) return
    const body = 'label' in choice
      ? { ask_id: ask.ask_id, answer_id: answerId, label: choice.label }
      : { ask_id: ask.ask_id, answer_id: answerId, text: choice.text }
    api.answerWaitingOnYou(slot, ask.key, body).then(() => {
      forgetAnswerIds(ask.ask_id)
      deleteWaitingDraft(slot, ask.ask_id)
      patch(ask.ask_id, { busy: false, settled: true })
    }).catch((err: unknown) => {
      const refusal = waitingRefusal(err)
      if (refusal.kind === 'stale') {
        forgetAnswerIds(ask.ask_id)
        deleteWaitingDraft(slot, ask.ask_id)
        // Answered elsewhere: that answer stands, so this one is dropped. The
        // agent replaced or withdrew it: the person decides whether this answer
        // still goes, so it waits in the composer and nothing is sent.
        const text = choiceText(choice).trim()
        const returned = refusal.code !== 'answered' && !!text && !!onReturnToComposer
        if (returned) onReturnToComposer!(text)
        patch(ask.ask_id, { busy: false, settled: true, stale: refusal.code, returned })
        return
      }
      if (refusal.kind === 'uncertain') {
        forgetAnswerIds(ask.ask_id)
        patch(ask.ask_id, { busy: false, uncertain: true })
        return
      }
      if (refusal.kind === 'invalid') {
        patch(ask.ask_id, { busy: false, errorKey: 'components.waitingOnYou.answer_invalid' })
        return
      }
      if (refusal.kind === 'refused') {
        patch(ask.ask_id, { busy: false, errorKey: 'components.waitingOnYou.answer_refused', errorDetail: refusal.reason || undefined })
        return
      }
      patch(ask.ask_id, { busy: false, errorKey: 'components.waitingOnYou.answer_failed' })
    })
  }, [slot, patch, claim, onReturnToComposer])

  const dismiss = useCallback((ask: WaitingOnYouAsk) => {
    if (!slot) return
    if (!claim(ask)) return
    api.dismissWaitingOnYou(slot, ask.key, ask.ask_id).then(() => {
      forgetAnswerIds(ask.ask_id)
      deleteWaitingDraft(slot, ask.ask_id)
      patch(ask.ask_id, { busy: false, settled: true })
    }).catch((err: unknown) => {
      const refusal = waitingRefusal(err)
      if (refusal.kind === 'stale') {
        forgetAnswerIds(ask.ask_id)
        deleteWaitingDraft(slot, ask.ask_id)
        patch(ask.ask_id, { busy: false, settled: true, stale: refusal.code })
        return
      }
      patch(ask.ask_id, { busy: false, errorKey: 'components.waitingOnYou.dismiss_failed' })
    })
  }, [slot, patch, claim])

  /** Forget what this surface knows about one ask: its notice was closed. */
  const forget = useCallback((askId: string) => patch(askId, null), [patch])

  return { states, answer, dismiss, forget }
}
