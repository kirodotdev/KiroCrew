/**
 * The cold-start welcome a crewmate opens its chat with (`mateGreeting.ts`):
 * the work it holds from before -- goals left open, then its recent sessions --
 * and a prompt to reply with one. Same message shell as the warm resume
 * (`MateGreetingMessage`), so the two read as one greeting. The hook draws it
 * only when there is something to list.
 */
import { useTranslation } from 'react-i18next'
import type { MemberRecap } from '../../api/client'
import type { CrewmateIdentity } from '../chat/CrewmateMessage'
import MateGreetingMessage from './MateGreetingMessage'

export default function MateWelcomeCard({ recap, crewmate, onDismiss }: { recap: MemberRecap; crewmate: CrewmateIdentity; onDismiss: () => void }) {
  const { t } = useTranslation()
  return (
    // A worded button, not a bare X: hiding the recap touches no work.
    <MateGreetingMessage
      crewmate={crewmate}
      label={t('pages.membersPage.welcome_title')}
      testId="member-welcome-card"
      dismissLabel={t('pages.membersPage.welcome_dismiss')}
      dismissTestId="member-welcome-dismiss"
      onDismiss={onDismiss}
    >
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
    </MateGreetingMessage>
  )
}
