import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'

// Controllable viewport switch: the shell branches on useIsNarrowViewport.
// Mock BOTH exports — a partial mock leaves the sibling undefined (module's
// own warning) and useIsMobile is consumed by nested library components.
let narrowViewport = false
vi.mock('../../hooks/useIsMobile', () => ({
  useIsNarrowViewport: () => narrowViewport,
  useIsMobile: () => narrowViewport,
}))
import { i18nT } from '../../i18n/t'
import { fmtNumber } from '../../i18n/format'
import type {
  AwsAccountsResponse, AvailableProfilesResponse, DriveStatus, SharesResponse,
  CostReport, LibraryResponse, BackupStatus,
} from './types'

/* ── AWS Control api client mock ──────────────────────────────────────────
 * The shell and every pane read only through these methods, so mocking them
 * keeps every case network-free. `AwsControlError` is the real class so
 * `instanceof` and `.status` behave as in production. */
vi.mock('./api', async () => {
  const actual = await vi.importActual<typeof import('./api')>('./api')
  return {
    ...actual,
    awsControlApi: {
      accounts: vi.fn(),
      reconnectPlan: vi.fn(),
      availableProfiles: vi.fn(),
      registerProfiles: vi.fn(),
      iamPolicy: vi.fn(),
      drive: vi.fn(),
      driveBootstrapPreview: vi.fn(),
      driveBootstrapConfirm: vi.fn(),
      driveList: vi.fn(),
      driveDownload: vi.fn(),
      driveUpload: vi.fn(),
      driveDelete: vi.fn(),
      driveFolderCreate: vi.fn(),
      driveFolderDelete: vi.fn(),
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

/* The paid-service gates fetch their own consent status through the shared
 * client; stub it so they mount without hitting the network. */
vi.mock('../../api/client', () => ({
  api: {
    awsConsent: vi.fn(),
    grantAwsConsent: vi.fn(),
    revokeAwsConsent: vi.fn(),
  },
}))

/* Radix dropdown menus need real pointer-capture flows the test DOM cannot
 * simulate; the stateful mock opens on trigger click and closes on item select. */
vi.mock('@radix-ui/react-dropdown-menu', async () =>
  await import('../../test/__mocks__/@radix-ui/react-dropdown-menu'))

import { awsControlApi, AwsControlError } from './api'
import { api } from '../../api/client'
import AwsControlPage from './AwsControlPage'

const SELECTED_KEY = 'awsControl.selectedAccount'

function accountsPayload(overrides: Partial<AwsAccountsResponse> = {}): AwsAccountsResponse {
  return {
    accounts: [
      {
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
      },
      {
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
      },
    ],
    totals: { accounts: 2, profiles: 2, profilesHealthy: 1 },
    generatedAt: '2026-08-24T05:00:00Z',
    ...overrides,
  }
}

/** The unresolved pseudo-row; the backend always returns it last. */
const UNRESOLVED_ROW = {
  account: '',
  name: '',
  health: 'unknown' as const,
  profiles: [
    {
      name: 'stale-profile', region: 'us-west-2', kind: 'sso' as const, identityOk: false,
      account: '', arn: '', detail: 'token expired', default: false,
    },
  ],
  summary: { storage: null, sites: null, tasks: null, costMonthToDate: null },
}

function availablePayload(
  overrides: Partial<AvailableProfilesResponse> = {},
): AvailableProfilesResponse {
  return {
    profiles: [
      { name: 'personal', registered: true },
      { name: 'staging', registered: false },
      { name: 'sandbox', registered: false },
    ],
    registeredCount: 1,
    max: 50,
    supported: true,
    ...overrides,
  }
}

const driveExists: DriveStatus = {
  exists: true,
  bucket: 'kirocrew-drive-abc123',
  region: 'us-west-2',
  usage: {
    bytes: 3_500_000_000,
    objects: 42,
    sections: {
      drive: { objects: 30, bytes: 3_000_000_000 },
      library: { objects: 10, bytes: 1_000_000 },
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

function share(id: string): SharesResponse['shares'][number] {
  return {
    id, account: '111122223333', section: 'drive', key: `f-${id}.txt`,
    createdAt: '2026-08-24T05:00:00Z', expiresAt: '2026-08-25T05:00:00Z', note: '',
  }
}

/** Everything a drive-backed pane needs to mount for real. */
function stubDrivePresent() {
  narrowViewport = false
  vi.mocked(awsControlApi.drive).mockResolvedValue(driveExists)
  vi.mocked(awsControlApi.costs).mockResolvedValue(costsFresh)
  vi.mocked(awsControlApi.library).mockResolvedValue(emptyLibrary)
  vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: [] })
  vi.mocked(awsControlApi.backup).mockResolvedValue(emptyBackup)
  vi.mocked(awsControlApi.shares).mockResolvedValue(noShares)
}

/** Open the rail's account switcher menu (stateful mock: click toggles it). */
async function openSwitcher() {
  fireEvent.click(screen.getByTestId('account-switcher'))
  await screen.findByTestId('switcher-manage')
}

beforeEach(() => {
  vi.clearAllMocks()
  // The selected account persists across visits; a leftover from a previous
  // test must not decide which account the next one lands on.
  localStorage.clear()
  // Keep the consent gates quiet: a never-resolving probe leaves them rendering
  // nothing, which is fine for the assertions here — we only need the page
  // around them to mount.
  vi.mocked(api.awsConsent).mockReturnValue(new Promise(() => {}) as ReturnType<typeof api.awsConsent>)
  // Defaults so cases that don't exercise these paths still mount their
  // queries without unhandled rejections.
  vi.mocked(awsControlApi.availableProfiles).mockResolvedValue(availablePayload())
  vi.mocked(awsControlApi.accounts).mockResolvedValue(accountsPayload())
  stubDrivePresent()
})

describe('AwsControlPage shell', () => {
  it('lands on the Files pane with the rail, no account chooser in the way', async () => {
    renderWithProviders(<AwsControlPage />)

    // The rail mounts, Files is the active item, and its pane renders.
    const rail = await screen.findByTestId('aws-rail')
    expect(within(rail).getByTestId('rail-files').getAttribute('aria-current')).toBe('page')
    expect(within(rail).getByTestId('rail-library').getAttribute('aria-current')).toBeNull()
    expect(await screen.findByTestId('drive-section')).toBeTruthy()

    // The account card names the first resolved account — as context, not a chooser.
    expect(screen.getByTestId('switcher-name')).toHaveTextContent('personal')
    expect(screen.getByTestId('switcher-id')).toHaveTextContent('111122223333')
    expect(screen.queryByTestId('accounts-pane')).toBeNull()
  })

  it('shows per-pane counts from drive usage + the share ledger, and the drive meta', async () => {
    vi.mocked(awsControlApi.shares).mockResolvedValue({ shares: [share('a'), share('b'), share('c')] })
    renderWithProviders(<AwsControlPage />)

    await screen.findByTestId('aws-rail')
    await waitFor(() => {
      expect(screen.getByTestId('rail-files-count')).toHaveTextContent(fmtNumber(30))
    })
    expect(screen.getByTestId('rail-library-count')).toHaveTextContent(fmtNumber(10))
    expect(screen.getByTestId('rail-backup-count')).toHaveTextContent(fmtNumber(2))
    expect(screen.getByTestId('rail-shares-count')).toHaveTextContent(fmtNumber(3))
    // The drive's identity at the rail's foot: bucket + region.
    const meta = screen.getByTestId('rail-meta')
    expect(meta).toHaveTextContent('kirocrew-drive-abc123')
    expect(meta).toHaveTextContent('us-west-2')
  })

  it('each rail item opens its pane', async () => {
    renderWithProviders(<AwsControlPage />)
    await screen.findByTestId('drive-section')

    fireEvent.click(screen.getByTestId('rail-library'))
    expect(await screen.findByTestId('library-section')).toBeTruthy()
    expect(screen.queryByTestId('drive-section')).toBeNull()

    fireEvent.click(screen.getByTestId('rail-backup'))
    expect(await screen.findByTestId('backup-section')).toBeTruthy()

    fireEvent.click(screen.getByTestId('rail-shares'))
    expect(await screen.findByTestId('access-section')).toBeTruthy()

    fireEvent.click(screen.getByTestId('rail-usage'))
    expect(await screen.findByTestId('usage-pane')).toBeTruthy()

    fireEvent.click(screen.getByTestId('rail-accounts'))
    expect(await screen.findByTestId('accounts-pane')).toBeTruthy()
    expect(screen.getByTestId('rail-accounts').getAttribute('aria-current')).toBe('page')

    fireEvent.click(screen.getByTestId('rail-files'))
    expect(await screen.findByTestId('drive-section')).toBeTruthy()
  })
})

describe('account selection', () => {
  it('honors the persisted account on mount, falling back when it no longer resolves', async () => {
    localStorage.setItem(SELECTED_KEY, '444455556666')
    const r = renderWithProviders(<AwsControlPage />)

    await screen.findByTestId('aws-rail')
    expect(screen.getByTestId('switcher-id')).toHaveTextContent('444455556666')
    expect(awsControlApi.drive).toHaveBeenCalledWith('444455556666')
    r.unmount()

    // A remembered id that matches no resolved account must not strand the
    // reader on a chooser: the first resolved account takes over.
    localStorage.setItem(SELECTED_KEY, '999988887777')
    renderWithProviders(<AwsControlPage />)
    await screen.findByTestId('aws-rail')
    expect(screen.getByTestId('switcher-id')).toHaveTextContent('111122223333')
  })

  it('switches account from the rail card, persists it, and survives a remount', async () => {
    // The unresolved pseudo-row must NOT appear in the switcher — there is no
    // account behind it to select.
    vi.mocked(awsControlApi.accounts).mockResolvedValue(accountsPayload({
      accounts: [...accountsPayload().accounts, UNRESOLVED_ROW],
      totals: { accounts: 3, profiles: 3, profilesHealthy: 1 },
    }))
    const r = renderWithProviders(<AwsControlPage />)
    await screen.findByTestId('aws-rail')

    await openSwitcher()
    const options = screen.getAllByTestId('switcher-option')
    expect(options.map((o) => o.getAttribute('data-account'))).toEqual([
      '111122223333', '444455556666',
    ])

    fireEvent.click(options.find((o) => o.getAttribute('data-account') === '444455556666')!)
    await waitFor(() => {
      expect(screen.getByTestId('switcher-id')).toHaveTextContent('444455556666')
    })
    // The drive queries re-key onto the new account…
    expect(awsControlApi.drive).toHaveBeenCalledWith('444455556666')
    // …and the choice persists: a fresh mount lands on the same account.
    await waitFor(() => expect(localStorage.getItem(SELECTED_KEY)).toBe('444455556666'))
    r.unmount()

    renderWithProviders(<AwsControlPage />)
    await screen.findByTestId('aws-rail')
    expect(screen.getByTestId('switcher-id')).toHaveTextContent('444455556666')
  })

  it('switching account resets the panes\u2019 account-bound transient state', async () => {
    // Regression pin for the cross-account confirm leak: transient pane state
    // (an armed confirm, an open disclosure) is ACCOUNT-BOUND \u2014 armed on
    // account A it must not stay armed once the reader switches to B, or the
    // action fires against B's same-named object. The pane container is keyed
    // by the selected account, so a switch remounts the pane tree clean.
    renderWithProviders(<AwsControlPage />)
    await screen.findByTestId('drive-section')

    // Arm account-bound transient state: open the folder-creation disclosure.
    fireEvent.click(screen.getByTestId('drive-folder-toggle'))
    expect(await screen.findByTestId('drive-folder-name')).toBeTruthy()

    // Switch to the other account.
    await openSwitcher()
    fireEvent.click(
      screen.getAllByTestId('switcher-option')
        .find((o) => o.getAttribute('data-account') === '444455556666')!,
    )
    await waitFor(() => {
      expect(screen.getByTestId('switcher-id')).toHaveTextContent('444455556666')
    })

    // The disclosure must be back at its collapsed default \u2014 the pane
    // remounted rather than carrying A's armed state onto B.
    await waitFor(() => {
      expect(screen.queryByTestId('drive-folder-name')).toBeNull()
      expect(screen.getByTestId('drive-folder-toggle')).toBeTruthy()
    })
  })

  it("the switcher menu's last item opens the accounts pane", async () => {
    renderWithProviders(<AwsControlPage />)
    await screen.findByTestId('drive-section')

    await openSwitcher()
    fireEvent.click(screen.getByTestId('switcher-manage'))

    expect(await screen.findByTestId('accounts-pane')).toBeTruthy()
    expect(screen.getByTestId('rail-accounts').getAttribute('aria-current')).toBe('page')
  })

  it('selecting a resolved row on the accounts pane switches account AND jumps to Files', async () => {
    renderWithProviders(<AwsControlPage />)
    await screen.findByTestId('drive-section')
    fireEvent.click(screen.getByTestId('rail-accounts'))

    const rows = await screen.findAllByTestId('account-card')
    // The row the app is currently on carries the check, the other does not.
    expect(rows[0].getAttribute('data-current')).toBe('true')
    expect(within(rows[0]).getByTestId('account-current')).toBeTruthy()
    expect(rows[1].getAttribute('data-current')).toBeNull()

    fireEvent.click(rows[1])
    // Back on Files, on the newly selected account, and remembered.
    expect(await screen.findByTestId('drive-section')).toBeTruthy()
    expect(screen.queryByTestId('accounts-pane')).toBeNull()
    expect(screen.getByTestId('switcher-id')).toHaveTextContent('444455556666')
    await waitFor(() => expect(localStorage.getItem(SELECTED_KEY)).toBe('444455556666'))
  })
})

describe('DrivePaneGate', () => {
  it('renders the pane header while the drive status is still loading', async () => {
    vi.mocked(awsControlApi.drive).mockReturnValue(
      new Promise(() => {}) as ReturnType<typeof awsControlApi.drive>,
    )
    renderWithProviders(<AwsControlPage />)

    // The rail selection and the pane title agree even before the drive answers.
    expect(await screen.findByTestId('gate-files')).toBeTruthy()
    expect(screen.queryByTestId('drive-section')).toBeNull()
    expect(screen.queryByTestId('console-unavailable')).toBeNull()
    expect(screen.queryByTestId('capability-drive-setup')).toBeNull()
  })

  it('a storage-consent 409 renders the s3 ask with a recheck that refetches the drive', async () => {
    vi.mocked(awsControlApi.drive).mockRejectedValue(new AwsControlError('aws_consent_required', 409))
    renderWithProviders(<AwsControlPage />)

    expect(await screen.findByTestId('console-storage-consent')).toBeTruthy()
    expect(screen.getByTestId('console-consent-recheck')).toBeTruthy()
    expect(screen.queryByTestId('console-unavailable')).toBeNull()
    expect(screen.queryByTestId('drive-section')).toBeNull()

    // Recheck invalidates the drive query; once the backend answers with a
    // real drive, the pane renders where the ask stood.
    vi.mocked(awsControlApi.drive).mockResolvedValue(driveExists)
    fireEvent.click(screen.getByTestId('console-consent-recheck'))
    expect(await screen.findByTestId('drive-section')).toBeTruthy()
    expect(screen.queryByTestId('console-storage-consent')).toBeNull()
  })

  it('any other 409 points back at the connection, not at consent', async () => {
    vi.mocked(awsControlApi.drive).mockRejectedValue(new AwsControlError('account_unavailable', 409))
    renderWithProviders(<AwsControlPage />)

    expect(await screen.findByTestId('console-unavailable')).toBeTruthy()
    expect(screen.queryByTestId('console-storage-consent')).toBeNull()
    expect(screen.queryByTestId('capability-drive-setup')).toBeNull()
  })

  it('no drive yet renders the setup card, and the usage counts stay off the rail', async () => {
    vi.mocked(awsControlApi.drive).mockResolvedValue({ exists: false })
    renderWithProviders(<AwsControlPage />)

    const setup = await screen.findByTestId('capability-drive-setup')
    expect(within(setup).getByTestId('drive-setup')).toBeTruthy()
    expect(screen.queryByTestId('drive-section')).toBeNull()
    // The usage-sourced counts have nothing to count until the drive exists…
    expect(screen.queryByTestId('rail-files-count')).toBeNull()
    expect(screen.queryByTestId('rail-library-count')).toBeNull()
    expect(screen.queryByTestId('rail-backup-count')).toBeNull()
    // …but the share ledger is its own endpoint, so its count still answers.
    await waitFor(() => {
      expect(screen.getByTestId('rail-shares-count')).toHaveTextContent(fmtNumber(0))
    })
    expect(screen.queryByTestId('rail-meta')).toBeNull()
  })

  it('the gate covers every drive pane, not just Files', async () => {
    vi.mocked(awsControlApi.drive).mockResolvedValue({ exists: false })
    renderWithProviders(<AwsControlPage />)
    await screen.findByTestId('gate-files')

    fireEvent.click(screen.getByTestId('rail-library'))
    expect(await screen.findByTestId('gate-library')).toBeTruthy()
    fireEvent.click(screen.getByTestId('rail-backup'))
    expect(await screen.findByTestId('gate-backup')).toBeTruthy()
    fireEvent.click(screen.getByTestId('rail-shares'))
    expect(await screen.findByTestId('gate-shares')).toBeTruthy()
    expect(screen.queryByTestId('access-section')).toBeNull()
  })
})

describe('edge states', () => {
  it('renders the standard disabled-app state on a 403 app_disabled, with no rail', async () => {
    vi.mocked(awsControlApi.accounts).mockRejectedValue(new AwsControlError('app_disabled', 403))
    renderWithProviders(<AwsControlPage />)

    expect(await screen.findByTestId('aws-control-disabled')).toBeTruthy()
    expect(screen.queryByTestId('aws-rail')).toBeNull()
    expect(screen.queryByTestId('accounts-error')).toBeNull()
  })

  it('renders an error state on a non-403 failure, and retry recovers', async () => {
    vi.mocked(awsControlApi.accounts)
      .mockRejectedValueOnce(new AwsControlError('http_500', 500))
      .mockResolvedValue(accountsPayload())
    renderWithProviders(<AwsControlPage />)

    expect(await screen.findByTestId('aws-control-error')).toBeTruthy()
    expect(screen.queryByTestId('aws-rail')).toBeNull()

    fireEvent.click(screen.getByTestId('error-retry'))
    expect(await screen.findByTestId('aws-rail')).toBeTruthy()
    expect(await screen.findByTestId('drive-section')).toBeTruthy()
  })

  it('a 403 that is NOT app_disabled is an error to diagnose, not a disabled app', async () => {
    // The same route answers 403 for a non-owner caller. Showing "this app is
    // disabled" for that would send the reader to wait out a setting that is
    // not the problem; the notice (with its agent hand-off) is the right answer.
    vi.mocked(awsControlApi.accounts).mockRejectedValue(
      new AwsControlError('dashboard_owner_required', 403),
    )
    renderWithProviders(<AwsControlPage />)

    const notice = await screen.findByTestId('aws-control-error')
    expect(notice).toHaveTextContent(i18nT('apps.awsControl.page.error_title'))
    // Permission-worded, not "try again in a moment": a retry cannot clear a 403.
    expect(notice).toHaveTextContent(i18nT('apps.awsControl.page.error_forbidden_body'))
    expect(notice).not.toHaveTextContent(i18nT('apps.awsControl.page.error_body'))
    expect(within(notice).getByRole('button', { name: /ask the agent/i })).toBeTruthy()
    expect(screen.queryByTestId('aws-control-disabled')).toBeNull()
  })

  it('while accounts are still loading, the accounts pane renders full width, no rail', async () => {
    // There is nothing for the rail or the drive panes to show before the list
    // answers, so the pane that will handle "no resolved account" also carries
    // the loading state — full width, with no half-built shell around it.
    vi.mocked(awsControlApi.accounts).mockReturnValue(
      new Promise(() => {}) as ReturnType<typeof awsControlApi.accounts>,
    )
    renderWithProviders(<AwsControlPage />)

    expect(await screen.findByTestId('accounts-pane')).toBeTruthy()
    expect(screen.getByTestId('accounts-loading')).toBeTruthy()
    expect(screen.queryByTestId('aws-rail')).toBeNull()
    expect(awsControlApi.drive).not.toHaveBeenCalled()
  })

  it('with NO resolved account the accounts pane IS the app, and a red row offers Reconnect', async () => {
    vi.mocked(awsControlApi.accounts).mockResolvedValue(accountsPayload({
      accounts: [UNRESOLVED_ROW],
      totals: { accounts: 1, profiles: 1, profilesHealthy: 0 },
    }))
    vi.mocked(awsControlApi.reconnectPlan).mockResolvedValue({
      method: 'terminal', kind: 'sso', command: 'aws sso login --profile stale-profile',
    })
    renderWithProviders(<AwsControlPage />)

    const row = await screen.findByTestId('account-card')
    expect(screen.queryByTestId('aws-rail')).toBeNull()
    // No selection either — there is no account behind the row.
    expect(screen.queryByTestId('accounts-connections')).toBeNull()

    // The click opens guidance, not a dead end.
    fireEvent.click(row)
    const panel = await screen.findByTestId('row-reconnect')
    fireEvent.click(within(panel).getByTestId('reconnect-toggle'))
    await screen.findByTestId('reconnect-command')
    expect(screen.getByTestId('reconnect-command')).toHaveTextContent('aws sso login --profile stale-profile')
    // Toggling the row again collapses the guidance.
    fireEvent.click(row)
    expect(screen.queryByTestId('row-reconnect')).toBeNull()
  })

  it('shows a friendly empty state when there are no accounts, full width', async () => {
    vi.mocked(awsControlApi.accounts).mockResolvedValue(
      accountsPayload({ accounts: [], totals: { accounts: 0, profiles: 0, profilesHealthy: 0 } }),
    )
    renderWithProviders(<AwsControlPage />)

    expect(await screen.findByTestId('aws-control-empty')).toBeTruthy()
    expect(screen.queryByTestId('account-card')).toBeNull()
    expect(screen.queryByTestId('aws-rail')).toBeNull()
    // The remedy is the Add-accounts disclosure on this same pane, so the copy
    // names it. Asserted against the KEY, not a fragment, so a copy edit moves
    // both sides of this pair together — and paired with the unsupported case
    // below, since a one-sided assertion passes just as well if both platforms
    // collapsed onto one string.
    expect(screen.getByTestId('aws-control-empty').textContent).toContain(
      i18nT('apps.awsControl.page.empty_body'),
    )
  })

  it('drops the Add-accounts pointer from the empty state where nothing can be added', async () => {
    vi.mocked(awsControlApi.accounts).mockResolvedValue(
      accountsPayload({ accounts: [], totals: { accounts: 0, profiles: 0, profilesHealthy: 0 } }),
    )
    vi.mocked(awsControlApi.availableProfiles).mockResolvedValue(
      availablePayload({ profiles: [], supported: false, registeredCount: 0 }),
    )
    renderWithProviders(<AwsControlPage />)

    // Discovery is POSIX-only, so an unsupported platform sits at zero accounts
    // permanently and the disclosure below reports that it cannot list profiles.
    // An empty state naming that disclosure would promise an action the next
    // paragraph refuses, which is the same defect as naming a page that does not
    // exist -- one scroll shorter. The subtitle goes away entirely rather than
    // being replaced: the title already says nothing is here, and the WSL
    // constraint belongs in the disclosure, once.
    await screen.findByTestId('add-accounts-unsupported')
    expect(screen.getByTestId('aws-control-empty-title')).toBeTruthy()
    expect(screen.queryByTestId('aws-control-empty-subtitle')).toBeNull()
  })
})

describe('accounts pane', () => {
  /** Land on the accounts pane with the default two-account payload. */
  async function openAccountsPane() {
    renderWithProviders(<AwsControlPage />)
    await screen.findByTestId('drive-section')
    fireEvent.click(screen.getByTestId('rail-accounts'))
    await screen.findByTestId('accounts-pane')
  }

  it('renders one thin row per account: name, full id, health dot, keys summary', async () => {
    await openAccountsPane()

    const rows = await screen.findAllByTestId('account-card')
    expect(rows).toHaveLength(2)

    const dots = screen.getAllByTestId('health-dot')
    expect(dots.map((d) => d.getAttribute('data-health'))).toEqual(['ok', 'degraded'])

    // Rows lead with the account name.
    expect(within(rows[0]).getByTestId('account-name')).toHaveTextContent('personal')
    expect(within(rows[1]).getByTestId('account-name')).toHaveTextContent('work')
    // The FULL 12-digit id renders (never a truncated "···1337" tail).
    const ids = screen.getAllByTestId('account-id').map((n) => n.textContent)
    expect(ids).toContain('111122223333')
    expect(ids).toContain('444455556666')
    expect(screen.queryByText(/···/)).toBeNull()
    // Per-row keys summary.
    expect(screen.getAllByTestId('account-keys')[0]).toHaveTextContent(
      i18nT('apps.awsControl.page.keys_summary', { count: 1 }),
    )
    // The totals strip answers "how much is connected and is it healthy".
    expect(screen.getByTestId('accounts-totals')).toHaveTextContent(
      i18nT('apps.awsControl.page.totals_summary', {
        accounts: fmtNumber(2), keys: fmtNumber(2), healthy: fmtNumber(1),
      }),
    )
    // The selected account's connection keys live on this pane too.
    const conns = screen.getByTestId('accounts-connections')
    expect(within(conns).getByTestId('connections-section')).toBeTruthy()
    expect(within(conns).getByTestId('connection-name')).toHaveTextContent('personal')
  })

  it('filters the list client-side by name or id, and says so when nothing matches', async () => {
    await openAccountsPane()
    await screen.findByTestId('accounts-list')

    fireEvent.change(screen.getByTestId('accounts-search'), { target: { value: '4444' } })
    const rows = screen.getAllByTestId('account-card')
    expect(rows).toHaveLength(1)
    expect(within(rows[0]).getByTestId('account-name')).toHaveTextContent('work')

    // A query that matches nothing drops the list and shows the search-empty line.
    fireEvent.change(screen.getByTestId('accounts-search'), { target: { value: 'zzz' } })
    expect(screen.getByTestId('accounts-search-empty')).toBeTruthy()
    expect(screen.queryByTestId('accounts-list')).toBeNull()
  })

  it('keeps the rescue off the pane when a listed account owns the grant', async () => {
    // A grant OWNED by a listed account renders on that account's usage pane,
    // not here — the accounts pane is accounts and nothing else.
    vi.mocked(api.awsConsent).mockResolvedValue(
      { granted: true, grant: { account: accountsPayload().accounts[0].account } } as never,
    )
    await openAccountsPane()

    await screen.findByTestId('accounts-list')
    await waitFor(() => expect(api.awsConsent).toHaveBeenCalledWith('s3'))
    expect(screen.queryByTestId('orphan-consent')).toBeNull()
  })

  it('rescues a grant whose account is not registered here', async () => {
    // A grant is keyed on the service, so it outlives the account it was
    // recorded for. A grant matching NO registered account has no usage pane to
    // live on and revoke has no caller anywhere — money confirmed with no way
    // to unconfirm it. Case 1: some accounts remain, none owns the grant.
    vi.mocked(api.awsConsent).mockResolvedValue(
      { granted: true, grant: { account: '999988887777' } } as never,
    )
    await openAccountsPane()
    expect(await screen.findByTestId('orphan-consent')).toBeTruthy()
    // The rescue's only control is destructive and the card names an account
    // that matches nothing on the list, so it never renders bare.
    expect(screen.getByTestId('orphan-consent-note')).toBeTruthy()
  })

  it('rescues an orphaned grant even with zero accounts registered', async () => {
    // Case 2: nothing registered at all — the full-width accounts pane still
    // carries the rescue, or the grant is unreachable forever.
    vi.mocked(awsControlApi.accounts).mockResolvedValue(
      accountsPayload({ accounts: [], totals: { accounts: 0, profiles: 0, profilesHealthy: 0 } }),
    )
    vi.mocked(api.awsConsent).mockResolvedValue(
      { granted: true, grant: { account: '999988887777' } } as never,
    )
    renderWithProviders(<AwsControlPage />)
    expect(await screen.findByTestId('orphan-consent')).toBeTruthy()
    expect(screen.getByTestId('orphan-consent-note')).toBeTruthy()
  })

  it('never flashes the rescue while the account list is still unknown', async () => {
    // `orphaned` asks whether any listed account owns the grant. An in-flight
    // accounts query has no list, and reading that as "nobody owns it" would put
    // a withdraw control on the ordinary accounts pane on every load where the
    // consent read resolves first. The list must be KNOWN first.
    let releaseAccounts: (v: unknown) => void = () => {}
    vi.mocked(awsControlApi.accounts).mockReturnValue(
      new Promise((res) => { releaseAccounts = res }) as never,
    )
    vi.mocked(api.awsConsent).mockResolvedValue(
      { granted: true, grant: { account: accountsPayload().accounts[0].account } } as never,
    )

    renderWithProviders(<AwsControlPage />)
    await screen.findByTestId('accounts-pane')
    await waitFor(() => expect(api.awsConsent).toHaveBeenCalledWith('s3'))
    expect(screen.queryByTestId('orphan-consent')).toBeNull()

    releaseAccounts(accountsPayload())
    await screen.findByTestId('aws-rail')
    fireEvent.click(screen.getByTestId('rail-accounts'))
    await screen.findByTestId('accounts-list')
    expect(screen.queryByTestId('orphan-consent')).toBeNull()
  })
})

describe('add accounts', () => {
  /** The disclosure lives on the accounts pane; reach it through the rail. */
  async function openAccountsPane() {
    renderWithProviders(<AwsControlPage />)
    await screen.findByTestId('drive-section')
    fireEvent.click(screen.getByTestId('rail-accounts'))
    await screen.findByTestId('accounts-pane')
  }

  it('lists only the UNREGISTERED profiles', async () => {
    await openAccountsPane()

    // The section is collapsed by default (the account list stays primary), so
    // open it before the picker renders.
    fireEvent.click(await screen.findByTestId('add-accounts-toggle'))
    const boxes = await screen.findAllByTestId('add-accounts-checkbox')
    const names = boxes.map((b) => b.getAttribute('data-name'))
    // Only staging + sandbox: the already-registered "personal" is not offered.
    expect(names).toEqual(['staging', 'sandbox'])
  })

  it('register posts exactly the checked names and refetches the account list', async () => {
    vi.mocked(awsControlApi.registerProfiles).mockResolvedValue({ added: 1, skipped: 0 })
    await openAccountsPane()
    // One accounts() call for the initial mount before we register.
    expect(awsControlApi.accounts).toHaveBeenCalledTimes(1)

    fireEvent.click(await screen.findByTestId('add-accounts-toggle'))
    const boxes = await screen.findAllByTestId('add-accounts-checkbox')
    const staging = boxes.find((b) => b.getAttribute('data-name') === 'staging')!
    fireEvent.click(staging)
    fireEvent.click(screen.getByTestId('add-accounts-register'))

    await waitFor(() => {
      expect(awsControlApi.registerProfiles).toHaveBeenCalledWith(['staging'])
    })
    // Invalidating the ['aws-control','accounts'] key must trigger a refetch so
    // the newly registered profile appears without a manual Refresh.
    await waitFor(() => {
      expect(awsControlApi.accounts).toHaveBeenCalledTimes(2)
    })
  })

  it('says so on an unsupported platform instead of showing a picker', async () => {
    vi.mocked(awsControlApi.availableProfiles).mockResolvedValue(
      availablePayload({ profiles: [], supported: false, registeredCount: 0 }),
    )
    await openAccountsPane()

    expect(await screen.findByTestId('add-accounts-unsupported')).toBeTruthy()
    // No picker, no toggle — an empty list here means "can't tell", not "none".
    expect(screen.queryByTestId('add-accounts-toggle')).toBeNull()
    expect(screen.queryByTestId('add-accounts-checkbox')).toBeNull()
  })

  it('surfaces a message when register fails, never silently', async () => {
    vi.mocked(awsControlApi.registerProfiles).mockRejectedValue(
      new AwsControlError('unknown_profile', 400),
    )
    await openAccountsPane()

    fireEvent.click(await screen.findByTestId('add-accounts-toggle'))
    const boxes = await screen.findAllByTestId('add-accounts-checkbox')
    fireEvent.click(boxes[0])
    fireEvent.click(screen.getByTestId('add-accounts-register'))

    const notice = await screen.findByTestId('add-accounts-error')
    // No hand-off beside unsaved input: the ticked profiles survive the refusal.
    expect(within(notice).queryByRole('button', { name: /ask the agent/i })).toBeNull()
    expect(boxes[0]).toBeChecked()
  })

  it('a failed profile scan is a notice, not "nothing left to add"', async () => {
    // With the scan failed, `unregistered` is an empty fallback — and the
    // none-left sentence would assert the opposite of what happened.
    vi.mocked(awsControlApi.availableProfiles).mockRejectedValue(
      new AwsControlError('http_500', 500),
    )
    await openAccountsPane()

    fireEvent.click(await screen.findByTestId('add-accounts-toggle'))
    expect(await screen.findByTestId('add-accounts-load-error')).toHaveTextContent(
      i18nT('apps.awsControl.page.add_accounts_load_error'),
    )
    expect(screen.queryByTestId('add-accounts-none')).toBeNull()
  })

  it('a ticked profile withholds every hand-off on the pane until the tick is cleared', async () => {
    // The ticks live only in the disclosure's state. "Ask the agent" on any
    // notice on this pane navigates to chat, which unmounts the disclosure and
    // drops the selection — so while a tick is open the pane's other notices
    // (here the row Reconnect) offer retry only, and the hand-off comes back
    // once the selection is empty again.
    vi.mocked(awsControlApi.accounts).mockResolvedValue(accountsPayload({
      accounts: [UNRESOLVED_ROW],
      totals: { accounts: 1, profiles: 1, profilesHealthy: 0 },
    }))
    vi.mocked(awsControlApi.reconnectPlan).mockRejectedValue(new AwsControlError('http_500', 500))
    renderWithProviders(<AwsControlPage />)

    fireEvent.click(await screen.findByTestId('account-card'))
    const panel = await screen.findByTestId('row-reconnect')
    fireEvent.click(within(panel).getByTestId('reconnect-toggle'))
    const notice = await screen.findByTestId('reconnect-error')
    // No draft yet: the hand-off is offered.
    expect(within(panel).getByRole('button', { name: /ask the agent/i })).toBeTruthy()

    fireEvent.click(screen.getByTestId('add-accounts-toggle'))
    const boxes = await screen.findAllByTestId('add-accounts-checkbox')
    fireEvent.click(boxes[0])
    expect(boxes[0]).toBeChecked()
    await waitFor(() =>
      expect(within(panel).queryByRole('button', { name: /ask the agent/i })).toBeNull(),
    )
    // The notice itself and its retry stay; only the navigating action is gone.
    expect(notice).toBeTruthy()
    expect(within(panel).getByTestId('reconnect-error-retry')).toBeTruthy()

    fireEvent.click(boxes[0])
    expect(boxes[0]).not.toBeChecked()
    await waitFor(() =>
      expect(within(panel).getByRole('button', { name: /ask the agent/i })).toBeTruthy(),
    )
  })
})

describe('AwsControlPage — path-based navigation', () => {
  it('deep link with a pane segment lands straight on that pane', async () => {
    renderWithProviders(<AwsControlPage />, { route: '/aws-control/usage' })
    expect(await screen.findByTestId('usage-pane')).toBeTruthy()
    // The rail marks the routed pane current, not the default one.
    expect(screen.getByTestId('rail-usage').getAttribute('aria-current')).toBe('page')
    expect(screen.getByTestId('rail-files').getAttribute('aria-current')).toBeNull()
  })

  it('a trailing slash reads as the bare path, not a drilled-in level', async () => {
    // The settings path-nav shipped a misfire where /settings/channels/ made a
    // length>=2 check treat the level as drilled-in. Pin the same class here:
    // /aws-control/ must render exactly what /aws-control renders.
    renderWithProviders(<AwsControlPage />, { route: '/aws-control/' })
    expect(await screen.findByTestId('drive-section')).toBeTruthy()
    expect(screen.getByTestId('rail-files').getAttribute('aria-current')).toBe('page')
  })

  it('an unknown segment falls back to Files rather than a blank pane', async () => {
    renderWithProviders(<AwsControlPage />, { route: '/aws-control/nonsense' })
    expect(await screen.findByTestId('drive-section')).toBeTruthy()
  })

  it('a rail click writes the pane path (deep-linkable)', async () => {
    renderWithProviders(<AwsControlPage />, { route: '/aws-control' })
    await screen.findByTestId('drive-section')
    fireEvent.click(screen.getByTestId('rail-backup'))
    expect(await screen.findByTestId('backup-section')).toBeTruthy()
    expect(screen.getByTestId('rail-backup').getAttribute('aria-current')).toBe('page')
  })
})

describe('AwsControlPage — narrow viewport (iOS push stack)', () => {
  beforeEach(() => { narrowViewport = true })

  it('the bare path is the grouped root list, with no rail', async () => {
    renderWithProviders(<AwsControlPage />, { route: '/aws-control' })
    expect(await screen.findByTestId('aws-root-list')).toBeTruthy()
    expect(screen.queryByTestId('aws-rail')).toBeNull()
    // Account card on top, then every pane as a tappable row.
    expect(screen.getByTestId('account-switcher')).toBeTruthy()
    for (const pane of ['files', 'library', 'backup', 'shares', 'accounts', 'usage']) {
      expect(screen.getByTestId(`root-${pane}`)).toBeTruthy()
    }
  })

  it('tapping a row pushes the detail with exactly one back bar, and back pops to the list', async () => {
    renderWithProviders(<AwsControlPage />, { route: '/aws-control' })
    await screen.findByTestId('aws-root-list')

    fireEvent.click(screen.getByTestId('root-usage'))
    expect(await screen.findByTestId('aws-pane-detail')).toBeTruthy()
    expect(screen.getByTestId('usage-pane')).toBeTruthy()
    // Exactly ONE back affordance per level — never two stacked bars.
    const backs = screen.getAllByText('AWS Control')
    expect(backs.length).toBe(1)
    expect(screen.queryByTestId('aws-root-list')).toBeNull()

    fireEvent.click(backs[0])
    expect(await screen.findByTestId('aws-root-list')).toBeTruthy()
    expect(screen.queryByTestId('aws-pane-detail')).toBeNull()
  })

  it('an unknown segment reads as Files on a phone too — one meaning per URL', async () => {
    renderWithProviders(<AwsControlPage />, { route: '/aws-control/bogus' })
    expect(await screen.findByTestId('aws-pane-detail')).toBeTruthy()
    expect(await screen.findByTestId('drive-section')).toBeTruthy()
    expect(screen.queryByTestId('aws-root-list')).toBeNull()
  })

  it('a pane->pane move keeps the push marker, so back still pops to the list', async () => {
    // Drill in from the root list (a PUSH), then move pane->pane via the
    // accounts pane's row (a REPLACE). The replace must carry the entry's
    // push marker forward — dropping it would stack a duplicate root entry
    // on back and leave the next platform back visibly inert.
    renderWithProviders(<AwsControlPage />, { route: '/aws-control' })
    await screen.findByTestId('aws-root-list')

    fireEvent.click(screen.getByTestId('root-accounts'))
    expect(await screen.findByTestId('accounts-pane')).toBeTruthy()

    // Selecting an account jumps to Files (pane->pane replace).
    fireEvent.click(screen.getAllByTestId('account-card')[0])
    expect(await screen.findByTestId('drive-section')).toBeTruthy()

    // Back must POP to the root list (marker preserved), not replace-write.
    fireEvent.click(screen.getByText('AWS Control'))
    expect(await screen.findByTestId('aws-root-list')).toBeTruthy()
  })

  it('a deep link goes straight to the detail pane', async () => {
    renderWithProviders(<AwsControlPage />, { route: '/aws-control/backup' })
    expect(await screen.findByTestId('aws-pane-detail')).toBeTruthy()
    expect(await screen.findByTestId('backup-section')).toBeTruthy()
    expect(screen.queryByTestId('aws-root-list')).toBeNull()
  })
})
