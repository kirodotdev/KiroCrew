import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import { i18nT } from '../../i18n/t'
import { fmtCurrency, fmtDate } from '../../i18n/format'
import type { AwsAccount, DriveStatus, CostReport } from './types'

/* The pane reads only through the api client; mocking it keeps every case
 * network-free while leaving `AwsControlError` real for the 409 paths. */
vi.mock('./api', async () => {
  const actual = await vi.importActual<typeof import('./api')>('./api')
  return {
    ...actual,
    awsControlApi: {
      accounts: vi.fn(),
      reconnectPlan: vi.fn(),
      iamPolicy: vi.fn(),
      drive: vi.fn(),
      driveBootstrapPreview: vi.fn(),
      driveBootstrapConfirm: vi.fn(),
      costs: vi.fn(),
    },
  }
})

/* Consent status (the CE ask and the paid-service receipts) fetches through
 * the shared client. */
vi.mock('../../api/client', () => ({
  api: {
    awsConsent: vi.fn(),
    grantAwsConsent: vi.fn(),
    revokeAwsConsent: vi.fn(),
  },
}))

import { awsControlApi } from './api'
import { api } from '../../api/client'
import UsagePane, { ReconnectAction, ConnectionsSection, SetupCard } from './ConsoleView'

const ACCOUNT: AwsAccount = {
  account: '111122223333',
  name: 'personal',
  health: 'ok',
  profiles: [
    {
      name: 'personal', region: 'us-west-2', kind: 'sso', identityOk: true,
      account: '111122223333', arn: 'arn:aws:iam::111122223333:role/x', detail: '', default: true,
    },
  ],
  summary: { storage: null, sites: null, tasks: null, costMonthToDate: null },
}

const DEGRADED: AwsAccount = {
  account: '444455556666',
  name: 'work',
  health: 'degraded',
  profiles: [
    {
      name: 'work', region: 'eu-west-1', kind: 'credential-process', identityOk: false,
      account: '444455556666', arn: '', detail: 'expired', default: true,
    },
  ],
  summary: { storage: null, sites: null, tasks: null, costMonthToDate: null },
}

const driveExists: DriveStatus = {
  exists: true,
  bucket: 'kirocrew-drive-abc123',
  region: 'us-west-2',
  usage: {
    bytes: 3_500_000_000,
    objects: 42,
    sections: {
      library: { objects: 10, bytes: 1_000_000 },
      drive: { objects: 30, bytes: 3_000_000_000 },
      backup: { objects: 2, bytes: 499_000_000 },
    },
  },
}

const costsFresh: CostReport = {
  fresh: true, monthToDate: 12.5, projected: 30, currency: 'USD',
  byService: [{ service: 'S3', amount: 12.5 }], fetchedAt: '2026-08-24T05:00:00Z',
}

/* Consent fixtures. A grant is service-scoped and carries the account it was
 * confirmed for; the receipt logic keys on that account matching. */
const grantHere = { granted: true, grant: { account: ACCOUNT.account } }
const grantElsewhere = { granted: true, grant: { account: '999988887777' } }
const notGranted = { granted: false }

/** Resolve the consent status per service ('s3' | 'ce'). */
function stubConsent(map: Record<string, unknown>) {
  vi.mocked(api.awsConsent).mockImplementation(
    (svc: string) => Promise.resolve(map[svc] ?? notGranted) as ReturnType<typeof api.awsConsent>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  // Default: consent status never resolves → granted is UNDEFINED, not false.
  vi.mocked(api.awsConsent).mockReturnValue(new Promise(() => {}) as ReturnType<typeof api.awsConsent>)
})

describe('UsagePane', () => {
  it('renders the pane header and the fresh month-to-date bill, with no "as of" hint', async () => {
    vi.mocked(awsControlApi.drive).mockResolvedValue(driveExists)
    vi.mocked(awsControlApi.costs).mockResolvedValue(costsFresh)

    renderWithProviders(<UsagePane account={ACCOUNT} />)

    const pane = await screen.findByTestId('usage-pane')
    expect(within(pane).getByTestId('page-title')).toHaveTextContent(i18nT('apps.awsControl.rail.usage'))

    // The bill arrives async — wait for the figure, not the '…' placeholder.
    const stats = screen.getByTestId('console-stats')
    await waitFor(() =>
      expect(within(stats).getByTestId('console-cost-value')).toHaveTextContent(fmtCurrency(12.5, 'USD')),
    )
    // fresh:true → no "as of" staleness hint.
    const asOf = i18nT('apps.awsControl.console.costs_as_of', { date: fmtDate(costsFresh.fetchedAt) })
    expect(within(stats).queryByText(asOf)).toBeNull()
  })

  it('does NOT mount the CE consent ask while the consent status is still unknown', async () => {
    // The gate mounts on granted === false only. The default awsConsent mock
    // never resolves, so `granted` is undefined here — the ask must not flash.
    vi.mocked(awsControlApi.drive).mockResolvedValue(driveExists)
    vi.mocked(awsControlApi.costs).mockResolvedValue(costsFresh)

    renderWithProviders(<UsagePane account={ACCOUNT} />)

    await screen.findByTestId('console-cost-value')
    expect(screen.queryByTestId('costs-consent-gate')).toBeNull()
  })

  it('renders an em dash on consentMissing and mounts the CE ask when consent reports granted:false', async () => {
    vi.mocked(awsControlApi.drive).mockResolvedValue(driveExists)
    vi.mocked(awsControlApi.costs).mockResolvedValue({
      fresh: false, monthToDate: 0, projected: 0, currency: 'USD',
      byService: [], fetchedAt: '2026-08-24T05:00:00Z', consentMissing: true,
    })
    stubConsent({ s3: notGranted, ce: notGranted })

    renderWithProviders(<UsagePane account={ACCOUNT} />)

    // The consentMissing branch renders "—", never a zero figure or the as-of line.
    const stats = await screen.findByTestId('console-stats')
    await waitFor(() =>
      expect(within(stats).getByTitle(i18nT('apps.awsControl.console.costs_consent_missing'))).toHaveTextContent('—'),
    )
    expect(within(stats).queryByTestId('console-cost-value')).toBeNull()

    // granted === false → the Cost Explorer ask mounts, and fetches ce status.
    expect(await screen.findByTestId('costs-consent-gate')).toBeTruthy()
    await waitFor(() => expect(api.awsConsent).toHaveBeenCalledWith('ce'))
  })

  it('renders the month-to-date stat as an em dash when the cost read fails, without a consent ask', async () => {
    vi.mocked(awsControlApi.drive).mockResolvedValue(driveExists)
    // A rejected costs read (CE not enabled / throttled) must settle to "—".
    vi.mocked(awsControlApi.costs).mockRejectedValue(new Error('CE disabled'))

    renderWithProviders(<UsagePane account={ACCOUNT} />)

    // The costs query carries retry:1, so its error settles after one
    // backoff — give waitFor room.
    const stats = await screen.findByTestId('console-stats')
    await waitFor(
      () => expect(within(stats).getByTitle(i18nT('apps.awsControl.console.costs_unavailable'))).toHaveTextContent('—'),
      { timeout: 4000 },
    )
    expect(within(stats).queryByTestId('console-cost-value')).toBeNull()
    // No consent ask fires — this is a failure, not a missing gate.
    expect(screen.queryByTestId('costs-consent-gate')).toBeNull()
    // The em dash stays quiet, but the failure itself is said — with the
    // hand-off, since "CE disabled" is exactly what the agent can act on.
    const notice = await screen.findByTestId('costs-error')
    expect(notice).toHaveTextContent(i18nT('apps.awsControl.console.costs_unavailable'))
    expect(within(notice).getByRole('button', { name: /ask the agent/i })).toBeTruthy()
  })

  it('a consent-refused 409 on costs is the ask, not an error notice', async () => {
    // The gate's own refusal is answered by the CE ask below the row; a red
    // banner beside it would report the reader's pending decision as a fault.
    const { AwsControlError } = await import('./api')
    vi.mocked(awsControlApi.drive).mockResolvedValue(driveExists)
    vi.mocked(awsControlApi.costs).mockRejectedValue(new AwsControlError('aws_consent_required', 409))
    stubConsent({ s3: notGranted, ce: notGranted })

    renderWithProviders(<UsagePane account={ACCOUNT} />)

    expect(await screen.findByTestId('costs-consent-gate')).toBeTruthy()
    await waitFor(
      () => expect(within(screen.getByTestId('console-stats')).getByTitle(i18nT('apps.awsControl.console.costs_unavailable'))).toBeTruthy(),
      { timeout: 4000 },
    )
    expect(screen.queryByTestId('costs-error')).toBeNull()
  })

  it('shows a visible "as of" hint next to the cost figure when it came from cache', async () => {
    vi.mocked(awsControlApi.drive).mockResolvedValue(driveExists)
    vi.mocked(awsControlApi.costs).mockResolvedValue({
      // fresh:false → the number is cached and must carry the "as of" hint.
      fresh: false, monthToDate: 9.99, projected: 20, currency: 'USD',
      byService: [{ service: 'S3', amount: 9.99 }], fetchedAt: '2026-08-24T05:00:00Z',
    })

    renderWithProviders(<UsagePane account={ACCOUNT} />)

    const stats = await screen.findByTestId('console-stats')
    await waitFor(() =>
      expect(within(stats).getByTestId('console-cost-value')).toHaveTextContent(fmtCurrency(9.99, 'USD')),
    )
    // Visible rather than a hover-only title, so staleness is stated, not hidden.
    const asOf = i18nT('apps.awsControl.console.costs_as_of', { date: fmtDate('2026-08-24T05:00:00Z') })
    expect(within(stats).getByText(asOf)).toBeTruthy()
  })

  it('renders the storage block (bucket line + meter) only when the drive exists', async () => {
    vi.mocked(awsControlApi.drive).mockResolvedValue(driveExists)
    vi.mocked(awsControlApi.costs).mockResolvedValue(costsFresh)

    renderWithProviders(<UsagePane account={ACCOUNT} />)

    const storage = await screen.findByTestId('usage-storage')
    expect(within(storage).getByTestId('drive-bucket')).toHaveTextContent('kirocrew-drive-abc123')
    expect(within(storage).getByTestId('drive-copy-bucket')).toBeTruthy()
    expect(storage).toHaveTextContent('us-west-2')
    // The StorageMeter mounts with the drive's usage split.
    expect(within(storage).getByTestId('drive-storage-meter')).toBeTruthy()
  })

  it('renders NO storage block before the drive bucket is created', async () => {
    vi.mocked(awsControlApi.drive).mockResolvedValue({ exists: false })
    vi.mocked(awsControlApi.costs).mockResolvedValue(costsFresh)

    renderWithProviders(<UsagePane account={ACCOUNT} />)

    await screen.findByTestId('console-cost-value')
    expect(screen.queryByTestId('usage-storage')).toBeNull()
    expect(screen.queryByTestId('drive-storage-meter')).toBeNull()
  })

  it('carries both receipts once each grant is recorded for THIS account, and no CE ask', async () => {
    vi.mocked(awsControlApi.drive).mockResolvedValue(driveExists)
    vi.mocked(awsControlApi.costs).mockResolvedValue(costsFresh)
    stubConsent({ s3: grantHere, ce: grantHere })

    renderWithProviders(<UsagePane account={ACCOUNT} />)

    const receipts = await screen.findByTestId('paid-services')
    expect(within(receipts).getByTestId('aws-consent-s3')).toBeTruthy()
    expect(within(receipts).getByTestId('aws-consent-ce')).toBeTruthy()
    // granted === true → the ask must not also be on screen.
    expect(screen.queryByTestId('costs-consent-gate')).toBeNull()
  })

  it('shows no receipts section at all when nothing is granted', async () => {
    // The section must not exist when there is nothing to show, or it becomes
    // an always-present placeholder heading.
    vi.mocked(awsControlApi.drive).mockResolvedValue(driveExists)
    vi.mocked(awsControlApi.costs).mockResolvedValue(costsFresh)
    stubConsent({ s3: notGranted, ce: notGranted })

    const r = renderWithProviders(<UsagePane account={ACCOUNT} />)

    await waitFor(() => expect(api.awsConsent).toHaveBeenCalledWith('s3'))
    await screen.findByTestId('console-cost-value')
    expect(r.container.querySelector('[data-testid="paid-services"]')).toBeNull()
  })

  it('never shows a receipt for a grant recorded under a DIFFERENT account', async () => {
    // The withdraw control is GLOBAL: one grant per service. A receipt shown on
    // the wrong account's pane is not a cosmetic mislabel — clicking it revokes
    // the grant the OTHER account's drive and cost figure run on.
    vi.mocked(awsControlApi.drive).mockResolvedValue(driveExists)
    vi.mocked(awsControlApi.costs).mockResolvedValue(costsFresh)
    stubConsent({ s3: grantElsewhere, ce: grantElsewhere })

    renderWithProviders(<UsagePane account={ACCOUNT} />)

    await waitFor(() => expect(api.awsConsent).toHaveBeenCalledWith('s3'))
    await screen.findByTestId('console-cost-value')
    expect(screen.queryByTestId('paid-services')).toBeNull()
  })

  it('suppresses the S3 receipt while the drive read is a 409 consent refusal, keeping the CE one', async () => {
    // Granting invalidates the consent query but not the drive cache: between a
    // withdraw and the next refetch, the cached 409 refusal and a receipt would
    // both be on screen saying opposite things about the same service.
    const { AwsControlError } = await import('./api')
    vi.mocked(awsControlApi.drive).mockRejectedValue(new AwsControlError('aws_consent_required', 409))
    vi.mocked(awsControlApi.costs).mockResolvedValue(costsFresh)
    stubConsent({ s3: grantHere, ce: grantHere })

    renderWithProviders(<UsagePane account={ACCOUNT} />)

    const receipts = await screen.findByTestId('paid-services')
    expect(within(receipts).getByTestId('aws-consent-ce')).toBeTruthy()
    expect(within(receipts).queryByTestId('aws-consent-s3')).toBeNull()
  })

  it('suppresses the CE receipt while costs report consentMissing, keeping the S3 one', async () => {
    vi.mocked(awsControlApi.drive).mockResolvedValue(driveExists)
    vi.mocked(awsControlApi.costs).mockResolvedValue({
      fresh: false, monthToDate: 0, projected: 0, currency: 'USD',
      byService: [], fetchedAt: '2026-08-24T05:00:00Z', consentMissing: true,
    })
    stubConsent({ s3: grantHere, ce: grantHere })

    renderWithProviders(<UsagePane account={ACCOUNT} />)

    const receipts = await screen.findByTestId('paid-services')
    expect(within(receipts).getByTestId('aws-consent-s3')).toBeTruthy()
    expect(within(receipts).queryByTestId('aws-consent-ce')).toBeNull()
  })
})

describe('ConnectionsSection', () => {
  it('renders one row per key with its kind, region and health — no Reconnect on a healthy key', async () => {
    renderWithProviders(<ConnectionsSection account={ACCOUNT} askAgent />)

    const conns = await screen.findByTestId('connections-section')
    const rows = within(conns).getAllByTestId('connection-row')
    expect(rows).toHaveLength(1)
    expect(within(rows[0]).getByTestId('connection-name')).toHaveTextContent('personal')
    expect(rows[0]).toHaveTextContent(i18nT('apps.awsControl.page.kind_sso'))
    expect(rows[0]).toHaveTextContent('us-west-2')
    expect(rows[0]).toHaveTextContent(i18nT('apps.awsControl.console.key_healthy'))
    expect(within(rows[0]).queryByTestId('reconnect-toggle')).toBeNull()
  })

  it('shows the empty line when the account has no keys', async () => {
    const bare: AwsAccount = { ...ACCOUNT, profiles: [] }
    renderWithProviders(<ConnectionsSection account={bare} askAgent />)

    expect(await screen.findByTestId('connections-empty')).toHaveTextContent(
      i18nT('apps.awsControl.page.not_connected_yet'),
    )
    expect(screen.queryByTestId('connection-row')).toBeNull()
  })

  it('shows an inline Reconnect on a failing key and loads its command on demand', async () => {
    vi.mocked(awsControlApi.reconnectPlan).mockResolvedValue({
      method: 'terminal', kind: 'credential-process', command: 'aws sso login --profile work',
    })

    renderWithProviders(<ConnectionsSection account={DEGRADED} askAgent />)

    const row = await screen.findByTestId('connection-row')
    expect(row).toHaveTextContent(i18nT('apps.awsControl.console.key_failed'))

    // The plan query is disabled until the toggle opens the panel.
    expect(awsControlApi.reconnectPlan).not.toHaveBeenCalled()
    fireEvent.click(within(row).getByTestId('reconnect-toggle'))
    await waitFor(() => expect(awsControlApi.reconnectPlan).toHaveBeenCalledWith('work'))
    expect(await screen.findByTestId('reconnect-command')).toHaveTextContent('aws sso login --profile work')
  })
})

describe('ReconnectAction', () => {
  it('shows the reconnect error state when the plan query fails', async () => {
    vi.mocked(awsControlApi.reconnectPlan).mockRejectedValue(new Error('plan failed'))

    renderWithProviders(<ReconnectAction profile={DEGRADED.profiles[0]} askAgent />)

    fireEvent.click(await screen.findByTestId('reconnect-toggle'))
    // The panel resolves to its error message, not a command block.
    expect(await screen.findByTestId('reconnect-error')).toBeTruthy()
    expect(screen.queryByTestId('reconnect-command')).toBeNull()
    expect(screen.getByRole('button', { name: /ask the agent/i })).toBeTruthy()
  })

  it('withholds the hand-off when the host says so', async () => {
    // The accounts pane hosts this next to the Add-accounts checkboxes; while a
    // selection is ticked it passes false, and the notice must not navigate.
    vi.mocked(awsControlApi.reconnectPlan).mockRejectedValue(new Error('plan failed'))

    renderWithProviders(<ReconnectAction profile={DEGRADED.profiles[0]} askAgent={false} />)

    fireEvent.click(await screen.findByTestId('reconnect-toggle'))
    expect(await screen.findByTestId('reconnect-error')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /ask the agent/i })).toBeNull()
    // The retry stays: it is the reader's own recovery and navigates nowhere.
    expect(screen.getByTestId('reconnect-error-retry')).toBeTruthy()
  })
})

describe('SetupCard', () => {
  it('previews the bootstrap payload, then confirms and creates the bucket', async () => {
    vi.mocked(awsControlApi.driveBootstrapPreview).mockResolvedValue({
      preview: true, account: ACCOUNT.account, region: 'us-west-2', resource: 'kirocrew-drive-abc123',
    })
    vi.mocked(awsControlApi.driveBootstrapConfirm).mockResolvedValue({ created: true, bucket: 'kirocrew-drive-abc123' })

    renderWithProviders(<SetupCard account={ACCOUNT.account} region="us-west-2" />)

    // Preview shows the payload, confirm creates the bucket.
    fireEvent.click(await screen.findByTestId('drive-preview-btn'))
    expect(await screen.findByTestId('drive-preview')).toHaveTextContent('kirocrew-drive-abc123')

    fireEvent.click(screen.getByTestId('drive-confirm-btn'))
    await waitFor(() => expect(awsControlApi.driveBootstrapConfirm).toHaveBeenCalledWith(ACCOUNT.account))
  })

  it('surfaces a preview error and does not advance to the confirm step', async () => {
    // The bootstrap preview rejects (e.g. AccessDenied): the error line shows
    // and the confirm button never appears.
    vi.mocked(awsControlApi.driveBootstrapPreview).mockRejectedValue(new Error('AccessDenied'))

    renderWithProviders(<SetupCard account={ACCOUNT.account} region="us-west-2" />)

    fireEvent.click(await screen.findByTestId('drive-preview-btn'))
    expect(await screen.findByTestId('drive-preview-error')).toBeTruthy()
    expect(screen.queryByTestId('drive-confirm-btn')).toBeNull()
  })

  it('a refused CONFIRM is said under the button, not swallowed', async () => {
    // AccessDenied on CreateBucket is the common way this card fails, and the
    // button used to just come back enabled. The confirm step stays so the
    // reader can paste the permissions below and try again.
    const { AwsControlError } = await import('./api')
    vi.mocked(awsControlApi.driveBootstrapPreview).mockResolvedValue({
      preview: true, account: ACCOUNT.account, region: 'us-west-2', resource: 'kirocrew-drive-abc123',
    })
    vi.mocked(awsControlApi.driveBootstrapConfirm).mockRejectedValue(new AwsControlError('aws_call_failed', 502))

    renderWithProviders(<SetupCard account={ACCOUNT.account} region="us-west-2" />)
    fireEvent.click(await screen.findByTestId('drive-preview-btn'))
    fireEvent.click(await screen.findByTestId('drive-confirm-btn'))

    const notice = await screen.findByTestId('drive-confirm-error')
    expect(notice).toHaveTextContent(i18nT('apps.awsControl.console.setup_confirm_error'))
    expect(within(notice).getByRole('button', { name: /ask the agent/i })).toBeTruthy()
    expect(screen.getByTestId('drive-confirm-btn')).toBeTruthy()
  })

  it('a failed permissions read is said inside the drawer', async () => {
    vi.mocked(awsControlApi.iamPolicy).mockRejectedValue(new Error('boom'))

    renderWithProviders(<SetupCard account={ACCOUNT.account} region="us-west-2" />)
    fireEvent.click(await screen.findByTestId('policy-toggle'))

    const drawer = await screen.findByTestId('policy-drawer')
    expect(await within(drawer).findByTestId('policy-error')).toHaveTextContent(
      i18nT('apps.awsControl.console.setup_policy_error'),
    )
    expect(within(drawer).queryByTestId('policy-copy')).toBeNull()
  })

  it('reveals the IAM policy in the setup drawer and offers it to copy', async () => {
    vi.mocked(awsControlApi.iamPolicy).mockResolvedValue({ policy: '{"Version":"2012-10-17"}' })

    renderWithProviders(<SetupCard account={ACCOUNT.account} region="us-west-2" />)

    // The policy drawer is closed and its query disabled until toggled.
    expect(awsControlApi.iamPolicy).not.toHaveBeenCalled()
    fireEvent.click(await screen.findByTestId('policy-toggle'))
    await waitFor(() => expect(awsControlApi.iamPolicy).toHaveBeenCalled())
    const drawer = await screen.findByTestId('policy-drawer')
    expect(drawer).toHaveTextContent('2012-10-17')
    expect(within(drawer).getByTestId('policy-copy')).toBeTruthy()
  })
})
