// The Overview pane's Refresh button keeps keyboard focus while the accounts
// re-probe runs. It used native `disabled` while `accountsQ.isFetching`, which
// takes the button out of the tab order and drops a keyboard user's focus to
// the page until the probe ends. Mocks mirror AwsControlPage.test.tsx, trimmed
// to what the Overview pane needs to mount.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithProviders as renderUnderHost } from '../../test/helpers'
import { AppIdentityProvider } from '../../app-sdk/identity'
import type {
  AwsAccountsResponse, DriveStatus, CostReport, LibraryResponse, BackupStatus, SharesResponse,
} from './types'

function renderWithProviders(ui: React.ReactElement) {
  return renderUnderHost(
    <AppIdentityProvider appId="aws-control" origin="builtin">{ui}</AppIdentityProvider>,
  )
}

vi.mock('../../hooks/useIsMobile', () => ({
  useIsNarrowViewport: () => false,
  useIsMobile: () => false,
}))

vi.mock('./api', async () => {
  const actual = await vi.importActual<typeof import('./api')>('./api')
  return {
    ...actual,
    awsControlApi: {
      accounts: vi.fn(),
      drive: vi.fn(),
      costs: vi.fn(),
      library: vi.fn(),
      driveList: vi.fn(),
      backup: vi.fn(),
      shares: vi.fn(),
      availableProfiles: vi.fn(),
    },
  }
})

vi.mock('../../api/client', () => ({
  api: { awsConsent: vi.fn(), grantAwsConsent: vi.fn(), revokeAwsConsent: vi.fn() },
}))

import { awsControlApi } from './api'
import { api } from '../../api/client'
import AwsControlPage from './AwsControlPage'

function accountsPayload(): AwsAccountsResponse {
  return {
    accounts: [{
      account: '111122223333', name: 'personal', health: 'ok',
      profiles: [{
        name: 'personal', region: 'us-west-2', kind: 'sso', identityOk: true,
        account: '111122223333', arn: 'arn:aws:iam::111122223333:role/x', detail: '', default: true,
      }],
      summary: { storage: null, sites: null, tasks: null, costMonthToDate: null },
    }],
    totals: { accounts: 1, profiles: 1, profilesHealthy: 1 },
    generatedAt: '2026-08-24T05:00:00Z',
  }
}

const driveExists: DriveStatus = {
  exists: true, bucket: 'kirocrew-drive-abc123', region: 'us-west-2',
  usage: { bytes: 1000, objects: 1, sections: {
    drive: { objects: 1, bytes: 1000 }, library: { objects: 0, bytes: 0 }, backup: { objects: 0, bytes: 0 },
  } },
}
const costsFresh: CostReport = {
  fresh: true, monthToDate: 1, projected: 2, currency: 'USD',
  byService: [{ service: 'S3', amount: 1 }], fetchedAt: '2026-08-24T05:00:00Z',
}
const emptyLibrary: LibraryResponse = { artifacts: [] }
const emptyBackup: BackupStatus = {
  nightly: false, runs: {}, install: { id: 'a'.repeat(32), label: 'This Mac' },
  remote: { snapshot: [], sessions: [], installs: [], others: 0, truncated: false, max: 8 },
}
const noShares: SharesResponse = { shares: [] }

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  vi.mocked(api.awsConsent).mockReturnValue(new Promise(() => {}) as ReturnType<typeof api.awsConsent>)
  vi.mocked(awsControlApi.drive).mockResolvedValue(driveExists)
  vi.mocked(awsControlApi.costs).mockResolvedValue(costsFresh)
  vi.mocked(awsControlApi.library).mockResolvedValue(emptyLibrary)
  vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: [] })
  vi.mocked(awsControlApi.backup).mockResolvedValue(emptyBackup)
  vi.mocked(awsControlApi.shares).mockResolvedValue(noShares)
})

describe('AwsControlPage Overview refresh button focus while re-probing', () => {
  it('keeps keyboard focus on Refresh while the accounts re-probe runs, and ignores a press then', async () => {
    // Initial load resolves; the refresh re-probe (accounts(true)) hangs so the
    // button stays in its busy (isFetching) state for the assertions.
    let holdRefresh: () => void = () => {}
    vi.mocked(awsControlApi.accounts).mockImplementation((refresh?: boolean) =>
      refresh
        ? new Promise<AwsAccountsResponse>((resolve) => { holdRefresh = () => resolve(accountsPayload()) })
        : Promise.resolve(accountsPayload()),
    )

    renderWithProviders(<AwsControlPage />)
    await screen.findByTestId('overview-pane')

    const button = await screen.findByTestId('refresh')
    button.focus()
    fireEvent.click(button)

    await waitFor(() => expect(button).toHaveAttribute('aria-disabled', 'true'))
    expect(button).not.toBeDisabled()
    expect(document.activeElement).toBe(button)

    // A second press while the re-probe is in flight runs nothing more.
    fireEvent.click(button)
    const refreshCalls = vi.mocked(awsControlApi.accounts).mock.calls.filter(([r]) => r === true)
    expect(refreshCalls).toHaveLength(1)

    holdRefresh()
  })
})
