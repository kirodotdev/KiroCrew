import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, CheckCircle2, Eye, EyeOff, KeyRound, Loader2, LogOut, RefreshCw } from 'lucide-react'
import { api, type KasLoginStatus } from '../../api/client'
import { isAccountOnly, parseKiroUsagePayload } from '../../api/kiroUsage'
import { KasLoginEmbedded } from '../../components/KasLoginGate'
import { providerLabelFromWire } from '../../components/kasProviderLabel'
import { KIRO_ACCOUNT_EMAIL_HIDDEN_KEY } from '../../components/KiroAccountModal'
import { usePersistedBool } from '../../hooks/usePersistedBool'
import { configuredAcpBackend, type AcpBackendConfig } from '../../api/acpBackend'
import { KIRO_SIGN_IN_BACKEND } from './kiroSignInLink'
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
 * Who the stored sign-in belongs to (email, else account name), read from the
 * top bar's `['kiro-usage']` reading -- the vault stores no e-mail, so that
 * whoami-backed reading is the only place a name exists. That reading names the
 * vault account only while the saved main harness is KAS (the gateway's
 * `_vault_owns_usage_sync`); on any other harness it names kiro-cli's login,
 * which this card's Sign out does not touch, so no name is shown then, nor
 * before the config has loaded. `enabled: false`: the top bar owns both reads,
 * this only observes them. `hidden`: an e-mail follows the account popup's hide
 * toggle (hidden by default), so a shared screen does not show it here either;
 * an account name is never hidden.
 */
function useSignedInAccount(): { name: string; hidden: boolean; isEmail: boolean } {
  const { data } = useQuery({
    queryKey: ['kiro-usage'],
    queryFn: () => api.sessionsUsage().then(parseKiroUsagePayload),
    enabled: false,
  })
  const { data: cfg } = useQuery<AcpBackendConfig>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
    enabled: false,
  })
  const [emailHidden] = usePersistedBool(KIRO_ACCOUNT_EMAIL_HIDDEN_KEY, true)
  const none = { name: '', hidden: false, isEmail: false }
  if (cfg === undefined || configuredAcpBackend(cfg) !== KIRO_SIGN_IN_BACKEND) return none
  if (typeof data !== 'object' || data === null || isAccountOnly(data)) return none
  if (data.email) return { name: data.email, hidden: emailHidden, isEmail: true }
  return { ...none, name: data.account || '' }
}

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
 * Settings > Agent Harness card for signing in to Kiro Crew's OWN Kiro identity
 * -- the one the gateway hands to agent processes (KAS relay) so they run as the
 * user without depending on `kiro-cli login`. Wraps the sign-in flow
 * `KasLoginGate` carries in its embedded chrome, and adds the two things a card
 * needs that a gate does not: a signed-in summary (provider, expiry,
 * renewability -- never a token) with sign-out, and an explicit "sign-in
 * expired" state that asks the user to sign in again instead of quietly falling
 * back to kiro-cli's login.
 *
 * Rendered by `AgentBackendTab` INSIDE the KAS row's detail, and only while KAS
 * is a backend that switch offers: the stored identity is consumed by the KAS
 * relay alone, so on a build or policy that cannot select KAS there is nothing
 * to sign in for, and on a row that is not KAS it would be a sign-in for an
 * agent that never uses it. That is also why it is NOT on Settings > Overview
 * (its former home): a sign-in chooser on the landing page read as a required
 * step to every user. People look for sign-out and account switching in the
 * account popup and in Settings search, so both link HERE instead of carrying a
 * second copy of the flow: the Kiro account popup's "Sign out or switch account"
 * and the search entry (`KIRO_SIGN_IN_SETTING_ID`), which waits on the
 * `data-setting-id` this card carries.
 *
 * `compact` is that in-detail form: a fold with a small heading rather than
 * a `Card` with a `CardTitle`, because the detail it sits in is already the
 * bordered panel and a card inside it would be a box inside a box. The anchor
 * (`data-setting-key`) and the test id are the same in both forms, so the
 * chat's deep link rings whichever one is on screen.
 *
 * The compact form FOLDS: a `<details>` that is open while the person still has
 * to sign in (signed out, lapsed, or the status read failed) and closed to its
 * heading plus "Signed in with …" once a usable sign-in is stored, so a done step
 * stops taking the page. Closed while the first answer is in flight, so a signed-in
 * page never flashes the chooser open. A sign-in that lapses later re-opens it. A
 * toggle by the person wins over that default; `openRequest` (bumped by the chat's
 * sign-in deep link) opens it whatever they last chose, once: the card reports it
 * handled so a later remount does not force it open again.
 */
export function KiroSignInCard({
  compact = false,
  openRequest = 0,
  onOpenRequestHandled,
}: { compact?: boolean; openRequest?: number; onOpenRequestHandled?: () => void } = {}) {
  const title = i18nT('pages.developer.kiroSignInCard.title')
  // Set after a successful sign-out, so the "applies to agent processes started
  // from now on" note shows at the moment the change was made. Held HERE, above
  // the authenticated-only summary, because that summary unmounts the instant
  // the status turns unauthenticated -- a note kept inside it was never seen.
  // Cleared by the next sign-in landing (the flow re-renders authenticated).
  const [signedOutNote, setSignedOutNote] = useState(false)
  // Observe the flow's status (same cache key, no extra fetch: the flow owns the
  // polling) so the note clears when a credential is stored again.
  const { data: observed, isError: statusFailed } = useQuery({
    queryKey: STATUS_QUERY_KEY,
    queryFn: api.kasLoginStatus,
    enabled: false,
  })
  const authenticated = observed?.authenticated === true
  const account = useSignedInAccount()
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
  // The compact fold: `null` follows the sign-in state, a boolean is the person's toggle.
  const signedIn = authenticated && observed !== undefined && !signInLapsed(observed)
  const [userOpen, setUserOpen] = useState<boolean | null>(null)
  // Set at the FIRST answer and only ever re-opened after that: signing in from the
  // open fold must not snap it shut under the person's pointer, but a sign-in that
  // lapses or is signed out elsewhere must not hide behind a closed heading.
  const [autoOpen, setAutoOpen] = useState<boolean | null>(null)
  const wasSignedIn = useRef(false)
  useEffect(() => {
    // A failed first read opens it and keeps it open once a retry answers.
    if (observed === undefined && statusFailed && autoOpen === null) setAutoOpen(true)
    else if (observed !== undefined && autoOpen === null) setAutoOpen(!signedIn)
    // Every signed-in -> not-signed-in change re-opens it, whatever the person last chose.
    if (wasSignedIn.current && !signedIn && observed !== undefined) {
      setAutoOpen(true)
      setUserOpen(null)
    }
    wasSignedIn.current = signedIn
  }, [observed, autoOpen, signedIn, statusFailed])
  const handled = useRef(onOpenRequestHandled)
  handled.current = onOpenRequestHandled
  useEffect(() => {
    if (openRequest > 0) {
      setUserOpen(true)
      handled.current?.()
    }
  }, [openRequest])
  // A failed status read is not an answer to fold on: its error and retry are inside.
  // Two different accounts open the fold too: a closed heading reading "Signed in"
  // would hide that sessions are not running as the kiro-cli account.
  const open = userOpen ?? (notice === 'two_accounts' ? true : (autoOpen ?? statusFailed))
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
      <KasLoginEmbedded
        renderPending={() => (
          <p className="flex items-center gap-2 text-[13px] text-muted" data-testid="kiro-sign-in-pending">
            <Loader2 className="lucide-inline animate-spin" aria-hidden="true" />
            {i18nT('pages.developer.kiroSignInCard.checking')}
          </p>
        )}
        renderAuthenticated={(status, { reauth }) => (
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
        renderReauthCancel={(cancel, busy) => (
          <button
            type="button"
            onClick={cancel}
            disabled={busy}
            className="mt-3 cursor-pointer text-[13px] text-muted underline-offset-2 hover:text-text hover:underline focus-ring disabled:cursor-not-allowed disabled:opacity-40"
            data-testid="kiro-sign-in-keep-current"
          >
            {i18nT('pages.developer.kiroSignInCard.keep_current_sign_in')}
          </button>
        )}
      />
    </>
  )
  if (compact) {
    return (
      <details
        open={open}
        aria-label={title}
        // `onToggle` also fires for the `open` prop's own changes; recording only a
        // value that differs keeps the default in charge until the person clicks.
        onToggle={e => {
          const next = e.currentTarget.open
          if (next !== open) setUserOpen(next)
        }}
        className="border-t border-border pt-3"
        data-setting-key={KIRO_SIGN_IN_HIGHLIGHT_ANCHOR}
        data-setting-id={KIRO_SIGN_IN_HIGHLIGHT_ANCHOR}
        data-setting-label={i18nT('pages.developer.kiroSignInCard.search_label')}
        data-testid="kiro-sign-in-card"
      >
        {/* Block, not flex: a flex summary loses the native disclosure triangle,
            the one cue that the heading toggles. The heading inside keeps the
            heading-navigation stop the section form had. */}
        <summary className="cursor-pointer text-[13px] text-text-strong focus-ring">
          <h3 className="m-0 inline text-[13px] font-semibold">
            <KeyRound className="lucide-inline mr-1.5" aria-hidden="true" />
            {title}
          </h3>
          {/* Closed only: the open body states the same sign-in itself. */}
          {!open && signedIn && observed && (
            <span data-testid="kiro-sign-in-fold-state" className="ml-1.5 font-normal text-muted">
              {'· '}
              {i18nT('pages.developer.kiroSignInCard.signed_in_with', {
                provider: providerLabelFromWire(observed.provider),
              })}
              {account.hidden ? null : <AccountName {...account} />}
            </span>
          )}
          {!open && !signedIn && notice === 'using_kiro_cli' && (
            <span data-testid="kiro-sign-in-fold-state" className="ml-1.5 font-normal text-muted">
              {'· '}
              {i18nT('pages.developer.kiroSignInCard.using_kiro_cli_title')}
            </span>
          )}
        </summary>
        <div className="mt-2">{body}</div>
      </details>
    )
  }
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
