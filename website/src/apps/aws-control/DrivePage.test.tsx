/**
 * DrivePage's section panes: share links, delete confirms, the shares ledger, backup row
 * states and refusal wording, Files folder navigation, the new-folder disclosure, downloads
 * and uploads.
 *
 * The suites split from this file sit beside it: DrivePage.library, .backup, .fileActions,
 * .errorSurfaces and .previewSearch (each a `.test.tsx`). Fixtures and mount helpers they
 * share with this file are in src/test/DrivePageTestUtils.tsx; each suite keeps its own
 * vi.mock blocks and top-level beforeEach.
 */
import { describe, it, expect, vi, beforeEach, onTestFinished } from 'vitest'
import { screen, fireEvent, waitFor, within, act } from '@testing-library/react'
import { i18nT } from '../../i18n/t'
import type {
  BackupStatus,
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
      backupNightlySessions: vi.fn(),
      backupRestore: vi.fn(),
      installLabel: vi.fn(),
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

/* The preview pane renders code through the dashboard's shared ContentRenderer,
 * whose code surface is Pierre -- and Pierre's real chunk never resolves under
 * vitest. Stub the mount so "the code viewer got the bytes" is assertable here;
 * its chrome is Playwright's to check. */
vi.mock('../../pierre', () => ({
  PierreCode: ({ file }: { file: { contents: string } }) => (
    <div data-testid="pierre-mounted">{file.contents}</div>
  ),
}))

import { awsControlApi } from './api'
import { api } from '../../api/client'
import {
  ACCOUNT_ID, chooseFromMenu, emptyBackup, renderDrive, stubDrivePresent,
} from '../../test/DrivePageTestUtils'

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
  // Reset, not clear: a clear keeps each mock's persistent and queued-once
  // implementations, so a case that stubs a running backup (or holds a read in
  // flight) would hand that state to whichever case runs next, and a case that
  // seeds nothing would pass only after one that did. Every case starts from
  // the same present, empty drive instead.
  for (const group of [awsControlApi, api] as unknown as Array<Record<string, unknown>>) {
    for (const fn of Object.values(group)) {
      if (vi.isMockFunction(fn)) fn.mockReset()
    }
  }
  stubDrivePresent()
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
    // Restored even if an assertion below throws, so no later test opens a dead tab.
    onTestFinished(() => openSpy.mockRestore())

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
    // Restored even if an assertion below throws, so no later test opens a dead tab.
    onTestFinished(() => openSpy.mockRestore())

    await renderDrive('drive')

    await chooseFromMenu(await screen.findByTestId('drive-more'), 'drive-download')
    await waitFor(() => expect(fakeTab.close).toHaveBeenCalled())
    // The tab was never navigated anywhere.
    expect((fakeTab as unknown as { location: { href: string } }).location.href).toBe('')
    // And the failure is REPORTED. Rethrowing here would surface as an
    // unhandled rejection from an onClick with no catch, which tells the user
    // nothing; the row must say the download did not start.
    expect(await screen.findByTestId('drive-download-error')).toBeTruthy()
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
