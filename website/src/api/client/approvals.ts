/**
 * Human-in-the-loop requests: pending tool approvals and their resolution,
 * and agent question cards (rehydrate, answer, dismiss).
 */

import type { ClientTransport } from './transport'

export function createApprovalsEndpoints({ post, j }: ClientTransport) {
  const requests = {
    approvals: (): Promise<{ id: string; source?: string; tool?: string; tool_input?: string; tool_call_id?: string; slot?: string; ts?: number }[]> => fetch('/api/approvals').then(j),
    resolveApproval: (id: string, action: 'approve' | 'reject' | 'reject_once') => post('/api/approvals/' + encodeURIComponent(id) + '/' + action, {}).then(j),
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
