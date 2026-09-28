import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, CheckCircle2, KeyRound, Loader2, LogOut, RefreshCw } from 'lucide-react'
import { api, type KasLoginStatus } from '../../api/client'
import { KasLoginEmbedded, providerLabelFromWire } from '../../components/KasLoginGate'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn, Card, CardTitle } from '../../components/ui'
import { KIRO_SIGN_IN_HIGHLIGHT_ANCHOR } from '../../hooks/useSettingHighlight'
import { fmtDateTime, fmtRelative } from '../../i18n/format'
import { i18nT } from '../../i18n/t'

// Same cache key the embedded flow reads status under (KasLoginGate.QUERY_KEY):
// a sign-out here must invalidate the flow's query, not a private copy of it.
const STATUS_QUERY_KEY = ['kas-login'] as const

/**
 * Whether the stored sign-in can still do its job. Two independent verdicts
 * from the gateway, both token-free: `usable` is the spawn-time predicate (an
 * expired access token with nothing to renew it), `refresh_rejected` is the
 * issuer's refusal recorded by the last refresh attempt. Either one means the
 * agents will be told they are not signed in until the user signs in again --
 * and NOTHING falls back to kiro-cli's login on its own, which is why the card
 * has to say so rather than let a live-looking account sit there.
 */
function signInLapsed(status: KasLoginStatus): boolean {
  return !status.usable || status.refresh_rejected
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
  const queryClient = useQueryClient()
  const logout = useMutation({
    mutationFn: () => api.kasLoginLogout(status.identity ?? ''),
    onSuccess: () => {
      onSignedOut()
      void queryClient.invalidateQueries({ queryKey: STATUS_QUERY_KEY })
    },
  })
  const lapsed = signInLapsed(status)
  const provider = providerLabelFromWire(status.provider)
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
            lapsed ? 'bg-warn/15 text-warn' : 'bg-ok/15 text-ok'
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
 * Developer > Agent Backend card for signing in to Kiro Crew's OWN Kiro identity
 * -- the one the gateway hands to agent processes (KAS relay) so they run as the
 * user without depending on `kiro-cli login`. Wraps the sign-in flow
 * `KasLoginGate` carries in its embedded chrome, and adds the two things a card
 * needs that a gate does not: a signed-in summary (provider, expiry,
 * renewability -- never a token) with sign-out, and an explicit "sign-in
 * expired" state that asks the user to sign in again instead of quietly falling
 * back to kiro-cli's login.
 *
 * Rendered by `AgentBackendTab` under the backend switch, and only while KAS is
 * a backend that switch offers: the stored identity is consumed by the KAS
 * relay alone, so on a build or policy that cannot select KAS there is nothing
 * to sign in for. That is also why it is NOT on Settings > Overview (its former
 * home) nor indexed into Settings search: KAS is a Developer Mode preview, and a
 * sign-in chooser on the landing page read as a required step to every user.
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
  useEffect(() => {
    if (authenticated) setSignedOutNote(false)
  }, [authenticated])
  return (
    <Card data-setting-key={KIRO_SIGN_IN_HIGHLIGHT_ANCHOR} data-testid="kiro-sign-in-card">
      <CardTitle>
        <KeyRound className="lucide-inline" aria-hidden="true" />
        {title}
      </CardTitle>
      {signedOutNote ? (
        <p className="mb-3 text-[12px] text-muted" role="status" data-testid="kiro-sign-in-takes-effect">
          {i18nT('pages.developer.kiroSignInCard.takes_effect_next_process')}
        </p>
      ) : null}
      <KasLoginEmbedded
        renderPending={() => (
          <p className="flex items-center gap-2 text-[13px] text-muted" data-testid="kiro-sign-in-pending">
            <Loader2 className="lucide-inline animate-spin" aria-hidden="true" />
            {i18nT('pages.developer.kiroSignInCard.checking')}
          </p>
        )}
        renderAuthenticated={(status, { reauth }) => (
          <SignedInSummary status={status} onReauth={reauth} onSignedOut={() => setSignedOutNote(true)} />
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
    </Card>
  )
}
