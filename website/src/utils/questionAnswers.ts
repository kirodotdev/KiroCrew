import { i18nT } from '../i18n/t'
import { ApiError, apiErrorCode } from '../api/apiError'
import type { ChatMessage } from '../types'

/** The recorded terminal reason carried by a rejected answer request. */
export function answerRejectionReason(err: unknown): string | undefined {
  if (!(err instanceof ApiError) || err.status !== 404) return undefined
  try {
    const parsed = JSON.parse(err.body) as unknown
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return undefined
    const reason = (parsed as { reason?: unknown }).reason
    return typeof reason === 'string' ? reason : undefined
  } catch {
    return undefined
  }
}

/** Answers as composer text: a lone answer bare, several as Q/A pairs. */
export function answersAsText(answers: Record<string, string>): string {
  // Drafts keep custom text as typed (a trailing space included); the text handed
  // to a composer is the finished answer, so trim it like a submit does.
  const pairs = Object.entries(answers).map(([question, answer]) => [question, answer.trim()] as const)
  if (pairs.length === 1) return pairs[0][1]
  return pairs
    .map(([question, answer]) => i18nT('components.pendingQuestionCard.qa_pair', { question, answer }))
    .join('\n\n')
}

/** Cap on the quoted question in a restored-answer notice, one line of a card. */
const QUESTION_NAME_MAX = 80

/** The question a restored-answer notice quotes: the card's first question, on one line. */
export function questionName(questions: ReadonlyArray<{ question: string }> | undefined): string {
  const text = (questions?.[0]?.question ?? '').replace(/\s+/g, ' ').trim()
  return text.length > QUESTION_NAME_MAX ? `${text.slice(0, QUESTION_NAME_MAX - 1)}…` : text
}

/** The session-view notice for an answer handed back to the composer; unnamed when the question is unknown. */
export function answerRestoredNotice(questions?: ReadonlyArray<{ question: string }>): string {
  const question = questionName(questions)
  return question
    ? i18nT('components.pendingQuestionCard.answer_restored_question', { question })
    : i18nT('components.pendingQuestionCard.answer_restored')
}

/**
 * The transcript row for an answer submit the server REJECTED (404: the wait is
 * gone). A rejected submit is a failure, so it renders as an ErrorNotice, not as a
 * warn-tone NoticeCard; only a passive retirement with no failed submit stays a
 * warning (see questionDraftHandBack).
 */
export function answerRejectedMessage(questions?: ReadonlyArray<{ question: string }>): ChatMessage {
  return { role: 'error', content: answerRestoredNotice(questions), cls: '' }
}

/** The Command Center notice, naming the session title when the caller has it. */
export function answerRestoredCommandCenterNotice(question: string, session?: string): string {
  if (!question.trim()) return i18nT('components.pendingQuestionCard.answer_restored_command_center')
  return session?.trim()
    ? i18nT('components.pendingQuestionCard.answer_restored_command_center_session', { question, session })
    : i18nT('components.pendingQuestionCard.answer_restored_command_center_question', { question })
}

/** The catalog key for a retryable answer failure, by the server's refusal code. */
export function answerFailureKey(err: unknown): string {
  return apiErrorCode(err) === 'answers_too_long'
    ? 'components.pendingQuestionCard.answers_too_long'
    : 'components.pendingQuestionCard.answer_failed'
}
