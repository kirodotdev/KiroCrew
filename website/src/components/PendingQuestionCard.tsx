import { useState } from 'react'
import QuestionCard from './QuestionCard'
import { useAppDispatch, useAppSelector } from '../store'
import { clearQuestionCard, pendingQuestionFor, resolveQuestionCard } from '../store/chatSlice'
import { api, ApiError } from '../api/client'

interface PendingQuestionCardProps {
  /** Slot this card belongs to. Cards are keyed per slot, so the single-chat
   *  view passes the active slot and each grid pane passes its own. */
  slotKey: string | null
  /** Send the answer as an ordinary chat message. Used for legacy cards (no
   *  `ask_id`, nothing is blocked on them) and when the wait has provably
   *  expired, so the user's input is not silently dropped. */
  onFallbackSend: (text: string) => void
  /**
   * Send the answer as a message IMMEDIATELY (no composer round-trip).
   *
   * Used only by the no-``ask_id`` card, where the card IS the primary
   * interaction: nothing is blocked, so there is no expired wait to guard and
   * no 404 to recover from, and pre-filling the composer would cost the user a
   * second click for no safety benefit. ``onFallbackSend`` keeps its original
   * job — recovering an answer whose blocked wait has already vanished, where
   * an explicit retry IS the right behaviour. Optional so existing callers and
   * tests that pass only ``onFallbackSend`` keep working unchanged.
   */
  onDirectSend?: (text: string) => void
}

/**
 * The pending `ask_question` card for one slot, with the answer round-trip.
 *
 * Shared by the single-chat view and the session-grid panes. It exists as a
 * component rather than inline JSX because both surfaces need identical submit
 * semantics: a pane that rendered the card but not the `ask_id` branch would
 * silently start a second turn and strand the blocked tool call. One
 * implementation means a pane cannot drift from the main view.
 */
/* The in-flight lock is keyed by ask_id, but a handoff card is a legacy
   (ask_id-less) card, so it needs its own key. Not a bare boolean and not null:
   null means "idle" and would leave the double-click guard open. */
const _HANDOFF_LOCK = '__plan_handoff__'

export default function PendingQuestionCard({ slotKey, onFallbackSend, onDirectSend }: PendingQuestionCardProps) {
  const dispatch = useAppDispatch()
  // Optional-chained: existing tests build partial preloaded chat state without
  // the pendingQuestions key.
  const pending = useAppSelector((s) => pendingQuestionFor(s.chat.pendingQuestions, slotKey))
  /* Which ask the in-flight request belongs to, NOT a bare boolean.
     One submission at a time: without a guard a double-click fires two
     answerQuestion calls -- the first resolves the wait, the second 404s, and the
     404 handler then sends the answer AGAIN as a chat message.
     Keyed by ask_id rather than a boolean because this component is mounted
     UNCONDITIONALLY inside each grid pane (it returns null when no card is
     pending, so its state survives): a plain `busy` left true after a successful
     submit would render the pane's every later card with Submit and Dismiss
     permanently disabled -- an unanswerable card, and a blocked agent. Comparing
     against the current ask makes a new card self-clearing, and also stops a
     stale in-flight response from locking it. */
  const [busyFor, setBusyFor] = useState<string | null>(null)
  if (!pending) return null

  const cardSlot = pending.slot
  const askId = pending.ask_id
  // Covers the handoff lock too: that card has no ask_id, so keying busy on
  // ask_id alone would leave Submit live while the disarm is in flight.
  const busy = (!!askId && busyFor === askId) || busyFor === _HANDOFF_LOCK
  const asText = (answers: Record<string, string>) => Object.values(answers).join('\n')

  /* Clearing by ask_id, never by slot: a slow response for ask A must not erase
     a newer ask B that already replaced it in the same slot, which would leave
     B on screen-less and blocked until its own timeout. */
  const clearThisCard = () => {
    if (askId) dispatch(resolveQuestionCard({ ask_id: askId }))
    else dispatch(clearQuestionCard({ slot: cardSlot }))
  }

  const resolve = (answers: Record<string, string> | undefined) => {
    if (!askId || busy) return
    setBusyFor(askId)
    api
      .answerQuestion(askId, answers)
      .then(() => clearThisCard())
      .catch((err) => {
        // 404 is the only proof the wait is gone (already answered, dismissed,
        // timed out, or its slot was reset) — then the answer is still worth
        // keeping as a message.
        if (err instanceof ApiError && err.status === 404) {
          clearThisCard()
          if (answers) {
            const text = asText(answers)
            if (text.trim()) onFallbackSend(text)
          }
          return
        }
        // Anything else (offline, 5xx, tunnel throttle) is retryable and the
        // agent is almost certainly STILL blocked. Keep the card so the user can
        // retry: clearing it would strand the tool call and start a second turn
        // it could never join.
      })
      // Released on EVERY path, success included. The success path clears the
      // card, but this component stays mounted in a grid pane, so a lock left
      // set here would disable the pane's next card too.
      .finally(() => {
        // A prior ask may settle after this pane has already submitted a newer
        // card. Release only this request's lock; clearing unconditionally would
        // unlock the newer request and permit a duplicate submission.
        setBusyFor((current) => (current === askId ? null : current))
      })
  }

  /* Leaving plan mode is a single server-side action, NOT a message: the
     endpoint disarms the gate and starts the implementation turn in that order,
     because a go-ahead sent while the gate is still armed just earns the agent
     another refusal on its first write. So this path deliberately does not send
     the answer text at all -- the endpoint appends its own.

     Fails CLOSED. If the call fails (a turn started in the meantime, offline,
     5xx) the card stays put and plan mode stays on, which costs the user a
     retry; the alternative -- clearing the card on a failed disarm -- strands
     them with a plan, no card, and a gate they did not know was still armed. */
  const startImplementing = () => {
    if (busyFor === _HANDOFF_LOCK) return
    setBusyFor(_HANDOFF_LOCK)
    Promise.resolve(api.approvePlan?.(cardSlot))
      .then(() => clearThisCard())
      .catch(() => { /* keep the card so the user can retry */ })
      .finally(() => setBusyFor((current) => (current === _HANDOFF_LOCK ? null : current)))
  }

  return (
    <QuestionCard
      // Remount per ask: QuestionCard holds the selections and custom-answer
      // text in its own state, so without a fresh key the next question in this
      // pane would inherit the previous one's picks.
      key={askId ?? cardSlot}
      questions={pending.questions}
      busy={busy}
      // Always offered. A blocked card resolves the wait with no answer; a
      // legacy card blocks nothing, so dismiss just takes it off screen —
      // withholding the control there left a card that could ONLY be answered,
      // parked on top of the composer until the session was reset.
      onDismiss={() => { if (askId) resolve(undefined); else clearThisCard() }}
      planHandoff={!!pending.plan_handoff}
      onSubmit={(answers, handoff) => {
        if (handoff === 'start') {
          startImplementing()
          return
        }
        if (!askId) {
          // Legacy card: nothing is blocked, so the answer is just a message —
          // and it is sent RIGHT NOW. Falls back to onFallbackSend only when no
          // direct sender was supplied (older callers / tests).
          const text = asText(answers)
          if (text.trim()) (onDirectSend ?? onFallbackSend)(text)
          clearThisCard()
          return
        }
        resolve(answers)
      }}
    />
  )
}
