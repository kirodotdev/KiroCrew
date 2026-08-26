import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api, type AwsConsentStatus } from '../api/client'
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
 */
export default function AwsConsentGate({ service }: { service: string }) {
  const qc = useQueryClient()
  const consentQ = useQuery<AwsConsentStatus>({
    queryKey: ['awsConsent', service],
    queryFn: () => api.awsConsent(service),
  })

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['awsConsent', service] })
    // The Polly voice catalogue is fetched through the same gate, so a grant
    // change flips that request between a real catalogue and a refusal.
    qc.invalidateQueries({ queryKey: ['voiceVoices'] })
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

  if (!consentQ.isSuccess) return null
  const c = consentQ.data
  const busy = grantMut.isPending || revokeMut.isPending
  const region = c.region || i18nT('components.awsConsentGate.provider_default')
  // Prefer the LIVE account, but fall back to the one the grant recorded. A
  // probe can fail for reasons that say nothing about the grant (no network, no
  // sandbox backend, expired SSO), and in that state the account the operator
  // actually confirmed is more useful than "could not be resolved" -- it is
  // also the account the gate is still enforcing against.
  const account = c.identityResolved ? c.account : c.grant?.account || ''

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
    </div>
  )
}
