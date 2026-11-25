import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, ArrowLeft, CheckCircle2, Eye, EyeOff, Loader2, LogOut, RefreshCw } from 'lucide-react'
import { emailInitials } from '../../lib/emailInitials'
import { LookingGhost } from '../../assets/onboarding/LookingGhost'
import { api, type KasLoginSources, type KasLoginStatus } from '../../api/client'
import { isPermissionRefusal } from '../../api/apiError'
import { isAccountOnly, parseKiroUsagePayload } from '../../api/kiroUsage'
import { kasInUse, type AcpBackendConfig } from '../../api/acpBackend'
import {
  KasLoginEmbedded,
  SIGN_IN_TEXT_BUTTON,
  SignInBackContext,
  type SignInBack,
  type SignInBackSlot,
} from '../../components/KasLoginGate'
import { KiroSignInDialog, KiroSignInFlow } from '../../components/KiroSignInDialog'
import { useKasLoginStatus } from '../../hooks/useKasLogin'
import { providerLabelFromWire } from '../../components/kasProviderLabel'
import { KIRO_ACCOUNT_EMAIL_HIDDEN_KEY } from '../../components/KiroAccountModal'
import { usePersistedBool } from '../../hooks/usePersistedBool'
import { useSignedInAccount } from './kiroSignedInAccount'
import ErrorNotice from '../../components/ErrorNotice'
import { reportForError } from '../../utils/errorReport'
import { failedWithNoData } from '../../api/queryState'
import { Btn } from '../../components/ui'
import { KIRO_SIGN_IN_HIGHLIGHT_ANCHOR } from '../../hooks/useSettingHighlight'
import { i18nT } from '../../i18n/t'
import { fmtDateTime, fmtRelative } from '../../i18n/format'
import { signInLapsed } from './kiroSignInState'
import { KiroAccountUnlocksList } from './KiroAccountUnlocks'
import {
  SignInStoreUnreadableErrors,
  KiroSignInSourcesNotice,
  noticeLeadsCard,
  SOURCES_QUERY_KEY,
  sourcesNotice,
  useKiroSignInSources,
  useKiroSignOut,
} from './KiroSignInSources'

// Same cache key the embedded flow reads status under (KasLoginGate.QUERY_KEY):
// a sign-out here must invalidate the flow's query, not a private copy of it.
const STATUS_QUERY_KEY = ['kas-login'] as const

// The account header's avatar disc: the same size whichever state it shows.
const AVATAR = 'flex h-20 w-20 items-center justify-center rounded-full'

/** The avatar above the account's name: its initials, a warning on a lapsed
 *  sign-in, a gray placeholder while the name may still arrive, else the ghost. */
function AccountAvatar({ lapsed, name, pending }: { lapsed: boolean; name: string; pending: boolean }) {
  if (lapsed) {
    return (
      <div className={`${AVATAR} border border-warn/40 bg-warn/10 text-warn-text`} aria-hidden="true">
        <AlertTriangle className="h-8 w-8" />
      </div>
    )
  }
  if (name) {
    return (
      <div className={`${AVATAR} border border-accent/30 bg-accent/10 text-accent`} aria-hidden="true">
        <span className="text-[24px] font-semibold tracking-[0.04em]">{emailInitials(name)}</span>
      </div>
    )
  }
  if (pending) return <div className={`${AVATAR} animate-pulse bg-border`} data-testid="kiro-account-avatar-placeholder" aria-hidden="true" />
  return <LookingGhost size={72} />
}

/**
 * The account's name as the page title. A hidden e-mail is blurred like the
 * account popup's, beside the same Show/Hide e-mail toggle, so the page itself
 * can reveal it; an account name is never hidden.
 */
function AccountName({ name, hidden, isEmail }: { name: string; hidden: boolean; isEmail: boolean }) {
  const [, setEmailHidden] = usePersistedBool(KIRO_ACCOUNT_EMAIL_HIDDEN_KEY, true)
  const toggleLabel = i18nT(hidden ? 'components.kiroAccountModal.show_email' : 'components.kiroAccountModal.hide_email')
  return (
    <span className="inline-flex max-w-full items-center gap-2" data-testid="kiro-sign-in-account">
      <span
        data-account-hidden={hidden ? 'true' : undefined}
        className={`min-w-0 break-all ${hidden ? 'select-none blur-[6px] opacity-60' : ''}`}
      >
        {name}
      </span>
      {isEmail ? (
        <button
          type="button"
          onClick={() => setEmailHidden(!hidden)}
          data-testid="kiro-account-email-toggle"
          className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-full border border-border text-muted transition-colors hover:border-accent/35 hover:bg-accent/10 hover:text-accent focus-ring-accent"
          aria-label={toggleLabel}
          title={toggleLabel}
        >
          {hidden ? <Eye className="lucide-inline" aria-hidden="true" /> : <EyeOff className="lucide-inline" aria-hidden="true" />}
        </button>
      ) : null}
    </span>
  )
}

/**
 * The kiro-cli account's name while sessions run on its sign-in: the top bar's
 * `['kiro-usage']` reading first (the identity that came with the balance the
 * Credits card shows), else the e-mail `/api/kas-login/sources` read.
 * `enabled: false`: the top bar owns that read, this only observes it. `''`
 * when neither names it.
 */
function useKiroCliAccountName(sources: KasLoginSources | undefined): { name: string; isEmail: boolean } {
  const { data } = useQuery({
    queryKey: ['kiro-usage'],
    queryFn: () => api.sessionsUsage().then(parseKiroUsagePayload),
    enabled: false,
  })
  // On Kiro CLI, an unnamed source may run on a key or another data home;
  // the usage cache can still name the default store's unrelated login.
  if (sources?.kiro_cli_harness && !(
    sources.kiro_cli.email || sources.kiro_cli.account_type || sources.kiro_cli.portal_host
  )) return { name: '', isEmail: false }
  if (data && typeof data === 'object' && !isAccountOnly(data)) {
    if (data.email) return { name: data.email, isEmail: true }
    if (data.account) return { name: data.account, isEmail: false }
  }
  if (sources?.kiro_cli.email) return { name: sources.kiro_cli.email, isEmail: true }
  return { name: '', isEmail: false }
}

/**
 * The header while Kiro Crew is signed out and sessions run on kiro-cli's
 * login: that account's initials and name, the way `SignedInSummary` shows
 * Crew's own. With no name known, the title says "Using your Kiro CLI sign-in".
 */
function KiroCliSignedInHeader({ name, isEmail }: { name: string; isEmail: boolean }) {
  const [emailHidden] = usePersistedBool(KIRO_ACCOUNT_EMAIL_HIDDEN_KEY, true)
  return (
    <div className="mb-6 flex flex-col items-center text-center" data-testid="kiro-sign-in-hero" data-hero="kiro-cli">
      <AccountAvatar lapsed={false} name={name} pending={false} />
      <h2 className="mt-4 max-w-full text-2xl font-bold tracking-tight text-text-strong" data-testid="kiro-sign-in-state">
        {name
          ? <AccountName name={name} hidden={isEmail && emailHidden} isEmail={isEmail} />
          : i18nT('pages.developer.kiroSignInCard.using_kiro_cli_title')}
      </h2>
    </div>
  )
}

/**
 * The signed-in account header: avatar, the account's name as the title (gray
 * placeholders while it may still arrive with the credit reading), "Signed in
 * with <provider>", then Switch account and Sign out, neither of them red. A
 * lapsed sign-in says so instead, with Sign in again as the one filled button.
 * Never a token.
 *
 * `secondary`: the Kiro CLI harness runs on kiro-cli's sign-in, which the
 * notice above names, so the one stored here is not the account in use. It
 * shows as one small line naming it as Kiro Agent's, with no avatar or title.
 */
function SignedInSummary({ status, onReauth, secondary = false }: { status: KasLoginStatus; onReauth: () => void; secondary?: boolean }) {
  const logout = useKiroSignOut(status.identity ?? '')
  const lapsed = signInLapsed(status)
  const provider = providerLabelFromWire(status.provider)
  const account = useSignedInAccount()
  const pending = !lapsed && !account.name && account.pending
  // Same config read (and cache key) as the app-level notice: when nothing runs
  // on KAS the agents keep running, so a lapse stops only the account features.
  // Until it answers, a lapsed card says only its title: a guessed body could
  // name the wrong thing as stopped.
  const cfgQuery = useQuery<AcpBackendConfig>({ queryKey: ['kirocrewConfig'], queryFn: () => api.kirocrewConfig() })
  const cfg = cfgQuery.data
  const lapsedBody = !lapsed || cfg === undefined
    ? null
    : i18nT(kasInUse(cfg)
      ? 'pages.developer.kiroSignInCard.sign_in_expired_body'
      : 'pages.developer.kiroSignInCard.sign_in_expired_body_account')
  let title: ReactNode
  if (lapsed) title = i18nT('pages.developer.kiroSignInCard.sign_in_expired')
  else if (account.name) title = <AccountName name={account.name} hidden={account.hidden} isEmail={account.isEmail} />
  else if (pending) {
    title = (
      <span
        className="inline-block h-7 w-56 max-w-full animate-pulse rounded-md bg-border align-middle"
        role="img"
        aria-label={i18nT('pages.developer.kiroSignInCard.loading_account')}
        data-testid="kiro-account-name-placeholder"
      />
    )
  } else title = i18nT('pages.developer.kiroSignInCard.signed_in_with', { provider })
  return (
    <div data-testid="kiro-sign-in-summary" data-lapsed={lapsed ? 'true' : 'false'} className="flex flex-col items-center text-center">
      {secondary ? (
        <p className="mt-2 max-w-full text-[13px] text-muted" data-testid="kiro-sign-in-state">
          {lapsed
            ? i18nT('pages.developer.kiroSignInCard.kiro_agent_sign_in_expired')
            : i18nT('pages.developer.kiroSignInCard.kiro_agent_sign_in', { provider })}
        </p>
      ) : (
        <>
          <AccountAvatar lapsed={lapsed} name={account.name} pending={pending} />
          <h2 className="mt-4 max-w-full text-2xl font-bold tracking-tight text-text-strong" data-testid="kiro-sign-in-state">
            {title}
          </h2>
        </>
      )}
      {/* The provider has its own line only when the title names the account. */}
      {!secondary && !lapsed && (account.name || pending) ? (
        <div className="mt-2 inline-flex items-center gap-1.5 rounded-full border border-border bg-bg px-2.5 py-1 text-[12px] text-muted" data-testid="kiro-sign-in-provider">
          <CheckCircle2 className="lucide-inline text-ok-text" aria-hidden="true" />
          {i18nT('app.signed_in_with', { provider })}
        </div>
      ) : null}
      {/* A live sign-in with nothing to renew it: the only signal that it will
          lapse on its own, so it is the one expiry note this header keeps. */}
      {!lapsed && status.has_refresh_token === false ? (
        <p className="mt-2 max-w-sm text-[12px] text-muted" data-testid="kiro-sign-in-no-refresh">
          {i18nT('pages.developer.kiroSignInCard.signed_in_body_no_refresh')}
        </p>
      ) : null}
      {!lapsed && status.has_refresh_token === false && status.expires_at ? (
        <p className="mt-1 text-[12px] text-muted" data-testid="kiro-sign-in-expiry">
          {i18nT('pages.developer.kiroSignInCard.access_token_expires', {
            when: fmtRelative(status.expires_at),
            at: fmtDateTime(status.expires_at),
          })}
        </p>
      ) : null}
      {lapsedBody ? <p className="mt-2 max-w-sm text-[13px] leading-relaxed text-muted">{lapsedBody}</p> : null}
      {/* The config read failed with nothing cached: say so, as the app-level
          notice would (it is hidden on this tab). askAgent on: this page
          holds no draft. */}
      {lapsed && failedWithNoData(cfgQuery) ? (
        <ErrorNotice
          className="mt-2 w-full text-left"
          askAgent
          title={i18nT('components.kiroSignInNotice.config_unavailable')}
          message={i18nT('components.kiroSignInNotice.config_unavailable_detail')}
          report={reportForError(cfgQuery.error)}
          testId="kiro-sign-in-config-error"
        />
      ) : null}
      <div className="mt-5 flex flex-wrap items-center justify-center gap-2">
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
        <Btn type="button" disabled={logout.isPending} onClick={() => logout.mutate()} data-testid="kiro-sign-in-logout">
          {logout.isPending ? (
            <Loader2 className="lucide-inline animate-spin" aria-hidden="true" />
          ) : (
            <LogOut className="lucide-inline" aria-hidden="true" />
          )}
          {i18nT('pages.developer.kiroSignInCard.sign_out')}
        </Btn>
      </div>
      {/* askAgent on: the page holds no draft (sign-out is a single press), and
          an unwritable or corrupt vault store is exactly the kind of failure the
          agent can diagnose (`kirocrew doctor`, permissions on the data home). */}
      <ErrorNotice
        className="mt-3 w-full text-left"
        askAgent
        title={i18nT('pages.developer.kiroSignInCard.could_not_sign_out')}
        message={logout.error?.message || null}
        testId="kiro-sign-in-logout-error"
      />
    </div>
  )
}

/**
 * The card's body: a signed-in view, or the shared sign-in choices in place
 * (`KiroSignInFlow`, the same content the chat's dialog wraps). Switching account
 * and signing in again open the shared sign-in dialog (`KiroSignInDialog`);
 * closing it backs out. A failed status read shows the shared flow's own error
 * and Check again.
 */
function SignInBody({
  renderSignedIn,
}: {
  renderSignedIn: (status: KasLoginStatus, reauth: () => void) => ReactNode
}) {
  // This card is what keeps the shared status fresh while Settings is open.
  const statusQuery = useKasLoginStatus({ poll: 30_000 })
  const [reauthOpen, setReauthOpen] = useState(false)
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
  } else {
    // One flow for both states: it stays mounted when a sign-in lands, so the
    // landed result settles (caches dropped) before the summary replaces the
    // choices. The choices keep the dialog's width on a wide card.
    // Signed out, what the account unlocks sits under the choices, in the
    // same centered column. The wrapper stays one element either way, so the
    // flow is not remounted when the sign-in lands.
    const signedOut = !status.authenticated
    content = (
      <div className={signedOut ? 'mx-auto flex w-full max-w-[320px] flex-col gap-6' : undefined}>
        <div data-testid={signedOut ? 'kiro-sign-in-inline' : undefined}>
          <KiroSignInFlow renderSignedIn={signedIn => renderSignedIn(signedIn, () => setReauthOpen(true))} />
        </div>
        {signedOut ? <KiroAccountUnlocksList /> : null}
      </div>
    )
  }
  return (
    <>
      {content}
      <KiroSignInDialog open={reauthOpen} onOpenChange={setReauthOpen} reauth />
    </>
  )
}

/**
 * Settings > Kiro account: one centered column for Kiro Crew's OWN Kiro
 * identity -- the one the gateway hands to agent processes (KAS relay) and to
 * the other services that run as the user's Kiro account, without depending on
 * `kiro-cli login`. It shows only what the current state needs. Signed out:
 * the ghost, "Sign in to Kiro", one line, and the sign-in choices the dialog
 * also carries (`KiroSignInFlow`, through `KasLoginEmbedded`'s card chrome); in
 * the device-code step the header says "Finish in your browser" and "Use another
 * way" sits above the code screen, which leaves its own heading to this header
 * (`SignInBackSlot.ownsHeading`). Signed in: the account header
 * (`SignedInSummary`), with an explicit "sign-in expired" state that asks the
 * user to sign in again instead of quietly falling back to kiro-cli's login.
 * Signed out while sessions run on kiro-cli's login (`using_kiro_cli`): the
 * header names that account (`KiroCliSignedInHeader`), the notice under it says
 * signing in here is optional, and the sign-in choices show in place below.
 * SettingsPage draws the Credits card under it while signed in.
 *
 * Rendered by SettingsPage on its own tab while the build offers Kiro sign-in
 * (`useKiroSignInOffered`), whichever harness is selected. People look for
 * sign-out and account switching in the account popup and in Settings search,
 * so both link HERE instead of carrying a second copy of the flow: the Kiro
 * account popup's "Sign out or switch account" and the search entry
 * (`KIRO_SIGN_IN_SETTING_ID`), which waits on the `data-setting-id` this
 * section carries.
 */
export function KiroSignInCard() {
  // The code screen hands its way back here while it shows: the header then
  // names the step, and "Use another way" sits above the screen.
  const [back, setBack] = useState<SignInBack | null>(null)
  const backSlot = useMemo<SignInBackSlot>(() => ({ set: setBack, ownsHeading: true }), [])
  // Observe the flow's status (same cache key, no extra fetch: the flow owns
  // the polling) to know whether the signed-out header shows.
  const { data: observed } = useQuery({
    queryKey: STATUS_QUERY_KEY,
    queryFn: api.kasLoginStatus,
    enabled: false,
  })
  const authenticated = observed?.authenticated === true
  // Which stored sign-in sessions run as, read apart from the status above so a
  // slow answer never holds the page's first paint back. Re-read whenever a
  // sign-in lands or leaves: both change which account that is.
  const queryClient = useQueryClient()
  const { data: sources, error: sourcesError } = useKiroSignInSources()
  const notice = sourcesNotice(sources)
  const lastAuthenticated = useRef<boolean | null>(null)
  useEffect(() => {
    if (observed === undefined) return
    if (lastAuthenticated.current !== null && lastAuthenticated.current !== authenticated) {
      void queryClient.invalidateQueries({ queryKey: SOURCES_QUERY_KEY })
    }
    lastAuthenticated.current = authenticated
  }, [authenticated, observed, queryClient])
  const signedOut = observed !== undefined && !authenticated
  // Signed out here while sessions run on kiro-cli's login: the header names that
  // account, and this app's own sign-in choices still show in place under it.
  // The code step keeps the normal header, which names the step.
  const cliAccount = useKiroCliAccountName(sources)
  const cliHeader = signedOut && (notice === 'using_kiro_cli' || notice === 'kiro_cli_harness') && !back
  return (
    <section
      data-setting-key={KIRO_SIGN_IN_HIGHLIGHT_ANCHOR}
      data-setting-id={KIRO_SIGN_IN_HIGHLIGHT_ANCHOR}
      data-setting-label={i18nT('pages.developer.kiroSignInCard.search_label')}
      data-testid="kiro-sign-in-card"
      className="flex flex-col items-center"
    >
      {cliHeader ? (
        <KiroCliSignedInHeader name={cliAccount.name} isEmail={cliAccount.isEmail} />
      ) : signedOut ? (
        <div className="mb-6 flex flex-col items-center text-center" data-testid="kiro-sign-in-hero">
          <LookingGhost size={72} />
          <h2 className="mt-4 text-2xl font-bold tracking-tight text-text-strong">
            {i18nT(back ? 'components.kasLogin.finish_in_browser' : 'components.kasLogin.sign_in_to_kiro')}
          </h2>
          <p className="mt-1 text-sm text-muted">
            {i18nT(back ? 'pages.developer.kiroSignInCard.code_step_hint' : 'pages.developer.kiroSignInCard.signed_out_hint')}
          </p>
        </div>
      ) : null}
      {noticeLeadsCard(notice) ? (
        // The header already says "Using your Kiro CLI sign-in" when it has no name.
        <KiroSignInSourcesNotice sources={sources} identity="" hideTitle={cliHeader && !cliAccount.name} />
      ) : null}
      {/* No hand-off: the embedded Company SSO form can hold an unsaved portal URL and region. */}
      <ErrorNotice
        className="mb-3 w-full"
        title={i18nT('pages.developer.kiroSignInCard.could_not_read_sources')}
        // Not the owner: refused like the status read below, which says so.
        message={isPermissionRefusal(sourcesError) ? null : sourcesError?.message || null}
        testId="kiro-sign-in-sources-error"
      />
      <SignInStoreUnreadableErrors sources={sources} />
      {back ? (
        <div className="mx-auto w-full max-w-[320px]">
          <Btn
            type="button"
            disabled={back.disabled}
            onClick={back.onBack}
            className={`mb-3 ${SIGN_IN_TEXT_BUTTON}`}
            data-testid="kas-login-back-to-options"
          >
            <ArrowLeft size={14} aria-hidden="true" />
            {i18nT('components.kasLogin.use_another_way')}
          </Btn>
        </div>
      ) : null}
      <SignInBackContext.Provider value={backSlot}>
        <div className="w-full">
          <SignInBody
            renderSignedIn={(status, reauth) => (
              <>
                {!noticeLeadsCard(notice) ? (
                  <KiroSignInSourcesNotice sources={sources} identity={status.identity ?? ''} />
                ) : null}
                <SignedInSummary status={status} onReauth={reauth} secondary={sources?.kiro_cli_harness === true} />
              </>
            )}
          />
        </div>
      </SignInBackContext.Provider>
    </section>
  )
}
