import { Coins } from 'lucide-react'
import { Card, CardTitle } from '../../components/ui'
import { AccountIdentity, accountIdentityName, CreditUsage, ManageKiroAccountLink, type KiroAccountUsage } from '../../components/KiroAccountModal'
import { i18nT } from '../../i18n/t'

const noop = () => {}

/**
 * Local refresh state belongs to the account that produced the reading. All
 * no-reading states share one key so a refresh can move between them without
 * losing its outcome; gaining an account, or changing its visible identity,
 * provider, or SSO portal, remounts only the usage subtree.
 */
function creditUsageAccountKey(usage: KiroAccountUsage): string {
  if (usage === null || typeof usage !== 'object') return 'no-account'
  const startUrl = 'startUrl' in usage ? usage.startUrl ?? '' : ''
  return JSON.stringify([accountIdentityName(usage), usage.accountType ?? '', startUrl])
}

/**
 * The Kiro Account popup's body as a section of Settings -> Kiro account: who
 * is signed in, the plan and the credit usage. On the phone this is the only
 * place it is shown, since the phone has no topbar credit capsule.
 *
 * `handoff` is false while the Kiro sign-in card is shown beside this one: it
 * can hold an unsaved Company SSO URL and region, which leaving for the chat
 * would drop. With no sign-in card there is no draft, so a failed read keeps
 * its "Ask the agent" way out.
 */
export function KiroCreditsCard({ usage, handoff }: { usage: KiroAccountUsage, handoff: boolean }) {
  return (
    <Card data-testid="kiro-credits-card">
      <CardTitle>
        <Coins className="lucide-inline" aria-hidden="true" />
        {i18nT('pages.developer.kiroAccountCredits.title')}
      </CardTitle>
      <div className="flex flex-col gap-4">
        <AccountIdentity usage={usage} />
        <CreditUsage key={creditUsageAccountKey(usage)} usage={usage} onClose={noop} handoff={handoff} />
        <ManageKiroAccountLink />
      </div>
    </Card>
  )
}
