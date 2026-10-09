/**
 * Human-in-the-loop requests: pending tool approvals and their resolution,
 * and agent question cards (rehydrate, answer, dismiss).
 */

import type { ClientTransport } from './transport'
import type { ApprovalTarget } from '../../types/approvalTarget'

export function createApprovalsEndpoints({ post, j, jfetch: fetch }: ClientTransport) {
  const requests = {
    approvals: (): Promise<{ id: string; instance?: string; source?: string; tool?: string; tool_input?: string; tool_purpose?: string; tool_call_id?: string; slot?: string; ts?: number }[]> => fetch('/api/approvals').then(j),
    /** Decide the one request *target* names (see types/approvalTarget). This
     *  is the dashboard client's only decide: there is no bare-id one, because
     *  an approval id recurs and names no request by itself
     *  (`approvalBareIdGuard.test.ts` fails if one comes back). A
     *  coordinator target goes to the decide route bound to its slot and
     *  instance (minted per record, so it already names what the record
     *  gates); a native one to its slot's approve route bound to the row's
     *  mid. Either is refused once another request holds the id. */
    decideApproval: (target: ApprovalTarget, action: 'approve' | 'reject' | 'reject_once') => target.origin === 'coordinator'
      ? post('/api/approvals/' + encodeURIComponent(target.id) + '/' + action + '?'
        + new URLSearchParams({ origin: 'coordinator', slot: target.slot, instance: target.instance }), {}).then(j)
      : post('/api/chat/slots/' + encodeURIComponent(target.slot) + '/approve', {
        action: action === 'approve' ? 'approved' : action === 'reject_once' ? 'rejected_once' : 'rejected',
        origin: 'native', request_id: target.id, request_mid: target.mid,
      }).then(j),
    /** Question cards still awaiting an answer, for rehydration after a reload or
     *  websocket reconnect (`question_card` is a one-shot broadcast). A blocking
     *  ask carries `ask_id`; a stateless card carries `card_id` instead, and
     *  `native` when it is kiro-cli's mid-turn `AskUserQuestion` card (whose
     *  answer steers into the live turn). */
    pendingQuestions: (): Promise<{ ask_id?: string; card_id?: string; native?: boolean; slot: string; questions: { question: string; header?: string; multiSelect?: boolean; options: { label: string; description?: string }[] }[]; ts?: number }[]> =>
      fetch('/api/ask-question/pending').then(j),
    /** Resolve a pending agent question that carries an `ask_id` — a server-side
     *  wait opened by `POST /api/ask-question`, not the MCP ask_question tool,
     *  which posts a stateless `card_id` card instead. Pass no answers to
     *  dismiss, which unblocks the waiting caller with a timeout-equivalent
     *  result. */
    answerQuestion: (askId: string, answers?: Record<string, string>) =>
      post('/api/ask-question/' + encodeURIComponent(askId) + '/answer',
        answers ? { answers } : { dismissed: true }).then(j),
    /** Retire the slot's needs-input status for a STATELESS card (no `ask_id`),
     *  which blocks nothing and is otherwise removed client-side only — leaving
     *  the sidebar and sessions board claiming the agent is still waiting.
     *  `cardId` is the server-minted identity from the `question_card` payload:
     *  the dismissal is a round-trip, so a newer card can replace this one before
     *  it lands, and the server refuses rather than retiring the wrong ask. */
    dismissQuestionCard: (slot: string, cardId: string) =>
      post('/api/ask-question/dismiss', { slot, card_id: cardId }).then(j),
  }

  return { requests }
}
