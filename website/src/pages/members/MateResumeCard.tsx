/**
 * The warm greeting a crewmate opens its chat with when the user comes back in
 * the middle of a goal (`mateGreeting.ts`): where the goal stands and the next
 * step, in the crewmate's voice, above the transcript. Read from the work
 * ledger, so it adds no turn to the chat and costs no model call.
 */
import { useTranslation } from 'react-i18next'
import { X } from 'lucide-react'
import CrewAvatar from '../../components/CrewAvatar'
import { IconButton } from '../../components/ui'
import type { CrewmateIdentity } from '../chat/CrewmateMessage'
import type { MateResume, ResumeReason } from './mateGreeting'

/** At most this many items are named; the counts above them carry the rest. */
const MAX_NAMED = 3

const REASON_KEY: Record<ResumeReason, string> = {
  question: 'pages.membersPage.resume_reason_question',
  blocked: 'pages.membersPage.resume_reason_blocked',
  quiet: 'pages.membersPage.resume_reason_quiet',
  done: 'pages.membersPage.resume_reason_done',
}

const NEXT_KEY: Record<ResumeReason, string> = {
  question: 'pages.membersPage.resume_next_question',
  blocked: 'pages.membersPage.resume_next_blocked',
  quiet: 'pages.membersPage.resume_next_quiet',
  done: 'pages.membersPage.resume_next_done',
}

export default function MateResumeCard({
  resume,
  crewmate,
  onDismiss,
}: {
  resume: MateResume
  crewmate: CrewmateIdentity
  onDismiss: () => void
}) {
  const { t } = useTranslation()
  const { next } = resume
  const name = crewmate.label || crewmate.name
  const nextText =
    next.kind === 'attention'
      ? t(NEXT_KEY[next.item.reason], { name, title: next.item.title })
      : next.kind === 'wait'
        ? t('pages.membersPage.resume_next_wait')
        : t('pages.membersPage.resume_next_continue', { name, title: next.title })
  return (
    <section
      className="mx-4 mt-3 flex items-start gap-2.5 rounded-2xl border border-border bg-card px-3.5 py-2.5 text-sm"
      aria-label={t('pages.membersPage.resume_title')}
      data-testid="member-resume-card"
    >
      <CrewAvatar seed={crewmate.name} avatar={crewmate.avatar} size={22} />
      <div className="min-w-0 flex-1">
        <p className="font-medium">{t('pages.membersPage.resume_title')}</p>
        {resume.goal && <p className="text-muted truncate" data-testid="member-resume-goal">{resume.goal}</p>}
        <p className="mt-1 text-[12px] text-muted" data-testid="member-resume-counts">
          {t('pages.membersPage.resume_counts', {
            finished: resume.finished,
            running: resume.running,
            idle: resume.idle,
            attention: resume.attention.length,
          })}
        </p>
        {resume.attention.length > 0 && (
          <ul className="mt-1 list-disc pl-4" data-testid="member-resume-attention">
            {resume.attention.slice(0, MAX_NAMED).map((item, i) => (
              <li key={i}>
                {t(REASON_KEY[item.reason], { title: item.title })}
                {item.detail && <span className="block text-[12px] text-muted line-clamp-2">{item.detail}</span>}
              </li>
            ))}
          </ul>
        )}
        <p className="mt-1.5" data-testid="member-resume-next">
          {t('pages.membersPage.resume_next_label', { next: nextText })}
        </p>
      </div>
      <IconButton
        aria-label={t('pages.membersPage.resume_dismiss')}
        onClick={onDismiss}
        className="shrink-0"
        data-testid="member-resume-dismiss"
      >
        <X className="lucide-inline" aria-hidden />
      </IconButton>
    </section>
  )
}
