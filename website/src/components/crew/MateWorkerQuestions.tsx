import { useState } from 'react'
import { MessageCircleQuestion } from 'lucide-react'
import { useAppDispatch, useAppSelector, useAppStore } from '../../store'
import { appendSlotMessage, pendingQuestionFor } from '../../store/chatSlice'
import { sendTurn } from '../../chat-core/transport/sendTurn'
import { i18nT } from '../../i18n/t'
import { mateRootOf } from '../../lib/crewMode'
import { sessionDisplayTitle } from '../../utils/sessionDisplayTitle'
import { answerRejectedMessage } from '../../utils/questionAnswers'
import { restoreToComposer } from '../../utils/composerRestore'
import ErrorNotice from '../ErrorNotice'
import PendingQuestionCard from '../PendingQuestionCard'
import type { ChatSlot } from '../../types'

type PendingQuestions = Parameters<typeof pendingQuestionFor>[0]

/** How an answer sent from here ended when it did not plainly land: refused
 *  (`failed`, the text is put back in the session's own composer), or sent with no
 *  word back (`unconfirmed`, it may have arrived, so nothing is parked). */
type AnswerTrouble = 'failed' | 'unconfirmed'

type QuestionNotices = Readonly<Record<string, unknown>> | undefined

/** The sessions *root* (a crewmate thread) dispatched that have an open
 *  question card, or a notice left by one that ended (an expired ask whose draft
 *  was handed back), in slot order. The notice keeps the session's card mounted,
 *  so the person learns here where the unsent answer went. Exported for tests. */
export function mateQuestionWorkers(
  slots: readonly ChatSlot[], root: string, pending: PendingQuestions, notices?: QuestionNotices,
): ChatSlot[] {
  if (!root) return []
  return slots.filter(s => mateRootOf(s) === root
    && (!!pendingQuestionFor(pending, s.key) || (!!notices && Object.prototype.hasOwnProperty.call(notices, s.key))))
}

/**
 * A crewmate's chat on the Mates page: the open question cards of the sessions
 * the mate dispatched, so a question is seen even where that session is not.
 *
 * Each card is the session's own `PendingQuestionCard`, keyed by the session's
 * slot, so it answers through exactly what the session's own chat uses: a
 * blocking ask resolves through `POST /api/ask-question/{ask_id}/answer`, and a
 * card nothing waits on is answered by a message sent to that session
 * (`sendTurn`), which retires the card on the server. Nobody may answer here who
 * could not answer there. Rule:
 * docs/request-for-change/rfc-crewmate-workers-under-the-crewmate.md, section 4.
 */
export default function MateWorkerQuestions({ root }: { root: string }) {
  const slots = useAppSelector(s => s.dashboard.slots)
  const pending = useAppSelector(s => s.chat.pendingQuestions)
  const notices = useAppSelector(s => s.chat.restoredQuestionNotices)
  // Sessions whose answer from here did not land. Kept here, not on the card:
  // the card clears itself on submit, so its own state would go with it.
  const [trouble, setTrouble] = useState<Readonly<Record<string, AnswerTrouble>>>({})
  const workers = mateQuestionWorkers(slots, root, pending, notices)
  const failed = slots.filter(s => mateRootOf(s) === root && Object.prototype.hasOwnProperty.call(trouble, s.key))
  if (workers.length === 0 && failed.length === 0) return null
  const mark = (key: string, next: AnswerTrouble | null) => setTrouble(cur => {
    const { [key]: _gone, ...rest } = cur
    return next ? { ...rest, [key]: next } : rest
  })
  return (
    <section
      aria-label={i18nT('components.mateWorkers.questions_label')}
      className="space-y-2"
      data-testid="mate-worker-questions"
    >
      {/* No hand-off: a refused answer is already back in the session's own
          composer draft (restoreToComposer), an unconfirmed one may have landed
          and is not offered again, and this chat's own draft is saved per chat,
          so nothing here is lost; the notice names the session to open. */}
      {failed.map(w => (
        <ErrorNotice
          key={`failed-${w.key}`}
          variant="inline"
          message={i18nT(trouble[w.key] === 'unconfirmed' ? 'components.mateWorkers.answer_maybe_lost' : 'components.mateWorkers.answer_not_sent', { name: sessionDisplayTitle(w) })}
          testId="mate-worker-question-failure"
          onDismiss={() => mark(w.key, null)}
        />
      ))}
      {workers.map(w => <MateWorkerQuestion key={w.key} worker={w} onTrouble={next => mark(w.key, next)} />)}
    </section>
  )
}

function MateWorkerQuestion({ worker, onTrouble }: { worker: ChatSlot; onTrouble: (next: AnswerTrouble | null) => void }) {
  const dispatch = useAppDispatch()
  const chatStore = useAppStore()
  const name = sessionDisplayTitle(worker)
  const headingId = `mate-question-${worker.key}`
  // The answer did not land: say so in this chat, put the text in the session's
  // own composer (the main chat's, a mounted pane's, or its saved draft, the
  // Command Center's restore), and leave the error row in its transcript, where
  // its card was.
  const notSent = (text: string, questions?: { question: string }[]) => {
    onTrouble('failed')
    restoreToComposer(worker.key, text)
    dispatch(appendSlotMessage({ slot: worker.key, message: answerRejectedMessage(questions) }))
  }
  // Called by the card before it clears itself, so the reads below still see it.
  const send = (text: string) => {
    onTrouble(null)
    const state = chatStore.getState()
    const questions = pendingQuestionFor(state.chat.pendingQuestions, worker.key)?.questions
    const native = pendingQuestionFor(state.chat.pendingQuestions, worker.key)?.native === true
    const running = !!state.dashboard.slots.find(s => s.key === worker.key)?.running
    // A native card is raised inside a running turn that waits for it: the
    // answer steers into that turn, as the session's own chat does.
    void sendTurn({ slot: worker.key, message: text, ...(native && running ? { steer: true } : {}) })
      .then(receipt => {
        if (receipt.status === 'dispatched' || receipt.status === 'queued') return
        // No answer came back: it may have landed, so it is not offered again.
        if (receipt.status === 'response-late' || receipt.status === 'unknown') onTrouble('unconfirmed')
        else notSent(text, questions)
      })
  }
  return (
    <article aria-labelledby={headingId} className="rounded-lg border border-border bg-card p-2 space-y-1" data-testid="mate-worker-question" data-worker={worker.key}>
      <h3 id={headingId} className="flex items-center gap-1.5 text-[13px] font-semibold min-w-0 px-1">
        <MessageCircleQuestion size={15} aria-hidden="true" className="shrink-0 text-accent" />
        <span className="truncate">{i18nT('components.mateWorkers.question_from', { name })}</span>
      </h3>
      <PendingQuestionCard
        slotKey={worker.key}
        onFallbackSend={(text, questions) => notSent(text, questions)}
        onDirectSend={send}
      />
    </article>
  )
}
