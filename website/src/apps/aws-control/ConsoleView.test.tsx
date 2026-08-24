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

  it('renders the General section with name, full id + copy, region, connection, keys', async () => {
    stubDrivePresent()
    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)

    const general = await screen.findByTestId('general-section')
    expect(within(general).getByTestId('general-name')).toHaveTextContent('personal')
    expect(within(general).getByTestId('general-account-id')).toHaveTextContent('111122223333')
    expect(within(general).getByTestId('general-copy-id')).toBeTruthy()
    expect(within(general).getByTestId('general-region')).toHaveTextContent('us-west-2')
    expect(within(general).getByTestId('general-connection')).toHaveTextContent(
      i18nT('apps.awsControl.console.connection_connected'),
    )
    expect(within(general).getByTestId('general-keys')).toHaveTextContent(
      i18nT('apps.awsControl.console.keys_count', { count: 1 }),
    )
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

  it('renders sites/tasks stats as em-dash ghosts', async () => {
    stubDrivePresent()
    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)

    await screen.findByTestId('console-ghosts')
    const stats = screen.getByTestId('console-stats')
    const values = within(stats).getAllByTestId('stat-card-value').map((n) => n.textContent)
    // Sites and Tasks are em dashes; a null figure must never read as 0.
    expect(values.filter((v) => v === '—').length).toBeGreaterThanOrEqual(2)
    expect(stats.textContent).not.toMatch(/\b0\b/)
    // Two dashed app-ghost cards render.
    expect(screen.getAllByTestId('app-ghost')).toHaveLength(2)
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

  it('mints a share link and shows the URL exactly once in the dialog', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'report.pdf', size: 2048, modified: '2026-08-20T00:00:00Z' }],
      folders: [],
    })
    vi.mocked(awsControlApi.driveShare).mockResolvedValue({
      url: 'https://example-presigned/report.pdf?sig=x',
      share: {
        id: 's1', account: ACCOUNT.account, section: 'drive', key: 'report.pdf',
        createdAt: '2026-08-24T05:00:00Z', expiresAt: '2026-08-24T06:00:00Z', note: '',
      },
    })

    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)

    // Share lives in the per-row overflow menu (rows carry at most two
    // sibling controls: Download + More).
    fireEvent.click(await screen.findByTestId('drive-more'))
    fireEvent.click(await screen.findByTestId('drive-share'))
    fireEvent.click(await screen.findByTestId('share-create'))

    const result = await screen.findByTestId('share-result')
    expect(result).toHaveTextContent('https://example-presigned/report.pdf?sig=x')
    // The URL lives only inside the dialog result — not duplicated on the page.
    expect(screen.getAllByText(/example-presigned/).length).toBe(1)
  })

  it('delete asks for confirmation restating the filename, and cancel keeps the file', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'report.pdf', size: 2048, modified: '2026-08-20T00:00:00Z' }],
      folders: [],
    })
    vi.mocked(awsControlApi.driveDelete).mockResolvedValue({ deleted: true })

    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)

    // Delete in the overflow menu opens a confirm strip — it must NOT delete.
    fireEvent.click(await screen.findByTestId('drive-more'))
    fireEvent.click(await screen.findByTestId('drive-delete'))
    const strip = await screen.findByTestId('drive-delete-confirm')
    expect(strip).toHaveTextContent('report.pdf')
    expect(awsControlApi.driveDelete).not.toHaveBeenCalled()

    // Cancel dismisses without deleting.
    fireEvent.click(screen.getByTestId('drive-delete-cancel'))
    expect(screen.queryByTestId('drive-delete-confirm')).toBeNull()
    expect(awsControlApi.driveDelete).not.toHaveBeenCalled()

    // Confirming actually deletes.
    fireEvent.click(await screen.findByTestId('drive-more'))
    fireEvent.click(await screen.findByTestId('drive-delete'))
    fireEvent.click(await screen.findByTestId('drive-delete-confirm-action'))
    await waitFor(() => expect(awsControlApi.driveDelete).toHaveBeenCalledWith(ACCOUNT.account, 'drive', 'report.pdf'))
  })

  it('renders the shares ledger with an expires-in countdown', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.shares).mockResolvedValue({
      shares: [{
        id: 's1', account: ACCOUNT.account, section: 'drive', key: 'report.pdf',
        createdAt: '2026-08-24T05:00:00Z', expiresAt: '2030-01-01T00:00:00Z', note: 'for review',
      }],
    })

    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)

    const row = await screen.findByTestId('access-row')
    expect(row).toHaveTextContent('report.pdf')
    expect(row).toHaveTextContent('for review')
    // A relative "expires …" phrase renders (not a raw ISO timestamp).
    expect(row.textContent).not.toContain('2030-01-01')
  })

  it('disables the backup row and spins while a run is in flight', async () => {
    stubDrivePresent()
    // A run that never resolves keeps the row in its busy state.
    vi.mocked(awsControlApi.backupRun).mockReturnValue(new Promise(() => {}) as ReturnType<typeof awsControlApi.backupRun>)

    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)

    const runBtn = await screen.findByTestId('backup-run-snapshot')
    fireEvent.click(runBtn)
    await waitFor(() => expect((screen.getByTestId('backup-run-snapshot') as HTMLButtonElement).disabled).toBe(true))
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

    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)

    expect(await screen.findByTestId('costs-consent-gate')).toBeTruthy()
    // The consent gate fetches the ce service status.
    await waitFor(() => expect(api.awsConsent).toHaveBeenCalledWith('ce'))
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

  it('renders the stored-usage stat and lists a folder and file with a download', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'report.pdf', size: 2048, modified: '2026-08-20T00:00:00Z' }],
      folders: ['invoices'],
      nextToken: 'tok-2',
    })

    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)

    // The Stored stat reads the real usage figure (drive exists), not an em dash.
    const stats = await screen.findByTestId('console-stats')
    const storedValue = i18nT('apps.awsControl.console.stat_stored_value', {
      size: fmtBytes(driveExists.usage.bytes), objects: driveExists.usage.objects,
    })
    await waitFor(() => expect(stats.textContent ?? '').toContain(storedValue))

    // A folder row and a file row both render.
    expect(await screen.findByTestId('drive-folder')).toHaveTextContent('invoices')
    const file = await screen.findByTestId('drive-file')
    expect(file).toHaveTextContent('report.pdf')
    // A nextToken produces a Load more control.
    expect(screen.getByTestId('drive-load-more')).toBeTruthy()
  })

  it('drills into a folder, refetching the listing for the new path', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [], folders: ['invoices'],
    })

    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)

    fireEvent.click(await screen.findByTestId('drive-folder'))
    // Clicking the folder re-queries driveList with the folder as the path.
    await waitFor(() =>
      expect(awsControlApi.driveList).toHaveBeenCalledWith(ACCOUNT.account, 'drive', 'invoices', ''),
    )
  })

  it('keeps the breadcrumb at two controls and still reaches an ancestor', async () => {
    // AUTOSDE max-two-buttons-per-row: Root plus ONE overflow, and the folder
    // you are in is text. The earlier shape rendered a button per crumb, so
    // `a/b` put three sibling buttons in one group. The ancestors moved into the
    // overflow rather than becoming flat text, so jumping to `a` from `a/b`
    // still works -- that capability is what this pins.
    // The listing returns folder values relative to the SECTION root, not to the
    // current subpath (storage.list_section strips only the section prefix), so
    // one click on `a/b` lands at depth 2.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [], folders: ['a/b'],
    })
    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)

    fireEvent.click(await screen.findByTestId('drive-folder'))
    await waitFor(() =>
      expect(awsControlApi.driveList).toHaveBeenCalledWith(ACCOUNT.account, 'drive', 'a/b', ''),
    )

    // The crumb group holds exactly two controls: Root and the overflow.
    const crumbs = await screen.findByTestId('drive-crumbs')
    expect(crumbs.querySelectorAll('button')).toHaveLength(2)
    // The current folder is rendered as text, not as a third control.
    expect(screen.getByTestId('drive-crumb-current')).toHaveTextContent('b')

    // The overflow still navigates to the ancestor `a`.
    fireEvent.click(screen.getByTestId('drive-crumb-more'))
    const menu = screen.getByTestId('drive-crumb-menu')
    expect(menu.querySelectorAll('button')).toHaveLength(1)
    fireEvent.click(menu.querySelectorAll('button')[0])
    await waitFor(() =>
      expect(awsControlApi.driveList).toHaveBeenCalledWith(ACCOUNT.account, 'drive', 'a', ''),
    )
  })

  it('shows no breadcrumb overflow at the top level or one folder deep', async () => {
    // With nothing to jump PAST, an overflow would be an empty affordance.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [], folders: ['invoices'],
    })
    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)

    // At the root: Root only.
    const crumbs = await screen.findByTestId('drive-crumbs')
    expect(crumbs.querySelectorAll('button')).toHaveLength(1)
    expect(screen.queryByTestId('drive-crumb-more')).toBeNull()

    // One level deep: still no overflow, and the folder shows as text.
    fireEvent.click(await screen.findByTestId('drive-folder'))
    await waitFor(() =>
      expect(screen.getByTestId('drive-crumb-current')).toHaveTextContent('invoices'),
    )
    expect(screen.queryByTestId('drive-crumb-more')).toBeNull()
    expect(crumbs.querySelectorAll('button')).toHaveLength(1)
  })

  it('shows the drive empty state when the listing has no files or folders', async () => {
    stubDrivePresent()
    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)
    expect(await screen.findByTestId('drive-empty')).toBeTruthy()
  })

  /* ── Drive: download handler (opens a tab synchronously) ─────────────────── */

  it('opens a blank tab synchronously and navigates it once the presign resolves', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'report.pdf', size: 2048, modified: '2026-08-20T00:00:00Z' }],
      folders: [],
    })
    vi.mocked(awsControlApi.driveDownload).mockResolvedValue({
      url: 'https://example-presigned/dl?sig=y', expiresSecs: 300,
    })
    // A fake tab whose location we can inspect: the handler must set its href.
    const fakeTab = { location: { href: '' }, close: vi.fn() } as unknown as Window
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(fakeTab)

    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)

    fireEvent.click(await screen.findByTestId('drive-download'))
    // The tab was opened blank inside the click, then navigated to the presign.
    // No 'noopener' feature: with it the standard makes window.open return null,
    // so requesting it would hand back no tab to navigate at all.
    expect(openSpy).toHaveBeenCalledWith('', '_blank')
    await waitFor(() => expect(fakeTab.location.href).toBe('https://example-presigned/dl?sig=y'))
    expect(fakeTab.close).not.toHaveBeenCalled()
    openSpy.mockRestore()
  })

  it('closes the blank tab when the download presign fails', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'report.pdf', size: 2048, modified: '2026-08-20T00:00:00Z' }],
      folders: [],
    })
    // The presign rejects — the orphan blank tab must be closed, not left open.
    vi.mocked(awsControlApi.driveDownload).mockRejectedValue(new Error('AccessDenied'))
    const fakeTab = { location: { href: '' }, close: vi.fn() } as unknown as Window
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(fakeTab)

    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)

    fireEvent.click(await screen.findByTestId('drive-download'))
    await waitFor(() => expect(fakeTab.close).toHaveBeenCalled())
    // The tab was never navigated anywhere.
    expect((fakeTab as unknown as { location: { href: string } }).location.href).toBe('')
    // And the failure is REPORTED. Rethrowing here would surface as an
    // unhandled rejection from an onClick with no catch, which tells the user
    // nothing; the row must say the download did not start.
    expect(await screen.findByTestId('drive-download-error')).toBeTruthy()
    openSpy.mockRestore()
  })

  /* ── Drive: upload flow, including the client-side bad-name guard ────────── */

  it('rejects a bad filename client-side without ever calling upload', async () => {
    stubDrivePresent()
    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)

    const input = await screen.findByTestId('drive-file-input')
    // A leading-dot name violates KEY_SEGMENT: the guard surfaces an error and
    // must NOT call the upload api.
    const bad = new File(['x'], '.hidden', { type: 'text/plain' })
    fireEvent.change(input, { target: { files: [bad] } })

    expect(await screen.findByTestId('drive-upload-error')).toBeTruthy()
    expect(awsControlApi.driveUpload).not.toHaveBeenCalled()
  })

  it('uploads a well-named file through the api and invalidates', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveUpload).mockResolvedValue({ uploaded: true, key: 'ok.txt', bytes: 1 })

    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)

    const input = await screen.findByTestId('drive-file-input')
    const good = new File(['x'], 'ok.txt', { type: 'text/plain' })
    fireEvent.change(input, { target: { files: [good] } })

    // The api is called with the section and the file, and no error strip shows.
    await waitFor(() =>
      expect(awsControlApi.driveUpload).toHaveBeenCalledWith(ACCOUNT.account, 'drive', 'ok.txt', good),
    )
    expect(screen.queryByTestId('drive-upload-error')).toBeNull()
  })

  /* ── Drive: delete ERROR state ───────────────────────────────────────────
   * The happy delete path is covered above; this drives the mutation into its
   * error rendering (the confirm strip must show the failure and keep itself
   * open so the owner can retry). */

  it('shows the delete-failed message and keeps the confirm strip open on error', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'report.pdf', size: 2048, modified: '2026-08-20T00:00:00Z' }],
      folders: [],
    })
    vi.mocked(awsControlApi.driveDelete).mockRejectedValue(new Error('AccessDenied'))

    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)

    fireEvent.click(await screen.findByTestId('drive-more'))
    fireEvent.click(await screen.findByTestId('drive-delete'))
    fireEvent.click(await screen.findByTestId('drive-delete-confirm-action'))

    // The failure surfaces in-strip and the strip stays open (no onSuccess close).
    expect(await screen.findByTestId('drive-delete-error')).toBeTruthy()
    expect(screen.getByTestId('drive-delete-confirm')).toBeTruthy()
  })

  /* ── Library: chip filtering + push flow ─────────────────────────────────── */

  it('filters library tiles by kind chip and pushes an out-of-date artifact', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.library).mockResolvedValue({
      artifacts: [
        // A markdown never synced (pushable), and an image (not pushable).
        { slug: 'notes', name: 'Notes', kind: 'markdown', version: 3, updatedAt: '2026-08-20T00:00:00Z', pushedVersion: null, pushedAt: null },
        { slug: 'pic', name: 'Pic', kind: 'image', version: 1, updatedAt: '2026-08-20T00:00:00Z', pushedVersion: null, pushedAt: null },
      ],
    })
    vi.mocked(awsControlApi.libraryPush).mockResolvedValue({ pushed: true, slug: 'notes', version: 3 } as never)

    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)

    // Both tiles show under "all" (wait for the library query to settle).
    await screen.findByTestId('library-section')
    expect(await screen.findAllByTestId('library-tile')).toHaveLength(2)

    // Selecting the image chip narrows to the single image tile.
    fireEvent.click(screen.getByTestId('library-chip-image'))
    expect(screen.getAllByTestId('library-tile')).toHaveLength(1)

    // Back to markdown chip, then push the markdown artifact.
    fireEvent.click(screen.getByTestId('library-chip-markdown'))
    fireEvent.click(screen.getByTestId('library-push'))
    await waitFor(() => expect(awsControlApi.libraryPush).toHaveBeenCalledWith(ACCOUNT.account, 'notes'))
  })

  it('shows the library empty state when a filter matches nothing', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.library).mockResolvedValue({
      artifacts: [
        { slug: 'notes', name: 'Notes', kind: 'markdown', version: 1, updatedAt: '2026-08-20T00:00:00Z', pushedVersion: 1, pushedAt: '2026-08-20T00:00:00Z' },
      ],
    })

    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)

    // Filter to a kind with no artifacts → the empty state renders.
    fireEvent.click(await screen.findByTestId('library-chip-webapp'))
    expect(await screen.findByTestId('library-empty')).toBeTruthy()
    // The synced markdown tile was up-to-date, so its push button is disabled.
    fireEvent.click(screen.getByTestId('library-chip-markdown'))
    expect((screen.getByTestId('library-push') as HTMLButtonElement).disabled).toBe(true)
  })

  /* ── Share: error branch, and cancel via the close button ────────────────── */

  it('leaves the share dialog on its form when share creation fails', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'report.pdf', size: 2048, modified: '2026-08-20T00:00:00Z' }],
      folders: [],
    })
    vi.mocked(awsControlApi.driveShare).mockRejectedValue(new Error('AccessDenied'))

    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)

    fireEvent.click(await screen.findByTestId('drive-more'))
    fireEvent.click(await screen.findByTestId('drive-share'))
    // Pick a non-default expiry and type a note before creating.
    fireEvent.click(await screen.findByTestId('share-expiry-7d'))
    fireEvent.change(screen.getByTestId('share-note'), { target: { value: 'quarterly' } })
    fireEvent.click(screen.getByTestId('share-create'))

    // The api was called with the chosen expiry seconds + note; on failure the
    // dialog keeps its form (no result panel) so the owner can retry.
    await waitFor(() =>
      expect(awsControlApi.driveShare).toHaveBeenCalledWith(ACCOUNT.account, 'drive', 'report.pdf', 604800, 'quarterly'),
    )
    expect(screen.queryByTestId('share-result')).toBeNull()
    // The close button dismisses the dialog entirely.
    fireEvent.click(screen.getByTestId('share-close'))
    await waitFor(() => expect(screen.queryByTestId('share-dialog')).toBeNull())
  })

  /* ── Backup: run success, nightly toggle, stored-archive disclosure, restore ─ */

  it('runs a backup, toggles nightly, and both invalidate through the api', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.backup).mockResolvedValue({
      nightly: false,
      runs: { snapshot: { key: 'snap-1', bytes: 1024, at: '2026-08-24T00:00:00Z' } },
      remote: { snapshot: [], sessions: [] },
    })
    vi.mocked(awsControlApi.backupRun).mockResolvedValue({
      ran: true, kind: 'snapshot', run: { key: 'snap-2', bytes: 2048, at: '2026-08-25T00:00:00Z' },
    })
    vi.mocked(awsControlApi.backupNightly).mockResolvedValue({ nightly: true } as never)

    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)

    // The snapshot row shows its last-run line, then Run now calls the api.
    await screen.findByTestId('backup-row-snapshot')
    fireEvent.click(screen.getByTestId('backup-run-snapshot'))
    await waitFor(() => expect(awsControlApi.backupRun).toHaveBeenCalledWith(ACCOUNT.account, 'snapshot'))

    // The sessions row carries its extra scope caveat.
    expect(screen.getByTestId('backup-sessions-scope')).toBeTruthy()

    // Flipping the nightly toggle calls the api with the new value.
    const toggle = within(screen.getByTestId('backup-nightly')).getByRole('switch')
    fireEvent.click(toggle)
    await waitFor(() => expect(awsControlApi.backupNightly).toHaveBeenCalledWith(ACCOUNT.account, true))
  })

  it('discloses the stored-backups archive and restores a file, showing the staged path', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.backup).mockResolvedValue({
      nightly: true,
      runs: {},
      remote: {
        snapshot: [{ key: 'backup/snapshot/2026-08-24.tar', size: 4096, modified: '2026-08-24T00:00:00Z' }],
        sessions: [],
      },
    })
    vi.mocked(awsControlApi.backupRestore).mockResolvedValue({
      downloaded: true, path: '/home/u/.kiro/restore/2026-08-24', bytes: 4096,
    })

    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)

    // The archive is behind a disclosure; before opening, no rows are shown.
    fireEvent.click(await screen.findByTestId('backup-remote-toggle'))
    const archive = await screen.findByTestId('backup-archive')
    expect(within(archive).getByTestId('backup-archive-row')).toHaveTextContent('2026-08-24.tar')
    // The write-only-tier restore caveat renders alongside.
    expect(screen.getByTestId('backup-restore-caveat')).toBeTruthy()

    // Restore stages the archive locally and echoes the landed path.
    fireEvent.click(within(archive).getByTestId('backup-restore'))
    await waitFor(() => expect(awsControlApi.backupRestore).toHaveBeenCalledWith(ACCOUNT.account, 'backup/snapshot/2026-08-24.tar'))
    expect(await screen.findByTestId('backup-restored')).toHaveTextContent('/home/u/.kiro/restore/2026-08-24')
  })

  it('shows the backup remote-error note when the archive could not be read', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.backup).mockResolvedValue({
      nightly: false, runs: {}, remote: null, remoteError: 'AccessDenied',
    })

    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)

    // A null remote with a reason renders the muted error note and NO disclosure.
    expect(await screen.findByTestId('backup-remote-error')).toBeTruthy()
    expect(screen.queryByTestId('backup-remote-toggle')).toBeNull()
  })

  /* ── Access: forget a share ──────────────────────────────────────────────── */

  it('forgets a share through the api', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.shares).mockResolvedValue({
      shares: [{
        id: 's1', account: ACCOUNT.account, section: 'library', key: 'w/notes',
        createdAt: '2026-08-24T05:00:00Z', expiresAt: '2030-01-01T00:00:00Z', note: '',
      }],
    })
    vi.mocked(awsControlApi.shareForget).mockResolvedValue({ forgotten: true } as never)

    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)

    fireEvent.click(await screen.findByTestId('access-forget'))
    await waitFor(() => expect(awsControlApi.shareForget).toHaveBeenCalledWith('s1'))
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

  /* ── CLI drawer disclosure ───────────────────────────────────────────────── */

  it('reveals a copyable CLI line in the library section drawer', async () => {
    stubDrivePresent()
    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)

    // The drawer is collapsed by default; opening it shows the aws s3 ls line
    // scoped to the artifacts/ prefix of the account's bucket.
    const drawers = await screen.findAllByTestId('cli-drawer-toggle')
    fireEvent.click(drawers[0])
    const body = await screen.findByTestId('cli-drawer-body')
    expect(body).toHaveTextContent('aws s3 ls s3://kirocrew-drive-abc123/artifacts/')
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


describe('download tab: the noopener trap', () => {
  it('opens the synchronous tab WITHOUT noopener and nulls opener instead', async () => {
    // Per the HTML standard a window.open carrying 'noopener' returns NULL, so
    // requesting it here makes the synchronous handle unusable and the whole
    // preserve-user-activation fix a no-op. The previous test suite could not
    // see that: it MOCKED window.open into returning a tab, so the mock was
    // greener than the browser. This asserts the CALL, which is the only part a
    // mock cannot lie about, plus the isolation that replaces the feature.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'report.pdf', size: 2048, modified: '2026-08-20T00:00:00Z' }],
      folders: [],
    })
    vi.mocked(awsControlApi.driveDownload).mockResolvedValue({
      url: 'https://signed.example/report.pdf',
      expiresSecs: 900,
    })
    const fakeTab = { location: { href: '' }, close: vi.fn(), opener: {} } as unknown as Window
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(fakeTab)

    renderWithProviders(<ConsoleView account={ACCOUNT} onBack={() => {}} />)
    fireEvent.click(await screen.findByTestId('drive-download'))

    const firstArgs = openSpy.mock.calls[0]
    expect(firstArgs[0]).toBe('')
    expect(String(firstArgs[2] ?? '')).not.toContain('noopener')
    // The isolation the feature would have given is applied to the handle.
    expect((fakeTab as unknown as { opener: unknown }).opener).toBeNull()
    await waitFor(() =>
      expect((fakeTab as unknown as { location: { href: string } }).location.href).toBe(
        'https://signed.example/report.pdf',
      ),
    )
    openSpy.mockRestore()
  })
})
