import { setQuestionNotice } from '../store/chatSlice'
import type { ChatState } from '../store/chatSlice'
import { answerRestoredNotice, answersAsText } from '../utils/questionAnswers'
import { restoreToComposer } from '../utils/composerRestore'

type PendingQuestion = ChatState['pendingQuestions'][string]

/** The unsent picks a retired blocking card holds, as composer text; '' when it holds none. */
export function questionDraftText(card: PendingQuestion | undefined): string {
  if (!card?.draftAnswers || !Object.keys(card.draftAnswers).length) return ''
  const text = answersAsText(card.draftAnswers)
  return text.trim() ? text : ''
}

/** Hand a retired card's unsent picks to its composer, with a slice notice that survives the chat_done refresh. */
export function handBackQuestionDraft(card: PendingQuestion | undefined, dispatch: (action: unknown) => void): boolean {
  const text = questionDraftText(card)
  if (!card || !text) return false
  restoreToComposer(card.slot, text)
  dispatch(setQuestionNotice({ slot: card.slot, message: answerRestoredNotice(card.questions), kind: 'restored' }))
  return true
}
