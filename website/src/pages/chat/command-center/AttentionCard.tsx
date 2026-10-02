import { useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { ArrowUpRight, Check, ShieldCheck } from 'lucide-react'
import { Link } from 'react-router-dom'
import { api } from '../../../api/client'
import { useAppDispatch, useAppSelector } from '../../../store'
import { clearQuestionCard, resolveQuestionCard, selectComposerBusy } from '../../../store/chatSlice'
import { sendTurn } from '../../../chat-core/transport/sendTurn'
import { Btn } from '../../../components/ui'
import QuestionCard from '../../../components/QuestionCard'
import ErrorNotice from '../../../components/ErrorNotice'
import { APPROVAL_MODE_KEYS, approvalTitle, questionText, type AttentionItem } from './model'
import { toApiDecision } from '../../../utils/approvalDecision'
import { ApiError, isTerminalApprovalRefusal } from '../../../api/apiError'

/** Kept mounted while other inbox items are selected, preserving each answer draft. */
export default function AttentionCard({ item, title, context, onDraftChange, onOpenSession }: {
  item: AttentionItem
  title: string
  context?: string
  onDraftChange?: (active: boolean) => void
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
  // After reload an inactive session may have no live chat run state yet.
  const busy = useAppSelector(state => selectComposerBusy(state, item.slot)
    || state.dashboard.slots.some(slot => slot.key === item.slot && slot.running))
  const locked = useRef(false)
  // QuestionCard drops a draft whenever its payload changes, so a follow-up's
  // heading is translated once per mount: a language switch must not erase a
  // pick in progress. New labels change the item id, which remounts the card.
  const [followUpQuestions] = useState(() => item.question?.followUp
    ? item.question.questions.map((q, i) => ({ ...q, question: questionText(item.question!, i) })) : null)
  const [delivered, setDelivered] = useState(false)
  const mutation = useMutation({
    retry: false,
    mutationFn: async (action: { answers: Record<string, string> } | { approval: 'approve' | 'reject_once' }) => {
      if ('approval' in action && item.approval) {
        if (item.native) await api.approveChatSlot(item.slot, action.approval === 'approve' ? 'approved' : 'rejected_once', { request_id: item.approval.id, request_mid: item.approval.request_mid || '', origin: 'native' })
        else await api.resolveApproval(item.approval.id, toApiDecision(action.approval === 'approve' ? 'approved' : 'rejected_once'), { origin: 'coordinator', slot: item.approval.slot || '', instance: item.approval.instance || '' })
      } else if ('answers' in action && item.question) {
        const q = item.question
        if (q.ask_id) {
          await api.answerQuestion(q.ask_id, action.answers)
          dispatch(resolveQuestionCard({ ask_id: q.ask_id }))
        } else {
          // A follow-up choice is sent bare, as the composer chips send it: the
          // card's question text is ours, not the agent's, so prefixing it would
          // put words in the user's mouth.
          const message = q.followUp ? Object.values(action.answers).join('\n')
            : Object.entries(action.answers).map(([question, answer]) => `${question}: ${answer}`).join('\n')
          const receipt = await sendTurn({ slot: item.slot, message, ...(q.native && busy ? { steer: true } : {}) })
          if (receipt.status !== 'dispatched' && receipt.status !== 'queued') {
            throw new Error(receipt.status === 'refused' ? receipt.reason || t('commandCenter.send_refused') : t('commandCenter.send_unknown'))
          }
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
      // Only the inventories a decision changes; artifact bodies and the work
      // board are unaffected, and their own frames refresh them.
      void queryClient.invalidateQueries({ queryKey: ['global-approvals'] })
      void queryClient.invalidateQueries({ queryKey: ['command-center', 'questions'] })
    },
  })
  const expired = !!item.approval && isTerminalApprovalRefusal(mutation.error)
  const approvalHeading = item.approval ? approvalTitle(item.approval) || t('commandCenter.approval_needed') : ''
  const submit = (action: Parameters<typeof mutation.mutate>[0]) => {
    if (locked.current || delivered || expired) return
    locked.current = true
    mutation.mutate(action)
  }
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
    {/* No hand-off: QuestionCard holds this session's unsent answer draft. */}
    <ErrorNotice message={expired ? t('components.approvalCard.approval_no_longer_pending') : mutation.error?.message} />
    {delivered ? <p role="status" className="text-sm text-ok flex items-center gap-2"><Check size={15} />{t('commandCenter.recorded')}</p>
      : item.question ? <QuestionCard questions={followUpQuestions || item.question.questions} submitLabel={t('commandCenter.send_answer')} busy={mutation.isPending} onDraftChange={onDraftChange} onSubmit={answers => submit({ answers })} />
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
