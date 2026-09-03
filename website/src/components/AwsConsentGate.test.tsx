import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from '../test/helpers'
import type { AwsConsentStatus } from '../api/client'

/* ── api client mock ───────────────────────────────────────────────────────
 * The gate reads and writes only through these three methods, so mocking them
 * keeps every case network-free. */
vi.mock('../api/client', () => ({
  api: {
    awsConsent: vi.fn(),
    grantAwsConsent: vi.fn(),
    revokeAwsConsent: vi.fn(),
  },
}))

import { api } from '../api/client'
import AwsConsentGate from './AwsConsentGate'

function status(overrides: Partial<AwsConsentStatus> = {}): AwsConsentStatus {
  return {
    service: 'polly',
    serviceLabel: 'Amazon Polly',
    profile: '',
    credentialSource: 'AWS CLI default credential provider chain',
    region: 'us-east-1',
    account: '111122223333',
    arn: 'arn:aws:iam::111122223333:user/x',
    identityResolved: true,
    identityDetail: '',
    granted: false,
    reason: 'Amazon Polly use has not been confirmed.',
    revokedOnAccountChange: false,
    grant: null,
    ...overrides,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api.grantAwsConsent).mockResolvedValue({ ok: true })
  vi.mocked(api.revokeAwsConsent).mockResolvedValue({ ok: true, removed: true })
})

describe('AwsConsentGate', () => {
  it('lets the caller invalidate the surfaces a grant gates', async () => {
    // A grant decides content this component cannot name. The AWS Control
    // console decides whether to show the ask from a CACHED drive 409 and a
    // cached consentMissing, so without this callback a withdraw would unmount
    // the receipt and leave no ask behind it - nothing on screen offering the
    // confirm back.
    vi.mocked(api.awsConsent).mockResolvedValue(
      status({
        granted: true,
        grant: { account: '111122223333', region: 'us-east-1', profile: '', granted_at: '2026-08-21T00:00:00+00:00' },
      }),
    )
    const onConsentChange = vi.fn()
    renderWithProviders(<AwsConsentGate service="polly" onConsentChange={onConsentChange} />)

    fireEvent.click(await screen.findByRole('button', { name: /withdraw confirmation/i }))
    await waitFor(() => expect(api.revokeAwsConsent).toHaveBeenCalledWith('polly'))
    await waitFor(() => expect(onConsentChange).toHaveBeenCalled())
  })

  it('does not invalidate the Polly catalogue for another AWS service', async () => {
    vi.mocked(api.awsConsent).mockResolvedValue(
      status({ service: 's3', serviceLabel: 'Amazon S3' }),
    )
    const { queryClient } = renderWithProviders(<AwsConsentGate service="s3" />)
    queryClient.setQueryData(['voiceVoices'], { voices: [{ id: 'Ruth' }] })

    fireEvent.click(await screen.findByRole('button', { name: /confirm and enable/i }))
    await waitFor(() => expect(api.grantAwsConsent).toHaveBeenCalledWith('s3', expect.any(Object)))

    expect(queryClient.getQueryState(['voiceVoices'])?.isInvalidated).toBe(false)
  })

  it('shows the service, region, credential source and account before confirming', async () => {
    vi.mocked(api.awsConsent).mockResolvedValue(status())
    renderWithProviders(<AwsConsentGate service="polly" />)

    // The four facts the operator needs in order to consent knowingly.
    expect(await screen.findByText('Amazon Polly')).toBeTruthy()
    expect(screen.getByText('us-east-1')).toBeTruthy()
    expect(screen.getByText('AWS CLI default credential provider chain')).toBeTruthy()
    expect(screen.getByText('111122223333')).toBeTruthy()
  })

  it('records the confirmation with the values it displayed', async () => {
    vi.mocked(api.awsConsent).mockResolvedValue(status({ profile: 'voice' }))
    renderWithProviders(<AwsConsentGate service="polly" />)

    fireEvent.click(await screen.findByRole('button', { name: /confirm and enable/i }))
    // The echoed values are what makes "confirm what you were shown" true: the
    // backend 409s on a mismatch, so a stale card cannot confirm a new account.
    await waitFor(() =>
      expect(api.grantAwsConsent).toHaveBeenCalledWith('polly', {
        profile: 'voice',
        region: 'us-east-1',
        account: '111122223333',
      }),
    )
  })

  it('cannot confirm while the account is unresolved', async () => {
    // The button is the only way to consent from the UI, so an unresolvable
    // account must leave it inert: consent that cannot name an account is not
    // informed consent, and the backend refuses it too.
    vi.mocked(api.awsConsent).mockResolvedValue(
      status({ identityResolved: false, account: '', identityDetail: 'creds did not resolve' }),
    )
    renderWithProviders(<AwsConsentGate service="polly" />)

    const button = await screen.findByRole('button', { name: /confirm and enable/i })
    expect(button.hasAttribute('disabled')).toBe(true)
    fireEvent.click(button)
    expect(api.grantAwsConsent).not.toHaveBeenCalled()
    expect(screen.getByText('creds did not resolve')).toBeTruthy()
  })

  it('offers withdrawal once confirmed, and no confirm button', async () => {
    vi.mocked(api.awsConsent).mockResolvedValue(
      status({
        granted: true,
        reason: '',
        grant: {
          account: '111122223333',
          region: 'us-east-1',
          profile: '',
          granted_at: '2026-08-21T00:00:00+00:00',
        },
      }),
    )
    renderWithProviders(<AwsConsentGate service="polly" />)

    fireEvent.click(await screen.findByRole('button', { name: /withdraw confirmation/i }))
    await waitFor(() => expect(api.revokeAwsConsent).toHaveBeenCalledWith('polly'))
    expect(screen.queryByRole('button', { name: /confirm and enable/i })).toBeNull()
  })

  it('explains an automatic withdrawal after the account changed', async () => {
    vi.mocked(api.awsConsent).mockResolvedValue(status({ revokedOnAccountChange: true }))
    renderWithProviders(<AwsConsentGate service="polly" />)

    expect(await screen.findByText(/confirmed for a different AWS account/i)).toBeTruthy()
  })

  it('falls back to the confirmed account when the live probe fails', async () => {
    // A probe can fail for reasons that say nothing about the grant (no
    // network, expired SSO). The account still being enforced against is more
    // useful there than "could not be resolved".
    vi.mocked(api.awsConsent).mockResolvedValue(
      status({
        granted: true,
        identityResolved: false,
        account: '',
        identityDetail: 'creds did not resolve',
        grant: {
          account: '444455556666',
          region: 'us-east-1',
          profile: '',
          granted_at: '2026-08-21T00:00:00+00:00',
        },
      }),
    )
    renderWithProviders(<AwsConsentGate service="polly" />)

    expect(await screen.findByText('444455556666')).toBeTruthy()
    expect(screen.queryByText('could not be resolved')).toBeNull()
    // The probe error is noise once confirmed: the account being enforced
    // against is already shown, and a stale probe does not change it.
    expect(screen.queryByText('creds did not resolve')).toBeNull()
  })

  it('labels an empty region as the provider default', async () => {
    vi.mocked(api.awsConsent).mockResolvedValue(status({ region: '' }))
    renderWithProviders(<AwsConsentGate service="polly" />)

    expect(await screen.findByText('(provider default)')).toBeTruthy()
  })

  it('a failed status read renders a notice instead of nothing', async () => {
    // Rendering null here made a broken gate look like a gate with nothing to
    // ask, on the surface that decides whether a paid service may bill.
    vi.mocked(api.awsConsent).mockRejectedValue(new Error('consent store unreadable'))
    renderWithProviders(<AwsConsentGate service="polly" />)

    const notice = await screen.findByTestId('aws-consent-polly-error')
    expect(notice).toHaveTextContent('consent store unreadable')
    // Off by default: the gate cannot know whether its host holds a draft.
    expect(screen.queryByRole('button', { name: /ask the agent/i })).toBeNull()
  })

  it('a failed status read offers a retry that re-reads the status', async () => {
    // With the grant/withdraw controls gone this notice is the card's only
    // surface, so the retry lives on the card itself rather than on the host.
    vi.mocked(api.awsConsent)
      .mockRejectedValueOnce(new Error('consent store unreadable'))
      .mockResolvedValue(status())
    renderWithProviders(<AwsConsentGate service="polly" />)

    await screen.findByTestId('aws-consent-polly-error')
    fireEvent.click(screen.getByTestId('aws-consent-polly-error-retry'))

    await waitFor(() => expect(api.awsConsent).toHaveBeenCalledTimes(2))
    // The second read succeeded: the ask replaces the notice.
    expect(await screen.findByRole('button', { name: /confirm and enable/i })).toBeTruthy()
    expect(screen.queryByTestId('aws-consent-polly-error')).toBeNull()
  })

  it('a refused confirmation is said under the card, with the hand-off when the host opts in', async () => {
    vi.mocked(api.awsConsent).mockResolvedValue(status())
    vi.mocked(api.grantAwsConsent).mockRejectedValue(new Error('account mismatch'))
    renderWithProviders(<AwsConsentGate service="polly" askAgent />)

    fireEvent.click(await screen.findByRole('button', { name: /confirm and enable/i }))
    const notice = await screen.findByTestId('aws-consent-polly-write-error')
    expect(notice).toHaveTextContent('account mismatch')
    expect(screen.getByRole('button', { name: /ask the agent/i })).toBeTruthy()
    // The ask is still there: nothing was recorded.
    expect(screen.getByRole('button', { name: /confirm and enable/i })).toBeTruthy()
  })
})
