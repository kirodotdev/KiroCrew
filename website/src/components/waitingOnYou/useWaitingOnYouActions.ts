/** Answer and dismiss for pinned asks.
 *
 *  An answer is an ordinary chat send of the chosen label or the typed note, with
 *  a claim naming its ask. The gateway checks that claim when the message is
 *  written, so the server's slot push removes the card. A queued receipt only
 *  clears this surface's in-flight state; every surface derives queued-answer
 *  state from the shared slot queue until that entry drains or is cancelled.
 *
 *  What a click left on an ask (in flight, refused, sent) is kept per slot outside
 *  any one component: the stack that sent an answer can unmount before the reply
 *  lands (its session's last ask cleared, the sidebar card closed), and every other
 *  stack for that slot still has to say the answer was refused. */
import { useCallback, useSyncExternalStore } from 'react'
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
  /** The ask's title, kept so a refusal can still be shown once the ask is gone. */
  title?: string
}

/** The message an answer sends: an action card answered with no note says
 *  what its button said. */
function choiceText(choice: WaitingChoice, emptyActionAnswer: string): string {
  return 'label' in choice ? choice.label : (choice.text.trim() || emptyActionAnswer)
}

// Read synchronously at the click, so it is also the one-request-at-a-time guard:
// a double click must not send twice before React has rendered the first.
const askStates = new Map<string, Map<string, WaitingAskState>>()
const listeners = new Set<() => void>()
const NO_STATES: Readonly<Record<string, WaitingAskState>> = Object.freeze({})
const NO_SLOTS: ReadonlySet<string> = Object.freeze(new Set<string>())
// Rebuilt only after a write, so `useSyncExternalStore` sees a stable value between writes.
let snapshots = new Map<string, Readonly<Record<string, WaitingAskState>>>()
let noticeSlots: ReadonlySet<string> | null = null

function emit(): void {
  snapshots = new Map()
  noticeSlots = null
  for (const l of [...listeners]) l()
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

function patchAsk(slot: string, askId: string, next: WaitingAskState): void {
  const slotStates = askStates.get(slot) ?? new Map<string, WaitingAskState>()
  slotStates.set(askId, { ...slotStates.get(askId), ...next })
  askStates.set(slot, slotStates)
  emit()
}

function clearAsk(slot: string, askId: string): void {
  if (askStates.get(slot)?.delete(askId)) emit()
}

function slotSnapshot(slot: string): Readonly<Record<string, WaitingAskState>> {
  const cached = snapshots.get(slot)
  if (cached) return cached
  const slotStates = askStates.get(slot)
  const value = slotStates?.size ? Object.fromEntries(slotStates) : NO_STATES
  snapshots.set(slot, value)
  return value
}

function slotsWithNotices(): ReadonlySet<string> {
  if (noticeSlots) return noticeSlots
  const found = new Set<string>()
  for (const [slot, slotStates] of askStates) {
    if ([...slotStates.values()].some(s => s.errorKey)) found.add(slot)
  }
  noticeSlots = found.size ? found : NO_SLOTS
  return noticeSlots
}

/** The slots holding a refusal or failure their stack has not shown and cleared. */
export function useSlotsWithWaitingNotices(): ReadonlySet<string> {
  return useSyncExternalStore(subscribe, slotsWithNotices)
}

export function __resetWaitingOnYouActionsForTests(): void {
  askStates.clear()
  emit()
}

export function useWaitingOnYouActions(slot: string | null) {
  const { t } = useTranslation()
  const emptyActionAnswer = t('components.waitingOnYou.done')
  const states = useSyncExternalStore(subscribe, () => (slot ? slotSnapshot(slot) : NO_STATES))
  const patch = useCallback((askId: string, next: WaitingAskState) => {
    if (slot) patchAsk(slot, askId, next)
  }, [slot])
  const claim = useCallback((ask: WaitingOnYouAsk): boolean => {
    const current = slot ? askStates.get(slot)?.get(ask.ask_id) : undefined
    if (current?.busy || current?.settled) return false
    patch(ask.ask_id, { busy: true, errorKey: undefined, errorDetail: undefined, title: ask.title })
    return true
  }, [slot, patch])
  /** Forget what a click left on an ask, once its notice is read. */
  const clear = useCallback((askId: string) => {
    if (slot) clearAsk(slot, askId)
  }, [slot])

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
      // The gateway may have taken the answer, so nothing here says it failed: the
      // draft stays, and the ask's own update says whether it was answered. A second
      // send to an ask already answered is refused by the gateway.
      if (receipt.status === 'unknown' || receipt.status === 'response-late') {
        patch(ask.ask_id, { busy: false })
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

  return { states, answer, dismiss, clear }
}
