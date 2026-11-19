import { useQuery } from '@tanstack/react-query'
import { useInRouterContext } from 'react-router-dom'
import { ArrowRight } from 'lucide-react'
import { api } from '../../api/client'
import { isPermissionRefusal } from '../../api/apiError'
import { providerLabelFromWire } from '../../components/kasProviderLabel'
import ErrorNotice from '../../components/ErrorNotice'
import { SettingsLink } from '../../components/SettingsLink'
import { i18nT } from '../../i18n/t'
import { useSignedInAccount } from './kiroSignedInAccount'
import { KIRO_SIGN_IN_TARGET } from './kiroSignInLink'
import { SignInStoreUnreadableErrors, sourcesNotice, useKiroSignInSources } from './KiroSignInSources'
import { signInLapsed } from './kiroSignInState'

/**
 * One line in the Kiro Agent row's detail: whether Crew's own Kiro sign-in is
 * stored, and the way to the Kiro account tab where it is managed. The card
 * itself lives on that tab, because the sign-in is used by more than this
 * harness. Reads the status under the key the card's flow polls, so a sign-in
 * made there shows here without a reload. While the status is unread (or the
 * read failed) only the link shows: the tab reports that read's state itself.
 */
export function KiroAccountStatusLine() {
  const inRouter = useInRouterContext()
  const { data: status } = useQuery({ queryKey: ['kas-login'], queryFn: api.kasLoginStatus })
  const account = useSignedInAccount()
  // KAS also runs on a kiro-cli login when Crew holds none: that is working,
  // not "Not signed in" (the card's `using_kiro_cli` notice, same read).
  // A sources read that failed, or a store it could not read, is said as the
  // card says it (ErrorNotice), never folded into "Not signed in".
  const { data: sources, error: sourcesError } = useKiroSignInSources()
  const storeUnknown = sources?.kiro_crew?.stored === null || sources?.kiro_cli?.stored === null
  let text: string | null = null
  if (status && !status.authenticated) {
    if (sourcesNotice(sources) === 'using_kiro_cli') text = i18nT('pages.developer.kiroSignInCard.using_kiro_cli_title')
    // Unknown, not signed out: the errors above say which read failed.
    else if (!storeUnknown && !sourcesError) text = i18nT('pages.developer.agentBackendTab.kiro_account_not_signed_in')
  } else if (status && signInLapsed(status)) text = i18nT('pages.developer.kiroSignInCard.sign_in_expired')
  else if (status && account.name && !account.hidden)
    text = i18nT('pages.developer.agentBackendTab.kiro_account_signed_in_as', { name: account.name })
  else if (status)
    text = i18nT('pages.developer.kiroSignInCard.signed_in_with', { provider: providerLabelFromWire(status.provider) })
  return (
    <div className="border-t border-border pt-3">
      {/* No hand-off: this row sits on the Agent Harness tab beside the unsaved Subagent Settings draft, which the hand-off's navigation would discard. */}
      <ErrorNotice
        className="mb-3"
        title={i18nT('pages.developer.kiroSignInCard.could_not_read_sources')}
        // Not the owner: the sign-in reads are refused, and only the link shows.
        message={isPermissionRefusal(sourcesError) ? null : sourcesError?.message || null}
        testId="kiro-sign-in-sources-error"
      />
      <SignInStoreUnreadableErrors sources={sources} />
      <p
        data-testid="kiro-account-status-line"
        className="m-0 flex flex-wrap items-center gap-x-2 gap-y-1 text-[13px] leading-relaxed text-text"
      >
        {text ? <span data-testid="kiro-account-status">{text}</span> : null}
        {inRouter && (
          <SettingsLink
            {...KIRO_SIGN_IN_TARGET}
            data-testid="kiro-account-status-link"
            className="inline-flex items-center gap-1 text-accent hover:underline focus-ring-accent"
          >
            {i18nT('settings.tabs.kiroAccount.go_to')}
            <ArrowRight className="lucide-inline" aria-hidden="true" />
          </SettingsLink>
        )}
      </p>
    </div>
  )
}
