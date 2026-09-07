import { describe, it, expect, vi, beforeEach, onTestFinished } from 'vitest'
import { screen, fireEvent, waitFor, within, act } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import { i18nT } from '../../i18n/t'
import { fmtBytes } from '../../i18n/format'
import type {
  DriveStatus, DriveUsage, DriveSearchHit, DriveListing, LibraryResponse, BackupStatus, SharesResponse, Share,
} from './types'

/* The sections read only through the api client; mocking it keeps every case
 * network-free while leaving `AwsControlError` real for the 403/409 paths. */
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
      drivePreview: vi.fn(),
      driveSearch: vi.fn(),
      driveMove: vi.fn(),
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
      libraryRemove: vi.fn(),
      backup: vi.fn(),
      backupRun: vi.fn(),
      backupNightly: vi.fn(),
      backupRestore: vi.fn(),
    },
  }
})

/* Library cards lazily fetch the full artifact through the shared client. */
vi.mock('../../api/client', () => ({
  api: {
    artifact: vi.fn(),
    awsConsent: vi.fn(),
    grantAwsConsent: vi.fn(),
    revokeAwsConsent: vi.fn(),
  },
}))

import { awsControlApi, AwsControlError } from './api'
import { api } from '../../api/client'
import {
  recordError, consumeChatHandoff, installSoftNavigate,
  __resetErrorJournalForTests, __resetNavSeamForTests,
} from '../../utils/errorReport'
import {
  DriveSectionView, LibrarySection, BackupSection, AccessSection, StorageMeter,
} from './DrivePage'

const ACCOUNT_ID = '111122223333'

const driveExists: Extract<DriveStatus, { exists: true }> = {
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

const emptyLibrary: LibraryResponse = { artifacts: [] }
const emptyBackup: BackupStatus = { nightly: false, runs: {}, remote: { snapshot: [], sessions: [] } }
const noShares: SharesResponse = { shares: [] }

function stubDrivePresent() {
  vi.mocked(awsControlApi.library).mockResolvedValue(emptyLibrary)
  vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: [] })
  vi.mocked(awsControlApi.backup).mockResolvedValue(emptyBackup)
  vi.mocked(awsControlApi.shares).mockResolvedValue(noShares)
}

/** A `BackupStatus` whose `kind` row is running, as the server would report it. */
function backupWithActive(kind: 'snapshot' | 'sessions'): BackupStatus {
  return {
    ...emptyBackup,
    jobs: {
      [kind]: {
        active: {
          run_id: 'r-live',
          kind,
          status: 'running',
          created_at: '2026-08-24T00:00:00Z',
          updated_at: '2026-08-24T00:00:00Z',
          finished_at: '',
          error: '',
        },
        lastFailed: null,
      },
    },
  }
}

function stubActiveJob(kind: 'snapshot' | 'sessions') {
  vi.mocked(awsControlApi.backup).mockResolvedValue(backupWithActive(kind))
}

beforeEach(() => {
  vi.clearAllMocks()
  // The grid/list toggle persists per section to localStorage, which outlives a
  // single test. Without this, a test that switches a section's view silently
  // changes what every LATER test in the file renders -- the table controls
  // vanish and the failure points at the wrong test.
  localStorage.clear()
  // Library card previews fetch the full artifact when near the viewport; a
  // never-resolving fetch keeps every preview a placeholder without touching
  // the network in either IntersectionObserver regime.
  vi.mocked(api.artifact).mockReturnValue(new Promise(() => {}) as ReturnType<typeof api.artifact>)
})

/**
 * Mount ONE of the drive's rail panes directly.
 *
 * The drive ROOT page (three section cards plus the ledger, with internal
 * section state) was removed in the flat-rail IA refactor: the four sections
 * are now named exports rendered as their own panes. So instead of mounting
 * the root and clicking into a section, tests mount the section under test
 * with the props the old flow passed -- the account id, plus the bucket for
 * the two sections that render a CLI drawer. The shares-ledger tests, which
 * used to sit at the root, mount AccessSection.
 */
async function renderDrive(section: 'drive' | 'library' | 'backup' | 'access') {
  const el =
    section === 'drive' ? <DriveSectionView account={ACCOUNT_ID} bucket={driveExists.bucket} />
    : section === 'library' ? <LibrarySection account={ACCOUNT_ID} bucket={driveExists.bucket} />
    : section === 'backup' ? <BackupSection account={ACCOUNT_ID} />
    : <AccessSection account={ACCOUNT_ID} />
  renderWithProviders(el)
}

/**
 * Open a per-item overflow menu and choose one of its items.
 *
 * Every per-item action in both folders lives behind a `⋮`, so a test that used
 * to click a bare button has to open the menu first. Enter on the trigger rather
 * than a click: the menu is a portaled Radix dropdown, and a bare click does not
 * open one in jsdom -- which is how the row-overflow tests in this file already
 * drive theirs.
 *
 * `trigger` is an element rather than a test id because a listing with several
 * rows has several triggers and the caller has to say which.
 */
async function chooseFromMenu(trigger: HTMLElement, itemTestId: string) {
  fireEvent.keyDown(trigger, { key: 'Enter' })
  fireEvent.click(await screen.findByTestId(itemTestId))
}

describe('DrivePage sections', () => {
  it('mints a share link and shows the URL exactly once in the dialog', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'report.pdf', size: 2048, modified: '2026-08-20T00:00:00Z' }],
      folders: [],
    })
    vi.mocked(awsControlApi.driveShare).mockResolvedValue({
      url: 'https://example-presigned/report.pdf?sig=x',
      share: {
        id: 's1', account: ACCOUNT_ID, section: 'drive', key: 'report.pdf',
        createdAt: '2026-08-24T05:00:00Z', expiresAt: '2026-08-24T06:00:00Z', note: '',
      },
    })

    await renderDrive('drive')

    // Share lives in the per-row overflow menu (rows carry at most two
    // sibling controls: Download + More).
    fireEvent.keyDown(await screen.findByTestId('drive-more'), { key: 'Enter' })
    fireEvent.click(await screen.findByTestId('drive-share'))
    // The dialog names the file it is about to publish.
    expect(await screen.findByTestId('share-dialog')).toHaveTextContent(
      i18nT('apps.awsControl.console.share_title', { name: 'report.pdf' }),
    )
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

    await renderDrive('drive')

    // Delete in the overflow menu opens a confirm strip — it must NOT delete.
    fireEvent.keyDown(await screen.findByTestId('drive-more'), { key: 'Enter' })
    fireEvent.click(await screen.findByTestId('drive-delete'))
    const strip = await screen.findByTestId('drive-delete-confirm')
    expect(strip).toHaveTextContent('report.pdf')
    expect(awsControlApi.driveDelete).not.toHaveBeenCalled()

    // Cancel dismisses without deleting.
    fireEvent.click(screen.getByTestId('drive-delete-cancel'))
    expect(screen.queryByTestId('drive-delete-confirm')).toBeNull()
    expect(awsControlApi.driveDelete).not.toHaveBeenCalled()

    // Confirming actually deletes.
    fireEvent.keyDown(await screen.findByTestId('drive-more'), { key: 'Enter' })
    fireEvent.click(await screen.findByTestId('drive-delete'))
    fireEvent.click(await screen.findByTestId('drive-delete-confirm-action'))
    await waitFor(() => expect(awsControlApi.driveDelete).toHaveBeenCalledWith(ACCOUNT_ID, 'drive', 'report.pdf'))
  })

  it('renders the shares ledger with an expires-in countdown', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.shares).mockResolvedValue({
      shares: [{
        id: 's1', account: ACCOUNT_ID, section: 'drive', key: 'report.pdf',
        createdAt: '2026-08-24T05:00:00Z', expiresAt: '2030-01-01T00:00:00Z', note: 'for review',
      }],
    })

    await renderDrive('access')

    const row = await screen.findByTestId('access-row')
    expect(row).toHaveTextContent('report.pdf')
    expect(row).toHaveTextContent('for review')
    // A relative "expires …" phrase renders (not a raw ISO timestamp).
    expect(row.textContent).not.toContain('2030-01-01')
  })

  it('flags a share whose object was deleted, and keeps the row', async () => {
    // A row that survives its object read as live exposure that is not live.
    // The row STAYS -- the link is unexpired and would resolve again if the key
    // were recreated -- and is marked instead.
    stubDrivePresent()
    vi.mocked(awsControlApi.shares).mockResolvedValue({
      checked: true,
      shares: [
        {
          id: 's1', account: ACCOUNT_ID, section: 'drive', key: 'gone.pdf',
          createdAt: '2026-08-24T05:00:00Z', expiresAt: '2030-01-01T00:00:00Z', note: '',
          objectMissing: true,
        },
        {
          id: 's2', account: ACCOUNT_ID, section: 'drive', key: 'here.pdf',
          createdAt: '2026-08-24T05:00:00Z', expiresAt: '2030-01-01T00:00:00Z', note: '',
        },
      ],
    })

    await renderDrive('access')

    const rows = await screen.findAllByTestId('access-row')
    expect(rows).toHaveLength(2)
    expect(rows[0]).toHaveTextContent('gone.pdf')
    expect(screen.getAllByTestId('access-object-missing')).toHaveLength(1)
    // A checked render says nothing about verification -- the absence of a flag
    // on the second row IS the answer.
    expect(screen.queryByTestId('access-unchecked')).toBeNull()
  })

  it('says so when the objects behind the links were not checked', async () => {
    // Without this line an unflagged row on an unchecked render would read as
    // "the object is still there", which is the claim the check exists to make.
    stubDrivePresent()
    vi.mocked(awsControlApi.shares).mockResolvedValue({
      checked: false,
      shares: [{
        id: 's1', account: ACCOUNT_ID, section: 'drive', key: 'report.pdf',
        createdAt: '2026-08-24T05:00:00Z', expiresAt: '2030-01-01T00:00:00Z', note: '',
      }],
    })

    await renderDrive('access')

    await screen.findByTestId('access-row')
    expect(screen.getByTestId('access-unchecked')).toBeTruthy()
  })

  it('does not qualify an EMPTY ledger as unchecked', async () => {
    // No rows means no claim to qualify; the note there would be noise on the
    // empty state.
    stubDrivePresent()
    vi.mocked(awsControlApi.shares).mockResolvedValue({ checked: false, shares: [] })

    await renderDrive('access')

    await screen.findByTestId('access-empty')
    expect(screen.queryByTestId('access-unchecked')).toBeNull()
  })

  it('disables the backup row and spins while a run is in flight', async () => {
    stubDrivePresent()
    // A run that never resolves keeps the row busy from the moment of the click,
    // before the server has a run to report.
    vi.mocked(awsControlApi.backupRun).mockReturnValue(new Promise(() => {}) as ReturnType<typeof awsControlApi.backupRun>)

    await renderDrive('backup')

    const runBtn = await screen.findByTestId('backup-run-snapshot')
    fireEvent.click(runBtn)
    await waitFor(() => expect((screen.getByTestId('backup-run-snapshot') as HTMLButtonElement).disabled).toBe(true))
  })

  it('shows a backup as running on a FRESH mount, with no click in this session', async () => {
    // The point of the migration. Nothing in this render started the run -- the
    // host reports it. Before the durable job the indicator lived in the
    // component, so a navigation away destroyed the only record that a backup
    // was in flight and coming back showed an idle row mid-upload. Under the
    // rail that unmount is what switching panes actually does.
    stubDrivePresent()
    stubActiveJob('snapshot')

    await renderDrive('backup')

    await waitFor(() =>
      expect((screen.getByTestId('backup-run-snapshot') as HTMLButtonElement).disabled).toBe(true),
    )
    expect(screen.getByTestId('backup-run-snapshot').textContent).toContain(
      i18nT('apps.awsControl.console.backup_running'),
    )
    // Adoption is not a side effect of having asked for one: no start was posted.
    expect(awsControlApi.backupRun).not.toHaveBeenCalled()
  })

  it('adopts only the kind that is running, leaving the sibling row usable', async () => {
    // The rows are independent: the host indexes a run by (kind, account), so a
    // sessions backup in flight must not disable Back up now on snapshot.
    stubDrivePresent()
    stubActiveJob('sessions')

    await renderDrive('backup')

    await waitFor(() =>
      expect((screen.getByTestId('backup-run-sessions') as HTMLButtonElement).disabled).toBe(true),
    )
    expect((screen.getByTestId('backup-run-snapshot') as HTMLButtonElement).disabled).toBe(false)
  })

  it('says a run failed instead of just stopping the spinner', async () => {
    // The app's own ledger records only SUCCESSES, so without the durable
    // record's status a failed backup renders exactly like one that was never
    // started -- the row silently going quiet is itself a false statement.
    vi.mocked(awsControlApi.backup).mockResolvedValue({
      ...emptyBackup,
      jobs: {
        snapshot: {
          active: null,
          lastFailed: {
            run_id: 'a'.repeat(32),
            kind: 'snapshot',
            status: 'failed',
            created_at: '2026-09-01T00:00:00Z',
            updated_at: '2026-09-01T00:00:02Z',
            finished_at: '2026-09-01T00:00:02Z',
            error: 'S3 consent no longer holds',
          },
        },
      },
    })

    await renderDrive('backup')

    const err = await screen.findByTestId('backup-error-snapshot')
    expect(err.textContent ?? '').toContain('S3 consent no longer holds')
    // A failure is history, not work in flight: the row must be usable again.
    expect((screen.getByTestId('backup-run-snapshot') as HTMLButtonElement).disabled).toBe(false)
    // And the sibling row says nothing about it.
    expect(screen.queryByTestId('backup-error-sessions')).toBeNull()
  })

  it('offers the archive control on a first paint with no remote data', async () => {
    // The state the bug lived in. The remote half is opt-in behind `?remote=1`,
    // which only this control can request -- so gating the control on
    // `data.remote` made it wait for the fetch it enables, and the archive and
    // Restore were unreachable for the whole session. A first paint has
    // `remote: null`, so this is the paint that matters.
    vi.mocked(awsControlApi.backup).mockResolvedValue({ ...emptyBackup, remote: null })

    await renderDrive('backup')

    const toggle = await screen.findByTestId('backup-remote-toggle')
    expect(toggle).toBeTruthy()
    // And it must actually open, since opening is what requests the data.
    fireEvent.click(toggle)
    expect(await screen.findByTestId('backup-archive')).toBeTruthy()
  })

  it('does not busy the row when the payload reports no run for this account', async () => {
    // The server answers per account now, so "another account is backing up"
    // arrives as an EMPTY jobs block here rather than as someone else's run. This
    // pins the client half: an absent entry must read as idle, not as unknown.
    vi.mocked(awsControlApi.backup).mockResolvedValue({ ...emptyBackup, jobs: {} })

    await renderDrive('backup')

    const btn = await screen.findByTestId('backup-run-snapshot')
    expect((btn as HTMLButtonElement).disabled).toBe(false)
    expect(screen.queryByTestId('backup-error-snapshot')).toBeNull()
  })

  it('invites a retry only for a refusal a retry can actually clear', async () => {
    // 502 `aws_call_failed` is a live AWS call that failed transiently -- the one
    // refusal on this path where pressing the button again is honest advice.
    vi.mocked(awsControlApi.backupRun).mockRejectedValue(new Error('aws_call_failed'))

    await renderDrive('backup')
    fireEvent.click(await screen.findByTestId('backup-run-snapshot'))

    const err = await screen.findByTestId('backup-error-snapshot')
    expect(err.textContent ?? '').toContain('Try again')
  })

  it('does not invite a retry for a refusal the owner must act on', async () => {
    // An UNRECOGNISED code: still not retryable, so the generic line promises
    // nothing rather than inviting a retry. Defaulting to "try again" and
    // excepting one code was the wrong way round.
    vi.mocked(awsControlApi.backupRun).mockRejectedValue(new Error('some_new_code'))

    await renderDrive('backup')
    fireEvent.click(await screen.findByTestId('backup-run-snapshot'))

    const err = await screen.findByTestId('backup-error-snapshot')
    expect(err.textContent ?? '').toContain('Could not start')
    expect(err.textContent ?? '').not.toContain('Try again')
  })

  it('names the cause and the next step for a refusal with a known code', async () => {
    // The route sends these codes so the UI can localise them. Collapsing them
    // into one generic line makes the owner guess which of several repairs to
    // attempt, so each reachable code names its own cause.
    vi.mocked(awsControlApi.backupRun).mockRejectedValue(new Error('aws_consent_required'))

    await renderDrive('backup')
    fireEvent.click(await screen.findByTestId('backup-run-snapshot'))

    const err = await screen.findByTestId('backup-error-snapshot')
    expect(err.textContent ?? '').toContain('S3 access')
    expect(err.textContent ?? '').not.toContain('Try again')
  })

  it('sends the owner to create a drive when there is not one yet', async () => {
    // Reachable as a RACE past the pane gate: the pane only mounts when the drive
    // exists, so this is the drive being deleted between that render and the
    // click. Rare is not impossible, which is why it earns a string where
    // `invalid_account` does not.
    vi.mocked(awsControlApi.backupRun).mockRejectedValue(new Error('drive_missing'))

    await renderDrive('backup')
    fireEvent.click(await screen.findByTestId('backup-run-snapshot'))

    expect((await screen.findByTestId('backup-error-snapshot')).textContent ?? '').toContain('no drive yet')
  })

  it('does not tell the owner to retry when the runtime is absent', async () => {
    // 503 `jobs_unavailable` means the job runtime was never registered -- a
    // missing manifest grant or a failed startup, not a transient blip. The
    // string deliberately avoids "right now", which implied waiting would help.
    vi.mocked(awsControlApi.backupRun).mockRejectedValue(new Error('jobs_unavailable'))

    await renderDrive('backup')
    fireEvent.click(await screen.findByTestId('backup-run-snapshot'))

    const err = await screen.findByTestId('backup-error-snapshot')
    expect(err.textContent ?? '').toContain('not available for this app')
    expect(err.textContent ?? '').not.toContain('Try again')
    expect(err.textContent ?? '').not.toContain('right now')
  })

  /* ── Drive: folder navigation and load-more ─────────────────────────────── */

  it('lists a folder and file with a download and a load-more control', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'report.pdf', size: 2048, modified: '2026-08-20T00:00:00Z' }],
      folders: ['invoices'],
      nextToken: 'tok-2',
    })

    await renderDrive('drive')

    // A folder row and a file row both render.
    expect(await screen.findByTestId('drive-folder')).toHaveTextContent('invoices')
    const file = await screen.findByTestId('drive-file')
    expect(file).toHaveTextContent('report.pdf')
    // A nextToken produces a Load more control.
    expect(screen.getByTestId('drive-load-more')).toBeTruthy()
  })

  /** "Load more" is a promise about the rows already on screen: Files must
   * append the next page below them, exactly as Library does. Keying the
   * query on the continuation token instead would make each press swap the
   * whole page. */
  it('load more appends the next Files page below the current one instead of swapping it', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList)
      .mockResolvedValueOnce({
        files: [{ key: 'alpha.txt', size: 1024, modified: '2026-08-20T00:00:00Z' }],
        folders: [], nextToken: 'tok-2',
      })
      .mockResolvedValueOnce({
        files: [{ key: 'beta.txt', size: 1024, modified: '2026-08-20T00:00:00Z' }],
        folders: [],
      })

    await renderDrive('drive')
    expect(await screen.findByText('alpha.txt')).toBeTruthy()

    fireEvent.click(screen.getByTestId('drive-load-more'))

    // Both pages are on screen -- the first one did not vanish.
    await waitFor(() => expect(screen.getAllByTestId('drive-file')).toHaveLength(2))
    const listing = screen.getByTestId('drive-listing').textContent ?? ''
    expect(listing).toContain('alpha.txt')
    expect(listing).toContain('beta.txt')
    // The second page is fetched WITH the first page's continuation token.
    expect(awsControlApi.driveList).toHaveBeenLastCalledWith(ACCOUNT_ID, 'drive', '', 'tok-2')
  })

  it('opening a folder shows that folder alone, not the accumulated parent rows', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList)
      .mockResolvedValueOnce({
        files: [{ key: 'alpha.txt', size: 1024, modified: '2026-08-20T00:00:00Z' }],
        folders: ['docs'], nextToken: 'tok-2',
      })
      .mockResolvedValueOnce({
        files: [{ key: 'beta.txt', size: 1024, modified: '2026-08-20T00:00:00Z' }],
        folders: [],
      })
      .mockResolvedValueOnce({
        files: [{ key: 'docs/gamma.txt', size: 1024, modified: '2026-08-20T00:00:00Z' }],
        folders: [],
      })

    await renderDrive('drive')
    fireEvent.click(await screen.findByTestId('drive-load-more'))
    // Premise: the parent listing really is deep (two accumulated pages).
    await waitFor(() => expect(screen.getAllByTestId('drive-file')).toHaveLength(2))

    fireEvent.click(screen.getByTestId('drive-folder'))

    // The new path starts from its own first page: no rows carried over.
    expect(await screen.findByText('gamma.txt')).toBeTruthy()
    expect(screen.getAllByTestId('drive-file')).toHaveLength(1)
    expect(screen.queryByText('alpha.txt')).toBeNull()
    expect(screen.queryByText('beta.txt')).toBeNull()
    // And the navigation fetches the folder from its FIRST page, no stale token.
    expect(awsControlApi.driveList).toHaveBeenLastCalledWith(ACCOUNT_ID, 'drive', 'docs', '')
  })

  it('drills into a folder from anywhere on the row, refetching for the new path', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [], folders: ['invoices'],
    })

    await renderDrive('drive')

    // The ROW, not the name: a folder row in a file table is expected to open
    // from any of its cells, and the artifact table's own folder row does the
    // same. Pinning the row here would fail if the handler shrank back to just
    // the name text, leaving the Kind/Size/Modified cells dead.
    fireEvent.click(await screen.findByTestId('drive-folder'))
    await waitFor(() =>
      expect(awsControlApi.driveList).toHaveBeenCalledWith(ACCOUNT_ID, 'drive', 'invoices', ''),
    )
  })

  it('opens the folder from its name control too, for keyboard reach', async () => {
    // The inner button is the real focusable control; the row click is a mouse
    // convenience on top of it, so both must navigate.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [], folders: ['invoices'],
    })

    await renderDrive('drive')

    fireEvent.click(await screen.findByTestId('drive-folder-open'))
    await waitFor(() =>
      expect(awsControlApi.driveList).toHaveBeenCalledWith(ACCOUNT_ID, 'drive', 'invoices', ''),
    )
  })

  it('attaches the folder confirm to the folder that was clicked, not the last one', async () => {
    // The confirm restates the folder name, and that name is the only guard
    // before an irreversible recursive delete - so the strip has to sit under
    // the row it belongs to. Rendered once after the whole folder list, deleting
    // the FIRST folder put the prompt under the LAST one, visually attached to a
    // folder the reader did not pick. Needs two folders to show up at all.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [], folders: ['invoices', 'receipts'],
    })

    await renderDrive('drive')
    await screen.findByTestId('drive-listing')

    const rows = screen.getAllByTestId('drive-folder')
    expect(rows).toHaveLength(2)
    fireEvent.keyDown(within(rows[0]).getByTestId('drive-folder-more'), { key: 'Enter' })
    fireEvent.click(await screen.findByTestId('drive-folder-delete'))

    const confirm = await screen.findByTestId('drive-folder-delete-confirm')
    expect(confirm).toHaveTextContent('invoices')
    // The strip is the very next row after the folder it targets.
    expect(rows[0].nextElementSibling).toBe(confirm)
  })

  it('reports how many objects the folder delete actually removed', async () => {
    // The count is only knowable from the RESPONSE - a figure shown before
    // consent would cost a second full recursive listing - so the page states
    // what was removed. Without this the endpoint's count had no consumer while
    // the type comment claimed the UI showed it.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: ['invoices'] })
    vi.mocked(awsControlApi.driveFolderDelete).mockResolvedValue({ deleted: true, path: 'invoices', objects: 12 })

    await renderDrive('drive')
    await screen.findByTestId('drive-listing')
    fireEvent.keyDown(await screen.findByTestId('drive-folder-more'), { key: 'Enter' })
    fireEvent.click(await screen.findByTestId('drive-folder-delete'))
    fireEvent.click(await screen.findByTestId('drive-folder-delete-action'))

    const line = await screen.findByTestId('drive-folder-deleted')
    expect(line.textContent ?? '').toContain('12')
  })

  it('the deleted count belongs to the folder the delete happened in, and leaving that folder retires it', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: ['invoices', 'docs'] })
    let finish: (r: { deleted: true; path: string; objects: number }) => void = () => {}
    vi.mocked(awsControlApi.driveFolderDelete).mockImplementation(
      () => new Promise((resolve) => { finish = resolve }),
    )
    await renderDrive('drive')
    await screen.findByTestId('drive-listing')

    fireEvent.keyDown((await screen.findAllByTestId('drive-folder-more'))[0], { key: 'Enter' })
    fireEvent.click(await screen.findByTestId('drive-folder-delete'))
    fireEvent.click(await screen.findByTestId('drive-folder-delete-action'))
    await waitFor(() => expect(awsControlApi.driveFolderDelete).toHaveBeenCalledWith(ACCOUNT_ID, 'drive', 'invoices'))

    // The reader walks into `docs` while the delete is still running. The
    // count that arrives now belongs to the top level, not to `docs`.
    fireEvent.click(screen.getAllByTestId('drive-folder-open')[1])
    await waitFor(() => expect(awsControlApi.driveList).toHaveBeenLastCalledWith(ACCOUNT_ID, 'drive', 'docs', ''))
    finish({ deleted: true, path: 'invoices', objects: 12 })
    await waitFor(() => expect(awsControlApi.driveFolderDelete).toHaveBeenCalledTimes(1))
    expect(screen.queryByTestId('drive-folder-deleted')).toBeNull()

    // Back at the top level the count is waiting where it happened...
    fireEvent.click(within(screen.getByTestId('drive-crumbs')).getByText(i18nT('apps.awsControl.console.section_files')))
    expect((await screen.findByTestId('drive-folder-deleted')).textContent ?? '').toContain('12')

    // ...and leaving again retires it: a later return does not replay it.
    fireEvent.click((await screen.findAllByTestId('drive-folder-open'))[1])
    await waitFor(() => expect(screen.queryByTestId('drive-folder-deleted')).toBeNull())
    fireEvent.click(within(screen.getByTestId('drive-crumbs')).getByText(i18nT('apps.awsControl.console.section_files')))
    await screen.findAllByTestId('drive-folder-open')
    expect(screen.queryByTestId('drive-folder-deleted')).toBeNull()
  })

  it('rejects a bad FOLDER name with folder wording, not the file message', async () => {
    // The shared drive_bad_name text names a file; the reader typed a folder.
    stubDrivePresent()
    await renderDrive('drive')

    // The name field is a disclosure now: open it first.
    fireEvent.click(await screen.findByTestId('drive-folder-toggle'))
    fireEvent.change(await screen.findByTestId('drive-folder-name'), { target: { value: '../escape' } })
    fireEvent.click(screen.getByTestId('drive-folder-create'))

    const err = await screen.findByTestId('drive-folder-error')
    expect(err).toHaveTextContent(i18nT('apps.awsControl.console.folder_bad_name'))
    expect(err).not.toHaveTextContent(i18nT('apps.awsControl.console.drive_bad_name'))
    // Never reached the endpoint.
    expect(awsControlApi.driveFolderCreate).not.toHaveBeenCalled()
  })
})

describe('DrivePage sections: folder disclosure and downloads', () => {
  it('folder disclosure swaps Upload out while open, and blurring the empty field collapses it', async () => {
    // Expanded, the row must stay one two-button action group (Create/Cancel):
    // Upload hides rather than becoming a third sibling. An abandoned empty
    // field must not leave the toolbar stuck expanded — blur puts it back.
    stubDrivePresent()
    await renderDrive('drive')

    expect(screen.getByTestId('drive-upload-btn')).toBeTruthy()
    fireEvent.click(screen.getByTestId('drive-folder-toggle'))
    expect(screen.queryByTestId('drive-upload-btn')).toBeNull()

    const name = screen.getByTestId('drive-folder-name')
    // Blur with TEXT keeps the disclosure open (the reader is mid-thought)...
    fireEvent.change(name, { target: { value: 'photos' } })
    fireEvent.blur(name)
    expect(screen.getByTestId('drive-folder-name')).toBeTruthy()
    // ...and blur with an EMPTY field collapses back to the toggle + Upload.
    fireEvent.change(name, { target: { value: '' } })
    fireEvent.blur(name)
    expect(screen.queryByTestId('drive-folder-name')).toBeNull()
    expect(screen.getByTestId('drive-upload-btn')).toBeTruthy()
  })

  it('collapsing after a failed create clears BOTH error rows, not just the local one', async () => {
    // drive-folder-create-error keys on the mutation's isError and renders
    // OUTSIDE the disclosure — without a reset, Cancel after a failed create
    // leaves a danger row orphaned under a toolbar whose input is gone.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveFolderCreate).mockRejectedValue(new Error('boom'))
    await renderDrive('drive')

    fireEvent.click(screen.getByTestId('drive-folder-toggle'))
    fireEvent.change(screen.getByTestId('drive-folder-name'), { target: { value: 'photos' } })
    fireEvent.click(screen.getByTestId('drive-folder-create'))
    await screen.findByTestId('drive-folder-create-error')

    fireEvent.click(screen.getByTestId('drive-folder-cancel'))
    expect(screen.queryByTestId('drive-folder-name')).toBeNull()
    expect(screen.queryByTestId('drive-folder-create-error')).toBeNull()
    expect(screen.queryByTestId('drive-folder-error')).toBeNull()
  })

  it('refuses to collapse while a create is in flight, so a failure returns to the typed name', async () => {
    // Escape/Cancel during a pending create must NOT erase the name being
    // created: the request can fail, and the reader needs their input back to
    // retry. The disclosure closes only once the mutation settles.
    stubDrivePresent()
    let rejectCreate: (e: Error) => void = () => {}
    vi.mocked(awsControlApi.driveFolderCreate).mockImplementation(
      () => new Promise((_res, rej) => { rejectCreate = rej }),
    )
    await renderDrive('drive')

    fireEvent.click(screen.getByTestId('drive-folder-toggle'))
    const name = screen.getByTestId('drive-folder-name')
    fireEvent.change(name, { target: { value: 'photos' } })
    fireEvent.click(screen.getByTestId('drive-folder-create'))
    // The mutationFn runs on a microtask: wait until it has actually been
    // invoked (and captured its reject) before exercising the in-flight paths —
    // otherwise the Escape below races a not-yet-pending mutation.
    await waitFor(() => expect(awsControlApi.driveFolderCreate).toHaveBeenCalledTimes(1))

    // In flight: Escape, Cancel and blur are all refused — input and text stay.
    fireEvent.keyDown(name, { key: 'Escape' })
    fireEvent.click(screen.getByTestId('drive-folder-cancel'))
    fireEvent.blur(name)
    expect(screen.getByTestId('drive-folder-name')).toHaveValue('photos')

    // The create fails: the typed name is still there to retry from, and the
    // disclosure can now be cancelled normally.
    await act(async () => { rejectCreate(new Error('boom')) })
    await screen.findByTestId('drive-folder-create-error')
    expect(screen.getByTestId('drive-folder-name')).toHaveValue('photos')
    fireEvent.click(screen.getByTestId('drive-folder-cancel'))
    expect(screen.queryByTestId('drive-folder-name')).toBeNull()
    expect(screen.queryByTestId('drive-folder-create-error')).toBeNull()
  })

  it('deleting a folder does not also open it', async () => {
    // The delete control sits inside a row whose own click navigates, so it must
    // stop the event: opening the folder you just asked to delete would swap the
    // listing out from under the confirm strip.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [], folders: ['invoices'],
    })

    await renderDrive('drive')
    await screen.findByTestId('drive-listing')
    vi.mocked(awsControlApi.driveList).mockClear()

    fireEvent.keyDown(await screen.findByTestId('drive-folder-more'), { key: 'Enter' })
    fireEvent.click(await screen.findByTestId('drive-folder-delete'))

    // The confirm strip is up, naming the folder...
    expect(await screen.findByTestId('drive-folder-delete-confirm')).toHaveTextContent('invoices')
    // ...and no navigation happened.
    expect(awsControlApi.driveList).not.toHaveBeenCalled()
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
    await renderDrive('drive')

    fireEvent.click(await screen.findByTestId('drive-folder-open'))
    await waitFor(() =>
      expect(awsControlApi.driveList).toHaveBeenCalledWith(ACCOUNT_ID, 'drive', 'a/b', ''),
    )

    // The crumb group holds exactly two controls: Root and the overflow.
    const crumbs = await screen.findByTestId('drive-crumbs')
    expect(crumbs.querySelectorAll('button')).toHaveLength(2)
    // The current folder is rendered as text, not as a third control.
    expect(screen.getByTestId('drive-crumb-current')).toHaveTextContent('b')

    // The overflow still navigates to the ancestor `a`. It is the shared
    // `ui/dropdown-menu` now, so it opens like every other row menu here and
    // its entries are menu items, not buttons.
    fireEvent.keyDown(screen.getByTestId('drive-crumb-more'), { key: 'Enter' })
    const menu = await screen.findByTestId('drive-crumb-menu')
    const items = within(menu).getAllByRole('menuitem')
    expect(items).toHaveLength(1)
    expect(items[0]).toHaveTextContent('a')
    fireEvent.click(items[0])
    await waitFor(() =>
      expect(awsControlApi.driveList).toHaveBeenCalledWith(ACCOUNT_ID, 'drive', 'a', ''),
    )
  })

  it('shows no breadcrumb at the section root, and no overflow one folder deep', async () => {
    // With nothing to jump PAST, an overflow would be an empty affordance - and
    // at the section root the whole breadcrumb would only repeat the section
    // header directly above it, so it is not rendered at all.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [], folders: ['invoices'],
    })
    await renderDrive('drive')

    // At the section root: no breadcrumb.
    await screen.findByTestId('drive-listing')
    expect(screen.queryByTestId('drive-crumbs')).toBeNull()
    expect(screen.queryByTestId('drive-crumb-more')).toBeNull()

    // One level deep: the breadcrumb appears with its root control and the
    // folder as text, and still no overflow.
    fireEvent.click(await screen.findByTestId('drive-folder-open'))
    await waitFor(() =>
      expect(screen.getByTestId('drive-crumb-current')).toHaveTextContent('invoices'),
    )
    const crumbs = screen.getByTestId('drive-crumbs')
    expect(crumbs.querySelectorAll('button')).toHaveLength(1)
    expect(screen.queryByTestId('drive-crumb-more')).toBeNull()
  })

  it('shows the drive empty state when the listing has no files or folders', async () => {
    stubDrivePresent()
    await renderDrive('drive')
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

    await renderDrive('drive')

    await chooseFromMenu(await screen.findByTestId('drive-more'), 'drive-download')
    // The tab was opened blank inside the click, then navigated to the presign.
    // No 'noopener' feature: with it the standard makes window.open return null,
    // so requesting it would hand back no tab to navigate at all.
    // Opening from a MENU ITEM keeps that synchronous: Radix dispatches
    // `onSelect` from the item's own click handler, so the window.open still runs
    // inside the user gesture and is not treated as an unattended popup. That is
    // the non-obvious half of moving Download into the overflow, and this
    // assertion is what proves it did not break.
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

    await renderDrive('drive')

    await chooseFromMenu(await screen.findByTestId('drive-more'), 'drive-download')
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
    await renderDrive('drive')

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

    await renderDrive('drive')

    const input = await screen.findByTestId('drive-file-input')
    const good = new File(['x'], 'ok.txt', { type: 'text/plain' })
    fireEvent.change(input, { target: { files: [good] } })

    // The api is called with the section and the file, and no error strip shows.
    await waitFor(() =>
      expect(awsControlApi.driveUpload).toHaveBeenCalledWith(ACCOUNT_ID, 'drive', 'ok.txt', good),
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

    await renderDrive('drive')

    fireEvent.keyDown(await screen.findByTestId('drive-more'), { key: 'Enter' })
    fireEvent.click(await screen.findByTestId('drive-delete'))
    fireEvent.click(await screen.findByTestId('drive-delete-confirm-action'))

    // The failure surfaces in-strip and the strip stays open (no onSuccess close).
    const notice = await screen.findByTestId('drive-delete-error')
    expect(screen.getByTestId('drive-delete-confirm')).toBeTruthy()
    // The strip is a wrapping row holding Cancel and Delete; the notice (and its
    // hand-off) takes a full line of its own rather than becoming a third action
    // beside them.
    expect(notice).toHaveClass('basis-full')
    expect(within(notice).getByRole('button', { name: /ask the agent/i })).toBeTruthy()
  })
})

describe('DrivePage sections: Library', () => {
  /**
   * THE assertion for this section's redesign.
   *
   * The Library folder used to render the LOCAL artifact list, so it showed
   * artifacts that were not in the drive at all -- every one labelled "not
   * synced" -- while the Files folder beside it sat empty, which left the two
   * folders impossible to tell apart. It now lists the bucket prefix, so a card
   * is here if and only if the object is in the cloud. The local library is only
   * a name/kind lookup for the cards.
   */
  it('lists the cloud prefix, not the local artifact library', async () => {
    stubDrivePresent()
    // One artifact IS in the cloud; a second exists locally and was never pushed.
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: ['notes'] })
    vi.mocked(awsControlApi.library).mockResolvedValue({
      artifacts: [
        { slug: 'notes', name: 'Notes', kind: 'markdown', version: 3, updatedAt: '2026-08-20T00:00:00Z', pushedVersion: 3, pushedAt: '2026-08-21T00:00:00Z' },
        { slug: 'draft', name: 'Draft', kind: 'markdown', version: 1, updatedAt: '2026-08-20T00:00:00Z', pushedVersion: null, pushedAt: null },
      ],
    })

    await renderDrive('library')
    await screen.findByTestId('library-section')

    // Exactly the one cloud object -- NOT the two local artifacts.
    const cards = await screen.findAllByTestId('library-card')
    expect(cards).toHaveLength(1)
    // Named from the local lookup, keyed by the prefix folder name.
    expect(cards[0].textContent).toContain('Notes')
    // The never-pushed local artifact does not appear in the folder at all.
    expect(screen.queryByText('Draft')).toBeNull()
    // And the listing came from the library section of the bucket.
    expect(awsControlApi.driveList).toHaveBeenCalledWith(ACCOUNT_ID, 'library', '', '')
  })

  it('falls back to a cloud-only card when no local artifact backs the object', async () => {
    stubDrivePresent()
    // In the cloud, but the local copy is gone (deleted locally, or pushed from
    // another machine) -- there is nothing to preview.
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: ['orphan'] })
    vi.mocked(awsControlApi.library).mockResolvedValue({ artifacts: [] })

    await renderDrive('library')

    const card = await screen.findByTestId('library-card')
    // The slug still identifies it, and the card says why there is no preview.
    expect(card.textContent).toContain('orphan')
    expect(screen.getByText(/only/i)).toBeTruthy()
  })

  it('the empty Library folder sends the reader to the picker', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: [] })
    vi.mocked(awsControlApi.library).mockResolvedValue({
      artifacts: [
        { slug: 'draft', name: 'Draft', kind: 'markdown', version: 1, updatedAt: '2026-08-20T00:00:00Z', pushedVersion: null, pushedAt: null },
      ],
    })

    await renderDrive('library')

    // The empty state, not a bare "this folder is empty" row.
    expect(await screen.findByTestId('library-empty')).toBeTruthy()
    /* No removal warning here: with nothing in the folder it warned about
       deleting copies that do not exist, in front of the one screen whose job is
       explaining what the folder is for. The picker carries its own one-way
       warning, so the person about to make the first copy is still told. */
    expect(screen.queryByTestId('library-remove-hint')).toBeNull()
    // Its action opens the picker rather than leaving the reader to hunt.
    fireEvent.click(screen.getByTestId('library-empty-add'))
    expect(await screen.findByTestId('library-add-dialog')).toBeTruthy()
  })

  it('the picker filters by kind chip and pushes an artifact into the drive', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: [] })
    vi.mocked(awsControlApi.library).mockResolvedValue({
      artifacts: [
        { slug: 'notes', name: 'Notes', kind: 'markdown', version: 3, updatedAt: '2026-08-20T00:00:00Z', pushedVersion: null, pushedAt: null },
        { slug: 'pic', name: 'Pic', kind: 'image', version: 1, updatedAt: '2026-08-20T00:00:00Z', pushedVersion: null, pushedAt: null },
      ],
    })
    vi.mocked(awsControlApi.libraryPush).mockResolvedValue({ pushed: true, slug: 'notes', version: 3 } as never)

    await renderDrive('library')
    fireEvent.click(await screen.findByTestId('library-add-open'))
    await screen.findByTestId('library-add-dialog')

    // Both candidates under "all"; the chips live in the picker now.
    expect(await screen.findAllByTestId('library-tile')).toHaveLength(2)
    fireEvent.click(screen.getByTestId('library-chip-image'))
    expect(screen.getAllByTestId('library-tile')).toHaveLength(1)

    fireEvent.click(screen.getByTestId('library-chip-markdown'))
    fireEvent.click(screen.getByTestId('library-push'))
    await waitFor(() => expect(awsControlApi.libraryPush).toHaveBeenCalledWith(ACCOUNT_ID, 'notes'))
  })

  /**
   * An image is refused by the backend (its kind has no pushed-file extension),
   * and images are the bulk of a real library -- so the card must SAY so. A
   * disabled button with no reason reads as a broken page.
   */
  it('tells the reader why an image cannot be added yet, instead of only disabling', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: [] })
    vi.mocked(awsControlApi.library).mockResolvedValue({
      artifacts: [
        { slug: 'pic', name: 'Pic', kind: 'image', version: 1, updatedAt: '2026-08-20T00:00:00Z', pushedVersion: null, pushedAt: null },
      ],
    })

    await renderDrive('library')
    fireEvent.click(await screen.findByTestId('library-add-open'))

    expect(await screen.findByTestId('library-not-pushable')).toBeTruthy()
    // No push control at all on an image card -- nothing to click and be refused.
    expect(screen.queryByTestId('library-push')).toBeNull()
  })

  /**
   * Removal lives on the CLOUD LISTING, behind the same `⋮` the Files folder's
   * cards use, and the confirm names the prefix it empties.
   *
   * The picker cannot host this control soundly: its rows are local artifacts
   * joined to a slug-keyed ledger, so a reused slug lends a never-pushed
   * artifact another one's push record and a removal there empties a different
   * artifact's copy. A row of this folder comes from `driveList`, so removing it
   * empties the object the reader was shown. The fixture makes that concrete --
   * the bucket holds `notes` while the LOCAL artifact of that slug reports
   * pushedVersion null, which is exactly the reused-slug shape the picker could
   * not tell apart, and the removal still targets the listed slug.
   *
   * DELETE THIS TEST and a visible danger button on every browse card, or a
   * removal keyed to local state, both pass again.
   */
  it('removes from the cloud listing, naming the artifacts/ prefix it empties', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: ['notes'] })
    vi.mocked(awsControlApi.library).mockResolvedValue({
      artifacts: [
        { slug: 'notes', name: 'Notes', kind: 'markdown', version: 1, updatedAt: '2026-08-20T00:00:00Z', pushedVersion: null, pushedAt: null },
      ],
    })
    vi.mocked(awsControlApi.libraryRemove).mockResolvedValue({ removed: true } as never)

    await renderDrive('library')
    expect(await screen.findByTestId('library-card')).toBeTruthy()

    // Hidden behind the card's `⋮`. A visible danger button on every card of a
    // browse surface is the design this replaces, so the control being a menu
    // item rather than a button is part of the contract.
    await chooseFromMenu(screen.getByTestId('library-more'), 'library-remove')
    const confirm = await screen.findByTestId('library-remove-confirm')
    // The BUCKET prefix, pinned literally: the library section maps to
    // `artifacts/` (`SECTION_PREFIXES`), which is what the page's own "Where this
    // lives" drawer tells the reader to `aws s3 ls`. Naming the `library/` API
    // route segment instead prints a folder the bucket does not contain, so the
    // one cross-check that catches a wrong-target delete comes up empty.
    expect(confirm).toHaveTextContent('artifacts/notes/')
    expect(confirm).not.toHaveTextContent('library/notes/')
    expect(awsControlApi.libraryRemove).not.toHaveBeenCalled()

    // Cancel closes the strip without a call.
    fireEvent.click(screen.getByTestId('library-remove-confirm-cancel'))
    expect(screen.queryByTestId('library-remove-confirm')).toBeNull()
    expect(awsControlApi.libraryRemove).not.toHaveBeenCalled()

    // Confirming removes the LISTED slug.
    await chooseFromMenu(screen.getByTestId('library-more'), 'library-remove')
    fireEvent.click(await screen.findByTestId('library-remove-confirm-action'))
    await waitFor(() => expect(awsControlApi.libraryRemove).toHaveBeenCalledWith(ACCOUNT_ID, 'notes'))
  })

  /**
   * A copy with no local row is removable, which is the whole reason the control
   * moved.
   *
   * `list_pushable` walks the LOCAL store, so a copy pushed from another machine
   * has no picker row to carry it -- under the old placement it was unreachable
   * while a locally reused slug could delete the wrong one, exactly backwards
   * from the console the spec asks for. Nothing here may be gated on local
   * state, so the fixture gives the local library none at all.
   *
   * DELETE THIS TEST and gating removal on `synced` passes again, which strands
   * every copy pushed from a second machine.
   */
  it('removes a cloud copy that has no local artifact behind it', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: ['from-elsewhere'] })
    vi.mocked(awsControlApi.library).mockResolvedValue({ artifacts: [] })
    vi.mocked(awsControlApi.libraryRemove).mockResolvedValue({ removed: true } as never)

    await renderDrive('library')
    expect(await screen.findByTestId('library-card')).toBeTruthy()

    await chooseFromMenu(screen.getByTestId('library-more'), 'library-remove')
    expect(await screen.findByTestId('library-remove-confirm')).toHaveTextContent('artifacts/from-elsewhere/')
    fireEvent.click(screen.getByTestId('library-remove-confirm-action'))
    await waitFor(() =>
      expect(awsControlApi.libraryRemove).toHaveBeenCalledWith(ACCOUNT_ID, 'from-elsewhere'))
  })

  /**
   * The list view is a way of LOOKING at this folder, not a capability tier.
   *
   * The view choice PERSISTS per section, so a control the grid has and the rows
   * do not is one a reader who once switched to the list can never reach again --
   * the same rule this folder's stale-version warning already follows.
   */
  it('carries the removal in list view too', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: ['notes'] })
    vi.mocked(awsControlApi.library).mockResolvedValue({ artifacts: [] })
    vi.mocked(awsControlApi.libraryRemove).mockResolvedValue({ removed: true } as never)

    await renderDrive('library')
    fireEvent.click(await screen.findByTitle('List view'))
    expect(await screen.findByTestId('library-list-row')).toBeTruthy()

    await chooseFromMenu(screen.getByTestId('library-more'), 'library-remove')
    expect(await screen.findByTestId('library-remove-confirm')).toHaveTextContent('artifacts/notes/')
    fireEvent.click(screen.getByTestId('library-remove-confirm-action'))
    await waitFor(() => expect(awsControlApi.libraryRemove).toHaveBeenCalledWith(ACCOUNT_ID, 'notes'))
  })

  /**
   * The picker must NOT offer removal. This is the regression guard for the
   * defect the relocation fixes, not a test of absence for its own sake.
   *
   * A picker row cannot identify which cloud copy is its own: `synced` IS the
   * possibly-inherited ledger record, and `pushedVersion === version` is
   * satisfied by a never-pushed artifact at v1 against a pushed v1. Any Remove
   * reachable from these cards is unsound by construction, in either form -- a
   * button or a menu -- which is why both ids are asserted absent.
   *
   * DELETE THIS TEST and the wrong-target delete can be reintroduced into the
   * dialog labelled "Add from Artifacts" without a single test going red.
   */
  it('does not offer removal from the picker, where identity is unprovable', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: [] })
    vi.mocked(awsControlApi.library).mockResolvedValue({
      artifacts: [
        { slug: 'notes', name: 'Notes', kind: 'markdown', version: 4, updatedAt: '2026-08-20T00:00:00Z', pushedVersion: 4, pushedAt: '2026-08-20T00:00:00Z' },
        { slug: 'draft', name: 'Draft', kind: 'markdown', version: 1, updatedAt: '2026-08-20T00:00:00Z', pushedVersion: 1, pushedAt: '2026-08-20T00:00:00Z' },
      ],
    })

    await renderDrive('library')
    fireEvent.click(await screen.findByTestId('library-add-open'))
    const dialog = await screen.findByTestId('library-add-dialog')

    // Both cards are synced -- the state that used to reveal the control.
    expect(await screen.findAllByTestId('library-tile')).toHaveLength(2)
    expect(within(dialog).queryByTestId('library-remove')).toBeNull()
    expect(within(dialog).queryByTestId('library-more')).toBeNull()
    expect(within(dialog).queryByTestId('library-remove-confirm')).toBeNull()
  })

  /**
   * The COMPLEMENT of the picker guard, and the boundary between them.
   *
   * A local twin existing does NOT make a listed copy's identity unprovable, so
   * Remove is offered here even when `local` is defined. The two surfaces differ
   * in where the row comes from:
   *
   *   picker   a row is a LOCAL artifact, and whether any cloud object belongs to
   *            it is inferred from a slug-keyed ledger. Nothing on that surface
   *            observes the bucket, so the delete target is a guess -- which is
   *            why the test above pins the absence of the control.
   *   listing  a row exists if and only if `artifacts/<slug>/` is in the bucket
   *            (`driveList(account, 'library')` enumerates the prefix). The
   *            removal empties exactly the prefix the row was listed from, so the
   *            target is the observed object, not an inference.
   *
   * `local` here supplies only the card's LABEL and thumbnail. This test uses the
   * slug-reuse shape deliberately -- a listed cloud object whose local twin is a
   * different artifact that took its slug -- and asserts the control stays and
   * targets the LISTED slug. Deleting this test would let a later round "fix" a
   * misread of the picker guard by hiding Remove wherever a local twin exists,
   * which is most copies, restoring the no-way-to-remove-a-copy gap this PR
   * exists to close.
   */
  it('offers removal on a listed copy with a local twin, and targets the listed prefix', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: ['notes'] })
    // The local twin is a DIFFERENT artifact wearing the same slug: the exact
    // case the picker cannot resolve, and the case the listing resolves by
    // having observed the object it is about to empty.
    vi.mocked(awsControlApi.library).mockResolvedValue({
      artifacts: [
        { slug: 'notes', name: 'A Later Artifact', kind: 'markdown', version: 1, updatedAt: '2026-08-20T00:00:00Z', pushedVersion: 1, pushedAt: '2026-08-20T00:00:00Z' },
      ],
    })
    vi.mocked(awsControlApi.libraryRemove).mockResolvedValue(undefined as never)

    await renderDrive('library')
    const card = (await screen.findAllByTestId('library-card'))[0]
    // The local join supplies the label, so the twin's name is what shows.
    expect(card).toHaveTextContent('A Later Artifact')

    // The control is present despite the twin.
    await chooseFromMenu(within(card).getByTestId('library-more'), 'library-remove')

    // The confirm names the folder it empties -- the identity that was observed,
    // not the one joined from the ledger.
    const strip = await screen.findByTestId('library-remove-confirm')
    expect(strip).toHaveTextContent('artifacts/notes/')

    // And the request targets the LISTED slug.
    fireEvent.click(screen.getByTestId('library-remove-confirm-action'))
    await waitFor(() =>
      expect(awsControlApi.libraryRemove).toHaveBeenCalledWith(ACCOUNT_ID, 'notes'))

    /* The LIST row carries the same guarantee, and it needs its own assertion:
       the existing list-view test lists an ORPHAN (no local twin), so a guard
       that hid Remove wherever a twin exists would pass it untouched. Measured --
       without these four lines that mutant survives. */
    fireEvent.click(await screen.findByTitle('List view'))
    const row = await screen.findByTestId('library-list-row')
    expect(row).toHaveTextContent('A Later Artifact')
    await chooseFromMenu(screen.getByTestId('library-more'), 'library-remove')
    expect(await screen.findByTestId('library-remove-confirm')).toHaveTextContent('artifacts/notes/')
  })

  /**
   * One removal completing must not close a DIFFERENT row's confirm.
   *
   * The section holds one `useMutation` for every row, which is what removed the
   * per-card confirm flag -- but its completion callback then has to prove it
   * still owns the state it clears. A `delete_prefix` sweep over several S3
   * objects is not instant, so the interleave needs nothing unusual: remove A,
   * open B's confirm while A is still running, and an unconditional
   * `setConfirmSlug(null)` closes B when A lands.
   *
   * DELETE the ownership guard and this reds. In user terms that redness is: a
   * removal you were about to confirm silently vanishes, along with any pending
   * or error state it was showing, because an unrelated removal finished.
   */
  it('does not close one card\'s confirm when a different removal completes', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: ['alpha', 'beta'] })
    vi.mocked(awsControlApi.library).mockResolvedValue({ artifacts: [] })
    // A's removal is held open, so B's confirm is opened while it is in flight.
    let resolveAlpha: (v: unknown) => void = () => {}
    vi.mocked(awsControlApi.libraryRemove).mockImplementation(
      () => new Promise((res) => { resolveAlpha = res }) as never)

    await renderDrive('library')
    const triggers = await screen.findAllByTestId('library-more')
    expect(triggers).toHaveLength(2)

    // Start A's removal and leave it pending.
    await chooseFromMenu(triggers[0], 'library-remove')
    fireEvent.click(await screen.findByTestId('library-remove-confirm-action'))
    await waitFor(() => expect(awsControlApi.libraryRemove).toHaveBeenCalledTimes(1))

    // Open B's confirm while A is still running.
    await chooseFromMenu(screen.getAllByTestId('library-more')[1], 'library-remove')
    const bStrip = await screen.findByTestId('library-remove-confirm')
    expect(bStrip).toHaveTextContent('artifacts/beta/')
    // B is a different row, so it must not wear A's in-flight state: its own
    // danger button still offers the action rather than reading "Removing", and
    // Cancel stays live so the reader can back out at all.
    expect(screen.getByTestId('library-remove-confirm-action')).not.toBeDisabled()
    expect(screen.getByTestId('library-remove-confirm-cancel')).not.toBeDisabled()

    // A lands. B's confirm must survive it.
    resolveAlpha({ removed: true })
    await waitFor(() => expect(awsControlApi.driveList).toHaveBeenCalled())
    expect(screen.getByTestId('library-remove-confirm')).toHaveTextContent('artifacts/beta/')
  })

  /**
   * A failed removal belongs to the card that asked for it, and opening another
   * card must not erase it.
   *
   * This is the second direction of the shared-observer coupling. One
   * `useMutation` has ONE slot for the outcome, so the first version of this
   * section called `removeMut.reset()` when a confirm opened -- which threw away
   * the in-flight state, and a removal that then FAILED had nowhere to report it:
   * the copy was still there and nothing on screen said so. The failure is keyed
   * by slug instead, and rendered on the card rather than inside whichever strip
   * happens to be open.
   *
   * DELETE the keying (restore `reset()` in `askRemove`) and this reds. In user
   * terms: a removal you asked for failed, and the app told you nothing because
   * you happened to be looking at a different card while it ran.
   */
  it('keeps a failure on the card that owns it when another confirm is opened', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: ['alpha', 'beta'] })
    vi.mocked(awsControlApi.library).mockResolvedValue({ artifacts: [] })
    let failAlpha: (e: unknown) => void = () => {}
    vi.mocked(awsControlApi.libraryRemove).mockImplementation(
      () => new Promise((_res, rej) => { failAlpha = rej }) as never)

    await renderDrive('library')
    const cards = await screen.findAllByTestId('library-card')
    expect(cards).toHaveLength(2)

    // Start A's removal, then open B's confirm while A is still in flight.
    await chooseFromMenu(screen.getAllByTestId('library-more')[0], 'library-remove')
    fireEvent.click(await screen.findByTestId('library-remove-confirm-action'))
    await waitFor(() => expect(awsControlApi.libraryRemove).toHaveBeenCalledTimes(1))
    await chooseFromMenu(screen.getAllByTestId('library-more')[1], 'library-remove')
    expect(await screen.findByTestId('library-remove-confirm')).toHaveTextContent('artifacts/beta/')

    // A fails. Its failure must report on A's own card, not vanish and not
    // appear on B, whose confirm is the one currently open.
    failAlpha(new Error('boom'))
    const err = await screen.findByTestId('library-remove-error')
    expect(err).toHaveTextContent(i18nT('apps.awsControl.console.library_remove_failed'))
    expect(within(screen.getAllByTestId('library-card')[0]).getByTestId('library-remove-error'))
      .toBeTruthy()
    expect(within(screen.getAllByTestId('library-card')[1]).queryByTestId('library-remove-error'))
      .toBeNull()
    // B's own strip is untouched by A's outcome and still offers its action.
    expect(screen.getByTestId('library-remove-confirm')).toHaveTextContent('artifacts/beta/')
    expect(screen.getByTestId('library-remove-confirm-action')).not.toBeDisabled()
  })

  /**
   * Two failed removals each keep their own error.
   *
   * The third layer of the shared-slot defect, and the one a slug-keyed SCALAR
   * still had: `errorFor` held one slug, so of two overlapping failures the later
   * write hid the earlier card's error. Membership in `failedSlugs` gives N cards
   * N slots.
   *
   * COLLAPSE `failedSlugs` to a scalar and this reds. In user terms: two removals
   * failed and the app only admits to one, so the reader retries a card it was
   * never told about -- or worse, believes that copy is gone.
   */
  it('keeps both failures when two removals fail', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: ['alpha', 'beta'] })
    vi.mocked(awsControlApi.library).mockResolvedValue({ artifacts: [] })
    const rejects: Record<string, (e: unknown) => void> = {}
    vi.mocked(awsControlApi.libraryRemove).mockImplementation(
      (_acct: string, slug: string) => new Promise((_res, rej) => { rejects[slug] = rej }) as never)

    await renderDrive('library')
    expect(await screen.findAllByTestId('library-card')).toHaveLength(2)

    // Start both removals before either settles.
    await chooseFromMenu(screen.getAllByTestId('library-more')[0], 'library-remove')
    fireEvent.click(await screen.findByTestId('library-remove-confirm-action'))
    await chooseFromMenu(screen.getAllByTestId('library-more')[1], 'library-remove')
    fireEvent.click(await screen.findByTestId('library-remove-confirm-action'))
    await waitFor(() => expect(awsControlApi.libraryRemove).toHaveBeenCalledTimes(2))

    // Fail both, earlier first, so a single slot would end up holding only beta.
    rejects['alpha'](new Error('boom'))
    rejects['beta'](new Error('boom'))

    await waitFor(() => expect(screen.getAllByTestId('library-remove-error')).toHaveLength(2))
    const cards = screen.getAllByTestId('library-card')
    expect(within(cards[0]).getByTestId('library-remove-error')).toBeTruthy()
    expect(within(cards[1]).getByTestId('library-remove-error')).toBeTruthy()
  })

  /**
   * The in-flight label belongs to the card being removed.
   *
   * `isPending` is one boolean for the section and `variables` holds only the most
   * recent slug, so either would put "Removing" and a disabled Cancel on whichever
   * card the reader opened last rather than the one actually being worked on.
   *
   * COLLAPSE `pendingSlugs` to `removeMut.isPending` and this reds. In user terms:
   * the app says it is working on a card it is not touching, and disables the
   * Cancel of a removal that has not started.
   */
  it('shows the in-flight label only on the card being removed', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: ['alpha', 'beta'] })
    vi.mocked(awsControlApi.library).mockResolvedValue({ artifacts: [] })
    let finishAlpha: () => void = () => {}
    vi.mocked(awsControlApi.libraryRemove).mockImplementation(
      () => new Promise((res) => { finishAlpha = () => res(undefined as never) }) as never)

    await renderDrive('library')
    expect(await screen.findAllByTestId('library-card')).toHaveLength(2)

    // A is in flight; the reader then opens B's confirm.
    await chooseFromMenu(screen.getAllByTestId('library-more')[0], 'library-remove')
    fireEvent.click(await screen.findByTestId('library-remove-confirm-action'))
    await waitFor(() => expect(awsControlApi.libraryRemove).toHaveBeenCalledTimes(1))
    await chooseFromMenu(screen.getAllByTestId('library-more')[1], 'library-remove')

    // B's strip is the open one and B is NOT being removed, so it must offer its
    // action and a usable Cancel rather than A's progress.
    const strip = await screen.findByTestId('library-remove-confirm')
    expect(strip).toHaveTextContent('artifacts/beta/')
    expect(strip).not.toHaveTextContent(i18nT('apps.awsControl.console.library_removing'))
    expect(screen.getByTestId('library-remove-confirm-action')).toHaveTextContent(
      i18nT('apps.awsControl.console.library_remove_action'))
    expect(screen.getByTestId('library-remove-confirm-cancel')).not.toBeDisabled()
    finishAlpha()
  })

  /**
   * An existing failure survives another card's confirm opening.
   *
   * The reverse order of the interleave above, and a distinct property: there the
   * failure arrives while a sibling's strip is already open, here it is already on
   * screen when the reader opens one. A blanket clear on open -- which is what
   * `removeMut.reset()` amounted to -- erases a failure for a copy that is still
   * in the bucket, purely because the reader looked at something else.
   *
   * DELETE the slug scoping on the clear and this reds. In user terms: the app
   * told you a removal failed, then quietly took the message back.
   */
  it('does not erase an existing failure when another confirm opens', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: ['alpha', 'beta'] })
    vi.mocked(awsControlApi.library).mockResolvedValue({ artifacts: [] })
    vi.mocked(awsControlApi.libraryRemove).mockRejectedValue(new Error('boom'))

    await renderDrive('library')
    expect(await screen.findAllByTestId('library-card')).toHaveLength(2)

    // A fails first, so its error is on screen BEFORE any other confirm opens.
    await chooseFromMenu(screen.getAllByTestId('library-more')[0], 'library-remove')
    fireEvent.click(await screen.findByTestId('library-remove-confirm-action'))
    await screen.findByTestId('library-remove-error')

    // Now open B's confirm. A's failure must still be reported on A's card.
    await chooseFromMenu(screen.getAllByTestId('library-more')[1], 'library-remove')
    expect(await screen.findByTestId('library-remove-confirm')).toHaveTextContent('artifacts/beta/')
    expect(within(screen.getAllByTestId('library-card')[0]).getByTestId('library-remove-error'))
      .toBeTruthy()
    expect(within(screen.getAllByTestId('library-card')[1]).queryByTestId('library-remove-error'))
      .toBeNull()

    // And backing out of B retires B's attempt, not A's failure. Cancel is the
    // one place a failure is deliberately retired, so it has to be as narrowly
    // owned as the clear on open.
    fireEvent.click(screen.getByTestId('library-remove-confirm-cancel'))
    await waitFor(() => expect(screen.queryByTestId('library-remove-confirm')).toBeNull())
    expect(within(screen.getAllByTestId('library-card')[0]).getByTestId('library-remove-error'))
      .toBeTruthy()
  })

  /**
   * A failed removal reports, and backing out retires it.
   *
   * The strip stays open on failure so a retry is one click, the failure renders
   * on the card that owns it, and Cancel retires that card's failure with the
   * attempt it describes -- otherwise a reader who backs out keeps a standing red
   * beside a copy that is still there.
   *
   * This replaces the old per-card re-arm test wholesale. That test guarded a
   * confirm flag surviving on a card that outlived its own sync state; holding
   * the state on the SECTION removes the flag, so there is nothing left to
   * re-arm and the property is now structural rather than asserted.
   */
  it('reports a failed removal on its card and retires it on cancel', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: ['notes', 'other'] })
    vi.mocked(awsControlApi.library).mockResolvedValue({ artifacts: [] })
    vi.mocked(awsControlApi.libraryRemove).mockRejectedValue(new Error('boom'))

    await renderDrive('library')
    expect(await screen.findAllByTestId('library-card')).toHaveLength(2)

    await chooseFromMenu(screen.getAllByTestId('library-more')[0], 'library-remove')
    fireEvent.click(await screen.findByTestId('library-remove-confirm-action'))

    // The failure renders on the card, and the strip is still there to retry.
    expect(await screen.findByTestId('library-remove-error')).toHaveTextContent(
      i18nT('apps.awsControl.console.library_remove_failed'))
    expect(screen.getByTestId('library-remove-confirm')).toBeTruthy()

    // Cancel retires this card's failure along with the attempt it describes.
    fireEvent.click(screen.getByTestId('library-remove-confirm-cancel'))
    await waitFor(() => expect(screen.queryByTestId('library-remove-error')).toBeNull())

    // And the OTHER card's confirm opens clean.
    await chooseFromMenu(screen.getAllByTestId('library-more')[1], 'library-remove')
    expect(await screen.findByTestId('library-remove-confirm')).toBeTruthy()
    expect(screen.queryByTestId('library-remove-error')).toBeNull()
  })

  it('the picker searches by name and reports when nothing matches', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: [] })
    vi.mocked(awsControlApi.library).mockResolvedValue({
      artifacts: [
        { slug: 'notes', name: 'Notes', kind: 'markdown', version: 1, updatedAt: '2026-08-20T00:00:00Z', pushedVersion: null, pushedAt: null },
      ],
    })

    await renderDrive('library')
    fireEvent.click(await screen.findByTestId('library-add-open'))
    await screen.findByTestId('library-tile')

    fireEvent.change(await screen.findByTestId('library-add-search'), { target: { value: 'zzz' } })
    expect(screen.queryByTestId('library-tile')).toBeNull()
    expect(screen.getByTestId('library-add-none')).toBeTruthy()
  })
})

describe('DrivePage sections: capability parity, honest counts, no self-contradiction', () => {
  /**
   * Grid mode is a way of LOOKING at a folder, not a capability tier.
   *
   * The first cut gave grid cards only a Download link, so a reader who
   * preferred tiles lost Share and Delete -- and because the choice persists per
   * section, they lost them on every future visit with nothing to tell them the
   * controls existed.
   */
  it('grid mode carries the same actions the table rows carry', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'a.txt', size: 10, modified: '2026-08-20T00:00:00Z' }],
      folders: ['invoices'],
    })

    await renderDrive('drive')
    fireEvent.click(await screen.findByTitle('Grid view'))
    await screen.findByTestId('drive-grid')

    // ONE overflow per file tile, holding every per-item action. Download used to
    // sit outside it as a bare button while Share and Delete were inside, so the
    // assertion is now that there is no second control: a card must offer one
    // grammar for "act on this item", not two.
    expect(screen.getByTestId('drive-grid-more')).toBeTruthy()
    fireEvent.keyDown(screen.getByTestId('drive-grid-more'), { key: 'Enter' })
    expect(await screen.findByTestId('drive-grid-download')).toBeTruthy()
    expect(screen.getByTestId('drive-grid-share')).toBeTruthy()
    expect(screen.getByTestId('drive-grid-delete')).toBeTruthy()
    // A folder tile is no longer action-less.
    expect(screen.getByTestId('drive-grid-folder-more')).toBeTruthy()
  })

  /** A Delete offered in grid mode must actually be able to complete. */
  it('grid mode renders its own delete confirmation', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'a.txt', size: 10, modified: '2026-08-20T00:00:00Z' }],
      folders: [],
    })
    vi.mocked(awsControlApi.driveDelete).mockResolvedValue({ deleted: true } as never)

    await renderDrive('drive')
    fireEvent.click(await screen.findByTitle('Grid view'))
    await screen.findByTestId('drive-grid')

    // The confirm strips live inside the TABLE, so grid mode needs its own or the
    // action sets state and nothing ever renders.
    // Enter on the trigger, matching how the row-overflow tests above open it:
    // the menu is a portaled Radix dropdown, which a bare click does not open.
    fireEvent.keyDown(screen.getByTestId('drive-grid-more'), { key: 'Enter' })
    fireEvent.click(await screen.findByTestId('drive-grid-delete'))
    expect(await screen.findByTestId('drive-grid-confirm')).toBeTruthy()
    fireEvent.click(screen.getByTestId('drive-grid-confirm-action'))
    await waitFor(() => expect(awsControlApi.driveDelete).toHaveBeenCalledWith(ACCOUNT_ID, 'drive', 'a.txt'))
  })

  /**
   * The empty state's primary button counts what can be added, not what exists.
   *
   * Images cannot be pushed and are the bulk of a real library, so counting them
   * made the first-run CTA promise work the picker then refuses.
   */
  it('the ready-to-add count excludes artifacts that cannot be added', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: [] })
    vi.mocked(awsControlApi.library).mockResolvedValue({
      artifacts: [
        { slug: 'notes', name: 'Notes', kind: 'markdown', version: 1, updatedAt: '2026-08-20T00:00:00Z', pushedVersion: null, pushedAt: null },
        { slug: 'pic', name: 'Pic', kind: 'image', version: 1, updatedAt: '2026-08-20T00:00:00Z', pushedVersion: null, pushedAt: null },
        { slug: 'pic2', name: 'Pic2', kind: 'image', version: 1, updatedAt: '2026-08-20T00:00:00Z', pushedVersion: null, pushedAt: null },
      ],
    })

    await renderDrive('library')
    const cta = await screen.findByTestId('library-empty-add')
    // One addable markdown, not three artifacts.
    expect(cta.textContent).toContain('1')
    expect(cta.textContent).not.toContain('3')
  })

  /** "In the cloud only" and "In the drive" on one card are two answers to one question. */
  it('a cloud-only card does not also claim to be in the drive', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: ['orphan'] })
    vi.mocked(awsControlApi.library).mockResolvedValue({ artifacts: [] })

    await renderDrive('library')
    const card = await screen.findByTestId('library-card')

    expect(card.textContent).toMatch(/only/i)
    // The footer that would have said "In the drive" is suppressed, and the slug
    // is not printed twice when it is already serving as the name.
    expect(card.textContent).not.toMatch(/In the drive/i)
    expect(card.textContent!.match(/orphan/g)).toHaveLength(1)
  })

  it('the picker closes on Escape, not only on its X', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: [] })

    await renderDrive('library')
    fireEvent.click(await screen.findByTestId('library-add-open'))
    expect(await screen.findByTestId('library-add-dialog')).toBeTruthy()

    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('library-add-dialog')).toBeNull())
  })

  /** "Load more" must ADD to what is on screen, not replace it. */
  it('load more appends the next page instead of swapping it', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.library).mockResolvedValue({ artifacts: [] })
    vi.mocked(awsControlApi.driveList)
      .mockResolvedValueOnce({ files: [], folders: ['first'], nextToken: 'tok' })
      .mockResolvedValueOnce({ files: [], folders: ['second'] })

    await renderDrive('library')
    expect(await screen.findAllByTestId('library-card')).toHaveLength(1)

    fireEvent.click(screen.getByTestId('library-load-more'))
    // Both pages are on screen -- the first did not vanish.
    await waitFor(() => expect(screen.getAllByTestId('library-card')).toHaveLength(2))
    const text = screen.getByTestId('library-grid').textContent!
    expect(text).toContain('first')
    expect(text).toContain('second')
  })

  /**
   * Every kind the backend can hand us must render a label, not a blank badge.
   *
   * `list_pushable` returns `artifact.kind` VERBATIM from the store's
   * ALLOWED_KINDS, with no filtering, and `_KIND_EXT` makes svg and text
   * pushable too -- so a union that listed only six kinds did not make the other
   * two unreachable, it just stopped the compiler from noticing their badge
   * resolved to `undefined`. A QA capture caught an SVG artifact rendering an
   * empty badge in the Library list. This is the ratchet: the list below is the
   * backend's set, so adding a kind there without a label here fails HERE.
   */
  it('renders a label for every artifact kind the backend can return', async () => {
    const BACKEND_KINDS = ['widget', 'html', 'markdown', 'svg', 'json', 'text', 'image', 'webapp'] as const
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: [] })
    vi.mocked(awsControlApi.library).mockResolvedValue({
      artifacts: BACKEND_KINDS.map((kind, i) => ({
        slug: `a${i}`, name: `A${i}`, kind, version: 1,
        updatedAt: '2026-08-20T00:00:00Z', pushedVersion: null, pushedAt: null,
      })),
    })

    await renderDrive('library')
    fireEvent.click(await screen.findByTestId('library-add-open'))
    await screen.findByTestId('library-add-dialog')

    // One chip per kind, and not one of them is blank.
    for (const kind of BACKEND_KINDS) {
      const chip = screen.getByTestId(`library-chip-${kind}`)
      // The chip carries a label plus its count; strip the digits and require
      // something legible is left.
      expect(chip.textContent!.replace(/\d+/g, '').trim(), `${kind} chip label`).not.toBe('')
    }
    // ...and every card's kind badge resolved too.
    const tiles = await screen.findAllByTestId('library-tile')
    expect(tiles).toHaveLength(BACKEND_KINDS.length)
  })

  /**
   * "Nothing left to add" is a FALSE claim for a library that is all images.
   *
   * Nothing is here and nothing was ever added -- and because the button is
   * hidden at a zero count, the picker's per-image explanation is unreachable, so
   * this one sentence is everything that cohort ever sees. Images are the bulk of
   * a real library, so this is the common case rather than a corner.
   */
  it('does not claim everything is added when everything left is an image', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: [] })
    vi.mocked(awsControlApi.library).mockResolvedValue({
      artifacts: [
        { slug: 'pic', name: 'Pic', kind: 'image', version: 1, updatedAt: '2026-08-20T00:00:00Z', pushedVersion: null, pushedAt: null },
        { slug: 'pic2', name: 'Pic2', kind: 'image', version: 1, updatedAt: '2026-08-20T00:00:00Z', pushedVersion: null, pushedAt: null },
      ],
    })

    await renderDrive('library')
    const msg = await screen.findByTestId('library-empty-none')
    // Says WHY nothing can be added, not that it already was.
    expect(msg.textContent).toMatch(/image/i)
    expect(msg.textContent).not.toMatch(/already/i)
  })

  /** ...but a genuinely fully-synced library should still say so. */
  it('does say everything is added when the library really is all up there', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: [] })
    vi.mocked(awsControlApi.library).mockResolvedValue({
      artifacts: [
        { slug: 'notes', name: 'Notes', kind: 'markdown', version: 1, updatedAt: '2026-08-20T00:00:00Z', pushedVersion: 1, pushedAt: '2026-08-21T00:00:00Z' },
      ],
    })

    await renderDrive('library')
    const msg = await screen.findByTestId('library-empty-none')
    expect(msg.textContent).not.toMatch(/image/i)
  })

  /** A failed listing is not an empty folder -- the one conclusion we cannot draw. */
  it('shows a retryable error instead of an empty folder when the listing fails', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockRejectedValue(new Error('list failed'))

    await renderDrive('library')
    expect(await screen.findByTestId('library-error')).toBeTruthy()
    expect(screen.getByTestId('library-retry')).toBeTruthy()
    // ...and it must NOT also claim the folder is empty.
    expect(screen.queryByTestId('library-empty')).toBeNull()
  })

  /**
   * The empty state asserts things about the LOCAL library, so it must not render
   * off an unanswered query -- the same mistake as reading orphan-hood out of an
   * empty map, in a second place.
   */
  it('makes no claim about what is left to add when the local lookup failed', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: [] })
    vi.mocked(awsControlApi.library).mockRejectedValue(new Error('lookup failed'))

    await renderDrive('library')
    // The empty folder itself is known and shown...
    expect(await screen.findByTestId('library-empty')).toBeTruthy()
    // ...but neither the count button nor the "nothing left" claim appears.
    expect(screen.queryByTestId('library-empty-add')).toBeNull()
    expect(screen.queryByTestId('library-empty-none')).toBeNull()
  })

  /**
   * A same-version push must stay AVAILABLE.
   *
   * The ledger is local, so a cloud object deleted outside this app -- the S3
   * console, a lifecycle rule, another machine -- leaves it still claiming the
   * version matches. Disabling the button on "up to date" then removed the only
   * way to restore the copy, and this page makes the contradiction visible: the
   * Library folder lists the real prefix, so it shows the object gone while the
   * picker insisted it was up to date.
   */
  it('lets an up-to-date artifact be pushed again, to restore a lost cloud copy', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: [] })
    vi.mocked(awsControlApi.library).mockResolvedValue({
      artifacts: [
        // Ledger says v2 is up there; the cloud listing says otherwise.
        { slug: 'notes', name: 'Notes', kind: 'markdown', version: 2, updatedAt: '2026-08-20T00:00:00Z', pushedVersion: 2, pushedAt: '2026-08-21T00:00:00Z' },
      ],
    })
    vi.mocked(awsControlApi.libraryPush).mockResolvedValue({ pushed: true, slug: 'notes', version: 2 } as never)

    await renderDrive('library')
    fireEvent.click(await screen.findByTestId('library-add-open'))

    const push = await screen.findByTestId('library-push')
    // The state is still stated...
    expect(screen.getByTestId('library-already')).toBeTruthy()
    // ...but it is a label, not a locked door.
    expect((push as HTMLButtonElement).disabled).toBe(false)
    fireEvent.click(push)
    await waitFor(() => expect(awsControlApi.libraryPush).toHaveBeenCalledWith(ACCOUNT_ID, 'notes'))
  })

  /**
   * The WHOLE grid folder tile navigates, not just its name.
   *
   * The tile carries a hover affordance, so a click on its icon or its body being
   * a silent no-op is the same defect the table rows already fix.
   */
  it('opens a grid folder from anywhere on the tile, not just the name', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: ['invoices'] })

    await renderDrive('drive')
    fireEvent.click(await screen.findByTitle('Grid view'))
    const tile = await screen.findByTestId('drive-grid-folder')

    // Click the TILE (not a name button -- there isn't one any more).
    fireEvent.click(tile)
    await waitFor(() =>
      expect(awsControlApi.driveList).toHaveBeenCalledWith(ACCOUNT_ID, 'drive', 'invoices', ''),
    )
  })

  /** ...and its overflow must not navigate on the way to the menu. */
  it('the grid folder overflow does not also open the folder', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: ['invoices'] })

    await renderDrive('drive')
    fireEvent.click(await screen.findByTitle('Grid view'))
    await screen.findByTestId('drive-grid-folder')
    vi.mocked(awsControlApi.driveList).mockClear()

    fireEvent.click(screen.getByTestId('drive-grid-folder-more'))
    expect(awsControlApi.driveList).not.toHaveBeenCalled()
  })

  /**
   * The tile must not hijack a nested control's keyboard activation.
   *
   * Enter and Space bubble, so the tile's own handler fired for the overflow
   * trigger and the confirm's buttons -- opening the folder the reader was acting
   * WITHIN, and worse, its `preventDefault` cancelled that control's activation,
   * so the menu and the confirm answered no keyboard at all. Stopping propagation
   * on the trigger's onClick only ever covered the pointer path.
   */
  it('does not open the folder when a nested control is keyboard-activated', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: ['invoices'] })

    await renderDrive('drive')
    fireEvent.click(await screen.findByTitle('Grid view'))
    await screen.findByTestId('drive-grid-folder')
    vi.mocked(awsControlApi.driveList).mockClear()

    // Enter on the overflow trigger opens the MENU, and must not navigate.
    fireEvent.keyDown(screen.getByTestId('drive-grid-folder-more'), { key: 'Enter' })
    expect(await screen.findByTestId('drive-grid-folder-delete')).toBeTruthy()
    expect(awsControlApi.driveList).not.toHaveBeenCalled()

    // ...and the confirm's own buttons stay usable from the keyboard.
    fireEvent.click(screen.getByTestId('drive-grid-folder-delete'))
    const cancel = await screen.findByTestId('drive-grid-confirm-cancel')
    fireEvent.keyDown(cancel, { key: ' ' })
    expect(awsControlApi.driveList).not.toHaveBeenCalled()
  })
})

describe('DrivePage sections: Library integrity and view modes', () => {
  /**
   * A failed add must not be swallowed by a later one.
   *
   * Add state used to be read off the single mutation (`variables === slug`), so
   * starting a second add reassigned it: the first card dropped its "Adding…"
   * and, when it failed, the error rendered on whichever card was clicked last.
   * The first card silently went back to "Add to Drive" and the reader closed
   * the dialog believing both had copied. Bulk-adding is this dialog's whole
   * job, so this was the common path.
   */
  it('keeps each add failure on its own card', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: [] })
    vi.mocked(awsControlApi.library).mockResolvedValue({
      artifacts: [
        { slug: 'alpha', name: 'Alpha', kind: 'markdown', version: 1, updatedAt: '2026-08-20T00:00:00Z', pushedVersion: null, pushedAt: null },
        { slug: 'beta', name: 'Beta', kind: 'markdown', version: 1, updatedAt: '2026-08-20T00:00:00Z', pushedVersion: null, pushedAt: null },
      ],
    })
    // alpha stays in flight until we reject it; beta resolves at once.
    let failAlpha: (e: Error) => void = () => {}
    vi.mocked(awsControlApi.libraryPush).mockImplementation((_acct: string, slug: string) =>
      slug === 'alpha'
        ? new Promise((_res, rej) => { failAlpha = rej })
        : Promise.resolve({ pushed: true, slug, version: 1 }),
    )

    await renderDrive('library')
    fireEvent.click(await screen.findByTestId('library-add-open'))
    await screen.findByTestId('library-add-dialog')
    const tiles = await screen.findAllByTestId('library-tile')
    expect(tiles).toHaveLength(2)

    fireEvent.click(within(tiles[0]).getByTestId('library-push'))
    expect(within(tiles[0]).getByText(i18nT('apps.awsControl.console.library_adding'))).toBeTruthy()

    // A second add while the first is in flight must not steal the first's state.
    fireEvent.click(within(tiles[1]).getByTestId('library-push'))
    expect(within(tiles[0]).getByText(i18nT('apps.awsControl.console.library_adding'))).toBeTruthy()

    failAlpha(new Error('nope'))
    // The failure belongs to alpha, and beta must not be wearing it.
    const failed = i18nT('apps.awsControl.console.library_push_failed')
    expect(await within(tiles[0]).findByText(failed)).toBeTruthy()
    expect(within(tiles[1]).queryByText(failed)).toBeNull()
  })

  /**
   * A Library card is a route to the artifact, not a dead end -- but only when a
   * local copy backs it. A cloud-only card has no artifact page to open.
   */
  it('links a Library card to its artifact, and leaves an orphan inert', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: ['notes', 'ghost'] })
    vi.mocked(awsControlApi.library).mockResolvedValue({
      artifacts: [
        { slug: 'notes', name: 'Notes', kind: 'markdown', version: 3, updatedAt: '2026-08-20T00:00:00Z', pushedVersion: 3, pushedAt: '2026-08-21T00:00:00Z' },
      ],
    })

    await renderDrive('library')
    const cards = await screen.findAllByTestId('library-card')
    expect(cards).toHaveLength(2)

    // The card SHELL is a plain container and the link is inside it, which is
    // what lets the overflow trigger be a real button: interactive content inside
    // an anchor is invalid, and a trigger that had to `preventDefault` its way
    // out of the surrounding navigation is the nested-control hijack this page
    // avoids. So these assert the link WITHIN a card, and the last one pins the
    // trigger out of it.
    const linked = cards.filter((c) => c.querySelector('a') !== null)
    expect(linked).toHaveLength(1)
    const anchor = linked[0].querySelector('a') as HTMLAnchorElement
    expect(anchor.getAttribute('href')).toBe('/artifacts/notes')
    expect(anchor.getAttribute('aria-label')).toContain('Notes')
    // The orphan stays inert: no link to a page that does not exist.
    expect(cards.filter((c) => c.querySelector('a') === null)).toHaveLength(1)
    // Both cards offer the actions menu, and on neither is its trigger inside
    // the link -- which is what lets it be a real button at all.
    expect(screen.getAllByTestId('library-more')).toHaveLength(2)
    expect(anchor.querySelector('[data-testid="library-more"]')).toBeNull()
  })

  /**
   * A failed read of the LOCAL library must not render as permanent silence.
   *
   * The cloud listing and the local lookup are two queries joined by slug. The
   * cloud side had an error card from the start; the local side had nothing, so
   * on failure the cards' placeholders never resolved, the empty state's action
   * never appeared, and the picker's body rendered blank -- each of which reads
   * as "there is nothing here", the one conclusion a failure cannot support.
   * The cards themselves must STAY: the folder's contents are known, only their
   * names and previews are not.
   */
  it('says so when the local library cannot be read, in the folder and in the picker', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: ['notes'] })
    vi.mocked(awsControlApi.library).mockRejectedValue(new Error('boom'))

    await renderDrive('library')

    // The folder says the lookup failed, and offers a way to try again...
    const notice = await screen.findByTestId('library-local-error')
    expect(notice.textContent).toContain(i18nT('apps.awsControl.console.library_local_failed'))
    expect(screen.getByTestId('library-local-retry')).toBeTruthy()
    // ...while still listing what the cloud said is in there.
    expect(screen.getAllByTestId('library-card')).toHaveLength(1)
    // And it must NOT claim the object is cloud-only off a failed lookup.
    expect(screen.queryByText(i18nT('apps.awsControl.console.library_cloud_only'))).toBeNull()

    // Same root cause, second surface: the picker body is not left blank.
    fireEvent.click(screen.getByTestId('library-add-open'))
    expect(await screen.findByTestId('library-add-error')).toBeTruthy()
    expect(screen.getByTestId('library-add-retry')).toBeTruthy()
    /* And it must not assert COUNTS either. The kind chips read from an
       empty-array fallback, so a failed read rendered "All 0 | Widget 0 |
       Markdown 0..." above "Could not read your artifacts" -- a confident
       zero-count library built on nothing, the one answer a failure cannot
       support. */
    expect(screen.queryByTestId('library-chips')).toBeNull()
  })

  /**
   * The list is a VIEW, not a lesser tier.
   *
   * Grid cards link to the artifact and carry the stale-version warning; rows
   * carried neither, and because the view choice persists per section, a reader
   * who once switched to the list silently never saw that warning again -- the
   * one disclosure the card's own comment calls its whole contract, since the
   * preview is drawn from the LOCAL copy and may not be what the bucket holds.
   */
  it('gives Library list rows the same link and stale warning as the cards', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: ['notes', 'ghost'] })
    vi.mocked(awsControlApi.library).mockResolvedValue({
      artifacts: [
        // Pushed at v2 but locally now v5: the stale-preview case.
        { slug: 'notes', name: 'Notes', kind: 'markdown', version: 5, updatedAt: '2026-08-20T00:00:00Z', pushedVersion: 2, pushedAt: '2026-08-21T00:00:00Z' },
      ],
    })

    await renderDrive('library')
    fireEvent.click(await screen.findByTitle('List view'))
    const rows = await screen.findAllByTestId('library-list-row')
    expect(rows).toHaveLength(2)

    const linked = rows.filter((r) => r.tagName === 'A')
    expect(linked).toHaveLength(1)
    expect(linked[0].getAttribute('href')).toBe('/artifacts/notes')
    // The stale-version disclosure must survive the view switch, worded for a
    // row (no preview to be wrong about) rather than for a card.
    const staleEl = within(linked[0]).getByTestId('library-list-stale')
    expect(staleEl.textContent)
      .toBe(i18nT('apps.awsControl.console.library_stale_list', { version: 2 }))
    /* Narrow viewports: jsdom has no layout engine, so this pins the MECHANISM
       that keeps a 320px row inside the viewport rather than measuring it. This
       is the longest string in the row; with `shrink-0` it pushed itself and the
       kind badge past the edge. It must be able to shrink, the row must be able
       to wrap, and it must NOT truncate -- an ellipsised warning is one the
       reader cannot read. */
    expect(staleEl.className).not.toContain('shrink-0')
    expect(staleEl.className).not.toContain('truncate')
    expect(linked[0].className).toContain('flex-wrap')
    // The grid's preview-specific wording must NOT leak into a row.
    expect(screen.queryByText(i18nT('apps.awsControl.console.library_stale_preview', { version: 2 }))).toBeNull()
    // The cloud-only row has no artifact page, so it stays inert.
    expect(rows.filter((r) => r.tagName === 'DIV')).toHaveLength(1)
  })

  /**
   * The cost disclosure must reach the person doing the adding.
   *
   * Adding fills storage the account pays for, and the empty state's button
   * opens this dialog directly, so a first-time user never passes the folder's
   * own copy. A warning on a path the reader never takes is not a warning.
   *
   * COST only: the picker's cards carry a Remove control, so a banner claiming
   * removal is unavailable would be disproved by a button in the same dialog.
   */
  it('states the storage cost inside the picker', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: [] })
    vi.mocked(awsControlApi.library).mockResolvedValue({
      artifacts: [
        { slug: 'notes', name: 'Notes', kind: 'markdown', version: 1, updatedAt: '2026-08-20T00:00:00Z', pushedVersion: null, pushedAt: null },
      ],
    })

    await renderDrive('library')
    // Open from the EMPTY STATE button, the path that bypasses the folder blurb.
    fireEvent.click(await screen.findByTestId('library-empty-add'))
    const dialog = await screen.findByTestId('library-add-dialog')
    const banner = within(dialog).getByTestId('library-add-oneway')
    expect(banner.textContent).toBe(i18nT('apps.awsControl.console.library_add_oneway'))
    // Pinned as a SUBSTRING guard rather than only as an equality, so it keeps
    // biting through a reword: what this banner must never say again is that
    // removal is unavailable, because Remove sits one card below it in the very
    // same overlay.
    expect(banner.textContent).not.toMatch(/remov/i)
  })

  /**
   * A brand-new install must not be told its cloud is already up to date.
   *
   * With no local artifacts at all, `pushable.length === 0` and `onlyUnaddable`
   * is false, so the empty state rendered "Nothing left to add -- everything
   * that can be is already here" directly under "Nothing copied to the cloud
   * yet". That contradicts itself AND is false, on the first screen a fresh
   * install sees. There is no true sentence to add that the empty state's body
   * does not already say, so this case says nothing.
   */
  it('claims nothing about the cloud when the local library is empty', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: [] })
    vi.mocked(awsControlApi.library).mockResolvedValue({ artifacts: [] })

    await renderDrive('library')
    expect(await screen.findByTestId('library-empty')).toBeTruthy()
    // Neither the false "already here" claim nor an unaddable-kinds explanation.
    expect(screen.queryByTestId('library-empty-none')).toBeNull()
    // And no count button, since there is nothing to count.
    expect(screen.queryByTestId('library-empty-add')).toBeNull()
    // The blurb is suppressed too: the empty body already states the same facts.
    expect(screen.queryByTestId('library-blurb')).toBeNull()
  })

  /**
   * A slug match is not identity.
   *
   * Slugs derive from names, so they collide across machines. A locally created,
   * NEVER-pushed artifact sharing a slug with an object someone else pushed used
   * to lend this card its name, kind and preview, under a footer asserting the
   * object is in the cloud -- a confident wrong answer from a card whose entire
   * contract is that it shows what IS up there. The stale-version warning could
   * not catch it either: that needs a recorded push to compare against.
   */
  it('will not dress a cloud object in a coincidentally matching local artifact', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: ['notes'] })
    vi.mocked(awsControlApi.library).mockResolvedValue({
      artifacts: [
        // Same slug as the cloud object, but THIS machine never pushed it.
        { slug: 'notes', name: 'My Unrelated Local Notes', kind: 'markdown', version: 1, updatedAt: '2026-08-20T00:00:00Z', pushedVersion: null, pushedAt: null },
      ],
    })

    await renderDrive('library')
    const card = await screen.findByTestId('library-card')
    // Not the local artifact's name...
    expect(card.textContent).not.toContain('My Unrelated Local Notes')
    // ...nor its kind, which only renders when local identity is trusted.
    // (Not asserted via library_in_cloud: "In the cloud only" contains it as a
    // substring, so that check would pass or fail for the wrong reason.)
    expect(card.textContent).not.toContain(i18nT('apps.awsControl.console.kind_markdown'))
    // It is the honest thing instead: a cloud object we cannot vouch for.
    expect(card.textContent).toContain(i18nT('apps.awsControl.console.library_cloud_only'))
    // The slug stands in for the name we do not have.
    expect(card.textContent).toContain('notes')
    // And inert, so it cannot link to a local artifact that may be unrelated.
    expect(card.tagName).toBe('DIV')
  })

  /** "(0 ready)" on a button opening a picker that refuses everything. */
  it('says nothing is left to add rather than offering a zero count', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: [] })
    vi.mocked(awsControlApi.library).mockResolvedValue({
      artifacts: [
        // Only an image: present, but not addable.
        { slug: 'pic', name: 'Pic', kind: 'image', version: 1, updatedAt: '2026-08-20T00:00:00Z', pushedVersion: null, pushedAt: null },
      ],
    })

    await renderDrive('library')
    expect(await screen.findByTestId('library-empty-none')).toBeTruthy()
    expect(screen.queryByTestId('library-empty-add')).toBeNull()
  })

  it('remembers the view mode per section, so Files opens as a table', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'a.txt', size: 10, modified: '2026-08-20T00:00:00Z' }],
      folders: [],
    })

    await renderDrive('drive')

    // Files holds arbitrary uploads with comparable columns, so it opens listed.
    expect(await screen.findByTestId('drive-listing')).toBeTruthy()
    expect(screen.queryByTestId('drive-grid')).toBeNull()
    // ...and the toggle switches it to tiles. The control is the shared
    // SegmentedControl (the same one the Artifacts gallery uses for grid/table),
    // so the segment is addressed by its accessible name rather than a testid of
    // its own.
    fireEvent.click(screen.getByTitle('Grid view'))
    expect(await screen.findByTestId('drive-grid')).toBeTruthy()
  })

  /* ── Share: error branch, and cancel via the close button ────────────────── */

  it('leaves the share dialog on its form when share creation fails', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'report.pdf', size: 2048, modified: '2026-08-20T00:00:00Z' }],
      folders: [],
    })
    vi.mocked(awsControlApi.driveShare).mockRejectedValue(new Error('AccessDenied'))

    await renderDrive('drive')

    fireEvent.keyDown(await screen.findByTestId('drive-more'), { key: 'Enter' })
    fireEvent.click(await screen.findByTestId('drive-share'))
    // Pick a non-default expiry and type a note before creating.
    fireEvent.click(await screen.findByTestId('share-expiry-7d'))
    fireEvent.change(screen.getByTestId('share-note'), { target: { value: 'quarterly' } })
    fireEvent.click(screen.getByTestId('share-create'))

    // The api was called with the chosen expiry seconds + note; on failure the
    // dialog keeps its form (no result panel) so the owner can retry.
    await waitFor(() =>
      expect(awsControlApi.driveShare).toHaveBeenCalledWith(ACCOUNT_ID, 'drive', 'report.pdf', 604800, 'quarterly'),
    )
    expect(screen.queryByTestId('share-result')).toBeNull()
    // The close button dismisses the dialog entirely.
    fireEvent.click(screen.getByTestId('share-close'))
    await waitFor(() => expect(screen.queryByTestId('share-dialog')).toBeNull())
  })
})

describe('DrivePage sections: backup, access, CLI drawer', () => {
  it('runs a backup, toggles nightly, and both invalidate through the api', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.backup).mockResolvedValue({
      nightly: false,
      runs: { snapshot: { key: 'snap-1', bytes: 1024, at: '2026-08-24T00:00:00Z' } },
      remote: { snapshot: [], sessions: [] },
    })
    vi.mocked(awsControlApi.backupRun).mockResolvedValue({
      started: true, kind: 'snapshot', runId: 'a'.repeat(32),
    })
    vi.mocked(awsControlApi.backupNightly).mockResolvedValue({ nightly: true } as never)

    await renderDrive('backup')

    // The snapshot row shows its last-run line, then Run now calls the api.
    await screen.findByTestId('backup-row-snapshot')
    fireEvent.click(screen.getByTestId('backup-run-snapshot'))
    await waitFor(() => expect(awsControlApi.backupRun).toHaveBeenCalledWith(ACCOUNT_ID, 'snapshot'))

    // The sessions row carries its extra scope caveat.
    expect(screen.getByTestId('backup-sessions-scope')).toBeTruthy()

    // Flipping the nightly toggle calls the api with the new value.
    const toggle = within(screen.getByTestId('backup-nightly')).getByRole('switch')
    fireEvent.click(toggle)
    await waitFor(() => expect(awsControlApi.backupNightly).toHaveBeenCalledWith(ACCOUNT_ID, true))
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

    await renderDrive('backup')

    // The archive is behind a disclosure; before opening, no rows are shown.
    fireEvent.click(await screen.findByTestId('backup-remote-toggle'))
    const archive = await screen.findByTestId('backup-archive')
    expect(within(archive).getByTestId('backup-archive-row')).toHaveTextContent('2026-08-24.tar')
    // The write-only-tier restore caveat renders alongside.
    expect(screen.getByTestId('backup-restore-caveat')).toBeTruthy()

    // Restore stages the archive locally and echoes the landed path.
    fireEvent.click(within(archive).getByTestId('backup-restore'))
    await waitFor(() => expect(awsControlApi.backupRestore).toHaveBeenCalledWith(ACCOUNT_ID, 'backup/snapshot/2026-08-24.tar'))
    expect(await screen.findByTestId('backup-restored')).toHaveTextContent('/home/u/.kiro/restore/2026-08-24')
  })

  it('shows the backup remote-error note when the archive could not be read', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.backup).mockResolvedValue({
      nightly: false, runs: {}, remote: null, remoteError: 'AccessDenied',
    })

    await renderDrive('backup')

    // The note still renders. The disclosure NO LONGER hides, and that change is
    // deliberate: since the remote half became opt-in behind `?remote=1`, a
    // `remoteError` can only exist because this control already requested it, so
    // "errored AND control hidden" is unreachable in the running app. Asserting
    // the old invariant is what produced the regression -- the control that
    // enables the fetch rendered only once the fetch had succeeded, leaving the
    // archive and Restore unreachable. Keeping it visible also lets the owner
    // collapse and retry instead of being stuck with the note.
    expect(await screen.findByTestId('backup-remote-error')).toBeTruthy()
    expect(screen.queryByTestId('backup-remote-toggle')).not.toBeNull()
  })

  /* ── Access: forget a share ──────────────────────────────────────────────── */

  it('forgets a share through the api', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.shares).mockResolvedValue({
      shares: [{
        id: 's1', account: ACCOUNT_ID, section: 'library', key: 'w/notes',
        createdAt: '2026-08-24T05:00:00Z', expiresAt: '2030-01-01T00:00:00Z', note: '',
      }],
    })
    vi.mocked(awsControlApi.shareForget).mockResolvedValue({ forgotten: true } as never)

    await renderDrive('access')

    fireEvent.click(await screen.findByTestId('access-forget'))
    await waitFor(() => expect(awsControlApi.shareForget).toHaveBeenCalledWith('s1'))
  })

  /* ── CLI drawer disclosure ───────────────────────────────────────────────── */

  it('reveals a copyable CLI line in the library section drawer', async () => {
    stubDrivePresent()
    await renderDrive('library')

    // The drawer is collapsed by default; opening it shows the aws s3 ls line
    // scoped to the artifacts/ prefix of the account's bucket.
    fireEvent.click(await screen.findByTestId('cli-drawer-toggle'))
    const body = await screen.findByTestId('cli-drawer-body')
    expect(body).toHaveTextContent('aws s3 ls s3://kirocrew-drive-abc123/artifacts/')
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

    await renderDrive('drive')
    // Download moved into the row's overflow, so the gesture now starts from the
    // menu item. It still survives: Radix dispatches onSelect synchronously from
    // the item's own click handler, so window.open keeps the user gesture.
    await chooseFromMenu(await screen.findByTestId('drive-more'), 'drive-download')

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

describe('StorageMeter', () => {
  /**
   * The meter is now an exported unit rendered by the usage pane, so its
   * semantics are pinned here directly: a section that exists gets a legend
   * row even at zero bytes (a 0-width segment alone would silently drop it),
   * and only non-zero sections paint a bar segment.
   */
  it('renders a legend row for every section, including a zero-byte one', () => {
    const usage: DriveUsage = {
      bytes: 3_001_000_000,
      objects: 40,
      sections: {
        library: { objects: 10, bytes: 1_000_000 },
        drive: { objects: 30, bytes: 3_000_000_000 },
        backup: { objects: 0, bytes: 0 },
      },
    }
    renderWithProviders(<StorageMeter usage={usage} />)

    // Every section is named in the legend, with its own size...
    expect(screen.getByTestId('drive-meter-legend-drive').textContent).toContain(fmtBytes(3_000_000_000))
    expect(screen.getByTestId('drive-meter-legend-library').textContent).toContain(fmtBytes(1_000_000))
    // ...and the empty section keeps its row rather than vanishing.
    expect(screen.getByTestId('drive-meter-legend-backup').textContent).toContain(fmtBytes(0))
    // But only sections WITH bytes paint a segment in the bar.
    expect(screen.getByTestId('drive-meter-segment-drive')).toBeTruthy()
    expect(screen.getByTestId('drive-meter-segment-library')).toBeTruthy()
    expect(screen.queryByTestId('drive-meter-segment-backup')).toBeNull()
  })

  it('renders a single muted track (no segments) when the drive is empty', () => {
    const empty: DriveUsage = {
      bytes: 0,
      objects: 0,
      sections: {
        library: { objects: 0, bytes: 0 },
        drive: { objects: 0, bytes: 0 },
        backup: { objects: 0, bytes: 0 },
      },
    }
    renderWithProviders(<StorageMeter usage={empty} />)

    // The bar itself renders (never a bare outline), holding zero segments.
    const bar = screen.getByTestId('drive-meter-bar')
    expect(bar.querySelector('[data-testid^="drive-meter-segment-"]')).toBeNull()
    // The legend still names all three sections.
    expect(screen.getByTestId('drive-meter-legend-drive')).toBeTruthy()
    expect(screen.getByTestId('drive-meter-legend-library')).toBeTruthy()
    expect(screen.getByTestId('drive-meter-legend-backup')).toBeTruthy()
  })
})

describe('DrivePage sections: drag and drop', () => {
  const listing = { files: [{ key: 'report.pdf', size: 10, modified: '2026-09-01T00:00:00Z' }], folders: ['docs'] }
  const dt = (over: Partial<DataTransfer>): DataTransfer => ({
    types: [], files: [] as unknown as FileList, getData: () => '', setData: () => {},
    effectAllowed: 'none', ...over,
  }) as unknown as DataTransfer

  it('dragging a file onto a folder row moves it into that folder', async () => {
    vi.mocked(awsControlApi.driveList).mockResolvedValue(listing)
    vi.mocked(awsControlApi.driveMove).mockResolvedValue({ moved: true })
    await renderDrive('drive')

    // A real gesture: OUR row's dragStart records the key, the folder row's
    // drop reads the recorded key (never the attacker-writable payload).
    fireEvent.dragStart(await screen.findByTestId('drive-file'), {
      dataTransfer: dt({ setData: () => {} }),
    })
    fireEvent.drop(await screen.findByTestId('drive-folder'), {
      dataTransfer: dt({
        types: ['application/x-drive-object-key'],
        getData: () => 'report.pdf',
      }),
    })
    await waitFor(() =>
      expect(awsControlApi.driveMove).toHaveBeenCalledWith(ACCOUNT_ID, 'drive', 'report.pdf', 'docs/report.pdf'),
    )
  })

  it('a drop carrying the move MIME without OUR dragStart is inert', async () => {
    // Drag data is attacker-writable: any external page can start a drag
    // whose DataTransfer carries our MIME and a REAL key. Landing it on a
    // folder here must not run an authenticated move — only a key recorded
    // by this component's own dragStart is trusted.
    vi.mocked(awsControlApi.driveList).mockResolvedValue(listing)
    await renderDrive('drive')

    fireEvent.drop(await screen.findByTestId('drive-folder'), {
      dataTransfer: dt({
        types: ['application/x-drive-object-key'],
        getData: () => 'report.pdf',
      }),
    })
    await waitFor(() => expect(screen.queryByTestId('drive-move-error')).toBeNull())
    expect(awsControlApi.driveMove).not.toHaveBeenCalled()
  })

  it('a drop onto the folder the file already lives in is a no-op', async () => {
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'docs/inner.pdf', size: 10, modified: '2026-09-01T00:00:00Z' }], folders: ['docs'],
    })
    await renderDrive('drive')

    fireEvent.dragStart(await screen.findByTestId('drive-file'), {
      dataTransfer: dt({ setData: () => {} }),
    })
    fireEvent.drop(await screen.findByTestId('drive-folder'), {
      dataTransfer: dt({
        types: ['application/x-drive-object-key'],
        getData: () => 'docs/inner.pdf',
      }),
    })
    // Same directory: no mutation, no error strip.
    await waitFor(() => expect(screen.queryByTestId('drive-move-error')).toBeNull())
    expect(awsControlApi.driveMove).not.toHaveBeenCalled()
  })

  it('a 409 destination conflict surfaces its own sentence', async () => {
    vi.mocked(awsControlApi.driveList).mockResolvedValue(listing)
    vi.mocked(awsControlApi.driveMove).mockRejectedValue(new AwsControlError('destination_exists', 409))
    await renderDrive('drive')

    fireEvent.dragStart(await screen.findByTestId('drive-file'), {
      dataTransfer: dt({ setData: () => {} }),
    })
    fireEvent.drop(await screen.findByTestId('drive-folder'), {
      dataTransfer: dt({
        types: ['application/x-drive-object-key'],
        getData: () => 'report.pdf',
      }),
    })
    expect(await screen.findByTestId('drive-move-error')).toBeTruthy()
  })

  it('a stale drag refusal does not stand in for a later delete failure', async () => {
    // The two are different failures on different slots. If one line served
    // both, a refused drop left on screen would mask the delete that failed
    // after it -- and the file that is still there would look deleted.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue(listing)
    vi.mocked(awsControlApi.driveMove).mockRejectedValue(new AwsControlError('destination_exists', 409))
    let failDelete: (e: unknown) => void = () => {}
    vi.mocked(awsControlApi.driveDelete).mockImplementation(
      () => new Promise((_resolve, reject) => { failDelete = reject }),
    )
    vi.mocked(awsControlApi.driveSearch).mockResolvedValue({ results: [], capped: false, limit: 200 })
    await renderDrive('drive')

    fireEvent.dragStart(await screen.findByTestId('drive-file'), { dataTransfer: dt({ setData: () => {} }) })
    fireEvent.drop(await screen.findByTestId('drive-folder'), {
      dataTransfer: dt({ types: ['application/x-drive-object-key'], getData: () => 'report.pdf' }),
    })
    const refusal = await screen.findByTestId('drive-move-error')

    // Now a delete whose inline strip is gone (the view swapped to search) fails.
    await chooseFromMenu(screen.getByTestId('drive-more'), 'drive-delete')
    fireEvent.click(await screen.findByTestId('drive-delete-confirm-action'))
    await waitFor(() => expect(awsControlApi.driveDelete).toHaveBeenCalledTimes(1))
    fireEvent.change(screen.getByTestId('drive-search-input'), { target: { value: 'zzz' } })
    await screen.findByTestId('drive-search-empty')
    failDelete(new Error('AccessDenied'))

    const failure = await screen.findByTestId('drive-page-error')
    expect(failure).toHaveTextContent('report.pdf')
    expect(failure).not.toBe(refusal)
  })

  it('moving a file with a live share link surfaces the share-specific refusal', async () => {
    // The backend refuses (409 share_active) because the presigned URL is
    // bound to the source key. The generic "same name at destination" text
    // would send the user hunting for a duplicate that does not exist.
    vi.mocked(awsControlApi.driveList).mockResolvedValue(listing)
    vi.mocked(awsControlApi.driveMove).mockRejectedValue(new AwsControlError('share_active', 409))
    await renderDrive('drive')

    fireEvent.dragStart(await screen.findByTestId('drive-file'), {
      dataTransfer: dt({ setData: () => {} }),
    })
    fireEvent.drop(await screen.findByTestId('drive-folder'), {
      dataTransfer: dt({
        types: ['application/x-drive-object-key'],
        getData: () => 'report.pdf',
      }),
    })
    const strip = await screen.findByTestId('drive-move-error')
    expect(strip).toHaveTextContent(i18nT('apps.awsControl.console.move_shared'))
    expect(strip).not.toHaveTextContent(i18nT('apps.awsControl.console.move_conflict'))
  })

  it('OS files dropped on a folder row upload into that folder', async () => {
    vi.mocked(awsControlApi.driveList).mockResolvedValue(listing)
    vi.mocked(awsControlApi.driveUpload).mockResolvedValue({ uploaded: true } as never)
    await renderDrive('drive')

    const file = new File(['x'], 'notes.md', { type: 'text/markdown' })
    fireEvent.drop(await screen.findByTestId('drive-folder'), {
      dataTransfer: dt({ types: ['Files'], files: [file] as unknown as FileList }),
    })
    await waitFor(() =>
      expect(awsControlApi.driveUpload).toHaveBeenCalledWith(ACCOUNT_ID, 'drive', 'docs/notes.md', file),
    )
  })

  it('OS files dropped on the listing upload into the open folder', async () => {
    vi.mocked(awsControlApi.driveList).mockResolvedValue(listing)
    vi.mocked(awsControlApi.driveUpload).mockResolvedValue({ uploaded: true } as never)
    await renderDrive('drive')

    const file = new File(['x'], 'root.md', { type: 'text/markdown' })
    fireEvent.drop(screen.getByTestId('drive-section'), {
      dataTransfer: dt({ types: ['Files'], files: [file] as unknown as FileList }),
    })
    await waitFor(() =>
      expect(awsControlApi.driveUpload).toHaveBeenCalledWith(ACCOUNT_ID, 'drive', 'root.md', file),
    )
  })

  it('a bad name in a multi-file drop is called out BY NAME, the rest still upload', async () => {
    // The picker's anonymous "that file name" is useless after a 10-file
    // drop: the user cannot tell which file was refused while the others
    // uploaded. The drop path interpolates the offending name.
    vi.mocked(awsControlApi.driveList).mockResolvedValue(listing)
    vi.mocked(awsControlApi.driveUpload).mockResolvedValue({ uploaded: true } as never)
    await renderDrive('drive')

    const bad = new File(['x'], 'bad\u0000name.md', { type: 'text/markdown' })
    const good = new File(['x'], 'good.md', { type: 'text/markdown' })
    fireEvent.drop(screen.getByTestId('drive-section'), {
      dataTransfer: dt({ types: ['Files'], files: [bad, good] as unknown as FileList }),
    })
    const strip = await screen.findByTestId('drive-upload-error')
    expect(strip.textContent).toContain('bad\u0000name.md')
    await waitFor(() =>
      expect(awsControlApi.driveUpload).toHaveBeenCalledWith(ACCOUNT_ID, 'drive', 'good.md', good),
    )
    expect(awsControlApi.driveUpload).toHaveBeenCalledTimes(1)
  })

  it('a drop-upload that fails on the wire surfaces an error naming the file', async () => {
    // Without this, a failed put shows nothing: the dropped file silently
    // never appears and the user believes it uploaded.
    vi.mocked(awsControlApi.driveList).mockResolvedValue(listing)
    vi.mocked(awsControlApi.driveUpload).mockRejectedValue(new AwsControlError('aws_failed', 502))
    await renderDrive('drive')

    const file = new File(['x'], 'doomed.md', { type: 'text/markdown' })
    fireEvent.drop(screen.getByTestId('drive-section'), {
      dataTransfer: dt({ types: ['Files'], files: [file] as unknown as FileList }),
    })
    const strip = await screen.findByTestId('drive-upload-error')
    expect(strip.textContent).toContain('doomed.md')
  })
})

/**
 * Every failure this app can show renders through the shared error notice, and
 * every notice offers the agent hand-off with the REAL failure attached — not
 * the localised sentence the reader saw. The cases below are the ones that used
 * to render nothing at all; the migrated ones keep their test ids and are
 * covered by the assertions above.
 */
describe('DrivePage sections: error surfaces reach the agent', () => {
  /** An `AwsControlError` exactly as `request` would build it: code + journaled report. */
  function failure(code: string, status: number, endpoint: string, body: string): AwsControlError {
    const report = recordError({ source: 'api', message: body, status, code, endpoint, detail: body })
    return new AwsControlError(code, status, report)
  }

  const navigated: string[] = []
  beforeEach(() => {
    __resetErrorJournalForTests()
    __resetNavSeamForTests()
    navigated.length = 0
    sessionStorage.clear()
    installSoftNavigate((to) => { navigated.push(to) })
    stubDrivePresent()
  })

  it('a failed Files listing is a notice with Retry, not an empty folder', async () => {
    vi.mocked(awsControlApi.driveList)
      .mockRejectedValueOnce(new AwsControlError('aws_call_failed', 502))
      .mockResolvedValue({ files: [], folders: [] })
    await renderDrive('drive')

    const notice = await screen.findByTestId('drive-list-error')
    expect(notice).toHaveTextContent(i18nT('apps.awsControl.console.files_list_failed'))
    expect(notice).toHaveAttribute('role', 'alert')
    // The empty state must NOT have claimed the folder is empty.
    expect(screen.queryByTestId('drive-empty')).toBeNull()

    fireEvent.click(screen.getByTestId('drive-list-error-retry'))
    expect(await screen.findByTestId('drive-empty')).toBeTruthy()
    expect(awsControlApi.driveList).toHaveBeenCalledTimes(2)
  })

  it('the hand-off carries the endpoint, status and code — not only the sentence on screen', async () => {
    vi.mocked(awsControlApi.driveList).mockRejectedValue(
      failure('aws_call_failed', 502, '/api/apps/aws-control/drive/111122223333/list', 'AccessDenied: s3:ListBucket'),
    )
    await renderDrive('drive')

    const notice = await screen.findByTestId('drive-list-error')
    fireEvent.click(within(notice).getByRole('button', { name: /ask the agent/i }))

    expect(navigated).toEqual(['/chat'])
    const staged = consumeChatHandoff() ?? ''
    expect(staged).toContain('/api/apps/aws-control/drive/111122223333/list')
    expect(staged).toContain('HTTP 502')
    expect(staged).toContain('aws_call_failed')
    expect(staged).toContain('AccessDenied: s3:ListBucket')
  })

  it('a refused share link is said in the dialog instead of re-enabling the button silently', async () => {
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'report.pdf', size: 2048, modified: '2026-08-20T00:00:00Z' }],
      folders: [],
    })
    vi.mocked(awsControlApi.driveShare).mockRejectedValue(new AwsControlError('publish_denied', 403))
    await renderDrive('drive')

    fireEvent.keyDown(await screen.findByTestId('drive-more'), { key: 'Enter' })
    fireEvent.click(await screen.findByTestId('drive-share'))
    fireEvent.change(await screen.findByTestId('share-note'), { target: { value: 'for the Q3 review' } })
    fireEvent.click(await screen.findByTestId('share-create'))

    const notice = await screen.findByTestId('share-error')
    expect(notice).toHaveTextContent(i18nT('apps.awsControl.console.share_failed'))
    // No hand-off here: the note field still holds the typed text, and the
    // hand-off would navigate away from it. The draft survives the refusal.
    expect(within(notice).queryByRole('button', { name: /ask the agent/i })).toBeNull()
    expect(screen.getByTestId('share-note')).toHaveValue('for the Q3 review')
    // Still no result: nothing was shared.
    expect(screen.queryByTestId('share-result')).toBeNull()
  })

  it('a failed backup-status read and a refused nightly toggle each get their own line', async () => {
    vi.mocked(awsControlApi.backup)
      .mockRejectedValueOnce(new AwsControlError('http_500', 500))
      .mockResolvedValue(emptyBackup)
    await renderDrive('backup')
    const notice = await screen.findByTestId('backup-status-error')
    expect(notice).toHaveTextContent(i18nT('apps.awsControl.console.backup_status_failed'))
    // No rows were invented for a status we do not have.
    expect(screen.queryByTestId('backup-row-snapshot')).toBeNull()

    // A read the reader can re-issue offers Retry, and Retry re-reads.
    fireEvent.click(screen.getByTestId('backup-status-error-retry'))
    expect(await screen.findByTestId('backup-row-snapshot')).toBeTruthy()
    expect(screen.queryByTestId('backup-status-error')).toBeNull()
  })

  it('a refused nightly toggle says so under the toggle it failed to move', async () => {
    vi.mocked(awsControlApi.backup).mockResolvedValue(emptyBackup)
    vi.mocked(awsControlApi.backupNightly).mockRejectedValue(new AwsControlError('restricted_session', 403))
    await renderDrive('backup')

    const toggle = within(await screen.findByTestId('backup-nightly')).getByRole('switch')
    fireEvent.click(toggle)
    const notice = await screen.findByTestId('backup-nightly-error')
    expect(notice).toHaveTextContent(i18nT('apps.awsControl.console.backup_nightly_failed'))
    // Adjacent to the row it explains: the notice is the nightly row's next sibling.
    expect(screen.getByTestId('backup-nightly').nextElementSibling).toContainElement(notice)
  })

  it('a rejected folder name is a notice WITHOUT the hand-off — nothing reached AWS', async () => {
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: [] })
    await renderDrive('drive')

    fireEvent.click(await screen.findByTestId('drive-folder-toggle'))
    fireEvent.change(await screen.findByTestId('drive-folder-name'), { target: { value: 'bad/name' } })
    fireEvent.click(screen.getByTestId('drive-folder-create'))

    const notice = await screen.findByTestId('drive-folder-error')
    expect(notice).toHaveTextContent(i18nT('apps.awsControl.console.folder_bad_name'))
    expect(within(notice).queryByRole('button', { name: /ask the agent/i })).toBeNull()
    expect(awsControlApi.driveFolderCreate).not.toHaveBeenCalled()
  })

  it('a refused restore is reported next to the caveat that predicts it', async () => {
    vi.mocked(awsControlApi.backup).mockResolvedValue({
      nightly: true,
      runs: {},
      remote: {
        snapshot: [{ key: 'backup/snapshot/2026-08-24.tar', size: 4096, modified: '2026-08-24T00:00:00Z' }],
        sessions: [],
      },
    })
    vi.mocked(awsControlApi.backupRestore).mockRejectedValue(new AwsControlError('aws_call_failed', 502))
    await renderDrive('backup')

    fireEvent.click(await screen.findByTestId('backup-remote-toggle'))
    const archive = await screen.findByTestId('backup-archive')
    fireEvent.click(within(archive).getByTestId('backup-restore'))

    expect(await screen.findByTestId('backup-restore-error')).toHaveTextContent(
      i18nT('apps.awsControl.console.backup_restore_failed'),
    )
    expect(screen.queryByTestId('backup-restored')).toBeNull()
  })

  it('the remote-archive reason the backend reports inside a 200 travels as the message', async () => {
    vi.mocked(awsControlApi.backup).mockResolvedValue({
      ...emptyBackup,
      remote: null,
      remoteError: 'AccessDenied: s3:ListBucket on backup/',
    })
    await renderDrive('backup')

    const notice = await screen.findByTestId('backup-remote-error')
    expect(notice).toHaveTextContent(i18nT('apps.awsControl.console.backup_remote_error'))
    expect(notice).toHaveTextContent('AccessDenied: s3:ListBucket on backup/')
    fireEvent.click(within(notice).getByRole('button', { name: /ask the agent/i }))
    expect(consumeChatHandoff() ?? '').toContain('AccessDenied: s3:ListBucket on backup/')
  })

  it('a failed share-ledger read is a notice, never "no links"', async () => {
    vi.mocked(awsControlApi.shares).mockRejectedValue(new AwsControlError('http_500', 500))
    await renderDrive('access')

    expect(await screen.findByTestId('access-list-error')).toHaveTextContent(
      i18nT('apps.awsControl.console.access_list_failed'),
    )
    expect(screen.queryByTestId('access-empty')).toBeNull()
  })

  it('a refused Forget keeps the row and says why', async () => {
    vi.mocked(awsControlApi.shares).mockResolvedValue({
      shares: [{
        id: 's1', account: ACCOUNT_ID, section: 'drive', key: 'report.pdf',
        createdAt: '2026-08-24T05:00:00Z', expiresAt: '2099-01-01T00:00:00Z', note: '',
      }],
    })
    vi.mocked(awsControlApi.shareForget).mockRejectedValue(new AwsControlError('http_500', 500))
    await renderDrive('access')

    fireEvent.click(await screen.findByTestId('access-forget'))
    expect(await screen.findByTestId('access-forget-error')).toHaveTextContent(
      i18nT('apps.awsControl.console.access_forget_failed'),
    )
    expect(screen.getByTestId('access-row')).toBeTruthy()
  })

  it('while the folder-name field is open, no Files notice offers the hand-off — the typed name survives', async () => {
    // Every notice on the pane shares the screen with that one draft, so the
    // gate is the disclosure, not the individual notice.
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'report.pdf', size: 2048, modified: '2026-08-20T00:00:00Z' }],
      folders: [],
    })
    vi.mocked(awsControlApi.driveDelete).mockRejectedValue(new AwsControlError('aws_call_failed', 502))
    await renderDrive('drive')

    fireEvent.click(await screen.findByTestId('drive-folder-toggle'))
    fireEvent.change(await screen.findByTestId('drive-folder-name'), { target: { value: 'quarterly' } })

    fireEvent.keyDown(await screen.findByTestId('drive-more'), { key: 'Enter' })
    fireEvent.click(await screen.findByTestId('drive-delete'))
    fireEvent.click(await screen.findByTestId('drive-delete-confirm-action'))

    const notice = await screen.findByTestId('drive-delete-error')
    expect(within(notice).queryByRole('button', { name: /ask the agent/i })).toBeNull()
    expect(screen.getByTestId('drive-folder-name')).toHaveValue('quarterly')
  })
})

describe('DrivePage sections: preview, rename, search', () => {
  const listing = {
    files: [
      { key: 'photo.png', size: 10, modified: '2026-09-01T00:00:00Z' },
      { key: 'notes.md', size: 20, modified: '2026-09-01T00:00:00Z' },
      { key: 'data.bin', size: 30, modified: '2026-09-01T00:00:00Z' },
    ],
    folders: [],
  }

  it('clicking an image name opens the preview with the presigned URL', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue(listing)
    vi.mocked(awsControlApi.driveDownload).mockResolvedValue({ url: 'https://signed/photo', expiresSecs: 60 })
    await renderDrive('drive')

    fireEvent.click((await screen.findAllByTestId('drive-preview-open'))[0])
    const img = await screen.findByTestId('drive-preview-image')
    expect(img).toHaveAttribute('src', 'https://signed/photo')
    // The dialog downloads through the SAME path the row uses, and closes.
    fireEvent.click(screen.getByTestId('drive-preview-close'))
    expect(screen.queryByTestId('drive-preview-dialog')).toBeNull()
  })

  it('a PDF previews in a fully sandboxed iframe, so a disguised HTML object cannot run script', async () => {
    // The preview branch is chosen by extension, not by the stored
    // Content-Type, so `report.pdf` may really be HTML. The empty sandbox is
    // what keeps that object from running script or navigating the top
    // window; a PDF renders without any sandbox permission.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'report.pdf', size: 2048, modified: '2026-09-01T00:00:00Z' }],
      folders: [],
    })
    vi.mocked(awsControlApi.driveDownload).mockResolvedValue({ url: 'https://signed/report', expiresSecs: 60 })
    await renderDrive('drive')

    fireEvent.click((await screen.findAllByTestId('drive-preview-open'))[0])
    const frame = await screen.findByTestId('drive-preview-pdf')
    expect(frame).toHaveAttribute('src', 'https://signed/report')
    expect(frame).toHaveAttribute('sandbox', '')
  })

  it('a text file previews through the gateway endpoint, with the truncation notice', async () => {
    // Text goes through the proxy, never a browser fetch of the presigned URL:
    // the bucket has no CORS config, so that fetch would fail while img tags
    // (CORS-exempt) succeed — the split is the contract this test pins.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue(listing)
    vi.mocked(awsControlApi.drivePreview).mockResolvedValue({ content: '# hello', truncated: true, redacted: false })
    await renderDrive('drive')

    fireEvent.click((await screen.findAllByTestId('drive-preview-open'))[1])
    expect((await screen.findByTestId('drive-preview-text')).textContent).toBe('# hello')
    expect(screen.getByTestId('drive-preview-truncated')).toBeTruthy()
    expect(awsControlApi.drivePreview).toHaveBeenCalledWith(ACCOUNT_ID, 'drive', 'notes.md')
    expect(awsControlApi.driveDownload).not.toHaveBeenCalled()
  })

  it('an unpreviewable type says so honestly instead of a broken pane', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue(listing)
    await renderDrive('drive')

    fireEvent.click((await screen.findAllByTestId('drive-preview-open'))[2])
    expect(await screen.findByTestId('drive-preview-fallback')).toBeTruthy()
    expect(awsControlApi.drivePreview).not.toHaveBeenCalled()
    expect(awsControlApi.driveDownload).not.toHaveBeenCalled()
  })

  it('rename is a same-directory move through the move endpoint', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'docs/old.md', size: 10, modified: '2026-09-01T00:00:00Z' }],
      folders: [],
    })
    vi.mocked(awsControlApi.driveMove).mockResolvedValue({ moved: true })
    await renderDrive('drive')

    await chooseFromMenu((await screen.findAllByTestId('drive-more'))[0], 'drive-rename')
    const input = await screen.findByTestId('drive-rename-input')
    expect(input).toHaveValue('old.md')
    fireEvent.change(input, { target: { value: 'new.md' } })
    fireEvent.click(screen.getByTestId('drive-rename-save'))
    await waitFor(() =>
      expect(awsControlApi.driveMove).toHaveBeenCalledWith(ACCOUNT_ID, 'drive', 'docs/old.md', 'docs/new.md'),
    )
    // The editor closes on success — the committed name needs no cleanup.
    await waitFor(() => expect(screen.queryByTestId('drive-rename-row')).toBeNull())
  })

  it('a rename finishing while another row is being edited leaves that editor alone', async () => {
    // The completion belongs to the row that started it. An unconditional
    // close on success would throw away the name being typed on the other
    // row -- half a filename gone without a word.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [
        { key: 'docs/a.md', size: 10, modified: '2026-09-01T00:00:00Z' },
        { key: 'docs/b.md', size: 10, modified: '2026-09-01T00:00:00Z' },
      ],
      folders: [],
    })
    let finishA: (v: { moved: boolean }) => void = () => {}
    vi.mocked(awsControlApi.driveMove).mockImplementation(
      () => new Promise((resolve) => { finishA = resolve }),
    )
    await renderDrive('drive')

    await chooseFromMenu((await screen.findAllByTestId('drive-more'))[0], 'drive-rename')
    fireEvent.change(await screen.findByTestId('drive-rename-input'), { target: { value: 'a2.md' } })
    fireEvent.click(screen.getByTestId('drive-rename-save'))
    await waitFor(() => expect(awsControlApi.driveMove).toHaveBeenCalledTimes(1))

    // While A flies, open B and start typing.
    await chooseFromMenu((await screen.findAllByTestId('drive-more'))[1], 'drive-rename')
    const inputB = await screen.findByTestId('drive-rename-input')
    expect(inputB).toHaveValue('b.md')
    fireEvent.change(inputB, { target: { value: 'b-half' } })

    // B's Rename button is held while A flies (one mutation observer); it
    // coming back is the signal that A has settled.
    expect(screen.getByTestId('drive-rename-save')).toBeDisabled()
    finishA({ moved: true })
    await waitFor(() => expect(screen.getByTestId('drive-rename-save')).not.toBeDisabled())
    // B's editor and its half-typed name survive A's completion.
    expect(screen.getByTestId('drive-rename-input')).toHaveValue('b-half')
  })

  it('a rename failing while another row is being edited is reported page-level, naming the file', async () => {
    // No strip under A to carry the message any more; dropping it would leave
    // A's old name in the listing looking like nothing was ever attempted.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [
        { key: 'docs/a.md', size: 10, modified: '2026-09-01T00:00:00Z' },
        { key: 'docs/b.md', size: 10, modified: '2026-09-01T00:00:00Z' },
      ],
      folders: [],
    })
    let failA: (e: unknown) => void = () => {}
    vi.mocked(awsControlApi.driveMove).mockImplementation(
      () => new Promise((_resolve, reject) => { failA = reject }),
    )
    await renderDrive('drive')

    await chooseFromMenu((await screen.findAllByTestId('drive-more'))[0], 'drive-rename')
    fireEvent.change(await screen.findByTestId('drive-rename-input'), { target: { value: 'a2.md' } })
    fireEvent.click(screen.getByTestId('drive-rename-save'))
    await waitFor(() => expect(awsControlApi.driveMove).toHaveBeenCalledTimes(1))
    await chooseFromMenu((await screen.findAllByTestId('drive-more'))[1], 'drive-rename')
    await screen.findByTestId('drive-rename-input')

    failA(new AwsControlError('destination_exists', 409))
    const notice = await screen.findByTestId('drive-page-error')
    expect(notice).toHaveTextContent('a.md')
    expect(notice).toHaveTextContent(i18nT('apps.awsControl.console.rename_conflict'))
    // B's editor is untouched: no inline error for a failure that was not its own.
    expect(screen.queryByTestId('drive-rename-error')).toBeNull()
    expect(screen.getByTestId('drive-rename-input')).toHaveValue('b.md')
  })

  it('a failed delete on one file does not pre-render its error under the next file\'s strip', async () => {
    // One mutation serves every row; without a reset on open, B's strip would
    // say "Delete failed" before anything was tried on B.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue(listing)
    vi.mocked(awsControlApi.driveDelete).mockRejectedValueOnce(new Error('AccessDenied'))
    await renderDrive('drive')

    await chooseFromMenu((await screen.findAllByTestId('drive-more'))[0], 'drive-delete')
    fireEvent.click(await screen.findByTestId('drive-delete-confirm-action'))
    await waitFor(() =>
      expect(screen.getByTestId('drive-delete-confirm')).toHaveTextContent(
        i18nT('apps.awsControl.console.delete_failed'),
      ),
    )
    fireEvent.click(screen.getByTestId('drive-delete-cancel'))

    await chooseFromMenu((await screen.findAllByTestId('drive-more'))[1], 'drive-delete')
    const strip = await screen.findByTestId('drive-delete-confirm')
    expect(strip).toHaveTextContent('notes.md')
    expect(strip).not.toHaveTextContent(i18nT('apps.awsControl.console.delete_failed'))
  })

  it('a delete failing while another file\'s strip is open lands page-level, not under that strip', async () => {
    // One mutation serves every row. The strip is keyed to the row whose
    // delete it is: A's failure goes to the page-level notice and must not
    // also appear inline under B just because the mutation is shared.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue(listing)
    let failA: (e: unknown) => void = () => {}
    vi.mocked(awsControlApi.driveDelete).mockImplementation(
      () => new Promise((_resolve, reject) => { failA = reject }),
    )
    await renderDrive('drive')

    await chooseFromMenu((await screen.findAllByTestId('drive-more'))[0], 'drive-delete')
    fireEvent.click(await screen.findByTestId('drive-delete-confirm-action'))
    await waitFor(() => expect(awsControlApi.driveDelete).toHaveBeenCalledTimes(1))

    await chooseFromMenu((await screen.findAllByTestId('drive-more'))[1], 'drive-delete')
    const strip = await screen.findByTestId('drive-delete-confirm')
    expect(strip).toHaveTextContent('notes.md')

    failA(new Error('AccessDenied'))
    const notice = await screen.findByTestId('drive-page-error')
    expect(notice).toHaveTextContent('photo.png')
    expect(screen.getByTestId('drive-delete-confirm')).not.toHaveTextContent(
      i18nT('apps.awsControl.console.delete_failed'),
    )
  })

  it('a delete that fails after the view swapped to search is reported page-level, naming the file', async () => {
    // The inline notice lives in the confirmation strip; a query change tears
    // that strip down with the folder view. Without a fallback the rejection
    // has no surface at all and the file, still there, looks deleted.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue(listing)
    vi.mocked(awsControlApi.driveSearch).mockResolvedValue({ results: [], capped: false, limit: 200 })
    let failDelete: (e: unknown) => void = () => {}
    vi.mocked(awsControlApi.driveDelete).mockImplementation(
      () => new Promise((_resolve, reject) => { failDelete = reject }),
    )
    await renderDrive('drive')

    await chooseFromMenu((await screen.findAllByTestId('drive-more'))[0], 'drive-delete')
    fireEvent.click(await screen.findByTestId('drive-delete-confirm-action'))
    await waitFor(() => expect(awsControlApi.driveDelete).toHaveBeenCalledTimes(1))

    fireEvent.change(screen.getByTestId('drive-search-input'), { target: { value: 'zzz' } })
    await screen.findByTestId('drive-search-empty')
    expect(screen.queryByTestId('drive-delete-confirm')).toBeNull()

    failDelete(new Error('AccessDenied'))
    const notice = await screen.findByTestId('drive-page-error')
    expect(notice).toHaveTextContent('photo.png')
  })

  it('a rename that fails after the view swapped to search is reported page-level', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue(listing)
    vi.mocked(awsControlApi.driveSearch).mockResolvedValue({ results: [], capped: false, limit: 200 })
    let failRename: (e: unknown) => void = () => {}
    vi.mocked(awsControlApi.driveMove).mockImplementation(
      () => new Promise((_resolve, reject) => { failRename = reject }),
    )
    await renderDrive('drive')

    await chooseFromMenu((await screen.findAllByTestId('drive-more'))[0], 'drive-rename')
    fireEvent.change(await screen.findByTestId('drive-rename-input'), { target: { value: 'photo2.png' } })
    fireEvent.click(screen.getByTestId('drive-rename-save'))
    await waitFor(() => expect(awsControlApi.driveMove).toHaveBeenCalledTimes(1))

    // The view swap closes the editor even though its commit is on the wire.
    fireEvent.change(screen.getByTestId('drive-search-input'), { target: { value: 'zzz' } })
    await screen.findByTestId('drive-search-empty')
    expect(screen.queryByTestId('drive-rename-row')).toBeNull()

    failRename(new AwsControlError('destination_exists', 409))
    const notice = await screen.findByTestId('drive-page-error')
    expect(notice).toHaveTextContent('photo.png')
    expect(notice).toHaveTextContent(i18nT('apps.awsControl.console.rename_conflict'))
  })

  it('a delete that fails while the Move picker is open lands on the page strip, not inside the picker', async () => {
    // The picker renders the move slot as ITS refusal, so a delete failure
    // written there would read as "this move was refused". Separate slots.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ ...listing, folders: ['docs'] })
    let failDelete: (e: unknown) => void = () => {}
    vi.mocked(awsControlApi.driveDelete).mockImplementation(
      () => new Promise((_resolve, reject) => { failDelete = reject }),
    )
    await renderDrive('drive')

    const more = await screen.findAllByTestId('drive-more')
    await chooseFromMenu(more[0], 'drive-delete')
    fireEvent.click(await screen.findByTestId('drive-delete-confirm-action'))
    await waitFor(() => expect(awsControlApi.driveDelete).toHaveBeenCalledTimes(1))
    // Move the confirmation to another row (its strip is gone from photo.png),
    // then open the picker for a third file.
    await chooseFromMenu(more[1], 'drive-delete')
    await chooseFromMenu(more[2], 'drive-move')
    await screen.findByTestId('move-dialog')

    failDelete(new Error('AccessDenied'))
    const strip = await screen.findByTestId('drive-page-error')
    expect(strip).toHaveTextContent('photo.png')
    expect(screen.queryByTestId('move-error')).toBeNull()
  })

  it('saving an untouched name that ends in a space is a no-op, not a move', async () => {
    // The key grammar allows a trailing space, so such a file can exist.
    // Trimming the editor's value before comparing would turn "open Rename,
    // press Save" into a rename to a different key.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'docs/notes ', size: 10, modified: '2026-09-01T00:00:00Z' }],
      folders: [],
    })
    vi.mocked(awsControlApi.driveMove).mockResolvedValue({ moved: true })
    await renderDrive('drive')

    await chooseFromMenu((await screen.findAllByTestId('drive-more'))[0], 'drive-rename')
    const input = await screen.findByTestId('drive-rename-input')
    expect(input).toHaveValue('notes ')
    fireEvent.click(screen.getByTestId('drive-rename-save'))
    await waitFor(() => expect(screen.queryByTestId('drive-rename-row')).toBeNull())
    expect(awsControlApi.driveMove).not.toHaveBeenCalled()
    expect(screen.queryByTestId('drive-rename-error')).toBeNull()
  })

  it('a rename refused for a live share shows the share-specific sentence inline', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'shared.md', size: 10, modified: '2026-09-01T00:00:00Z' }],
      folders: [],
    })
    vi.mocked(awsControlApi.driveMove).mockRejectedValue(new AwsControlError('share_active', 409))
    await renderDrive('drive')

    await chooseFromMenu((await screen.findAllByTestId('drive-more'))[0], 'drive-rename')
    fireEvent.change(await screen.findByTestId('drive-rename-input'), { target: { value: 'renamed.md' } })
    fireEvent.click(screen.getByTestId('drive-rename-save'))
    const err = await screen.findByTestId('drive-rename-error')
    // Rename's OWN sentence: the user pressed Rename, so the refusal must not
    // talk about moving or a destination folder they never chose.
    expect(err).toHaveTextContent(i18nT('apps.awsControl.console.rename_shared'))
    expect(err).not.toHaveTextContent(i18nT('apps.awsControl.console.move_shared'))
  })

  it('a rename that collides with an existing name says so in rename terms', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'a.md', size: 10, modified: '2026-09-01T00:00:00Z' }],
      folders: [],
    })
    vi.mocked(awsControlApi.driveMove).mockRejectedValue(new AwsControlError('exists', 409))
    await renderDrive('drive')

    await chooseFromMenu((await screen.findAllByTestId('drive-more'))[0], 'drive-rename')
    fireEvent.change(await screen.findByTestId('drive-rename-input'), { target: { value: 'b.md' } })
    fireEvent.click(screen.getByTestId('drive-rename-save'))
    const err = await screen.findByTestId('drive-rename-error')
    expect(err).toHaveTextContent(i18nT('apps.awsControl.console.rename_conflict'))
  })

  it('a grid tile hides its name (the preview trigger) while its rename editor is open', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'a.md', size: 10, modified: '2026-09-01T00:00:00Z' }],
      folders: [],
    })
    await renderDrive('drive')
    fireEvent.click(await screen.findByTitle('Grid view'))
    await screen.findByTestId('drive-grid')
    expect(await screen.findByTestId('drive-grid-preview-open')).toBeTruthy()

    await chooseFromMenu((await screen.findAllByTestId('drive-grid-more'))[0], 'drive-grid-rename')
    await screen.findByTestId('drive-grid-rename-input')
    // Opening a preview over the editor would stack two editing contexts.
    expect(screen.queryByTestId('drive-grid-preview-open')).toBeNull()
    fireEvent.click(screen.getByTestId('drive-grid-rename-cancel'))
    expect(await screen.findByTestId('drive-grid-preview-open')).toBeTruthy()
  })

  it('a media error re-mints the presign, re-arms on each successful load, and falls back on two in a row', async () => {
    // The download presign is a 60s grant; a clip longer than that 403s
    // mid-play and the element reports a media error. Re-minting (another
    // short grant, not a longer one) is the recovery, and it must work for
    // EVERY expiry a long clip crosses — so a successful load re-arms it.
    // Two errors with no load between them is a URL that never worked.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'clip.mp4', size: 10, modified: '2026-09-01T00:00:00Z' }],
      folders: [],
    })
    vi.mocked(awsControlApi.driveDownload)
      .mockResolvedValueOnce({ url: 'https://signed/1', expiresSecs: 60 })
      .mockResolvedValueOnce({ url: 'https://signed/2', expiresSecs: 60 })
      .mockResolvedValueOnce({ url: 'https://signed/3', expiresSecs: 60 })
    await renderDrive('drive')

    fireEvent.click((await screen.findAllByTestId('drive-preview-open'))[0])
    const video = await screen.findByTestId('drive-preview-video')
    expect(video).toHaveAttribute('src', 'https://signed/1')
    // First expiry: re-mint.
    fireEvent.error(video)
    await waitFor(() => expect(screen.getByTestId('drive-preview-video')).toHaveAttribute('src', 'https://signed/2'))
    expect(awsControlApi.driveDownload).toHaveBeenCalledTimes(2)
    // The new URL loads -> re-armed; second expiry re-mints again.
    fireEvent.loadedMetadata(screen.getByTestId('drive-preview-video'))
    fireEvent.error(screen.getByTestId('drive-preview-video'))
    await waitFor(() => expect(screen.getByTestId('drive-preview-video')).toHaveAttribute('src', 'https://signed/3'))
    expect(awsControlApi.driveDownload).toHaveBeenCalledTimes(3)
    // An error with NO load since the last re-mint is a real failure — reported
    // through the shared notice, whose Try again re-mints once more.
    fireEvent.error(screen.getByTestId('drive-preview-video'))
    const notice = await screen.findByTestId('drive-preview-error')
    expect(notice).toHaveTextContent(i18nT('apps.awsControl.console.preview_failed'))
    expect(screen.queryByTestId('drive-preview-fallback')).toBeNull()
    expect(awsControlApi.driveDownload).toHaveBeenCalledTimes(3)
    fireEvent.click(screen.getByTestId('drive-preview-error-retry'))
    await waitFor(() => expect(awsControlApi.driveDownload).toHaveBeenCalledTimes(4))
  })

  it('searching replaces the folder view with whole-section hits, and clearing restores it', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue(listing)
    vi.mocked(awsControlApi.driveSearch).mockResolvedValue({
      results: [{ key: 'docs/deep/report.pdf', size: 55, modified: '2026-09-01T00:00:00Z' }],
      capped: false,
      limit: 200,
    })
    await renderDrive('drive')
    await screen.findAllByTestId('drive-file')

    fireEvent.change(screen.getByTestId('drive-search-input'), { target: { value: 'report' } })
    // Debounced 300ms — waitFor absorbs the timer.
    const hit = await screen.findByTestId('drive-search-hit')
    // The FULL relative key, because hits span the whole section -- and the
    // untruncated key rides on the title, since a long folder prefix is what
    // the cell truncates (the basename stays whole).
    expect(hit.textContent).toContain('docs/deep/report.pdf')
    expect(screen.getByTestId('drive-search-open')).toHaveAttribute('title', 'docs/deep/report.pdf')
    expect(awsControlApi.driveSearch).toHaveBeenCalledWith(ACCOUNT_ID, 'drive', 'report')
    expect(screen.queryByTestId('drive-listing')).toBeNull()

    fireEvent.click(screen.getByTestId('drive-search-clear'))
    await screen.findAllByTestId('drive-file')
    expect(screen.queryByTestId('drive-search-results')).toBeNull()
  })

  it('a capped search says the walk stopped, and the goto action jumps to the folder', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue(listing)
    vi.mocked(awsControlApi.driveSearch).mockResolvedValue({
      results: [{ key: 'docs/deep/report.pdf', size: 55, modified: '2026-09-01T00:00:00Z' }],
      capped: true,
      limit: 37,
    })
    await renderDrive('drive')

    fireEvent.change(screen.getByTestId('drive-search-input'), { target: { value: 'report' } })
    // The number in the notice is the SERVER's cap, echoed in the payload —
    // no locale spells it, so a cap change never strands a translation.
    expect(await screen.findByTestId('drive-search-capped')).toHaveTextContent('37')
    // Hit actions sit behind the same labeled overflow the file rows use.
    await chooseFromMenu(screen.getByTestId('drive-search-more'), 'drive-search-goto')
    // Search cleared, folder view restored at the hit's directory.
    await waitFor(() => expect(screen.queryByTestId('drive-search-results')).toBeNull())
    expect(await screen.findByTestId('drive-crumbs')).toBeTruthy()
  })

  it('the goto action marks the hit in the landed folder and scrolls it into view', async () => {
    // A large folder with no pointer to the file makes the reader re-find by
    // eye what they just searched for; the row is marked and centred instead.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockImplementation(async (_acct, _section, prefix) =>
      prefix === 'docs/deep'
        ? {
            files: [
              { key: 'docs/deep/other.md', size: 1, modified: '2026-09-01T00:00:00Z' },
              { key: 'docs/deep/report.pdf', size: 55, modified: '2026-09-01T00:00:00Z' },
            ],
            folders: [],
          }
        : listing,
    )
    vi.mocked(awsControlApi.driveSearch).mockResolvedValue({
      results: [{ key: 'docs/deep/report.pdf', size: 55, modified: '2026-09-01T00:00:00Z' }],
      capped: false,
      limit: 200,
    })
    // jsdom has no scrollIntoView; install one for the assertion and put the
    // prototype back so nothing leaks into the next test.
    const scrolled = vi.fn()
    const original = Element.prototype.scrollIntoView
    Element.prototype.scrollIntoView = scrolled
    onTestFinished(() => { Element.prototype.scrollIntoView = original })
    await renderDrive('drive')

    fireEvent.change(screen.getByTestId('drive-search-input'), { target: { value: 'report' } })
    await screen.findByTestId('drive-search-hit')
    await chooseFromMenu(screen.getByTestId('drive-search-more'), 'drive-search-goto')

    const rows = await screen.findAllByTestId('drive-file')
    const marked = rows.filter((r) => r.getAttribute('data-highlighted') === 'true')
    expect(marked).toHaveLength(1)
    expect(marked[0]).toHaveTextContent('report.pdf')
    expect(scrolled).toHaveBeenCalled()
    // The menu trigger unmounted with the search view; the row takes focus so
    // keyboard and assistive-technology users land on the hit, not on <body>.
    expect(marked[0]).toHaveAttribute('tabindex', '-1')
    expect(document.activeElement).toBe(marked[0])
  })

  it('a hit past the first listing page is paged in until its row mounts', async () => {
    // The marker fires on mount; a row on page two never mounts until Load
    // more is pressed, so while a marker is pending the pages are pulled in
    // automatically -- a locator that only works in small folders is not one.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockImplementation(async (_acct, _section, prefix, token) => {
      if (prefix !== 'docs/deep') return listing
      return token === 'p2'
        ? { files: [{ key: 'docs/deep/report.pdf', size: 55, modified: '2026-09-01T00:00:00Z' }], folders: [] }
        : { files: [{ key: 'docs/deep/first.md', size: 1, modified: '2026-09-01T00:00:00Z' }], folders: [], nextToken: 'p2' }
    })
    vi.mocked(awsControlApi.driveSearch).mockResolvedValue({
      results: [{ key: 'docs/deep/report.pdf', size: 55, modified: '2026-09-01T00:00:00Z' }],
      capped: false,
      limit: 200,
    })
    await renderDrive('drive')

    fireEvent.change(screen.getByTestId('drive-search-input'), { target: { value: 'report' } })
    await screen.findByTestId('drive-search-hit')
    await chooseFromMenu(screen.getByTestId('drive-search-more'), 'drive-search-goto')

    // No Load more click: the second page arrives on its own and the hit is marked.
    await waitFor(() =>
      expect(awsControlApi.driveList).toHaveBeenCalledWith(ACCOUNT_ID, 'drive', 'docs/deep', 'p2'),
    )
    const rows = await screen.findAllByTestId('drive-file')
    const marked = rows.filter((r) => r.getAttribute('data-highlighted') === 'true')
    expect(marked).toHaveLength(1)
    expect(marked[0]).toHaveTextContent('report.pdf')
  })

  it('a listing slower than the highlight window still gets the marker and the scroll', async () => {
    // The window is counted from the moment the ROW appears, not from the
    // click: the listing behind "Open containing folder" is a CLI round-trip,
    // and a clock started at the click would run out during a slow load and
    // land the user unanchored in a folder of hundreds of rows.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    onTestFinished(() => vi.useRealTimers())
    stubDrivePresent()
    let releaseListing: (v: DriveListing) => void = () => {}
    vi.mocked(awsControlApi.driveList).mockImplementation((_acct, _section, prefix) =>
      prefix === 'docs/deep'
        ? new Promise<DriveListing>((resolve) => { releaseListing = resolve })
        : Promise.resolve(listing),
    )
    vi.mocked(awsControlApi.driveSearch).mockResolvedValue({
      results: [{ key: 'docs/deep/report.pdf', size: 55, modified: '2026-09-01T00:00:00Z' }],
      capped: false,
      limit: 200,
    })
    const scrolled = vi.fn()
    const original = Element.prototype.scrollIntoView
    Element.prototype.scrollIntoView = scrolled
    onTestFinished(() => { Element.prototype.scrollIntoView = original })
    await renderDrive('drive')

    fireEvent.change(screen.getByTestId('drive-search-input'), { target: { value: 'report' } })
    await screen.findByTestId('drive-search-hit')
    await chooseFromMenu(screen.getByTestId('drive-search-more'), 'drive-search-goto')

    // Well past the window with the listing still in flight.
    await vi.advanceTimersByTimeAsync(6000)
    releaseListing({
      files: [{ key: 'docs/deep/report.pdf', size: 55, modified: '2026-09-01T00:00:00Z' }],
      folders: [],
    })
    const rows = await screen.findAllByTestId('drive-file')
    expect(rows[0]).toHaveAttribute('data-highlighted', 'true')
    expect(scrolled).toHaveBeenCalled()

    // And the clock does run once the row is there.
    await vi.advanceTimersByTimeAsync(4500)
    await waitFor(() => expect(screen.getByTestId('drive-file')).not.toHaveAttribute('data-highlighted'))
  })

  it('the search-table editor strips are pinned to the viewport like the folder rows', async () => {
    // The search table is wider (a path column); on a narrow, horizontally
    // scrolled viewport an unpinned strip puts Cancel/Rename off-screen.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue(listing)
    vi.mocked(awsControlApi.driveSearch).mockResolvedValue({
      results: [{ key: 'docs/deep/report.pdf', size: 55, modified: '2026-09-01T00:00:00Z' }],
      capped: false,
      limit: 200,
    })
    await renderDrive('drive')
    fireEvent.change(screen.getByTestId('drive-search-input'), { target: { value: 'report' } })
    await screen.findByTestId('drive-search-hit')

    await chooseFromMenu(screen.getByTestId('drive-search-more'), 'drive-search-rename')
    const strip = (await screen.findByTestId('drive-rename-input')).closest('div')
    expect(strip?.className).toContain('sticky left-0')
    expect(strip?.className).toContain('max-w-[calc(100vw-2.5rem)]')
  })

  it('search hides the folder-scoped write controls and the view toggle along with the crumbs', async () => {
    // With the crumbs gone, Upload and New folder would write into a folder the
    // reader cannot see and show nothing; the view toggle would visibly do
    // nothing because hits are always a table. All three leave with the crumbs
    // and come back when the search is cleared.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue(listing)
    vi.mocked(awsControlApi.driveSearch).mockResolvedValue({ results: [], capped: false, limit: 200 })
    await renderDrive('drive')
    await screen.findAllByTestId('drive-file')
    expect(screen.getByTestId('drive-upload-btn')).toBeTruthy()
    expect(screen.getByTitle('Grid view')).toBeTruthy()

    fireEvent.change(screen.getByTestId('drive-search-input'), { target: { value: 'zzz' } })
    await screen.findByTestId('drive-search-empty')
    expect(screen.queryByTestId('drive-upload-btn')).toBeNull()
    expect(screen.queryByTestId('drive-folder-toggle')).toBeNull()
    expect(screen.queryByTitle('Grid view')).toBeNull()
    expect(screen.queryByTestId('drive-crumbs')).toBeNull()

    fireEvent.click(screen.getByTestId('drive-search-clear'))
    expect(await screen.findByTestId('drive-upload-btn')).toBeTruthy()
    expect(screen.getByTestId('drive-folder-toggle')).toBeTruthy()
    expect(screen.getByTitle('Grid view')).toBeTruthy()
  })

  it('the search box names what it searches: names and paths, never contents', () => {
    // "Search files…" reads as content search; a reader looking for text
    // inside a file would take "No files match." as "the file does not exist".
    // And the match runs over the whole relative key, so a folder segment is
    // a valid query -- the placeholder says so.
    expect(i18nT('apps.awsControl.console.search_files')).toBe('Search by file name or path…')
  })

  it('a file dropped mid-search is swallowed, not uploaded and not navigated to', async () => {
    // The section is this page's only dragover/drop preventDefault. Removing it
    // while searching would hand a dropped OS file to the browser, which opens
    // it and unloads the dashboard; uploading it would land in a hidden folder.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue(listing)
    vi.mocked(awsControlApi.driveSearch).mockResolvedValue({ results: [], capped: false, limit: 200 })
    await renderDrive('drive')
    await screen.findAllByTestId('drive-file')
    fireEvent.change(screen.getByTestId('drive-search-input'), { target: { value: 'zzz' } })
    await screen.findByTestId('drive-search-empty')

    const section = screen.getByTestId('drive-section')
    const file = new File(['x'], 'dropped.txt', { type: 'text/plain' })
    const dataTransfer = {
      types: ['Files'], files: [file] as unknown as FileList, getData: () => '', setData: () => {},
    } as unknown as DataTransfer
    // fireEvent returns dispatchEvent's verdict: false means preventDefault ran.
    expect(fireEvent.dragOver(section, { dataTransfer })).toBe(false)
    expect(fireEvent.drop(section, { dataTransfer })).toBe(false)
    expect(awsControlApi.driveUpload).not.toHaveBeenCalled()
    // And no drop-target ring, which would promise an upload that will not come.
    expect(section.className).not.toContain('ring-accent')
  })

  it('a refinement keeps the previous hits on screen but says a new search is running', async () => {
    // `keepPreviousData` shows "rep"'s rows while "report" walks the section;
    // without a pending signal the stale rows read as the new answer.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue(listing)
    let release: (v: { results: DriveSearchHit[]; capped: boolean; limit: number }) => void = () => {}
    vi.mocked(awsControlApi.driveSearch)
      .mockResolvedValueOnce({
        results: [{ key: 'docs/report.pdf', size: 55, modified: '2026-09-01T00:00:00Z' }],
        capped: false,
        limit: 200,
      })
      .mockImplementationOnce(() => new Promise((resolve) => { release = resolve }))
    await renderDrive('drive')
    fireEvent.change(screen.getByTestId('drive-search-input'), { target: { value: 'rep' } })
    await screen.findByTestId('drive-search-hit')
    expect(screen.queryByTestId('drive-search-pending')).toBeNull()

    fireEvent.change(screen.getByTestId('drive-search-input'), { target: { value: 'report' } })
    await screen.findByTestId('drive-search-pending')
    // The previous hit is still there, dimmed and marked busy, not blanked.
    expect(screen.getByTestId('drive-search-hit')).toBeTruthy()
    expect(screen.getByTestId('drive-search-body')).toHaveAttribute('aria-busy', 'true')

    release({ results: [], capped: false, limit: 200 })
    await screen.findByTestId('drive-search-empty')
    expect(screen.queryByTestId('drive-search-pending')).toBeNull()
    expect(screen.getByTestId('drive-search-body')).not.toHaveAttribute('aria-busy')
  })

  it('a search hit can be renamed in place, against its own folder', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue(listing)
    vi.mocked(awsControlApi.driveSearch).mockResolvedValue({
      results: [{ key: 'docs/deep/report.pdf', size: 55, modified: '2026-09-01T00:00:00Z' }],
      capped: false,
      limit: 200,
    })
    vi.mocked(awsControlApi.driveMove).mockResolvedValue({ moved: true })
    await renderDrive('drive')
    fireEvent.change(screen.getByTestId('drive-search-input'), { target: { value: 'report' } })
    await screen.findByTestId('drive-search-hit')

    await chooseFromMenu(screen.getByTestId('drive-search-more'), 'drive-search-rename')
    const input = await screen.findByTestId('drive-rename-input')
    expect((input as HTMLInputElement).value).toBe('report.pdf')
    fireEvent.change(input, { target: { value: 'final.pdf' } })
    fireEvent.click(screen.getByTestId('drive-rename-save'))
    // The directory is the HIT's, not the open folder's (which is the root).
    await waitFor(() =>
      expect(awsControlApi.driveMove).toHaveBeenCalledWith(ACCOUNT_ID, 'drive', 'docs/deep/report.pdf', 'docs/deep/final.pdf'),
    )
    // The results re-run so the new name (or the hit's disappearance) shows.
    await waitFor(() => expect(awsControlApi.driveSearch).toHaveBeenCalledTimes(2))
  })

  it('a search hit can be shared and deleted without leaving the results', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue(listing)
    vi.mocked(awsControlApi.driveSearch).mockResolvedValue({
      results: [{ key: 'docs/deep/report.pdf', size: 55, modified: '2026-09-01T00:00:00Z' }],
      capped: false,
      limit: 200,
    })
    vi.mocked(awsControlApi.driveDelete).mockResolvedValue({ deleted: true })
    await renderDrive('drive')
    fireEvent.change(screen.getByTestId('drive-search-input'), { target: { value: 'report' } })
    await screen.findByTestId('drive-search-hit')

    await chooseFromMenu(screen.getByTestId('drive-search-more'), 'drive-search-share')
    expect(await screen.findByTestId('share-dialog')).toBeTruthy()
    fireEvent.click(screen.getByTestId('share-close'))
    await waitFor(() => expect(screen.queryByTestId('share-dialog')).toBeNull())

    await chooseFromMenu(screen.getByTestId('drive-search-more'), 'drive-search-delete')
    // The FULL key: two same-named files from different folders can sit side by side here.
    expect(await screen.findByTestId('drive-delete-confirm')).toHaveTextContent('docs/deep/report.pdf')
    fireEvent.click(screen.getByTestId('drive-delete-confirm-action'))
    await waitFor(() => expect(awsControlApi.driveDelete).toHaveBeenCalledWith(ACCOUNT_ID, 'drive', 'docs/deep/report.pdf'))
    // Still in search mode: the results, not the folder view, are what refresh.
    expect(screen.getByTestId('drive-search-input')).toHaveValue('report')
    await waitFor(() => expect(awsControlApi.driveSearch).toHaveBeenCalledTimes(2))
  })

  it('a text preview says when the redactor masked values, so a masked line is not read as the file', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue(listing)
    vi.mocked(awsControlApi.drivePreview).mockResolvedValue({
      content: 'key = [REDACTED]',
      truncated: false,
      redacted: true,
    })
    await renderDrive('drive')
    fireEvent.click((await screen.findAllByTestId('drive-preview-open'))[1])
    await screen.findByTestId('drive-preview-text')
    const notice = screen.getByTestId('drive-preview-redacted')
    expect(notice).toHaveTextContent(i18nT('apps.awsControl.console.preview_redacted'))
    // It changes the meaning of every byte below it, so it is not set in the
    // truncation note's muted small type.
    expect(notice.className).not.toContain('text-muted')
    expect(notice.className).toContain('font-medium')
    expect(screen.queryByTestId('drive-preview-truncated')).toBeNull()
  })

  it('a preview opened on a nested key shows the folder beside the name', async () => {
    // Two same-named hits from different folders are indistinguishable once
    // the dialog is up unless the folder rides along.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'docs/deep/notes.md', size: 20, modified: '2026-09-01T00:00:00Z' }],
      folders: [],
    })
    vi.mocked(awsControlApi.drivePreview).mockResolvedValue({ content: 'x', truncated: false, redacted: false })
    await renderDrive('drive')
    fireEvent.click((await screen.findAllByTestId('drive-preview-open'))[0])
    await screen.findByTestId('drive-preview-text')
    expect(screen.getByTestId('drive-preview-dir')).toHaveTextContent('docs/deep/')
    expect(screen.getByTestId('drive-preview-dialog')).toHaveTextContent('notes.md')
  })

  it('a root-level preview shows no folder affix', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue(listing)
    vi.mocked(awsControlApi.drivePreview).mockResolvedValue({ content: 'x', truncated: false, redacted: false })
    await renderDrive('drive')
    fireEvent.click((await screen.findAllByTestId('drive-preview-open'))[1])
    await screen.findByTestId('drive-preview-text')
    expect(screen.queryByTestId('drive-preview-dir')).toBeNull()
  })

  it('a download that fails from the preview header is reported inside the dialog', async () => {
    // The pane's own notice sits behind the dialog's scrim; from the dialog
    // the only visible outcome would be a tab flashing closed.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue(listing)
    vi.mocked(awsControlApi.drivePreview).mockResolvedValue({ content: 'hi', truncated: false, redacted: false })
    vi.mocked(awsControlApi.driveDownload).mockRejectedValue(new Error('AccessDenied'))
    const fakeTab = { location: { href: '' }, close: vi.fn() } as unknown as Window
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(fakeTab)
    await renderDrive('drive')

    fireEvent.click((await screen.findAllByTestId('drive-preview-open'))[1])
    await screen.findByTestId('drive-preview-text')
    fireEvent.click(screen.getByTestId('drive-preview-download'))
    await waitFor(() => expect(fakeTab.close).toHaveBeenCalled())
    const notice = await screen.findByTestId('drive-preview-download-error')
    expect(notice).toHaveTextContent(i18nT('apps.awsControl.console.download_failed'))
    // Inside the dialog, not only in the pane behind it.
    expect(screen.getByTestId('drive-preview-dialog').contains(notice)).toBe(true)
    openSpy.mockRestore()
  })

  it("another file's download failure does not appear inside a preview", async () => {
    // The pane's error is keyed by object: a lingering failure for photo.png
    // must not read as notes.md's download having failed.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue(listing)
    vi.mocked(awsControlApi.drivePreview).mockResolvedValue({ content: 'hi', truncated: false, redacted: false })
    vi.mocked(awsControlApi.driveDownload).mockRejectedValue(new Error('AccessDenied'))
    const fakeTab = { location: { href: '' }, close: vi.fn() } as unknown as Window
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(fakeTab)
    await renderDrive('drive')

    // Fail a download from photo.png's row menu (first file row)...
    await chooseFromMenu((await screen.findAllByTestId('drive-more'))[0], 'drive-download')
    expect(await screen.findByTestId('drive-download-error')).toBeTruthy()
    // ...then preview notes.md: the pane still shows it, the dialog does not.
    fireEvent.click(screen.getAllByTestId('drive-preview-open')[1])
    await screen.findByTestId('drive-preview-text')
    expect(screen.queryByTestId('drive-preview-download-error')).toBeNull()
    openSpy.mockRestore()
  })

  it('changing the query closes a rename editor that was open on a hit', async () => {
    // The hit row unmounts with the old results; a rename left open there
    // would vanish with the half-typed name and keep suppressing the agent
    // hand-off on every later notice.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue(listing)
    vi.mocked(awsControlApi.driveSearch).mockResolvedValue({
      results: [{ key: 'docs/deep/report.pdf', size: 55, modified: '2026-09-01T00:00:00Z' }],
      capped: false,
      limit: 200,
    })
    await renderDrive('drive')
    fireEvent.change(screen.getByTestId('drive-search-input'), { target: { value: 'report' } })
    await screen.findByTestId('drive-search-hit')
    await chooseFromMenu(screen.getByTestId('drive-search-more'), 'drive-search-rename')
    await screen.findByTestId('drive-rename-input')

    fireEvent.change(screen.getByTestId('drive-search-input'), { target: { value: 'rep' } })
    await waitFor(() => expect(screen.queryByTestId('drive-rename-input')).toBeNull())
    // And it stays closed once the new results render.
    await waitFor(() => expect(awsControlApi.driveSearch).toHaveBeenCalledWith(ACCOUNT_ID, 'drive', 'rep'))
    expect(screen.queryByTestId('drive-rename-row')).toBeNull()
  })

  it('a .pdf served as octet-stream routes to the unsupported fallback instead of a blank frame', async () => {
    // Objects uploaded before content types were set are served as
    // octet-stream; the sandboxed iframe (no allow-downloads) renders nothing
    // and fires no error, so the stored type decides the branch.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'legacy.pdf', size: 2048, modified: '2026-09-01T00:00:00Z' }],
      folders: [],
    })
    vi.mocked(awsControlApi.driveDownload).mockResolvedValue({
      url: 'https://signed/legacy', expiresSecs: 60, contentType: 'application/octet-stream',
    })
    await renderDrive('drive')
    fireEvent.click((await screen.findAllByTestId('drive-preview-open'))[0])
    expect(await screen.findByTestId('drive-preview-fallback')).toHaveTextContent(
      i18nT('apps.awsControl.console.preview_unsupported'),
    )
    expect(screen.queryByTestId('drive-preview-pdf')).toBeNull()
  })

  it('clearing the search restores the folder view without waiting for the debounce', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue(listing)
    vi.mocked(awsControlApi.driveSearch).mockResolvedValue({ results: [], capped: false, limit: 200 })
    await renderDrive('drive')
    fireEvent.change(screen.getByTestId('drive-search-input'), { target: { value: 'zzz' } })
    await screen.findByTestId('drive-search-empty')

    fireEvent.click(screen.getByTestId('drive-search-clear'))
    // Synchronous: no 300 ms of stale hits under an empty box.
    expect(screen.queryByTestId('drive-search-results')).toBeNull()
    expect(screen.getByTestId('drive-upload-btn')).toBeTruthy()
  })

  it('a bad name typed into the rename editor gets rename wording, not "rename it and try again"', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue(listing)
    await renderDrive('drive')
    await chooseFromMenu((await screen.findAllByTestId('drive-more'))[1], 'drive-rename')
    const input = await screen.findByTestId('drive-rename-input')
    fireEvent.change(input, { target: { value: 'bad/name.md' } })
    fireEvent.click(screen.getByTestId('drive-rename-save'))
    const err = await screen.findByTestId('drive-rename-error')
    expect(err).toHaveTextContent(i18nT('apps.awsControl.console.rename_bad_name'))
    expect(err).not.toHaveTextContent(i18nT('apps.awsControl.console.drive_bad_name'))
    expect(awsControlApi.driveMove).not.toHaveBeenCalled()
  })

  it('the empty search state echoes the query, like the account search does', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue(listing)
    vi.mocked(awsControlApi.driveSearch).mockResolvedValue({ results: [], capped: false, limit: 200 })
    await renderDrive('drive')
    fireEvent.change(screen.getByTestId('drive-search-input'), { target: { value: 'zzz' } })
    /* The shared `FilteredEmpty` draws this now, so the sentence is the
       primitive's ("No Files match “zzz”") rather than this app's own
       `search_no_results`. What the test pins is unchanged and is the whole
       point of the state: the query is echoed back, and the way OUT of it is
       offered right there rather than only in the toolbar. */
    const empty = await screen.findByTestId('drive-search-empty')
    expect(empty).toHaveTextContent('zzz')
    expect(empty).toHaveTextContent(i18nT('components.ui.no_noun_match', {
      noun: i18nT('apps.awsControl.console.section_files'),
    }))
    fireEvent.click(screen.getByTestId('drive-search-empty-clear'))
    expect((screen.getByTestId('drive-search-input') as HTMLInputElement).value).toBe('')
  })
})

describe('DrivePage sections: move without a pointer', () => {
  const listing = { files: [{ key: 'report.pdf', size: 10, modified: '2026-09-01T00:00:00Z' }], folders: ['docs', 'archive'] }

  it('the row menu offers Move to folder…, and the picker lists the folders a drop could reach', async () => {
    // Drag is a convention; the menu is the path every reader can reach. The
    // destinations are exactly the folders on screen: at the top level there
    // is no "up", so only the two sub-folders are offered.
    vi.mocked(awsControlApi.driveList).mockResolvedValue(listing)
    vi.mocked(awsControlApi.driveMove).mockResolvedValue({ moved: true })
    await renderDrive('drive')

    await chooseFromMenu(await screen.findByTestId('drive-more'), 'drive-move')
    const dialog = await screen.findByTestId('move-dialog')
    expect(dialog).toHaveTextContent('report.pdf')
    expect(within(dialog).queryByTestId('move-root')).toBeNull()
    expect(within(dialog).queryByTestId('move-up')).toBeNull()
    const folders = within(dialog).getAllByTestId('move-folder')
    expect(folders.map((b) => b.getAttribute('data-folder'))).toEqual(['docs', 'archive'])

    fireEvent.click(folders[1])
    await waitFor(() =>
      expect(awsControlApi.driveMove).toHaveBeenCalledWith(ACCOUNT_ID, 'drive', 'report.pdf', 'archive/report.pdf'),
    )
    // Success closes the picker.
    await waitFor(() => expect(screen.queryByTestId('move-dialog')).toBeNull())
  })

  it('inside a folder the picker offers the top level and the parent, and a refusal keeps it open', async () => {
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'a/b/report.pdf', size: 10, modified: '2026-09-01T00:00:00Z' }], folders: [],
    })
    vi.mocked(awsControlApi.driveMove).mockRejectedValue(new AwsControlError('destination_exists', 409))
    // Land two folders deep the way the listing does: one folder value is
    // relative to the section root, so opening `a/b` is one click.
    vi.mocked(awsControlApi.driveList).mockResolvedValueOnce({ files: [], folders: ['a/b'] })
    await renderDrive('drive')
    fireEvent.click(await screen.findByTestId('drive-folder-open'))
    await screen.findByTestId('drive-file')

    await chooseFromMenu(screen.getByTestId('drive-more'), 'drive-move')
    const dialog = await screen.findByTestId('move-dialog')
    expect(within(dialog).getByTestId('move-root').getAttribute('data-folder')).toBe('')
    expect(within(dialog).getByTestId('move-up').getAttribute('data-folder')).toBe('a')
    expect(within(dialog).getByTestId('move-up')).toHaveTextContent('a')

    fireEvent.click(within(dialog).getByTestId('move-root'))
    await waitFor(() =>
      expect(awsControlApi.driveMove).toHaveBeenCalledWith(ACCOUNT_ID, 'drive', 'a/b/report.pdf', 'report.pdf'),
    )
    // The conflict is reported IN the dialog, which stays open for another pick;
    // the page-level move strip does not double it.
    expect(await within(dialog).findByTestId('move-error')).toHaveTextContent(
      i18nT('apps.awsControl.console.move_conflict'),
    )
    expect(screen.queryByTestId('drive-move-error')).toBeNull()
  })

  it('a file at the top level with no folders is told there is nowhere to move it', async () => {
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: listing.files, folders: [] })
    await renderDrive('drive')

    await chooseFromMenu(await screen.findByTestId('drive-more'), 'drive-move')
    expect(await screen.findByTestId('move-no-folders')).toBeTruthy()
    expect(screen.queryByTestId('move-options')).toBeNull()
  })

  it('the source row dims and reports busy while its move is in flight, and only then', async () => {
    vi.mocked(awsControlApi.driveList).mockResolvedValue(listing)
    let finish: (v: { moved: boolean }) => void = () => {}
    vi.mocked(awsControlApi.driveMove).mockReturnValue(
      new Promise<{ moved: boolean }>((resolve) => { finish = resolve }),
    )
    await renderDrive('drive')

    const row = await screen.findByTestId('drive-file')
    expect(row.getAttribute('aria-busy')).toBeNull()
    await chooseFromMenu(screen.getByTestId('drive-more'), 'drive-move')
    fireEvent.click((await screen.findAllByTestId('move-folder'))[0])

    await waitFor(() => expect(screen.getByTestId('drive-file').getAttribute('aria-busy')).toBe('true'))
    expect(screen.getByTestId('drive-file').className).toContain('opacity-50')
    expect(screen.getByTestId('move-pending')).toBeTruthy()
    // Escape mid-move dismisses the picker without cancelling anything: a slow
    // or hung copy must never trap the reader in a modal. The row stays busy
    // until the move lands, so the outcome is still visible on the page.
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('move-dialog')).toBeNull())
    expect(screen.getByTestId('drive-file').getAttribute('aria-busy')).toBe('true')
    // ...and not by opacity alone: with the picker gone the row itself says so.
    expect(within(screen.getByTestId('drive-file')).getByTestId('drive-moving').textContent).toBe('Moving…')

    finish({ moved: true })
    await waitFor(() => expect(screen.getByTestId('drive-file').getAttribute('aria-busy')).toBeNull())
    expect(screen.queryByTestId('drive-moving')).toBeNull()
  })

  it('one move at a time: while a move runs, Move to folder… is disabled and a drop is ignored', async () => {
    // Serialization is the contract: the busy marker, the refusal, and the
    // picker's close all belong to the single move in flight, so no second
    // move may start until it settles.
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [
        { key: 'first.pdf', size: 10, modified: '2026-09-01T00:00:00Z' },
        { key: 'second.pdf', size: 10, modified: '2026-09-01T00:00:00Z' },
      ],
      folders: ['docs'],
    })
    let finish: (v: { moved: boolean }) => void = () => {}
    vi.mocked(awsControlApi.driveMove).mockReturnValue(
      new Promise<{ moved: boolean }>((resolve) => { finish = resolve }),
    )
    await renderDrive('drive')
    const rows = await screen.findAllByTestId('drive-file')
    expect(rows[1].getAttribute('draggable')).toBe('true')

    await chooseFromMenu(within(rows[0]).getByTestId('drive-more'), 'drive-move')
    fireEvent.click((await screen.findAllByTestId('move-folder'))[0])
    await waitFor(() => expect(screen.getAllByTestId('drive-file')[0].getAttribute('aria-busy')).toBe('true'))
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('move-dialog')).toBeNull())

    // The other row can neither be dragged nor sent to the picker.
    expect(screen.getAllByTestId('drive-file')[1].getAttribute('draggable')).toBe('false')
    fireEvent.keyDown(within(screen.getAllByTestId('drive-file')[1]).getByTestId('drive-more'), { key: 'Enter' })
    const item = await screen.findByTestId('drive-move')
    expect(item.getAttribute('data-disabled')).not.toBeNull()
    fireEvent.click(item)
    expect(screen.queryByTestId('move-dialog')).toBeNull()
    expect(awsControlApi.driveMove).toHaveBeenCalledTimes(1)

    finish({ moved: true })
    await waitFor(() => expect(screen.getAllByTestId('drive-file')[0].getAttribute('aria-busy')).toBeNull())
    // The gate reopens with the move.
    await waitFor(() => expect(screen.getAllByTestId('drive-file')[1].getAttribute('draggable')).toBe('true'))
  })

  it('a move refused after the picker was dismissed is reported on the page strip instead', async () => {
    vi.mocked(awsControlApi.driveList).mockResolvedValue(listing)
    let fail: (e: unknown) => void = () => {}
    vi.mocked(awsControlApi.driveMove).mockReturnValue(
      new Promise<{ moved: boolean }>((_resolve, reject) => { fail = reject }),
    )
    await renderDrive('drive')

    await chooseFromMenu(await screen.findByTestId('drive-more'), 'drive-move')
    fireEvent.click((await screen.findAllByTestId('move-folder'))[0])
    await screen.findByTestId('move-pending')
    fireEvent.click(screen.getByTestId('move-close'))
    await waitFor(() => expect(screen.queryByTestId('move-dialog')).toBeNull())

    fail(new AwsControlError('destination_exists', 409))
    expect(await screen.findByTestId('drive-move-error')).toHaveTextContent(
      i18nT('apps.awsControl.console.move_conflict'),
    )
    // The picker did not come back to report it.
    expect(screen.queryByTestId('move-dialog')).toBeNull()
  })
})

describe('DrivePage sections: keyboard paths and honest copy', () => {
  it('focus returns to the row menu even when the pointer opened it without focusing it (Safari)', async () => {
    // Safari does not focus a button on pointer click, so at open time
    // `document.activeElement` is still whatever had focus before. The opener
    // must be taken from the trigger's own event, not from the active element.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'report.pdf', size: 2048, modified: '2026-08-20T00:00:00Z' }], folders: [],
    })
    await renderDrive('drive')

    const trigger = await screen.findByTestId('drive-more')
    const elsewhere = screen.getByTestId('drive-folder-toggle')
    elsewhere.focus()
    // Pointer open: the button is NOT focused first, exactly as Safari leaves it.
    fireEvent.pointerDown(trigger, { pointerType: 'mouse', button: 0, ctrlKey: false })
    fireEvent.click(await screen.findByTestId('drive-share'))
    await screen.findByTestId('share-dialog')

    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('share-dialog')).toBeNull())
    expect(document.activeElement).toBe(trigger)
  })

  it('the share dialog closes on Escape and returns focus to the row menu, but not mid-mint', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'report.pdf', size: 2048, modified: '2026-08-20T00:00:00Z' }], folders: [],
    })
    let finish: (v: { url: string; share: Share }) => void = () => {}
    vi.mocked(awsControlApi.driveShare).mockReturnValue(
      new Promise<{ url: string; share: Share }>((resolve) => { finish = resolve }),
    )
    await renderDrive('drive')

    const trigger = await screen.findByTestId('drive-more')
    trigger.focus()
    await chooseFromMenu(trigger, 'drive-share')
    const dialog = await screen.findByTestId('share-dialog')
    // The panel is the dialog and holds focus; the scrim is presentational.
    expect(dialog.getAttribute('role')).toBe('dialog')
    // `useDialogFocusTrap` moves focus in a passive effect, one tick after the
    // commit that `findByTestId` resolved on, so "holds focus" is a condition
    // to wait for, not a property of the first frame the dialog is in the DOM.
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true))

    // Escape while the link is being created is refused.
    fireEvent.click(screen.getByTestId('share-create'))
    await waitFor(() =>
      expect(screen.getByTestId('share-create')).toHaveTextContent(i18nT('apps.awsControl.console.share_creating')),
    )
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.getByTestId('share-dialog')).toBeTruthy()

    finish({
      url: 'https://example-presigned/report.pdf?sig=x',
      share: {
        id: 's1', account: ACCOUNT_ID, section: 'drive', key: 'report.pdf',
        createdAt: '2026-08-24T05:00:00Z', expiresAt: '2026-08-24T06:00:00Z', note: '',
      },
    })
    await screen.findByTestId('share-result')
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('share-dialog')).toBeNull())
    // Focus went back to where the reader was, not to <body>.
    expect(document.activeElement).toBe(trigger)
  })

  it('the deleted-count line is a real plural, not a parenthetical', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: ['one', 'many'] })
    vi.mocked(awsControlApi.driveFolderDelete)
      .mockResolvedValueOnce({ deleted: true, path: 'one', objects: 1 })
      .mockResolvedValueOnce({ deleted: true, path: 'many', objects: 12 })
    await renderDrive('drive')
    await screen.findByTestId('drive-listing')

    const deleteFolder = async (index: number) => {
      const rows = screen.getAllByTestId('drive-folder')
      fireEvent.keyDown(within(rows[index]).getByTestId('drive-folder-more'), { key: 'Enter' })
      fireEvent.click(await screen.findByTestId('drive-folder-delete'))
      fireEvent.click(await screen.findByTestId('drive-folder-delete-action'))
    }
    await deleteFolder(0)
    expect(await screen.findByTestId('drive-folder-deleted')).toHaveTextContent(
      i18nT('apps.awsControl.console.folder_deleted', { count: 1, name: 'one' }),
    )
    expect(screen.getByTestId('drive-folder-deleted').textContent).not.toMatch(/\(s\)/)
    await deleteFolder(1)
    await waitFor(() =>
      expect(screen.getByTestId('drive-folder-deleted')).toHaveTextContent(
        i18nT('apps.awsControl.console.folder_deleted', { count: 12, name: 'many' }),
      ),
    )
  })

  it('closing the folder disclosure hands focus back to the New folder button', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: [] })
    await renderDrive('drive')

    const toggle = await screen.findByTestId('drive-folder-toggle')
    toggle.focus()
    fireEvent.click(toggle)
    const input = await screen.findByTestId('drive-folder-name')
    expect(document.activeElement).toBe(input)

    fireEvent.keyDown(input, { key: 'Escape' })
    // The disclosure unmounted the input and both buttons; focus must not fall
    // to <body> and force a keyboard reader to re-tab from the page top.
    await waitFor(() => expect(document.activeElement).toBe(screen.getByTestId('drive-folder-toggle')))
  })

  it('an EMPTY library says so, distinct from a search that matched nothing', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: [] })
    vi.mocked(awsControlApi.library).mockResolvedValue({ artifacts: [] })
    await renderDrive('library')

    fireEvent.click(await screen.findByTestId('library-add-open'))
    await screen.findByTestId('library-add-dialog')
    // Nothing was searched, so "no artifacts match" would be a lie about a
    // search that never ran.
    expect(await screen.findByTestId('library-add-empty')).toHaveTextContent(
      i18nT('apps.awsControl.console.library_add_empty'),
    )
    expect(screen.queryByTestId('library-add-none')).toBeNull()
    // The cost disclosure reads in body tone, not as the dialog's quietest line.
    expect(screen.getByTestId('library-add-oneway').className).toContain('text-text')
    expect(screen.getByTestId('library-add-oneway').className).not.toContain('text-muted')
  })

  it('the pinned Actions column paints the page surface, not a card, on the borderless drive table', async () => {
    // `--card` and `--bg` differ in every theme; a `bg-card` pin inside a table
    // with no card fill is a permanently tinted right-hand stripe.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'report.pdf', size: 10, modified: '2026-09-01T00:00:00Z' }], folders: ['docs'],
    })
    await renderDrive('drive')

    const listing = await screen.findByTestId('drive-listing')
    const pinned = Array.from(listing.querySelectorAll('th, td')).filter((c) => c.className.includes('sticky'))
    expect(pinned.length).toBe(3)
    for (const cell of pinned) {
      expect(cell.className).toContain('bg-bg')
      expect(cell.className).not.toMatch(/\bbg-card\b/)
    }

    // The search-results table sits on the same page surface; its pinned
    // header must not keep the card default the folder table just dropped.
    vi.mocked(awsControlApi.driveSearch).mockResolvedValue({
      results: [{ key: 'docs/report.pdf', size: 10, modified: '2026-09-01T00:00:00Z' }], capped: false, limit: 200,
    })
    fireEvent.change(screen.getByTestId('drive-search-input'), { target: { value: 'report' } })
    const table = await screen.findByTestId('drive-search-table')
    const pinnedHead = Array.from(table.querySelectorAll('th')).filter((c) => c.className.includes('sticky'))
    expect(pinnedHead.length).toBe(1)
    expect(pinnedHead[0].className).toContain('bg-bg')
    expect(pinnedHead[0].className).not.toMatch(/\bbg-card\b/)
  })

  it('the share dialog opened from a search hit returns focus to that hit\u2019s menu', async () => {
    // Third ShareDialog opener. Without remembering itself, closing would hand
    // focus to whichever folder-row trigger was remembered earlier -- or nowhere.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'report.pdf', size: 2048, modified: '2026-08-20T00:00:00Z' }], folders: [],
    })
    vi.mocked(awsControlApi.driveSearch).mockResolvedValue({
      results: [{ key: 'docs/deep/report.pdf', size: 55, modified: '2026-09-01T00:00:00Z' }], capped: false, limit: 200,
    })
    await renderDrive('drive')
    await screen.findByTestId('drive-file')
    fireEvent.change(screen.getByTestId('drive-search-input'), { target: { value: 'report' } })
    const trigger = await screen.findByTestId('drive-search-more')

    fireEvent.pointerDown(trigger, { pointerType: 'mouse', button: 0, ctrlKey: false })
    fireEvent.click(await screen.findByTestId('drive-search-share'))
    await screen.findByTestId('share-dialog')

    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('share-dialog')).toBeNull())
    expect(document.activeElement).toBe(trigger)
  })
})
