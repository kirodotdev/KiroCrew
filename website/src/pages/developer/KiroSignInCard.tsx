import { useEffect, useRef, useState, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, CheckCircle2, Eye, EyeOff, KeyRound, Loader2, LogOut, RefreshCw } from 'lucide-react'
import { api, type KasLoginStatus } from '../../api/client'
import { KasLoginEmbedded } from '../../components/KasLoginGate'
import { KiroSignInDialog } from '../../components/KiroSignInDialog'
import { useKasLoginStatus } from '../../hooks/useKasLogin'
import { providerLabelFromWire } from '../../components/kasProviderLabel'
import { KIRO_ACCOUNT_EMAIL_HIDDEN_KEY } from '../../components/KiroAccountModal'
import { usePersistedBool } from '../../hooks/usePersistedBool'
import { useSignedInAccount } from './kiroSignedInAccount'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn, Card, CardTitle } from '../../components/ui'
import { KIRO_SIGN_IN_HIGHLIGHT_ANCHOR } from '../../hooks/useSettingHighlight'
import { fmtDateTime, fmtRelative } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { signInLapsed } from './kiroSignInState'
import {
  SignInStoreUnreadableErrors,
  KiroSignInSourcesNotice,
  SOURCES_QUERY_KEY,
  sourcesNotice,
  useKiroSignInSources,
  useKiroSignOut,
} from './KiroSignInSources'

// Same cache key the embedded flow reads status under (KasLoginGate.QUERY_KEY):
// a sign-out here must invalidate the flow's query, not a private copy of it.
const STATUS_QUERY_KEY = ['kas-login'] as const

/**
 * The account after the provider, as ` · <name>`. A hidden e-mail is blurred like
 * the popup's and, as there, sits beside the Show/Hide e-mail toggle, so the card
 * itself can reveal it. The folded heading passes no toggle and shows only a
 * visible name: a blur with nothing to press would not say which account it is.
 */
function AccountName({
  name,
  hidden,
  isEmail,
  withToggle = false,
  testId,
}: {
  name: string
  hidden: boolean
  isEmail: boolean
  withToggle?: boolean
  testId?: string
}) {
  const [, setEmailHidden] = usePersistedBool(KIRO_ACCOUNT_EMAIL_HIDDEN_KEY, true)
  if (!name) return null
  const toggleLabel = i18nT(hidden ? 'components.kiroAccountModal.show_email' : 'components.kiroAccountModal.hide_email')
  return (
    <span data-testid={testId} className="ml-1.5 inline-flex items-center gap-1.5 font-normal text-muted">
      {'· '}
      <span
        data-account-hidden={hidden ? 'true' : undefined}
        className={hidden ? 'select-none blur-[5px] opacity-60' : undefined}
      >
        {name}
      </span>
      {withToggle && isEmail ? (
        <button
          type="button"
          onClick={() => setEmailHidden(!hidden)}
          className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-full border border-border text-muted transition-colors hover:border-accent/35 hover:bg-accent/10 hover:text-accent focus-ring-accent"
          aria-label={toggleLabel}
          title={toggleLabel}
        >
          {hidden ? <Eye className="lucide-inline" aria-hidden="true" /> : <EyeOff className="lucide-inline" aria-hidden="true" />}
        </button>
      ) : null}
    </span>
  )
}

function SignedInSummary({
  status,
  onReauth,
  onSignedOut,
}: {
  status: KasLoginStatus
  onReauth: () => void
  /** Called once a sign-out has landed. The note it drives lives on the card,
   *  not here: this summary unmounts the moment the status turns unauthenticated. */
  onSignedOut: () => void
}) {
  const logout = useKiroSignOut(status.identity ?? '', onSignedOut)
  const lapsed = signInLapsed(status)
  const provider = providerLabelFromWire(status.provider)
  const account = useSignedInAccount()
  // What renews the session, in words the user can act on: an access token the
  // engine refreshes itself, one that has already lapsed, or one the issuer
  // refused to renew. Never the token, never its length.
  const expiryLine = status.refresh_rejected
    ? i18nT('pages.developer.kiroSignInCard.refresh_rejected')
    : status.expired
      ? status.has_refresh_token
        ? i18nT('pages.developer.kiroSignInCard.expired_renews_on_next_use')
        : i18nT('pages.developer.kiroSignInCard.expired_no_refresh')
      : status.expires_at
        ? i18nT('pages.developer.kiroSignInCard.access_token_expires', {
            when: fmtRelative(status.expires_at),
            at: fmtDateTime(status.expires_at),
          })
        : ''
  return (
    <div data-testid="kiro-sign-in-summary" data-lapsed={lapsed ? 'true' : 'false'}>
      <div className="flex flex-wrap items-start gap-3">
        <div
          className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-lg ${
            lapsed ? 'bg-warn/15 text-warn-text' : 'bg-ok/15 text-ok-text'
          }`}
          aria-hidden="true"
        >
          {lapsed ? <AlertTriangle className="lucide-inline" /> : <CheckCircle2 className="lucide-inline" />}
        </div>
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium text-text-strong" data-testid="kiro-sign-in-state">
            {lapsed
              ? i18nT('pages.developer.kiroSignInCard.sign_in_expired')
              : i18nT('pages.developer.kiroSignInCard.signed_in_with', { provider })}
            {!lapsed ? <AccountName {...account} withToggle testId="kiro-sign-in-account" /> : null}
          </p>
          <p className="mt-0.5 text-[13px] leading-relaxed text-muted">
            {lapsed
              ? i18nT('pages.developer.kiroSignInCard.sign_in_expired_body', { provider })
              : status.has_refresh_token
                ? i18nT('pages.developer.kiroSignInCard.signed_in_body')
                : // A live token with nothing to renew it: say so instead of
                  // promising an automatic renewal that will not happen.
                  i18nT('pages.developer.kiroSignInCard.signed_in_body_no_refresh')}
          </p>
          {expiryLine ? (
            <p className="mt-1 text-[12px] text-muted" data-testid="kiro-sign-in-expiry">
              {expiryLine}
            </p>
          ) : null}
        </div>
      </div>
      <div className="mt-4 flex flex-wrap items-center gap-2">
        {lapsed ? (
          <Btn type="button" primary onClick={onReauth} disabled={logout.isPending}>
            <RefreshCw className="lucide-inline" aria-hidden="true" />
            {i18nT('pages.developer.kiroSignInCard.sign_in_again')}
          </Btn>
        ) : (
          <Btn type="button" onClick={onReauth} disabled={logout.isPending}>
            <RefreshCw className="lucide-inline" aria-hidden="true" />
            {i18nT('pages.developer.kiroSignInCard.switch_account')}
          </Btn>
        )}
        <Btn
          type="button"
          danger
          disabled={logout.isPending}
          onClick={() => logout.mutate()}
          data-testid="kiro-sign-in-logout"
        >
          {logout.isPending ? (
            <Loader2 className="lucide-inline animate-spin" aria-hidden="true" />
          ) : (
            <LogOut className="lucide-inline" aria-hidden="true" />
          )}
          {i18nT('pages.developer.kiroSignInCard.sign_out')}
        </Btn>
      </div>
      {/* askAgent on: the card holds no draft (sign-out is a single press), and
          an unwritable or corrupt vault store is exactly the kind of failure the
          agent can diagnose (`kirocrew doctor`, permissions on the data home). */}
      <ErrorNotice
        className="mt-3"
        askAgent
        title={i18nT('pages.developer.kiroSignInCard.could_not_sign_out')}
        message={logout.error?.message || null}
        testId="kiro-sign-in-logout-error"
      />
    </div>
  )
}

/**
 * The card's body: a signed-in view, or one Sign in button. Signing in,
 * switching account and signing in again all open the shared sign-in dialog
 * (`KiroSignInDialog`), the same one the chat opens; closing it backs out. A
 * failed status read shows the shared flow's own error and Check again.
 */
function SignInBody({
  renderSignedIn,
}: {
  renderSignedIn: (status: KasLoginStatus, reauth: () => void) => ReactNode
}) {
  // This card is what keeps the shared status fresh while Settings is open.
  const statusQuery = useKasLoginStatus({ poll: 30_000 })
  const [dialog, setDialog] = useState<null | 'sign_in' | 'reauth'>(null)
  const status = statusQuery.data
  let content: ReactNode
  if (statusQuery.isPending) {
    content = (
      <p className="flex items-center gap-2 text-[13px] text-muted" data-testid="kiro-sign-in-pending">
        <Loader2 className="lucide-inline animate-spin" aria-hidden="true" />
        {i18nT('pages.developer.kiroSignInCard.checking')}
      </p>
    )
  } else if (!status) {
    content = <KasLoginEmbedded renderAuthenticated={() => null} />
  } else if (status.authenticated) {
    content = renderSignedIn(status, () => setDialog('reauth'))
  } else {
    content = (
      <Btn type="button" primary onClick={() => setDialog('sign_in')} data-testid="kiro-sign-in-open">
        {i18nT('components.kiroSignInPrompt.sign_in')}
      </Btn>
    )
  }
  return (
    <>
      {content}
      <KiroSignInDialog
        open={dialog !== null}
        onOpenChange={o => { if (!o) setDialog(null) }}
        reauth={dialog === 'reauth'}
      />
    </>
  )
}

/**
 * Settings > Kiro account card for signing in to Kiro Crew's OWN Kiro identity
 * -- the one the gateway hands to agent processes (KAS relay) and to the other
 * services that run as the user's Kiro account, without depending on
 * `kiro-cli login`. Wraps the sign-in flow `KasLoginGate` carries in its
 * embedded chrome, and adds the two things a card needs that a gate does not: a
 * signed-in summary (provider, expiry, renewability -- never a token) with
 * sign-out, and an explicit "sign-in expired" state that asks the user to sign
 * in again instead of quietly falling back to kiro-cli's login.
 *
 * Rendered by SettingsPage on its own tab while the build offers Kiro sign-in
 * (`useKiroSignInOffered`), whichever harness is selected. People look for
 * sign-out and account switching in the account popup and in Settings search,
 * so both link HERE instead of carrying a second copy of the flow: the Kiro
 * account popup's "Sign out or switch account" and the search entry
 * (`KIRO_SIGN_IN_SETTING_ID`), which waits on the `data-setting-id` this card
 * carries.
 */
export function KiroSignInCard() {
  const title = i18nT('pages.developer.kiroSignInCard.title')
  // Set after a successful sign-out, so the "applies to agent processes started
  // from now on" note shows at the moment the change was made. Held HERE, above
  // the authenticated-only summary, because that summary unmounts the instant
  // the status turns unauthenticated -- a note kept inside it was never seen.
  // Cleared by the next sign-in landing (the flow re-renders authenticated).
  const [signedOutNote, setSignedOutNote] = useState(false)
  // Observe the flow's status (same cache key, no extra fetch: the flow owns the
  // polling) so the note clears when a credential is stored again.
  const { data: observed } = useQuery({
    queryKey: STATUS_QUERY_KEY,
    queryFn: api.kasLoginStatus,
    enabled: false,
  })
  const authenticated = observed?.authenticated === true
  // Which stored sign-in sessions run as, read apart from the status above so a
  // slow answer never holds the card's first paint back. Re-read whenever a
  // sign-in lands or leaves: both change which account that is.
  const queryClient = useQueryClient()
  const { data: sources, error: sourcesError } = useKiroSignInSources()
  const notice = sourcesNotice(sources)
  const lastAuthenticated = useRef<boolean | null>(null)
  useEffect(() => {
    if (authenticated) setSignedOutNote(false)
    if (observed === undefined) return
    if (lastAuthenticated.current !== null && lastAuthenticated.current !== authenticated) {
      void queryClient.invalidateQueries({ queryKey: SOURCES_QUERY_KEY })
    }
    lastAuthenticated.current = authenticated
  }, [authenticated, observed, queryClient])
  const body = (
    <>
      {signedOutNote ? (
        <p className="mb-3 text-[12px] text-muted" role="status" data-testid="kiro-sign-in-takes-effect">
          {i18nT('pages.developer.kiroSignInCard.takes_effect_next_process')}
        </p>
      ) : null}
      {notice === 'using_kiro_cli' ? (
        <KiroSignInSourcesNotice sources={sources} identity="" onRemoved={() => undefined} />
      ) : null}
      {/* No hand-off: the embedded Company SSO form can hold an unsaved portal URL and region. */}
      <ErrorNotice
        className="mb-3"
        title={i18nT('pages.developer.kiroSignInCard.could_not_read_sources')}
        message={sourcesError?.message || null}
        testId="kiro-sign-in-sources-error"
      />
      <SignInStoreUnreadableErrors sources={sources} />
      <SignInBody
        renderSignedIn={(status, reauth) => (
          <>
            {notice !== 'using_kiro_cli' ? (
              <KiroSignInSourcesNotice
                sources={sources}
                identity={status.identity ?? ''}
                onRemoved={() => setSignedOutNote(true)}
              />
            ) : null}
            <SignedInSummary status={status} onReauth={reauth} onSignedOut={() => setSignedOutNote(true)} />
          </>
        )}
      />
    </>
  )
  return (
    <Card
      data-setting-key={KIRO_SIGN_IN_HIGHLIGHT_ANCHOR}
      data-setting-id={KIRO_SIGN_IN_HIGHLIGHT_ANCHOR}
      data-setting-label={i18nT('pages.developer.kiroSignInCard.search_label')}
      data-testid="kiro-sign-in-card"
    >
      <CardTitle>
        <KeyRound className="lucide-inline" aria-hidden="true" />
        {title}
      </CardTitle>
      {body}
    </Card>
  )
}
