import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { RefreshCw, ShieldCheck } from 'lucide-react'
import { api, type AwsConsentStatus } from '../api/client'
import ErrorNotice from './ErrorNotice'
import { Btn } from './ui'
import { i18nT } from '../i18n/t'

/**
 * Confirmation gate for a paid AWS service (Amazon Polly, Amazon Transcribe).
 *
 * Renders inline in the settings card rather than as a blocking modal, and that
 * is deliberate: the confirmation is durable state the operator should be able
 * to SEE and withdraw at any time, not a one-shot dialog that disappears once
 * answered. A modal would also have nowhere to live for the case that matters
 * most -- a grant that was revoked automatically because the account changed.
 *
 * The three facts the issue asks for (service, region, credential source) come
 * from the backend along with the account it actually resolves to, because only
 * the backend can run the identity probe. Nothing here decides anything: the
 * backend refuses to record a confirmation it cannot attach an account to, so
 * this component cannot manufacture consent by rendering a button.
 *
 * `onConsentChange` exists because a grant gates data this component cannot
 * name. It invalidates its own status, while callers invalidate only the
 * service-specific surfaces they own. The AWS Control console, for example,
 * decides whether to show the ask from a cached 409 on the drive read and from
 * `costs.consentMissing`. Without a way to invalidate those too, withdrawing
 * leaves the operator with no receipt, no ask, and a stale drive row: nothing
 * on screen offers the confirm back. Callers whose surfaces do not depend on a
 * grant omit it.
 */
export default function AwsConsentGate({
  service,
  onConsentChange,
  compact = false,
  askAgent = false,
}: {
  service: string
  /** Invalidate caller-owned queries whose content depends on this grant. */
  onConsentChange?: () => void
  /**
   * Render a GRANTED receipt as one thin row instead of the full card. The ask
   * state ignores this: a confirmation that starts billing must keep its full
   * facts (service, region, credential source, account) regardless of where it
   * mounts. Receipts are records, not decisions, so a row is enough — the
   * withdraw stays reachable but no longer dominates the page.
   */
  compact?: boolean
  /**
   * Offer the agent hand-off on this gate's error notices. Off by default for
   * the same reason `ErrorNotice` defaults it off: the gate sits inside settings
   * panels that hold unsaved drafts, and only the HOST knows whether the
   * navigation would destroy one. A host with nothing to lose opts in.
   */
  askAgent?: boolean
}) {
  const qc = useQueryClient()
  const consentQ = useQuery<AwsConsentStatus>({
    queryKey: ['awsConsent', service],
    queryFn: () => api.awsConsent(service),
  })

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['awsConsent', service] })
    onConsentChange?.()
  }

  const grantMut = useMutation({
    // Send the values this render DISPLAYED, not whatever the server reads at
    // POST time: the backend 409s on a mismatch, so a confirmation cannot land
    // on an account the operator never saw.
    mutationFn: () =>
      api.grantAwsConsent(service, {
        profile: consentQ.data?.profile ?? '',
        region: consentQ.data?.region ?? '',
        account: consentQ.data?.account ?? '',
      }),
    onSettled: invalidate,
  })
  const revokeMut = useMutation({
    mutationFn: () => api.revokeAwsConsent(service),
    onSettled: invalidate,
  })

  // A status read that failed used to render NOTHING — indistinguishable from a
  // gate that has nothing to ask, on the surface that decides whether a paid
  // service may bill. The message is the transport's own (journaled), so the
  // notice recovers its endpoint and status from that. A transient read is the
  // one failure the reader can clear alone, and with the grant/withdraw
  // controls gone this notice is the card's only surface, so it carries the
  // retry itself rather than relying on the host to offer one.
  if (consentQ.isError) {
    return (
      <div className="flex flex-col items-start gap-2" data-testid={'aws-consent-' + service + '-error-card'}>
        <ErrorNotice
          message={consentQ.error instanceof Error ? consentQ.error.message : String(consentQ.error)}
          title={i18nT('components.awsConsentGate.status_failed')}
          askAgent={askAgent}
          className="w-full"
          testId={'aws-consent-' + service + '-error'}
        />
        <Btn onClick={() => consentQ.refetch()} data-testid={'aws-consent-' + service + '-error-retry'}>
          <RefreshCw size={13} />
          {i18nT('components.awsConsentGate.retry')}
        </Btn>
      </div>
    )
  }
  if (!consentQ.isSuccess) return null
  const c = consentQ.data
  const busy = grantMut.isPending || revokeMut.isPending
  // The write that last failed, if any. `onSettled` re-reads the status either
  // way, so the card below already shows the truth; this line says WHY the
  // click did not change it.
  const writeError = grantMut.error ?? revokeMut.error
  const writeNotice = (className: string) => writeError ? (
    <ErrorNotice
      message={writeError instanceof Error ? writeError.message : String(writeError)}
      title={i18nT(grantMut.error
        ? 'components.awsConsentGate.confirm_failed'
        : 'components.awsConsentGate.withdraw_failed')}
      askAgent={askAgent}
      className={className}
      testId={'aws-consent-' + service + '-write-error'}
    />
  ) : null
  const region = c.region || i18nT('components.awsConsentGate.provider_default')
  // Prefer the LIVE account, but fall back to the one the grant recorded. A
  // probe can fail for reasons that say nothing about the grant (no network, no
  // sandbox backend, expired SSO), and in that state the account the operator
  // actually confirmed is more useful than "could not be resolved" -- it is
  // also the account the gate is still enforcing against.
  const account = c.identityResolved ? c.account : c.grant?.account || ''

  // A granted receipt in compact mode is one row: the service, where it runs,
  // and the account it bills — with withdraw kept small on the right. The
  // account-changed warning still forces the full card, because that state
  // needs its sentence.
  if (compact && c.granted && !c.revokedOnAccountChange) {
    return (
      <div
        className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2 text-[13px]"
        data-testid={'aws-consent-' + service}
      >
        <ShieldCheck size={14} className="shrink-0 text-ok" aria-hidden="true" />
        <span className="font-medium text-text-strong">{c.serviceLabel}</span>
        <span className="text-muted">{region}</span>
        <span className="min-w-0 truncate font-mono text-[12px] text-muted">
          {account || i18nT('components.awsConsentGate.unresolved_account')}
        </span>
        <span className="flex-1" />
        <button
          className="cursor-pointer bg-transparent border-none p-0 text-[12px] text-muted underline hover:text-danger"
          disabled={busy}
          onClick={() => revokeMut.mutate()}
        >
          {i18nT('components.awsConsentGate.withdraw')}
        </button>
        {/* A full-width child of the row, so a refused withdraw is said under the
            receipt it failed to remove rather than breaking the row's layout. */}
        {writeNotice('basis-full')}
      </div>
    )
  }

  return (
    <div
      className={
        'rounded-md border px-3 py-2.5 text-[13px] ' +
        (c.granted ? 'border-border' : 'border-warn')
      }
      data-testid={'aws-consent-' + service}
    >
      <div className="font-medium mb-1">
        {c.granted
          ? i18nT('components.awsConsentGate.confirmed_title')
          : i18nT('components.awsConsentGate.confirm_title')}
      </div>

      {c.revokedOnAccountChange ? (
        <div className="text-warn mb-1.5">
          {i18nT('components.awsConsentGate.account_changed')}
        </div>
      ) : null}

      {/* One column by default, two from `sm` up. A translated label can be long
          ("Quelle der Anmeldedaten"), and an `auto` label column sized to it
          would leave the value column nothing at 320px. Stacking below `sm`
          gives each value the full width instead. */}
      <dl className="grid grid-cols-1 sm:grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 mb-2 text-muted">
        <dt>{i18nT('components.awsConsentGate.service')}</dt>
        <dd className="text-text min-w-0 break-all mb-1 sm:mb-0">{c.serviceLabel}</dd>
        <dt>{i18nT('components.awsConsentGate.region')}</dt>
        <dd className="text-text min-w-0 break-all mb-1 sm:mb-0">{region}</dd>
        <dt>{i18nT('components.awsConsentGate.credential_source')}</dt>
        <dd className="text-text min-w-0 break-all mb-1 sm:mb-0">{c.credentialSource}</dd>
        <dt>{i18nT('components.awsConsentGate.aws_account')}</dt>
        <dd className="text-text min-w-0 break-all">
          {account || i18nT('components.awsConsentGate.unresolved_account')}
        </dd>
      </dl>

      {!c.identityResolved && c.identityDetail && !c.granted ? (
        <div className="text-muted mb-2">{c.identityDetail}</div>
      ) : null}

      {c.granted ? (
        <button
          className="underline cursor-pointer bg-transparent border-none text-danger p-0"
          disabled={busy}
          onClick={() => revokeMut.mutate()}
        >
          {i18nT('components.awsConsentGate.withdraw')}
        </button>
      ) : (
        <>
          <div className="mb-2">{i18nT('components.awsConsentGate.billing_notice')}</div>
          <button
            className="rounded-md border border-border px-2 py-1 cursor-pointer bg-transparent disabled:opacity-50"
            disabled={busy || !c.identityResolved}
            onClick={() => grantMut.mutate()}
          >
            {i18nT('components.awsConsentGate.confirm_button')}
          </button>
        </>
      )}
      {writeNotice('mt-2')}
    </div>
  )
}
