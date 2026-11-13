import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Info, Loader2 } from 'lucide-react'
import { api, type KasLoginSourceAccount, type KasLoginSources } from '../../api/client'
import { accountProviderLabel } from '../../components/kasProviderLabel'
import { KIRO_ACCOUNT_EMAIL_HIDDEN_KEY } from '../../components/KiroAccountModal'
import { usePersistedBool } from '../../hooks/usePersistedBool'
import { clearCachedModels } from '../../providers/adapters/acp'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn } from '../../components/ui'
import { i18nT } from '../../i18n/t'

/** Cache key of `GET /api/kas-login/sources`: which stored sign-in sessions run as. */
export const SOURCES_QUERY_KEY = ['kas-login-sources'] as const
// Same key the sign-in flow reads status under (KasLoginGate.QUERY_KEY).
const STATUS_QUERY_KEY = ['kas-login'] as const
// How often to ask again while the gateway is still reading kiro-cli's account name.
const NAME_POLL_MS = 2000
// How often to ask again otherwise: a `kiro-cli login` or a sign-in made in another
// tab changes the answer without this card seeing it happen.
const SOURCES_REREAD_MS = 60_000

/**
 * Which stored sign-in sessions run as. A read of its own, apart from the sign-in
 * status the card paints from, so a slow answer never holds the card back. Asks
 * again quickly while the gateway reports the kiro-cli account name as still being
 * read, and slowly after that.
 */
export function useKiroSignInSources() {
  return useQuery({
    queryKey: SOURCES_QUERY_KEY,
    queryFn: api.kasLoginSources,
    refetchInterval: query => (query.state.data?.name_pending ? NAME_POLL_MS : SOURCES_REREAD_MS),
  })
}

/**
 * Remove the sign-in stored in this app (the card's Sign out, and "Use the Kiro
 * CLI account"). One mutation for both, so each leaves the same state behind.
 */
export function useKiroSignOut(identity: string, onSignedOut: () => void) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => api.kasLoginLogout(identity),
    onSuccess: () => {
      onSignedOut()
      void queryClient.invalidateQueries({ queryKey: STATUS_QUERY_KEY })
      void queryClient.invalidateQueries({ queryKey: SOURCES_QUERY_KEY })
      // The signed-out account's model list goes with it: the gateway forgot its
      // catalog on this sign-out, and `['available-models']` would otherwise stay
      // fresh (global `staleTime: Infinity`) until a session spawns.
      clearCachedModels()
      void queryClient.resetQueries({ queryKey: ['available-models'] })
      // So does its usage reading, which names it: no stale name on the next sign-in.
      void queryClient.resetQueries({ queryKey: ['kiro-usage'] })
    },
  })
}

export type SourcesNotice = 'using_kiro_cli' | 'two_accounts' | 'maybe_two_accounts'

/** What the card has to say about the two stored sign-ins, or `null` for nothing. */
export function sourcesNotice(sources: KasLoginSources | undefined): SourcesNotice | null {
  if (!sources) return null
  if (sources.active === 'kiro_cli') return 'using_kiro_cli'
  if (sources.active !== 'kiro_crew') return null
  // `usable` is false for a kiro-cli sign-in no session could run on (the backend
  // decides by edition: on LTS an expired access token; on oss only none stored).
  if (sources.kiro_cli.stored !== true || !sources.kiro_cli.usable) return null
  if (sources.same_account === false) return 'two_accounts'
  if (sources.same_account === null) return 'maybe_two_accounts'
  return null
}

/**
 * An account as the card names it: its e-mail, else its kind and portal, else `null`
 * (no name). `emailHidden`: the account popup's hide toggle is on, so the e-mail is
 * left out and the kind and portal name the account instead.
 */
function accountName(account: KasLoginSourceAccount, pending: boolean, emailHidden: boolean): string | null {
  if (account.email && !emailHidden) return account.email
  const label = accountProviderLabel(
    account.account_type,
    account.portal_host ? `https://${account.portal_host}` : undefined,
  )
  if (label) return label
  return pending ? i18nT('pages.developer.kiroSignInCard.account_name_checking') : null
}

/**
 * The failed read of kiro-cli's account name, as an error rather than a sentence.
 * Inline, not the boxed banner: Kiro CLI is still signed in, so a missing name is
 * a quiet line beside the account notice rather than an alarm over its button.
 * `besideChooser`: rendered where the sign-in chooser is mounted too (nothing is
 * stored here), so the hand-off is withheld; otherwise it hands off.
 */
function AccountNameError({ sources, besideChooser }: { sources: KasLoginSources; besideChooser: boolean }) {
  const title = i18nT('pages.developer.kiroSignInCard.could_not_read_account_name')
  const message = sources.name_unreadable
    ? i18nT('pages.developer.kiroSignInCard.account_name_unreadable_detail')
    : null
  if (besideChooser) {
    return (
      // No hand-off: the sign-in chooser beside it holds the Company SSO form's unsaved portal URL and region.
      <ErrorNotice className="mt-3" variant="inline" messagePlacement="below" title={title} message={message} testId="kiro-sign-in-account-name-error" />
    )
  }
  // askAgent on: signed in, the chooser is not mounted (a switch of account
  // replaces this notice with it), so the hand-off loses nothing; a `kiro-cli
  // whoami` that fails is something the agent can look into.
  return <ErrorNotice className="mt-3" variant="inline" messagePlacement="below" askAgent title={title} message={message} testId="kiro-sign-in-account-name-error" />
}

/**
 * A sign-in store that could not be read (`stored === null`): the one stored in
 * this app, kiro-cli's, or both, one error each, naming the store. Not "signed
 * out": whether that store holds a sign-in is unknown. Shown apart from the
 * account notice, which has nothing to say about an unknown store, and leaves the
 * card's sign-in status and controls as they are.
 */
export function SignInStoreUnreadableErrors({ sources }: { sources: KasLoginSources | undefined }) {
  return (
    <>
      {/* No hand-off: the embedded Company SSO form can hold an unsaved portal URL and region. */}
      <ErrorNotice
        className="mb-3"
        message={sources?.kiro_crew.stored === null ? i18nT('pages.developer.kiroSignInCard.could_not_check_kiro_crew') : null}
        testId="kiro-sign-in-kiro-crew-unreadable"
      />
      {/* No hand-off: the embedded Company SSO form can hold an unsaved portal URL and region. */}
      <ErrorNotice
        className="mb-3"
        message={sources?.kiro_cli.stored === null ? i18nT('pages.developer.kiroSignInCard.could_not_check_kiro_cli') : null}
        testId="kiro-sign-in-kiro-cli-unreadable"
      />
    </>
  )
}

/**
 * Says which account Kiro Agent runs as when kiro-cli is signed in too:
 * - nothing stored here: sessions use the kiro-cli sign-in, named;
 * - both stored for different accounts (or not provably the same): names both and
 *   offers to remove the one stored here, so sessions go back to kiro-cli's.
 * Removing asks once, in place, and is undone by signing in here again.
 */
export function KiroSignInSourcesNotice({
  sources,
  identity,
  onRemoved,
}: {
  sources: KasLoginSources | undefined
  /** The stored sign-in's slot (`KasLoginStatus.identity`), which the removal names. */
  identity: string
  onRemoved: () => void
}) {
  const kind = sourcesNotice(sources)
  const [confirming, setConfirming] = useState(false)
  const remove = useKiroSignOut(identity, onRemoved)
  const [emailHidden] = usePersistedBool(KIRO_ACCOUNT_EMAIL_HIDDEN_KEY, true)
  if (!sources || !kind) return null
  const cli = accountName(sources.kiro_cli, sources.name_pending, emailHidden)
  if (kind === 'using_kiro_cli') {
    return (
      <div
        className="mb-3 flex items-start gap-3"
        role="status"
        data-testid="kiro-sign-in-sources"
        data-notice={kind}
      >
        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-ok/15 text-ok-text" aria-hidden="true">
          <Info className="lucide-inline" />
        </div>
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium text-text-strong">
            {i18nT('pages.developer.kiroSignInCard.using_kiro_cli_title')}
          </p>
          <p className="mt-0.5 text-[13px] leading-relaxed text-muted">
            {cli
              ? i18nT('pages.developer.kiroSignInCard.using_kiro_cli_body', { account: cli })
              : i18nT('pages.developer.kiroSignInCard.using_kiro_cli_body_unnamed')}
          </p>
          <AccountNameError sources={sources} besideChooser />
        </div>
      </div>
    )
  }
  const here = accountName(sources.kiro_crew, false, emailHidden)
  // A missing name, or two that read the same ("Builder ID" beside "Builder ID"),
  // names no one: say where each sign-in lives instead.
  const byPlace = !here || !cli || here === cli
  return (
    <div className="mb-4" role="status" data-testid="kiro-sign-in-sources" data-notice={kind}>
      <div className="flex items-start gap-3">
        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-warn/15 text-warn-text" aria-hidden="true">
          <AlertTriangle className="lucide-inline" />
        </div>
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium text-text-strong">
            {kind === 'two_accounts'
              ? i18nT('pages.developer.kiroSignInCard.two_accounts_title')
              : i18nT('pages.developer.kiroSignInCard.maybe_two_accounts_title')}
          </p>
          <p className="mt-0.5 text-[13px] leading-relaxed text-muted">
            {byPlace
              ? kind === 'two_accounts'
                ? i18nT('pages.developer.kiroSignInCard.two_accounts_body_by_place')
                : i18nT('pages.developer.kiroSignInCard.maybe_two_accounts_body_by_place')
              : kind === 'two_accounts'
                ? i18nT('pages.developer.kiroSignInCard.two_accounts_body', { here, cli })
                : i18nT('pages.developer.kiroSignInCard.maybe_two_accounts_body', { here, cli })}
          </p>
          <AccountNameError sources={sources} besideChooser={false} />
          {sources.can_use_kiro_cli && !confirming ? (
            <>
              <Btn
                type="button"
                className="mt-2"
                onClick={() => setConfirming(true)}
                data-testid="kiro-sign-in-use-kiro-cli"
              >
                {i18nT('pages.developer.kiroSignInCard.use_kiro_cli_account')}
              </Btn>
              <p className="mt-1 text-[12px] text-muted">
                {i18nT('pages.developer.kiroSignInCard.use_kiro_cli_removes')}
              </p>
            </>
          ) : null}
          {sources.can_use_kiro_cli && confirming ? (
            <div className="mt-2" role="group" aria-label={i18nT('pages.developer.kiroSignInCard.use_kiro_cli_account')}>
              <p className="text-[13px] leading-relaxed text-text" data-testid="kiro-sign-in-use-kiro-cli-explain">
                {i18nT('pages.developer.kiroSignInCard.use_kiro_cli_explain')}
              </p>
              <div className="mt-2 flex flex-wrap items-center gap-2">
                <Btn
                  type="button"
                  danger
                  disabled={remove.isPending}
                  onClick={() => remove.mutate()}
                  data-testid="kiro-sign-in-remove"
                >
                  {remove.isPending ? <Loader2 className="lucide-inline animate-spin" aria-hidden="true" /> : null}
                  {i18nT('pages.developer.kiroSignInCard.remove_this_sign_in')}
                </Btn>
                {/* Focus lands on the choice that changes nothing. */}
                <Btn
                  type="button"
                  autoFocus
                  disabled={remove.isPending}
                  onClick={() => setConfirming(false)}
                  data-testid="kiro-sign-in-keep"
                >
                  {i18nT('pages.developer.kiroSignInCard.keep_it')}
                </Btn>
              </div>
            </div>
          ) : null}
        </div>
      </div>
      <ErrorNotice
        className="mt-3"
        askAgent
        title={i18nT('pages.developer.kiroSignInCard.could_not_sign_out')}
        message={remove.error?.message || null}
        testId="kiro-sign-in-remove-error"
      />
    </div>
  )
}
