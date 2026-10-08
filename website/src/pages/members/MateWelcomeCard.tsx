/**
 * The cold-start welcome a crewmate opens its chat with (`mateGreeting.ts`):
 * the work it holds from before -- goals left open, then its recent sessions --
 * and a prompt to reply with one. Same shell as the warm resume card, so the
 * two read as one greeting. The hook draws it only when there is something to
 * list.
 */
import { useTranslation } from 'react-i18next'
import CrewAvatar from '../../components/CrewAvatar'
import { Btn } from '../../components/ui'
import type { MemberRecap } from '../../api/client'
import type { CrewmateIdentity } from '../chat/CrewmateMessage'

export default function MateWelcomeCard({ recap, crewmate, onDismiss }: { recap: MemberRecap; crewmate: CrewmateIdentity; onDismiss: () => void }) {
  const { t } = useTranslation()
  return (
    <section
      className="mx-4 mt-3 flex items-start gap-2.5 rounded-2xl border border-border bg-card px-3.5 py-2.5 text-sm"
      aria-label={t('pages.membersPage.welcome_title')}
      data-testid="member-welcome-card"
    >
      <CrewAvatar seed={crewmate.name} avatar={crewmate.avatar} size={22} />
      <div className="min-w-0 flex-1">
        <p className="font-medium">{t('pages.membersPage.welcome_title')}</p>
        <ul className="mt-1 list-disc pl-4" data-testid="member-welcome-items">
          {recap.paused.map((p, i) => (
            <li key={`p${i}`}>
              {p.next ? t('pages.membersPage.welcome_paused_next', { goal: p.goal, next: p.next }) : t('pages.membersPage.welcome_paused', { goal: p.goal })}
            </li>
          ))}
          {recap.recent.map((r, i) => <li key={`r${i}`}>{t('pages.membersPage.welcome_recent', { title: r.title })}</li>)}
        </ul>
        <p className="mt-1.5">{t('pages.membersPage.welcome_ask')}</p>
      </div>
      {/* A worded button, not a bare X: hiding the recap touches no work. */}
      <Btn onClick={onDismiss} className="shrink-0" data-testid="member-welcome-dismiss">
        {t('pages.membersPage.welcome_dismiss')}
      </Btn>
    </section>
  )
}
