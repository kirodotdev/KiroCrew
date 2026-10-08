import { useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { ArrowUpRight, Check, ShieldCheck } from 'lucide-react'
import { Link } from 'react-router-dom'
import { api } from '../../../api/client'
import { useAppDispatch, useAppSelector, useAppStore } from '../../../store'
import { appendSlotMessage, clearQuestionCard, isAnsweredQuestionEnding, markQuestionSettled, resolveQuestionCard, selectComposerBusy, setQuestionRequestInFlight } from '../../../store/chatSlice'
import { restoreToComposer } from '../../../utils/composerRestore'
import { answerFailureKey, answerRejectionReason, answerRestoredCommandCenterNotice, answersAsText, questionName } from '../../../utils/questionAnswers'
import { sendTurn } from '../../../chat-core/transport/sendTurn'
import { slotBusySteer } from '../../../components/chat-input/busySend'
import { useJevAutoSend } from '../useJevAutoSend'
import { Btn } from '../../../components/ui'
import QuestionCard from '../../../components/QuestionCard'
import ErrorNotice from '../../../components/ErrorNotice'
import StatusNotice from '../../../components/StatusNotice'
import { APPROVAL_MODE_KEYS, approvalTitle, questionText, type AttentionItem } from './model'
import { toApiDecision } from '../../../utils/approvalDecision'
import { ApiError, apiErrorCode, isTerminalApprovalRefusal } from '../../../api/apiError'

export function RestoredQuestionNotice({ slot, question, title, onDismiss }: {
  slot: string
  /** The retired ask's question text; empty falls back to the unnamed wording. */
  question?: string
  /** The session title the host's row shows for this slot, when it still has one. */
  title?: string
  onDismiss: () => void
}) {
  const { t } = useTranslation()
  return <section className="rounded-lg border border-border bg-card p-3 space-y-3">
    {/* Not an error: the answer is already saved in that session's composer, nothing was lost. */}
    <StatusNotice
      message={answerRestoredCommandCenterNotice(question ?? '', title)}
      onDismiss={onDismiss}
    />
    <Link
      to={`/chat?sid=${encodeURIComponent(slot)}`}
      onClick={onDismiss}
      className="text-accent text-[12px] inline-flex items-center gap-1"
    >
      {t('commandCenter.open_session')}<ArrowUpRight size={13} />
    </Link>
  </section>
}

/** Kept mounted while other inbox items are selected, preserving each answer draft. */
export default function AttentionCard({ item, title, context, onDraftChange, onOpenSession }: {
  item: AttentionItem
  title: string
  context?: string
  onDraftChange?: (answers: Record<string, string>) => void
  /** How to leave for this item's session, when the HOST must be asked first.
   *  Open session sits in this card's own header, one line above the composer
   *  holding the unsent answer, so it is the likeliest way to lose one -- and a
   *  bare `<Link>` reaches no leave guard (`NavigationLeaveGuard`: coverage is
   *  opt-in per navigation surface). A host that can unmount this subtree passes
   *  its guarded exit here; with none the link is an ordinary one, which is
   *  right where the route change does not take the draft with it. */
  onOpenSession?: (slot: string) => void
}) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const dispatch = useAppDispatch()
  const chatStore = useAppStore()
  // After reload an inactive session may have no live chat run state yet.
  const busy = useAppSelector(state => selectComposerBusy(state, item.slot)
    || state.dashboard.slots.some(slot => slot.key === item.slot && slot.running))
  const jevAutoConsented = useJevAutoSend()
  const hadChatCardAtSubmitRef = useRef(false)
  const chatDraftAtSubmitRef = useRef('')
  const locked = useRef(false)
  // QuestionCard drops a draft whenever its payload changes, so a follow-up's
  // heading is translated once per mount: a language switch must not erase a
  // pick in progress. New labels change the item id, which remounts the card.
  const [followUpQuestions] = useState(() => item.question?.followUp
    ? item.question.questions.map((q, i) => ({ ...q, question: questionText(item.question!, i) })) : null)
  const [delivered, setDelivered] = useState(false)
  const [restored, setRestored] = useState(false)
  const [settledElsewhere, setSettledElsewhere] = useState(false)
  const restoredNotice = answerRestoredCommandCenterNotice(questionName(item.question?.questions), title)
  const mutation = useMutation({
    retry: false,
    mutationFn: async (action: { answers: Record<string, string> } | { approval: 'approve' | 'reject_once' }) => {
      if ('approval' in action && item.approval) {
        if (item.native) await api.approveChatSlot(item.slot, action.approval === 'approve' ? 'approved' : 'rejected_once', { request_id: item.approval.id, request_mid: item.approval.request_mid || '', origin: 'native' })
        else await api.resolveApproval(item.approval.id, toApiDecision(action.approval === 'approve' ? 'approved' : 'rejected_once'), { origin: 'coordinator', slot: item.approval.slot || '', instance: item.approval.instance || '' })
      } else if ('answers' in action && item.question) {
        const q = item.question
        if (q.ask_id) {
          try {
            await api.answerQuestion(q.ask_id, action.answers)
          } catch (err) {
            if (!(err instanceof ApiError && err.status === 404)) throw err
            const answered = isAnsweredQuestionEnding(answerRejectionReason(err))
            if (!answered) {
              const text = answersAsText(action.answers)
              const chatText = chatDraftAtSubmitRef.current
              const retiredElsewhere = hadChatCardAtSubmitRef.current
                && !Object.values(chatStore.getState().chat.pendingQuestions ?? {})
                  .some((card) => card.ask_id === q.ask_id)
              if (text.trim() && (!retiredElsewhere || text.trim() !== chatText.trim())) {
                restoreToComposer(item.slot, text)
                if (!retiredElsewhere || !chatText.trim()) {
                  // The server rejected this submit, so the transcript row is an ErrorNotice.
                  dispatch(appendSlotMessage({ slot: item.slot, message: { role: 'error', content: restoredNotice, cls: '' } }))
                }
              }
              if (!retiredElsewhere && chatText.trim() && chatText.trim() !== text.trim()) {
                restoreToComposer(item.slot, chatText)
              }
            }
            if (answered) {
              dispatch(markQuestionSettled({ ask_id: q.ask_id }))
              dispatch(resolveQuestionCard({ ask_id: q.ask_id, settled: true }))
            } else {
              dispatch(resolveQuestionCard({ ask_id: q.ask_id }))
            }
            onDraftChange?.({})
            if (answered) setSettledElsewhere(true)
            else setRestored(true)
            return
          }
          dispatch(markQuestionSettled({ ask_id: q.ask_id }))
          onDraftChange?.({})
          dispatch(resolveQuestionCard({ ask_id: q.ask_id, settled: true }))
        } else {
          // A follow-up choice is sent bare, as the composer chips send it: the
          // card's question text is ours, not the agent's, so prefixing it would
          // put words in the user's mouth.
          const message = q.followUp ? Object.values(action.answers).join('\n')
            : Object.entries(action.answers).map(([question, answer]) => `${question}: ${answer}`).join('\n')
          // A follow-up choice is a chip send, so it takes the composer's busy
          // decision for its slot (steer, queue or auto per the busy-send mode).
          const steer = q.followUp ? slotBusySteer(chatStore.getState(), item.slot, jevAutoConsented) : (q.native && busy ? true : undefined)
          const receipt = await sendTurn({ slot: item.slot, message, ...(steer ? { steer } : {}) })
          if (receipt.status !== 'dispatched' && receipt.status !== 'queued') {
            throw new Error(receipt.status === 'refused' ? receipt.reason || t('commandCenter.send_refused') : t('commandCenter.send_unknown'))
          }
          onDraftChange?.({})
          // Never resend after a confirmed acceptance, even if retiring the
          // visual card fails. The next inventory read reconciles server state.
          setDelivered(true)
          if (q.card_id) {
            try {
              await api.dismissQuestionCard(item.slot, q.card_id)
            } catch (err) {
              // The answer's own user row retires a stateless card server-side,
              // often before this dismiss lands: a 404 means it is already gone.
              if (!(err instanceof ApiError && err.status === 404)) throw err
            }
            dispatch(clearQuestionCard({ slot: item.slot, card_id: q.card_id }))
          }
        }
      }
      setDelivered(true)
    },
    onSettled: () => {
      locked.current = false
      if (item.question?.ask_id) {
        dispatch(setQuestionRequestInFlight({ ask_id: item.question.ask_id, inFlight: false }))
      }
      // Only the inventories a decision changes; artifact bodies and the work
      // board are unaffected, and their own frames refresh them.
      void queryClient.invalidateQueries({ queryKey: ['global-approvals'] })
      void queryClient.invalidateQueries({ queryKey: ['command-center', 'questions'] })
    },
  })
  const expired = !!item.approval && isTerminalApprovalRefusal(mutation.error)
  // A refused answer body carries a code; its prose is the server's, not the catalog's.
  const answerRefused = !!item.question?.ask_id && apiErrorCode(mutation.error) === 'answers_too_long'
  const approvalHeading = item.approval ? approvalTitle(item.approval) || t('commandCenter.approval_needed') : ''
  const submit = (action: Parameters<typeof mutation.mutate>[0]) => {
    if (locked.current || delivered || restored || expired) return
    locked.current = true
    if ('answers' in action && item.question?.ask_id) {
      const chatCard = Object.values(chatStore.getState().chat.pendingQuestions ?? {})
        .find((card) => card.ask_id === item.question?.ask_id)
      hadChatCardAtSubmitRef.current = !!chatCard
      chatDraftAtSubmitRef.current = chatCard?.draftAnswers
        ? answersAsText(chatCard.draftAnswers)
        : ''
      dispatch(setQuestionRequestInFlight({ ask_id: item.question.ask_id, inFlight: true }))
    }
    mutation.mutate(action)
  }
  if (settledElsewhere) return null
  return <section className="rounded-lg border border-border bg-card p-3 space-y-3">
    <div className="flex items-center gap-2 min-w-0">
      <h3 className="text-sm font-semibold break-words min-w-0 flex-1">{item.approval && <ShieldCheck size={15} className="lucide-inline" />}{approvalHeading || title}</h3>
      {onOpenSession
        ? <button type="button" onClick={() => onOpenSession(item.slot)} className="text-accent text-[12px] inline-flex items-center gap-1 shrink-0">{t('commandCenter.open_session')}<ArrowUpRight size={13} /></button>
        : <Link to={`/chat?sid=${encodeURIComponent(item.slot)}`} className="text-accent text-[12px] inline-flex items-center gap-1 shrink-0">{t('commandCenter.open_session')}<ArrowUpRight size={13} /></Link>}
    </div>
    {item.approval && <p className="text-[12px] text-muted break-words">{t('commandCenter.from_session', { name: title })}</p>}
    {context && <p className="text-sm text-muted break-words">{context}</p>}
    {item.approval && <p className="text-[12px] text-warn">{t('commandCenter.approval_needed')}{item.approvalMode ? ` · ${t('commandCenter.permission_mode', { mode: t(APPROVAL_MODE_KEYS[item.approvalMode]) })}` : ''}</p>}
    {item.approval && item.approvalMode === 'normal' && <p className="text-[12px] text-muted">{t('commandCenter.normal_help')}</p>}
    {(item.approval || item.question) && <p className="text-[12px] text-muted">{t('commandCenter.explicit_input')}</p>}
    {/* No hand-off: retryable question failures retain the unsent answer draft,
        and a rejected submission's answer now sits in that session's composer,
        which the hand-off's prefill would overwrite. */}
    {/* A rejected submission is an error surface (errors-use-error-notice), even
        though its answer is safe in that session's composer. A retirement with no
        failed submission is RestoredQuestionNotice's warn-tone status instead. */}
    <ErrorNotice message={restored ? restoredNotice : expired ? t('components.approvalCard.approval_no_longer_pending') : answerRefused ? t(answerFailureKey(mutation.error)) : mutation.error?.message} />
    {restored ? null
      : delivered ? <p role="status" className="text-sm text-ok flex items-center gap-2"><Check size={15} />{t('commandCenter.recorded')}</p>
      : item.question ? <QuestionCard questions={followUpQuestions || item.question.questions} askId={item.question.ask_id} submitLabel={t('commandCenter.send_answer')} busy={mutation.isPending} onDraftChange={onDraftChange} onSubmit={answers => submit({ answers })} />
      : item.approval ? <>
        <p className="text-sm break-words">{item.approval.tool_purpose?.trim() || t('commandCenter.purpose_missing')}</p>
        {/* eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- keyboard users must be able to scroll the exact command without splitting its tokens. */}
        <pre tabIndex={0} role="region" aria-label={t('commandCenter.approval_needed')} className="max-w-full max-h-[40vh] overflow-auto whitespace-pre break-normal bg-bg-hover rounded-md px-3 py-2 text-[13px] font-mono">{typeof item.approval.tool_input === 'string' ? item.approval.tool_input : JSON.stringify(item.approval.tool_input, null, 2) || ''}</pre>
        <p className="text-[12px] text-muted">{t('commandCenter.reject_help')}</p>
        <p className="text-[12px] text-muted">{t('commandCenter.shared_request_help')}</p>
        {!expired && <div className="flex gap-2">
          <Btn primary className="min-h-11" disabled={mutation.isPending} onClick={() => submit({ approval: 'approve' })}>{t('commandCenter.approve')}</Btn>
          <Btn className="min-h-11" disabled={mutation.isPending} onClick={() => submit({ approval: 'reject_once' })}>{t('commandCenter.reject')}</Btn>
        </div>}
      </> : <p className="text-sm text-muted">{t('commandCenter.open_to_answer')}</p>}
  </section>
}
