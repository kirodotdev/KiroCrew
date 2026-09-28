import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { RefreshCw, ShieldCheck, Receipt } from 'lucide-react'
import { api, type AwsConsentStatus } from '../api/client'
import ErrorNotice from './ErrorNotice'
import { Btn, Badge, Card, CardTitle } from './ui'
import { i18nT } from '../i18n/t'
import { fmtDate } from '../i18n/format'

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
   * Render as ONE ROW instead of the full card, for a host that already owns a
   * "Paid services" card and stacks several gates in a `divide-y` list. Every
   * state — receipt, ask, failed status read — is one row, and compact mode
   * draws no container of its own, so the host's card is the only card.
   *
   * The ask keeps the facts a confirmation needs (region, credential source,
   * and the account it would bill) in the row's own meta line rather than
   * dropping them: the row is smaller, not less informed. The receipt keeps the
   * credential source for the same reason — the only other way to read it was
   * to withdraw and re-read the full ask, a destructive act to answer an audit
   * question.
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

  // One row geometry for all three compact states, so a receipt, an ask and a
  // failed read line up in the host's list. `flex-wrap` plus a text-block basis
  // is what makes the right-hand cluster drop UNDER the text at 390px instead
  // of squeezing the service name to nothing.
  const rowClass = 'flex flex-wrap items-center gap-x-3 gap-y-2 py-2.5 text-[13px]'
  const rowTextClass = 'min-w-0 flex-1 basis-[12rem]'

  // A status read that failed used to render NOTHING — indistinguishable from a
  // gate that has nothing to ask, on the surface that decides whether a paid
  // service may bill. The message is the transport's own (journaled), so the
  // notice recovers its endpoint and status from that. A transient read is the
  // one failure the reader can clear alone, and with the grant/withdraw
  // controls gone this notice is the card's only surface, so it carries the
  // retry itself rather than relying on the host to offer one.
  if (consentQ.isError) {
    const notice = (
      <ErrorNotice
        message={consentQ.error instanceof Error ? consentQ.error.message : String(consentQ.error)}
        title={i18nT('components.awsConsentGate.status_failed')}
        askAgent={askAgent}
        // Inline in a row, boxed when the gate is the card. The notice is the
        // whole content either way, so it takes the full width it is given.
        variant={compact ? 'inline' : 'block'}
        className="w-full"
        testId={'aws-consent-' + service + '-error'}
      />
    )
    const retry = (
      <Btn onClick={() => consentQ.refetch()} data-testid={'aws-consent-' + service + '-error-retry'}>
        <RefreshCw size={13} />
        {i18nT('components.awsConsentGate.retry')}
      </Btn>
    )
    if (compact) {
      return (
        <div className={rowClass} data-testid={'aws-consent-' + service + '-error-card'}>
          <div className={rowTextClass}>{notice}</div>
          {retry}
        </div>
      )
    }
    return (
      <div className="flex flex-col items-start gap-2" data-testid={'aws-consent-' + service + '-error-card'}>
        {notice}
        {retry}
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
  const accountText = account || i18nT('components.awsConsentGate.unresolved_account')
  const sep = <span aria-hidden="true">·</span>

  // A granted receipt in compact mode is one row: what is confirmed, the
  // account and credentials it bills through, when it was confirmed, and the
  // withdraw on the right. The withdraw keeps its object ("Withdraw
  // confirmation") even here: a bare "Withdraw" beside a cloud-drive row read
  // as withdrawing the drive itself.
  if (compact && c.granted && !c.revokedOnAccountChange) {
    return (
      <div className={rowClass} data-testid={'aws-consent-' + service}>
        <ShieldCheck className="h-3.5 w-3.5 shrink-0 text-ok" aria-hidden="true" />
        <div className={rowTextClass}>
          <div className="truncate font-medium text-text-strong">{c.serviceLabel}</div>
          <div className="mt-0.5 flex flex-wrap items-center gap-x-1.5 text-[12px] text-muted">
            <span className="font-mono" data-testid="aws-consent-account">{accountText}</span>
            {sep}
            <span className="min-w-0 truncate font-mono" data-testid="aws-consent-source">{c.credentialSource}</span>
            {c.grant?.granted_at && (
              <>
                {sep}
                <span data-testid="aws-consent-granted-at">
                  {i18nT('components.awsConsentGate.confirmed_on', { date: fmtDate(c.grant.granted_at) })}
                </span>
              </>
            )}
          </div>
          {/* What the withdraw DOES, said before it is clicked: a reader who
              could not tell whether the drive would keep working did not dare
              click it, and the row is its only surface on the landing pane. */}
          <div className="mt-0.5 text-[12px] text-muted" data-testid="aws-consent-withdraw-effect">
            {i18nT('components.awsConsentGate.withdraw_effect')}
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Badge variant="ok">{i18nT('components.awsConsentGate.badge_confirmed')}</Badge>
          <Btn
            danger
            disabled={busy}
            onClick={() => revokeMut.mutate()}
            data-testid={'aws-consent-' + service + '-withdraw'}
          >
            {i18nT('components.awsConsentGate.withdraw')}
          </Btn>
        </div>
        {/* A full-width child of the row, so a refused withdraw is said under the
            receipt it failed to remove rather than breaking the row's layout. */}
        {writeNotice('basis-full')}
      </div>
    )
  }

  // The compact ASK: the same row, with the billing sentence and the facts the
  // confirmation would apply to — the ACCOUNT it would bill first among them,
  // because the gate resolves that from the credentials and not from the
  // account the app has selected, and the row is the only place that mismatch
  // can show. `revokedOnAccountChange` keeps its own sentence here rather than
  // forcing the full card — the state's whole point is that the reader must
  // re-read the account, and the meta line names it.
  //
  // A grant that is BOTH recorded and account-revoked is not a state the
  // backend produces; if it ever appears it falls through to the full card
  // below, which has room to say both things.
  if (compact && !c.granted) {
    return (
      <div className={rowClass} data-testid={'aws-consent-' + service}>
        <Receipt className="h-3.5 w-3.5 shrink-0 text-muted" aria-hidden="true" />
        <div className={rowTextClass}>
          <div className="truncate font-medium text-text-strong">{c.serviceLabel}</div>
          <div className="mt-0.5 flex flex-wrap items-center gap-x-1.5 text-[12px] text-muted">
            <span>{i18nT('components.awsConsentGate.billing_notice_short')}</span>
            {sep}
            <span className="font-mono" data-testid="aws-consent-account">{accountText}</span>
            {sep}
            <span>{region}</span>
            {sep}
            <span className="min-w-0 truncate font-mono" data-testid="aws-consent-source">{c.credentialSource}</span>
          </div>
          {c.revokedOnAccountChange && (
            <div className="mt-0.5 text-[12px] text-warn" data-testid="aws-consent-account-changed">
              {i18nT('components.awsConsentGate.account_changed')}
            </div>
          )}
        </div>
        <div className="flex items-center gap-2">
          <Badge variant="muted">{i18nT('components.awsConsentGate.badge_not_enabled')}</Badge>
          <Btn
            primary
            disabled={busy || !c.identityResolved}
            onClick={() => grantMut.mutate()}
            data-testid={'aws-consent-' + service + '-confirm'}
          >
            {i18nT('components.awsConsentGate.confirm_button')}
          </Btn>
        </div>
        {/* Why the confirm is inert. `identityDetail` is only set when the
            identity probe FAILED, so it is an error surface and the hand-off is
            the host's call, exactly as in the full card. */}
        {!c.identityResolved && c.identityDetail ? (
          <ErrorNotice
            variant="inline"
            className="basis-full"
            askAgent={askAgent}
            testId="aws-consent-identity-error"
            message={c.identityDetail}
          />
        ) : null}
        {writeNotice('basis-full')}
      </div>
    )
  }

  return (
    <Card
      // No bottom margin: every host of this card already spaces it (a settings
      // panel's own row rhythm, or the accounts pane's `gap-3` column), and a
      // margin here would stack on top of that.
      className={'mb-0' + (c.granted ? '' : ' border-warn')}
      data-testid={'aws-consent-' + service}
    >
      <CardTitle>
        <ShieldCheck className={'h-3.5 w-3.5 shrink-0 ' + (c.granted ? 'text-ok' : 'text-accent')} aria-hidden="true" />
        {c.granted
          ? i18nT('components.awsConsentGate.confirmed_title')
          : i18nT('components.awsConsentGate.confirm_title')}
      </CardTitle>

      {c.revokedOnAccountChange ? (
        <p className="mb-2 text-[13px] text-warn">
          {i18nT('components.awsConsentGate.account_changed')}
        </p>
      ) : null}

      {/* One column by default, two from `sm` up. A translated label can be long
          ("Quelle der Anmeldedaten"), and an `auto` label column sized to it
          would leave the value column nothing at 320px. Stacking below `sm`
          gives each value the full width instead. Labels are the 12px meta
          size, values the 13px reading size, so the pair reads as one fact. */}
      <dl className="mb-3 grid grid-cols-1 gap-x-3 gap-y-1 sm:grid-cols-[auto_1fr]">
        <dt className="text-[12px] text-muted">{i18nT('components.awsConsentGate.service')}</dt>
        <dd className="mb-1 min-w-0 break-all text-[13px] text-text sm:mb-0">{c.serviceLabel}</dd>
        <dt className="text-[12px] text-muted">{i18nT('components.awsConsentGate.region')}</dt>
        <dd className="mb-1 min-w-0 break-all text-[13px] text-text sm:mb-0">{region}</dd>
        <dt className="text-[12px] text-muted">{i18nT('components.awsConsentGate.credential_source')}</dt>
        <dd className="mb-1 min-w-0 break-all font-mono text-[13px] text-text sm:mb-0" data-testid="aws-consent-source">
          {c.credentialSource}
        </dd>
        <dt className="text-[12px] text-muted">{i18nT('components.awsConsentGate.aws_account')}</dt>
        <dd className="min-w-0 break-all font-mono text-[13px] text-text" data-testid="aws-consent-account">
          {accountText}
        </dd>
      </dl>

      {!c.identityResolved && c.identityDetail && !c.granted ? (
        // `identityDetail` is only set when the identity probe FAILED (the
        // backend's last_error for the credential check), so it is an error
        // surface. The hand-off decision is the host's, via the same delegated
        // `askAgent` prop the read/write notices use (see the prop doc above).
        <ErrorNotice
          variant="inline"
          className="mb-2"
          askAgent={askAgent}
          testId="aws-consent-identity-error"
          message={c.identityDetail}
        />
      ) : null}

      {c.granted ? (
        <Btn danger disabled={busy} onClick={() => revokeMut.mutate()} data-testid={'aws-consent-' + service + '-withdraw'}>
          {i18nT('components.awsConsentGate.withdraw')}
        </Btn>
      ) : (
        <>
          <p className="mb-2 text-[13px] text-muted">{i18nT('components.awsConsentGate.billing_notice')}</p>
          <Btn
            primary
            disabled={busy || !c.identityResolved}
            onClick={() => grantMut.mutate()}
            data-testid={'aws-consent-' + service + '-confirm'}
          >
            {i18nT('components.awsConsentGate.confirm_button')}
          </Btn>
        </>
      )}
      {writeNotice('mt-2')}
    </Card>
  )
}
