import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import { i18nT } from '../../i18n/t'
import { fmtBytes, fmtDate } from '../../i18n/format'
import type {
  AwsAccount, DriveStatus, CostReport, LibraryResponse, BackupStatus, SharesResponse,
} from './types'

/* The console reads only through the api client; mocking it keeps every case
 * network-free while leaving `AwsControlError` real for the page's 403/409 paths. */
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
      driveList: vi.fn(),
      driveDownload: vi.fn(),
      driveUpload: vi.fn(),
      driveDelete: vi.fn(),
      driveShare: vi.fn(),
      shares: vi.fn(),
      shareForget: vi.fn(),
      costs: vi.fn(),
      library: vi.fn(),
      libraryPush: vi.fn(),
      backup: vi.fn(),
      backupRun: vi.fn(),
      backupNightly: vi.fn(),
      backupRestore: vi.fn(),
    },
  }
})

/* The Cost Explorer consent nudge fetches through the shared client. */
vi.mock('../../api/client', () => ({
  api: {
    awsConsent: vi.fn(),
    grantAwsConsent: vi.fn(),
    revokeAwsConsent: vi.fn(),
  },
}))

import { awsControlApi } from './api'
import { api } from '../../api/client'
import ConsoleView from './ConsoleView'
import AwsControlPage from './AwsControlPage'

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

const emptyLibrary: LibraryResponse = { artifacts: [] }
const emptyBackup: BackupStatus = { nightly: false, runs: {}, remote: { snapshot: [], sessions: [] } }
const noShares: SharesResponse = { shares: [] }

function stubDrivePresent() {
  vi.mocked(awsControlApi.drive).mockResolvedValue(driveExists)
  vi.mocked(awsControlApi.costs).mockResolvedValue(costsFresh)
  vi.mocked(awsControlApi.library).mockResolvedValue(emptyLibrary)
  vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: [] })
  vi.mocked(awsControlApi.backup).mockResolvedValue(emptyBackup)
  vi.mocked(awsControlApi.shares).mockResolvedValue(noShares)
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api.awsConsent).mockReturnValue(new Promise(() => {}) as ReturnType<typeof api.awsConsent>)
})

describe('AwsControlPage → ConsoleView navigation', () => {
  it('opens the console when an account row is clicked, and the crumb returns', async () => {
    vi.mocked(awsControlApi.accounts).mockResolvedValue({
      accounts: [ACCOUNT],
      totals: { accounts: 1, profiles: 1, profilesHealthy: 1 },
      generatedAt: '2026-08-24T05:00:00Z',
    })
    stubDrivePresent()
    renderWithProviders(<AwsControlPage />)

    fireEvent.click(await screen.findByTestId('account-card'))

    // The console mounts (crumb + its own stats strip appear).
    expect(await screen.findByTestId('console-crumb')).toBeTruthy()
    expect(screen.getByTestId('console-stats')).toBeTruthy()

    // The crumb returns to the accounts list.
    fireEvent.click(screen.getByTestId('console-crumb'))
    expect(await screen.findByTestId('accounts-list')).toBeTruthy()
    expect(screen.queryByTestId('console-crumb')).toBeNull()
  })
})

describe('ConsoleView', () => {
  it('leads the header with the name and the FULL account id (no truncated tail)', async () => {
    stubDrivePresent()
    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)

    const crumb = await screen.findByTestId('console-crumb')
    // The crumb shows the account name, not a "···tail".
    expect(crumb).toHaveTextContent('personal')
    expect(crumb.textContent).not.toContain('···')
    // The header carries the full 12-digit id.
    expect(screen.getByTestId('console-account-id')).toHaveTextContent('111122223333')
    expect(screen.queryByText(/···/)).toBeNull()
  })

  it('states name, id and connection once each, with no General card', async () => {
    // The General card repeated every field it showed: name (crumb + title),
    // account id (title), region and key count (the connection rows), and a
    // connection state the title dot already carried -- at a coarser precision,
    // which was its own small lie. Identity is stated once now.
    stubDrivePresent()
    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)

    await screen.findByTestId('connections-section')
    expect(screen.queryByTestId('general-section')).toBeNull()
    expect(screen.getByTestId('console-account-id')).toHaveTextContent('111122223333')
    expect(screen.getByTestId('console-health')).toBeTruthy()
    // Region and key count are the connection row's to state.
    const conns = screen.getByTestId('connections-section')
    expect(conns).toHaveTextContent('us-west-2')
  })

  it('renders one Connections row per key with its kind, region and health', async () => {
    stubDrivePresent()
    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)

    const conns = await screen.findByTestId('connections-section')
    const rows = within(conns).getAllByTestId('connection-row')
    expect(rows).toHaveLength(1)
    expect(within(rows[0]).getByTestId('connection-name')).toHaveTextContent('personal')
    expect(rows[0]).toHaveTextContent(i18nT('apps.awsControl.page.kind_sso'))
    expect(rows[0]).toHaveTextContent('us-west-2')
    // A healthy key shows the healthy state and NO reconnect action.
    expect(rows[0]).toHaveTextContent(i18nT('apps.awsControl.console.key_healthy'))
    expect(within(rows[0]).queryByTestId('reconnect-toggle')).toBeNull()
  })

  it('shows an inline Reconnect on a failing key in Connections and loads its command', async () => {
    const degraded: AwsAccount = {
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
    vi.mocked(awsControlApi.drive).mockResolvedValue({ exists: false })
    vi.mocked(awsControlApi.costs).mockResolvedValue(costsFresh)
    vi.mocked(awsControlApi.reconnectPlan).mockResolvedValue({
      method: 'terminal', kind: 'credential-process', command: 'aws sso login --profile work',
    })

    renderWithProviders(<ConsoleView account={degraded} onBack={() => {}} />)

    const row = await screen.findByTestId('connection-row')
    expect(row).toHaveTextContent(i18nT('apps.awsControl.console.key_failed'))

    fireEvent.click(within(row).getByTestId('reconnect-toggle'))
    await waitFor(() => expect(awsControlApi.reconnectPlan).toHaveBeenCalledWith('work'))
    expect(await screen.findByTestId('reconnect-command')).toHaveTextContent('aws sso login --profile work')
  })

  it('renders only the tiles that have a data source, and no ghosts', async () => {
    // SITES and TASKS were hardcoded em-dashes with no query behind them, and
    // they said the same "connects later" as the two dashed ghost cards that
    // closed the page: four elements for two features that do not exist.
    stubDrivePresent()
    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)

    const stats = await screen.findByTestId('console-stats')
    // ONE tile, because the month-to-date bill is the only figure this page
    // alone can state - the stored bytes and object count are on the Cloud
    // drive row, which is the thing they describe. The bill arrives async, so
    // wait for it rather than reading the skeleton StatCard renders while it is
    // undefined.
    await waitFor(() => expect(within(stats).getAllByTestId('stat-card-value')).toHaveLength(1))
    expect(stats.textContent).not.toMatch(/\b0\b/)
    expect(screen.queryByTestId('console-ghosts')).toBeNull()
    expect(screen.queryAllByTestId('app-ghost')).toHaveLength(0)
    expect(screen.queryByTestId('console-guard')).toBeNull()
  })

  it('carries no Payments control: it would only restate this screen', async () => {
    // Consent is service-scoped and stays page-wide on the accounts list;
    // everything else a panel here could show is already on this page (kind and
    // region in the connection row, account id in the title, the bill in a tile).
    stubDrivePresent()
    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)

    await screen.findByTestId('connections-section')
    expect(screen.queryByTestId('console-payments-toggle')).toBeNull()
    expect(screen.queryByTestId('console-payments')).toBeNull()
  })

  it('keeps the three-state connection label and the id copy button', async () => {
    // Both came off the deleted General card. A binary dot would announce "not
    // connected" for a degraded account whose keys still partly work.
    stubDrivePresent()
    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)

    const dot = await screen.findByTestId('console-health')
    expect(dot).toHaveAttribute('aria-label', i18nT('apps.awsControl.console.connection_connected'))
    expect(screen.getByTestId('console-copy-id')).toBeTruthy()
  })

  it('shows the drive-missing setup card, previews, then confirms and invalidates', async () => {
    vi.mocked(awsControlApi.drive).mockResolvedValueOnce({ exists: false })
    vi.mocked(awsControlApi.costs).mockResolvedValue(costsFresh)
    vi.mocked(awsControlApi.driveBootstrapPreview).mockResolvedValue({
      preview: true, account: ACCOUNT.account, region: 'us-west-2', resource: 'kirocrew-drive-abc123',
    })
    vi.mocked(awsControlApi.driveBootstrapConfirm).mockResolvedValue({ created: true, bucket: 'kirocrew-drive-abc123' })

    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)

    // Setup card replaces the drive sections.
    expect(await screen.findByTestId('drive-setup')).toBeTruthy()
    expect(screen.queryByTestId('library-section')).toBeNull()

    // Preview shows the payload, confirm creates the bucket.
    fireEvent.click(screen.getByTestId('drive-preview-btn'))
    expect(await screen.findByTestId('drive-preview')).toHaveTextContent('kirocrew-drive-abc123')

    fireEvent.click(screen.getByTestId('drive-confirm-btn'))
    await waitFor(() => expect(awsControlApi.driveBootstrapConfirm).toHaveBeenCalledWith(ACCOUNT.account))
  })

  it('offers the cost confirmation when nothing has ever been confirmed', async () => {
    // The state a never-confirmed account is always in: no cached cost reading
    // for the backend to attach `consentMissing` to, so the costs request is a
    // bare 409 and that field never exists. Keying the ask on it left Cost
    // Explorer with no confirmation control anywhere in the product, so the ask
    // is driven by the consent status instead.
    vi.mocked(awsControlApi.drive).mockResolvedValue(driveExists)
    const { AwsControlError } = await import('./api')
    vi.mocked(awsControlApi.costs).mockRejectedValue(
      new AwsControlError('aws_consent_required', 409),
    )
    vi.mocked(awsControlApi.library).mockResolvedValue(emptyLibrary)
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: [] })
    vi.mocked(awsControlApi.backup).mockResolvedValue(emptyBackup)
    vi.mocked(awsControlApi.shares).mockResolvedValue(noShares)
    vi.mocked(api.awsConsent).mockResolvedValue({ granted: false } as never)

    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)

    expect(await screen.findByTestId('costs-consent-gate')).toBeTruthy()
  })

  it('shows the cost-consent nudge when costs report consentMissing', async () => {
    vi.mocked(awsControlApi.drive).mockResolvedValue(driveExists)
    vi.mocked(awsControlApi.costs).mockResolvedValue({
      fresh: false, monthToDate: 0, projected: 0, currency: 'USD',
      byService: [], fetchedAt: '2026-08-24T05:00:00Z', consentMissing: true,
    })
    vi.mocked(awsControlApi.library).mockResolvedValue(emptyLibrary)
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: [] })
    vi.mocked(awsControlApi.backup).mockResolvedValue(emptyBackup)
    vi.mocked(awsControlApi.shares).mockResolvedValue(noShares)
    vi.mocked(api.awsConsent).mockResolvedValue({ granted: false } as never)

    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)

    expect(await screen.findByTestId('costs-consent-gate')).toBeTruthy()
    // The consent gate fetches the ce service status.
    await waitFor(() => expect(api.awsConsent).toHaveBeenCalledWith('ce'))
  })

  it('carries the granted confirmations, and no empty heading without them', async () => {
    // This section is why the account list no longer shows confirmations: it is
    // the surface that keeps a granted one readable and withdrawable. It must
    // not exist when there is nothing to show, or it becomes exactly the
    // always-present placeholder this page was cleaned of.
    vi.mocked(awsControlApi.library).mockResolvedValue(emptyLibrary)
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: [] })
    vi.mocked(awsControlApi.backup).mockResolvedValue(emptyBackup)
    vi.mocked(awsControlApi.shares).mockResolvedValue(noShares)
    vi.mocked(api.awsConsent).mockResolvedValue({ granted: false } as never)

    const nothing = renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)
    await waitFor(() => expect(api.awsConsent).toHaveBeenCalledWith('s3'))
    expect(nothing.container.querySelector('[data-testid="paid-services"]')).toBeNull()
    nothing.unmount()

    vi.mocked(api.awsConsent).mockResolvedValue(
      { granted: true, grant: { account: ACCOUNT.account } } as never,
    )
    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)
    expect(await screen.findByTestId('paid-services')).toBeTruthy()
  })

  it('never shows a receipt for a grant recorded under a different account', async () => {
    // The withdraw control is GLOBAL: one grant per service. So a receipt shown
    // on the wrong account's console is not a cosmetic mislabel — clicking it
    // revokes the grant the OTHER account's drive and cost figure run on. The
    // grant carries the account it was confirmed for, and that is the test.
    vi.mocked(awsControlApi.library).mockResolvedValue(emptyLibrary)
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: [] })
    vi.mocked(awsControlApi.backup).mockResolvedValue(emptyBackup)
    vi.mocked(awsControlApi.shares).mockResolvedValue(noShares)
    vi.mocked(api.awsConsent).mockResolvedValue(
      { granted: true, grant: { account: '999988887777' } } as never,
    )

    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)
    await waitFor(() => expect(api.awsConsent).toHaveBeenCalledWith('s3'))
    expect(screen.queryByTestId('paid-services')).toBeNull()
  })

  /* ── Cost-strip failure branches ──────────────────────────────────────────
   * The month-to-date stat has three distinct renderings; the consentMissing
   * one is covered above. These two exercise the isError em-dash (a dead bill
   * read must resolve to "—", never skeleton forever) and the stale-cache "as
   * of" title (fresh:false). */

  it('renders the month-to-date stat as an em dash when the cost read fails', async () => {
    vi.mocked(awsControlApi.drive).mockResolvedValue(driveExists)
    // A rejected costs read (CE not enabled / throttled) must settle to "—".
    vi.mocked(awsControlApi.costs).mockRejectedValue(new Error('CE disabled'))
    vi.mocked(awsControlApi.library).mockResolvedValue(emptyLibrary)
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: [] })
    vi.mocked(awsControlApi.backup).mockResolvedValue(emptyBackup)
    vi.mocked(awsControlApi.shares).mockResolvedValue(noShares)

    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)

    // The month-to-date query carries retry:1, so its error settles after one
    // backoff — give waitFor room. Once it errors, the card renders its "—"
    // value and an InfoTip whose title is the "unavailable" explanation.
    const stats = await screen.findByTestId('console-stats')
    await waitFor(
      () => expect(within(stats).getByTitle(i18nT('apps.awsControl.console.costs_unavailable'))).toBeTruthy(),
      { timeout: 4000 },
    )
    // No cost-consent nudge fires — this is a failure, not a missing gate.
    expect(screen.queryByTestId('costs-consent-gate')).toBeNull()
  })

  it('shows an "as of" hint on the cost stat when the figure came from cache', async () => {
    vi.mocked(awsControlApi.drive).mockResolvedValue(driveExists)
    vi.mocked(awsControlApi.costs).mockResolvedValue({
      // fresh:false → the number is cached and must carry the "as of" title.
      fresh: false, monthToDate: 9.99, projected: 20, currency: 'USD',
      byService: [{ service: 'S3', amount: 9.99 }], fetchedAt: '2026-08-24T05:00:00Z',
    })
    vi.mocked(awsControlApi.library).mockResolvedValue(emptyLibrary)
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: [] })
    vi.mocked(awsControlApi.backup).mockResolvedValue(emptyBackup)
    vi.mocked(awsControlApi.shares).mockResolvedValue(noShares)

    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)

    const stats = await screen.findByTestId('console-stats')
    // The cached figure renders, and the stale-cache branch attaches an
    // "as of <date>" InfoTip title (the fresh branch would attach no title).
    await waitFor(() => expect(stats.textContent ?? '').toContain('9.99'))
    const asOf = i18nT('apps.awsControl.console.costs_as_of', { date: fmtDate('2026-08-24T05:00:00Z') })
    expect(within(stats).getByTitle(asOf)).toBeTruthy()
  })

  /* ── Drive: stored-usage stat, folder navigation, load-more ──────────────── */

  it('states the stored-usage figure ONCE, on the Cloud drive row and not in the stats strip', async () => {
    // The drive CONTENTS moved to DrivePage. The usage figure stayed on this
    // page but only in one place: it used to be a Stored stat tile AND the
    // capability row, which is the same fact twice on one screen. The
    // listing/folder/file/load-more assertions live in DrivePage.test.tsx.
    stubDrivePresent()

    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} onOpenDrive={() => {}} />)

    const usage = i18nT('apps.awsControl.console.stat_stored_value', {
      size: fmtBytes(driveExists.usage.bytes), objects: driveExists.usage.objects,
    })
    // On the row, reading the real figure rather than an em dash.
    const row = await screen.findByTestId('capability-drive-usage')
    await waitFor(() => expect(row.textContent ?? '').toContain(usage))
    // And nowhere else on the page.
    const stats = await screen.findByTestId('console-stats')
    expect(stats.textContent ?? '').not.toContain(usage)
  })

  /* ── Setup card: preview ERROR, and the IAM policy drawer ────────────────── */

  it('surfaces a setup preview error and does not advance to the confirm step', async () => {
    vi.mocked(awsControlApi.drive).mockResolvedValue({ exists: false })
    vi.mocked(awsControlApi.costs).mockResolvedValue(costsFresh)
    // The bootstrap preview rejects (e.g. AccessDenied): the error line shows
    // and the confirm button never appears.
    vi.mocked(awsControlApi.driveBootstrapPreview).mockRejectedValue(new Error('AccessDenied'))

    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)

    fireEvent.click(await screen.findByTestId('drive-preview-btn'))
    expect(await screen.findByTestId('drive-preview-error')).toBeTruthy()
    expect(screen.queryByTestId('drive-confirm-btn')).toBeNull()
  })

  it('reveals the IAM policy in the setup drawer and offers it to copy', async () => {
    vi.mocked(awsControlApi.drive).mockResolvedValue({ exists: false })
    vi.mocked(awsControlApi.costs).mockResolvedValue(costsFresh)
    vi.mocked(awsControlApi.iamPolicy).mockResolvedValue({ policy: '{"Version":"2012-10-17"}' })

    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)

    // The policy drawer is closed and its query disabled until toggled.
    fireEvent.click(await screen.findByTestId('policy-toggle'))
    await waitFor(() => expect(awsControlApi.iamPolicy).toHaveBeenCalled())
    const drawer = await screen.findByTestId('policy-drawer')
    expect(drawer).toHaveTextContent('2012-10-17')
    expect(within(drawer).getByTestId('policy-copy')).toBeTruthy()
  })

  /* ── Reconnect: error branch of the plan query ───────────────────────────── */

  it('shows the reconnect error state when the plan query fails', async () => {
    const degraded: AwsAccount = {
      account: '444455556666', name: 'work', health: 'degraded',
      profiles: [{
        name: 'work', region: 'eu-west-1', kind: 'other', identityOk: false,
        account: '444455556666', arn: '', detail: 'expired', default: true,
      }],
      summary: { storage: null, sites: null, tasks: null, costMonthToDate: null },
    }
    vi.mocked(awsControlApi.drive).mockResolvedValue({ exists: false })
    vi.mocked(awsControlApi.costs).mockResolvedValue(costsFresh)
    vi.mocked(awsControlApi.reconnectPlan).mockRejectedValue(new Error('plan failed'))

    renderWithProviders(<ConsoleView account={degraded} onBack={() => {}} />)

    const row = await screen.findByTestId('connection-row')
    fireEvent.click(within(row).getByTestId('reconnect-toggle'))
    // The panel resolves to its error message, not a command block.
    expect(await screen.findByTestId('reconnect-error')).toBeTruthy()
    expect(screen.queryByTestId('reconnect-command')).toBeNull()
  })

  /* ── Storage 409 branches (drive query error) ────────────────────────────── */

  it('renders the S3 consent gate when the drive read returns a 409 consent-required', async () => {
    const { AwsControlError } = await import('./api')
    vi.mocked(awsControlApi.drive).mockRejectedValue(new AwsControlError('aws_consent_required', 409))
    vi.mocked(awsControlApi.costs).mockResolvedValue(costsFresh)

    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)

    // The storage-consent block renders with a recheck button; no drive sections.
    expect(await screen.findByTestId('console-storage-consent')).toBeTruthy()
    expect(screen.getByTestId('console-consent-recheck')).toBeTruthy()
    expect(screen.queryByTestId('library-section')).toBeNull()
  })

  it('renders the account-unavailable line for a non-consent 409', async () => {
    const { AwsControlError } = await import('./api')
    vi.mocked(awsControlApi.drive).mockRejectedValue(new AwsControlError('dead_connection', 409))
    vi.mocked(awsControlApi.costs).mockResolvedValue(costsFresh)

    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)

    // A 409 that is NOT consent maps to the quiet "account unavailable" line.
    expect(await screen.findByTestId('console-unavailable')).toBeTruthy()
    expect(screen.queryByTestId('console-storage-consent')).toBeNull()
  })
})

