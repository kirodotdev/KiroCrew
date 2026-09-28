import { api, ApiError } from '../api/client'
import { resolveQuestionCard, shouldResolveAskOnSend } from '../store/chatSlice'

/** Minimal shape of the send endpoint's acceptance the card rule reads. */
interface SendAcceptance {
  ok?: boolean
  queued?: boolean
}

/**
 * Retire the blocking question card a composer send just answered around.
 *
 * The user answering in the composer rather than in the card makes the card
 * stale, but it cannot simply be dropped from the store the way a stateless one
 * is: an agent is parked on its open HTTP request, so it has to be resolved
 * through the answer endpoint or it waits out its whole window with nothing on
 * screen.
 *
 * It is DISMISSED, not answered with the typed text. Pressing Send visibly
 * promises a chat message, and consuming that text into a tool result instead
 * would leave the transcript without the bubble the user just watched themselves
 * send. The agent gets no answer for the question and then immediately receives
 * the message, so it still has what the user said.
 *
 * The card is removed only once the endpoint has actually released the agent —
 * on success, or on a 404, which is itself proof the wait is already gone
 * (answered, dismissed, timed out, or its slot was reset). On any other failure
 * (offline, 5xx, tunnel throttle) the agent is still blocked, so the card STAYS:
 * it is the user's only affordance for releasing that agent by hand, and
 * deleting it optimistically would leave a silent stall with nothing pending on
 * screen — worse than the bug this fixes.
 *
 * A QUEUED send is resolved here too, and must be: the queue cannot pop until the
 * turn ends, and the turn cannot end while the agent is blocked on this card, so
 * deferring to queue_pop would hold the two against each other for the entire ask
 * window. The trade-off that buys is an ordering one — the agent receives the
 * dismissal and may act on the rest of its blocked turn BEFORE the queued composer
 * text pops at turn end — accepted because the alternative is the deadlock.
 *
 * Shared by the two send sites (ChatPage.send / ChatPane.doSend) so the rule
 * cannot drift between them, which is the same reason their entry-time captures
 * are shared. Resolves to whether the card was retired.
 */
export async function resolveAskAfterSend(
  accepted: SendAcceptance | null | undefined,
  askAtSend: string | null,
  dispatch: (action: unknown) => void,
): Promise<boolean> {
  if (!shouldResolveAskOnSend(accepted, askAtSend)) return false
  const askId = askAtSend as string
  try {
    await api.answerQuestion(askId)
  } catch (err) {
    if (!(err instanceof ApiError && err.status === 404)) return false
  }
  dispatch(resolveQuestionCard({ ask_id: askId }))
  return true
}
