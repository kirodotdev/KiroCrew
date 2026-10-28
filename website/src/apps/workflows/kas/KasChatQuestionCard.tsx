/**
 * The chat's OWN Kiro Agent question, answered in the composer (G2).
 *
 * A KAS `user_input` question raised by the chat's own model
 * session (not a workflow step) always waits for a person — `meta.person_only`,
 * no auto-approve rung resolves it. Its card still offered only Allow / Reject,
 * so Allow silently picked the first answer. This renders the REAL answers with
 * the same form the workflow-step question card uses (`KasQuestionForm`), and
 * answers them back through the chat approve route the backend accepts:
 *
 *   answer  → POST /api/chat/slots/{slot}/approve {action:'answer', request_id, answers}
 *   dismiss → POST /api/chat/slots/{slot}/approve {action:'rejected', request_id}
 *
 * The card is retired by the usual `approval_resolved` frame; the optimistic
 * `resolveByApprovalId` here only takes it off screen at once so the composer
 * does not wait on a round trip.
 */
import { useState } from 'react'

import type { KasQuestionItem } from '../../../api/client'
import { api, ApiError } from '../../../api/client'
import { i18nT } from '../../../i18n/t'
import { useAppDispatch } from '../../../store'
import { resolveByApprovalId } from '../../../store/chatSlice'
import { KasQuestionForm } from './KasQuestionCard'

/** The chat question carried on the permission card's `meta.kas_question`, in
 *  the shape of one `/state` `questions[]` entry (workflowId/nodeId are null
 *  here — this is not a workflow step — so only `key`, `title` and the
 *  `questions` the form renders matter). */
export interface KasChatQuestion {
  key: string
  title?: string | null
  questions: KasQuestionItem[]
}

export function KasChatQuestionCard({
  slot,
  requestId,
  question,
}: {
  slot: string
  requestId: string
  question: KasChatQuestion
}) {
  const dispatch = useAppDispatch()
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const title = question.title || i18nT('apps.workflows.kas.question_title')

  const submit = (action: 'answer' | 'rejected', extra: Record<string, unknown>) => {
    setPending(true)
    setError(null)
    api
      .approveChatSlot(slot, action, { request_id: requestId, ...extra })
      .then(() => {
        setPending(false)
        dispatch(resolveByApprovalId({ id: requestId, slot, decision: action === 'answer' ? 'answered' : 'rejected' }))
      })
      .catch((e: unknown) => {
        setPending(false)
        // The card is the only way to answer, so a stale request (the turn
        // moved on) retires it rather than leaving dead buttons; anything else
        // shows under the form so the person can retry.
        if (e instanceof ApiError && e.status === 404) {
          dispatch(resolveByApprovalId({ id: requestId, slot, decision: 'stale', allPending: true }))
          return
        }
        setError(e instanceof ApiError ? e.message : e)
      })
  }

  return (
    <KasQuestionForm
      question={question}
      title={title}
      pending={pending}
      error={error}
      onSubmit={answers => submit('answer', { answers })}
      onDismiss={() => submit('rejected', {})}
    />
  )
}
