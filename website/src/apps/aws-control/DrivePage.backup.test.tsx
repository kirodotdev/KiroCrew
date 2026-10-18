/**
 * DrivePage suites: the Backup pane (runs, the nightly toggle, the stored-archive list and
 * restores, per-install attribution), plus one Access and one CLI-drawer case.
 * DrivePage.test.tsx names the suites split from it; shared fixtures are in
 * src/test/DrivePageTestUtils.tsx.
 */
import { describe, it, expect, vi, beforeEach, onTestFinished } from 'vitest'
import { screen, fireEvent, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import { i18nT } from '../../i18n/t'
import type {
  BackupStatus, RemoteBackup,
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

import { awsControlApi, AwsControlError } from './api'
import { api } from '../../api/client'
import {
  BackupSection,
} from './DrivePage'
import {
  ACCOUNT_ID, emptyBackup, emptyRemote, INSTALL_ID, renderDrive, stubDrivePresent,
} from '../../test/DrivePageTestUtils'

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

describe('DrivePage sections: backup, access, CLI drawer', () => {
  it('runs a backup, toggles nightly, and both invalidate through the api', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.backup).mockResolvedValue({
      nightly: false,
      runs: { snapshot: { key: 'snap-1', bytes: 1024, at: '2026-08-24T00:00:00Z' } },
      install: { id: INSTALL_ID, label: 'This Mac' },
      remote: emptyRemote,
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

  it('discloses the stored-backups archive and restores a self-owned file, showing the staged path', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.backup).mockResolvedValue({
      nightly: true,
      runs: {},
      install: { id: INSTALL_ID, label: 'This Mac' },
      remote: {
        snapshot: [{ key: 'snapshots/' + INSTALL_ID + '/2026-08-24.tar', size: 4096, modified: '2026-08-24T00:00:00Z', install: INSTALL_ID, origin: 'self' }],
        sessions: [],
        installs: [{ id: INSTALL_ID, label: 'This Mac', origin: 'self' }],
        others: 0,
        truncated: false,
        max: 8,
      },
    })
    vi.mocked(awsControlApi.backupRestore).mockResolvedValue({
      downloaded: true, path: '/home/u/.kiro/restore/2026-08-24', bytes: 4096, origin: 'self', install: INSTALL_ID,
    })

    await renderDrive('backup')

    // The archive is behind a disclosure; before opening, no rows are shown.
    fireEvent.click(await screen.findByTestId('backup-remote-toggle'))
    const archive = await screen.findByTestId('backup-archive')
    expect(within(archive).getByTestId('backup-archive-row')).toHaveTextContent('2026-08-24.tar')
    // The write-only-tier restore caveat renders alongside.
    expect(screen.getByTestId('backup-restore-caveat')).toBeTruthy()

    // A self-owned row restores at once, with no confirm and no foreignOk.
    fireEvent.click(within(archive).getByTestId('backup-restore'))
    expect(screen.queryByTestId('backup-restore-confirm')).toBeNull()
    await waitFor(() => expect(awsControlApi.backupRestore).toHaveBeenCalledWith(ACCOUNT_ID, 'snapshots/' + INSTALL_ID + '/2026-08-24.tar', undefined))
    expect(await screen.findByTestId('backup-restored')).toHaveTextContent('/home/u/.kiro/restore/2026-08-24')
  })

  it('distinguishes "no run recorded here" from "no archive kept" when archives are remembered', async () => {
    // #13566 item 2: a row with no run record but a nonzero recorded count showed
    // "Not backed up yet" beside "Archives this install has recorded: N", which
    // reads as a contradiction -- the ledger keeps one run record per kind and
    // holds only this install's, so a record can age out while archives stay on
    // the drive. The meta line must then say which kind of "none" it means.
    vi.mocked(awsControlApi.backup).mockResolvedValue({
      ...emptyBackup,
      runs: {},
      rememberedArchives: { snapshot: 3 },
    })

    await renderDrive('backup')

    const meta = await screen.findByTestId('backup-last-snapshot')
    expect(meta.textContent ?? '').toContain('No run recorded here')
    expect(meta.textContent ?? '').not.toContain('Not backed up yet')
    // The count line still invites the reader to the list that says what is there.
    expect(await screen.findByTestId('backup-remembered-snapshot')).toBeTruthy()
    // A kind with nothing recorded keeps the plain line -- the clause is only for
    // the contradiction.
    expect((screen.getByTestId('backup-last-sessions').textContent ?? '')).toContain('Not backed up yet')
  })

  it('scrolls the stored-archive disclosure into view when the recorded-count line is clicked', async () => {
    // #13566 item 3: clicking the count line opened the disclosure but left it
    // off-screen on a long pane, so the reader asked to see the archives and the
    // view did not move. jsdom has no scrollIntoView; install one for the
    // assertion and restore it so nothing leaks into the next test.
    const scrolled = vi.fn()
    const original = Element.prototype.scrollIntoView
    Element.prototype.scrollIntoView = scrolled
    onTestFinished(() => { Element.prototype.scrollIntoView = original })
    // requestAnimationFrame drives the deferred scroll; run it synchronously.
    const raf = vi.spyOn(window, 'requestAnimationFrame').mockImplementation((cb) => { cb(0); return 0 })
    onTestFinished(() => raf.mockRestore())

    vi.mocked(awsControlApi.backup).mockResolvedValue({
      ...emptyBackup,
      runs: {},
      rememberedArchives: { snapshot: 2 },
    })

    await renderDrive('backup')

    fireEvent.click(await screen.findByTestId('backup-remembered-snapshot'))
    // The disclosure opened AND it was scrolled to.
    expect(await screen.findByTestId('backup-archive')).toBeTruthy()
    expect(scrolled).toHaveBeenCalled()
  })

  it('re-scrolls to the archive list once the remote rows arrive after the click', async () => {
    // #13566 item 3, the reviewer's path: on the FIRST open `setShowRemote(true)`
    // changes the query key and `placeholderData` keeps the prior response, which
    // has no `remote`, so the reveal scroll runs against skeleton height and the
    // rows that load afterwards grow below the viewport. The fix re-scrolls once
    // `data.remote` resolves while a reveal is pending. Simulated here by a first
    // response with no remote rows and a second (the `remote: true` refetch) that
    // carries them: scrollIntoView must fire again after the rows land.
    const scrolled = vi.fn()
    const original = Element.prototype.scrollIntoView
    Element.prototype.scrollIntoView = scrolled
    onTestFinished(() => { Element.prototype.scrollIntoView = original })
    const raf = vi.spyOn(window, 'requestAnimationFrame').mockImplementation((cb) => { cb(0); return 0 })
    onTestFinished(() => raf.mockRestore())

    vi.mocked(awsControlApi.backup)
      // First load: remembered count is known, but the remote half is not fetched
      // yet (it is opt-in behind the disclosure), so `remote` is null.
      .mockResolvedValueOnce({
        ...emptyBackup,
        runs: {},
        remote: null,
        rememberedArchives: { snapshot: 2 },
      })
      // After the click flips `showRemote`, the refetch returns the rows.
      .mockResolvedValue({
        ...emptyBackup,
        runs: {},
        remote: { ...emptyRemote, snapshot: [{ key: 'snap-1', size: 1024, modified: '2026-08-20T00:00:00Z', install: INSTALL_ID, origin: 'self' }] },
        rememberedArchives: { snapshot: 2 },
      })

    await renderDrive('backup')

    fireEvent.click(await screen.findByTestId('backup-remembered-snapshot'))
    // The rows arrive on the refetch; the effect must re-scroll once they do.
    await screen.findByTestId('backup-archive')
    await waitFor(() => expect(scrolled.mock.calls.length).toBeGreaterThan(1))
  })

  it('does not scroll on a later refetch when the list was already open before the count-line click', async () => {
    // Ray's blocking review on #17702: `revealRemote` used to set the pending
    // flag unconditionally. If the reader opened the list with the disclosure
    // toggle (so `data.remote` is already loaded) and THEN clicked the count
    // line, the `[data?.remote]` effect never ran -- `data.remote` had not
    // changed -- so the flag stayed set. The next refetch that changed
    // `data.remote` by reference (a poll, an `invalidate`, or flipping the
    // co-tenant toggle) then scrolled the pane into view unasked. The fix sets
    // the flag only when the rows are not loaded yet, so this path leaves it
    // unset and the later refetch does not scroll.
    const scrolled = vi.fn()
    const original = Element.prototype.scrollIntoView
    Element.prototype.scrollIntoView = scrolled
    onTestFinished(() => { Element.prototype.scrollIntoView = original })
    const raf = vi.spyOn(window, 'requestAnimationFrame').mockImplementation((cb) => { cb(0); return 0 })
    onTestFinished(() => raf.mockRestore())

    // Every call returns a FRESH `remote` object so the refetch triggered by the
    // co-tenant toggle changes `data.remote` by reference -- exactly the fact the
    // clearing effect keys on. The rows are present from the first load, so when
    // the count line is clicked the list is already open.
    const row = { key: 'snap-1', size: 1024, modified: '2026-08-20T00:00:00Z', install: INSTALL_ID, origin: 'self' as const }
    vi.mocked(awsControlApi.backup).mockImplementation(async () => ({
      ...emptyBackup,
      runs: {},
      remote: { ...emptyRemote, snapshot: [{ ...row }] },
      rememberedArchives: { snapshot: 2 },
    }))

    await renderDrive('backup')

    // Open the list with the DISCLOSURE TOGGLE (not the count line). The toggle
    // does not call `revealRemote`, so nothing has scrolled yet.
    fireEvent.click(await screen.findByTestId('backup-remote-toggle'))
    await screen.findByTestId('backup-archive')
    await waitFor(() => expect(screen.getByTestId('backup-archive-row')).toBeTruthy())
    expect(scrolled).not.toHaveBeenCalled()

    // Click the count line while the list is already open: one immediate scroll,
    // and -- the point of the fix -- the pending flag is NOT set.
    fireEvent.click(screen.getByTestId('backup-remembered-snapshot'))
    await waitFor(() => expect(scrolled).toHaveBeenCalledTimes(1))

    // A later refetch changes `data.remote` by reference. If the flag had leaked,
    // the clearing effect would fire a second scroll here.
    vi.mocked(awsControlApi.backup).mockClear()
    const toggle = within(screen.getByTestId('backup-others-toggle')).getByRole('switch')
    fireEvent.click(toggle)
    await waitFor(() =>
      expect(awsControlApi.backup).toHaveBeenCalledWith(ACCOUNT_ID, { remote: true, others: true }),
    )
    // The refetch has landed a new `remote` object; give the effect a chance to
    // run, then assert it did not scroll.
    await waitFor(() => expect(within(screen.getByTestId('backup-others-toggle')).getByRole('switch')).toBeTruthy())
    expect(scrolled).toHaveBeenCalledTimes(1)
  })

  it('does not scroll on a later successful poll when the reveal fetch failed', async () => {
    // #17702 open sub-case: the reader clicks the count line while the list is
    // not loaded, so `revealRemote` sets the pending flag. The `remote: true`
    // refetch then FAILS -- the backend returns `remote: null` with the reason
    // in `remoteError` -- so the clearing effect's old `if (!data?.remote)
    // return` left the flag set. A later successful poll/invalidate (rows now
    // present) fired the deferred scroll long after the click, yanking the pane
    // into a list the reader never saw. The fix clears the pending flag when the
    // response resolves to a failure, so this later success does not scroll.
    const scrolled = vi.fn()
    const original = Element.prototype.scrollIntoView
    Element.prototype.scrollIntoView = scrolled
    onTestFinished(() => { Element.prototype.scrollIntoView = original })
    const raf = vi.spyOn(window, 'requestAnimationFrame').mockImplementation((cb) => { cb(0); return 0 })
    onTestFinished(() => raf.mockRestore())

    const row = { key: 'snap-1', size: 1024, modified: '2026-08-20T00:00:00Z', install: INSTALL_ID, origin: 'self' as const }
    vi.mocked(awsControlApi.backup)
      // First load: remembered count known, remote half not fetched yet.
      .mockResolvedValueOnce({
        ...emptyBackup,
        runs: {},
        remote: null,
        rememberedArchives: { snapshot: 2 },
      })
      // The `remote: true` refetch the count-line click triggers FAILS: no rows,
      // only the error reason. This is the response that must cancel the pending
      // reveal.
      .mockResolvedValueOnce({
        ...emptyBackup,
        runs: {},
        remote: null,
        remoteError: 'AccessDenied: s3:ListBucket on backup/',
        rememberedArchives: { snapshot: 2 },
      })
      // A later refetch (here forced by flipping the co-tenant toggle, standing
      // in for a poll/invalidate) finally returns the rows. If the flag had
      // leaked past the failure, the clearing effect would scroll here.
      .mockResolvedValue({
        ...emptyBackup,
        runs: {},
        remote: { ...emptyRemote, snapshot: [{ ...row }] },
        rememberedArchives: { snapshot: 2 },
      })

    await renderDrive('backup')

    // Click the count line while the list is not open: `revealRemote` sets the
    // pending flag because `data.remote` is not loaded yet. The reveal scroll
    // fires once immediately against skeleton height.
    fireEvent.click(await screen.findByTestId('backup-remembered-snapshot'))
    await waitFor(() => expect(scrolled).toHaveBeenCalledTimes(1))

    // The failed refetch lands: the error note appears, and the clearing effect
    // drops the pending flag because the response resolved to a failure.
    await screen.findByTestId('backup-remote-error')
    expect(scrolled).toHaveBeenCalledTimes(1)

    // Force a later successful refetch by flipping the co-tenant toggle (a stand
    // -in for a poll/invalidate). The rows arrive and change `data.remote` by
    // reference -- exactly what the clearing effect keys on. Before the fix the
    // leaked flag fired a second scroll here; now it must not.
    const toggle = within(screen.getByTestId('backup-others-toggle')).getByRole('switch')
    fireEvent.click(toggle)
    await screen.findByTestId('backup-archive-row')
    expect(scrolled).toHaveBeenCalledTimes(1)
  })

  it('shows the backup remote-error note when the archive could not be read', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.backup).mockResolvedValue({
      nightly: false, runs: {}, install: { id: INSTALL_ID, label: 'This Mac' }, remote: null, remoteError: 'AccessDenied',
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

describe('DrivePage sections: per-install backup attribution', () => {
  const OTHER_ID = 'b'.repeat(32)

  /**
   * A remote half carrying one archive per origin, and an install roster that
   * gives the foreign install a label. `label` on the other row is chosen by the
   * caller so the label vs id split can be exercised.
   */
  function remoteWith(opts?: { otherLabel?: string; others?: number; truncated?: boolean }): RemoteBackup {
    return {
      snapshot: [
        { key: `snapshots/${INSTALL_ID}/self.tar`, size: 10, modified: '2026-09-01T00:00:00Z', install: INSTALL_ID, origin: 'self' },
        { key: `snapshots/${OTHER_ID}/other.tar`, size: 10, modified: '2026-09-01T00:00:00Z', install: OTHER_ID, origin: 'other' },
        { key: 'snapshots/legacy.tar', size: 10, modified: '2026-09-01T00:00:00Z', install: '', origin: 'legacy' },
      ],
      sessions: [],
      installs: [
        { id: INSTALL_ID, label: 'This Mac', origin: 'self' },
        ...(opts?.otherLabel !== undefined ? [{ id: OTHER_ID, label: opts.otherLabel, origin: 'other' as const }] : []),
      ],
      others: opts?.others ?? 1,
      truncated: opts?.truncated ?? false,
      // Served by the backend rather than derived from `installs.length`, which
      // counts THIS install too and would be off by one.
      max: 8,
    }
  }

  function stubBackup(remote: RemoteBackup, install = { id: INSTALL_ID, label: 'This Mac' }) {
    vi.mocked(awsControlApi.backup).mockResolvedValue({
      nightly: false, runs: {}, install, remote,
    } as BackupStatus)
  }

  async function openArchive() {
    await renderDrive('backup')
    fireEvent.click(await screen.findByTestId('backup-remote-toggle'))
    return within(await screen.findByTestId('backup-archive'))
  }

  it('renders the self row as the install label and the self origin', async () => {
    stubDrivePresent()
    stubBackup(remoteWith({ otherLabel: 'Alice laptop' }))
    const archive = await openArchive()
    const rows = archive.getAllByTestId('backup-archive-origin')
    // self row: the catalog's own self shape, with this install's label in it.
    expect(rows[0].textContent).toBe(
      i18nT('apps.awsControl.console.backup_attribution_self', { label: 'This Mac' }),
    )
  })

  it('keeps the attribution caption on every row, not hidden below a viewport width', async () => {
    // The caption is the one thing this whole change adds, so a narrow window
    // must still show it -- it wraps to its own line, it does not disappear.
    // jsdom has no layout, so this pins the CLASS rather than a measured width:
    // a future `hidden`/`sm:inline` pair would silently remove it on a phone.
    stubDrivePresent()
    stubBackup(remoteWith({ otherLabel: 'Alice laptop' }))
    const archive = await openArchive()
    const captions = archive.getAllByTestId('backup-archive-origin')
    expect(captions).toHaveLength(3)
    for (const caption of captions) {
      // The text is really there (not an empty node the layout would show).
      expect(caption.textContent?.trim()).not.toBe('')
      // And nothing hides it at a breakpoint.
      expect(caption.className).not.toMatch(/\bhidden\b/)
      expect(caption.className).not.toMatch(/\bsm:inline\b/)
    }
    // The self row's caption reads as this install, proving it is the real
    // attribution and not a placeholder kept only to satisfy the class check.
    expect(captions[0].textContent).toBe(
      i18nT('apps.awsControl.console.backup_attribution_self', { label: 'This Mac' }),
    )
  })

  it('renders a labelled foreign row with the quoted label, the other origin, and 8 hex', async () => {
    stubDrivePresent()
    stubBackup(remoteWith({ otherLabel: 'Alice laptop' }))
    const archive = await openArchive()
    const rows = archive.getAllByTestId('backup-archive-origin')
    // other + label: the catalog quotes the label, because the string was written
    // by whoever wrote the archive, and names the id beside it.
    expect(rows[1].textContent).toBe(
      i18nT('apps.awsControl.console.backup_attribution_other', {
        label: 'Alice laptop',
        id: OTHER_ID.slice(0, 8),
      }),
    )
    expect(rows[1].textContent).toContain('"Alice laptop"')
  })

  it('renders an unlabelled foreign row as the other origin and 8 hex, no quotes', async () => {
    stubDrivePresent()
    // No roster entry for the other install -> label falls back to the no-label form.
    stubBackup(remoteWith())
    const archive = await openArchive()
    const rows = archive.getAllByTestId('backup-archive-origin')
    expect(rows[1].textContent).toBe(
      i18nT('apps.awsControl.console.backup_attribution_other_unnamed', {
        id: OTHER_ID.slice(0, 8),
      }),
    )
    expect(rows[1].textContent).not.toContain('"')
  })

  it('renders a legacy row as unknown origin', async () => {
    stubDrivePresent()
    stubBackup(remoteWith({ otherLabel: 'Alice laptop' }))
    const archive = await openArchive()
    const rows = archive.getAllByTestId('backup-archive-origin')
    expect(rows[2].textContent).toBe(i18nT('apps.awsControl.console.backup_origin_unknown'))
  })

  it('refuses a foreign restore until confirmed, then sends foreignOk true', async () => {
    stubDrivePresent()
    stubBackup(remoteWith({ otherLabel: 'Alice laptop' }))
    vi.mocked(awsControlApi.backupRestore).mockResolvedValue({
      downloaded: true, path: '/tmp/x', bytes: 10, origin: 'other', install: OTHER_ID,
    })
    const archive = await openArchive()
    const rows = archive.getAllByTestId('backup-archive-row')

    // The foreign row is the second one. Its Restore asks first -- nothing sent.
    fireEvent.click(within(rows[1]).getByTestId('backup-restore'))
    expect(awsControlApi.backupRestore).not.toHaveBeenCalled()
    const confirm = within(rows[1]).getByTestId('backup-restore-confirm')
    // The confirm names the archive's owning id (first 8 hex), not its label.
    expect(confirm.textContent).toContain(OTHER_ID.slice(0, 8))

    fireEvent.click(within(rows[1]).getByTestId('backup-restore-confirm-yes'))
    await waitFor(() =>
      expect(awsControlApi.backupRestore).toHaveBeenCalledWith(ACCOUNT_ID, `snapshots/${OTHER_ID}/other.tar`, true),
    )
  })

  it('confirms a legacy restore, then sends the override like every unproven row', async () => {
    stubDrivePresent()
    stubBackup(remoteWith({ otherLabel: 'Alice laptop' }))
    vi.mocked(awsControlApi.backupRestore).mockResolvedValue({
      downloaded: true, path: '/tmp/x', bytes: 10, origin: 'legacy', install: '',
    })
    const archive = await openArchive()
    const rows = archive.getAllByTestId('backup-archive-row')

    // The legacy row is the third. It asks first...
    fireEvent.click(within(rows[2]).getByTestId('backup-restore'))
    expect(awsControlApi.backupRestore).not.toHaveBeenCalled()
    expect(within(rows[2]).getByTestId('backup-restore-confirm').textContent).toContain(
      i18nT('apps.awsControl.console.backup_restore_unknown_confirm'),
    )

    // ...and confirming sends the override. The backend refuses every origin it
    // cannot PROVE is this install's own, and an archive with no id has an unknown
    // author -- "unknown" includes "somebody else's".
    fireEvent.click(within(rows[2]).getByTestId('backup-restore-confirm-yes'))
    await waitFor(() =>
      expect(awsControlApi.backupRestore).toHaveBeenCalledWith(ACCOUNT_ID, 'snapshots/legacy.tar', true),
    )
  })

  /**
   * An archive under THIS install's own prefix but with no local upload record.
   *
   * The prefix is a folder name any bucket writer can create, so a match on it
   * proves nothing. Such a row is `unverified`, not `self`: it renders the
   * unverified attribution and confirms before it downloads, exactly like a
   * legacy archive -- and, like legacy, it then sends the override, because the
   * backend refuses every origin it cannot prove is this install's own.
   */
  const unverifiedRemote: RemoteBackup = {
    snapshot: [
      { key: `snapshots/${INSTALL_ID}/planted.tar`, size: 10, modified: '2026-09-01T00:00:00Z', install: INSTALL_ID, origin: 'unverified' },
    ],
    sessions: [],
    installs: [{ id: INSTALL_ID, label: 'This Mac', origin: 'self' }],
    others: 0,
    truncated: false,
    max: 8,
  }

  it('renders an unverified row as the unverified attribution, not the self one', async () => {
    stubDrivePresent()
    stubBackup(unverifiedRemote)
    const archive = await openArchive()
    const origin = archive.getByTestId('backup-archive-origin')
    expect(origin.textContent).toBe(
      i18nT('apps.awsControl.console.backup_attribution_unverified'),
    )
    // It must NOT be dressed as this install's own archive, named or unnamed.
    expect(origin.textContent).not.toBe(
      i18nT('apps.awsControl.console.backup_attribution_self', { label: 'This Mac' }),
    )
    expect(origin.textContent).not.toBe(
      i18nT('apps.awsControl.console.backup_attribution_self_unnamed'),
    )
  })

  it('asks before restoring an unverified row, then sends the override', async () => {
    stubDrivePresent()
    stubBackup(unverifiedRemote)
    vi.mocked(awsControlApi.backupRestore).mockResolvedValue({
      downloaded: true, path: '/tmp/x', bytes: 10, origin: 'unverified', install: INSTALL_ID,
    })
    const archive = await openArchive()
    const row = archive.getByTestId('backup-archive-row')

    // Unlike a self row, it does not restore at once: it asks first.
    fireEvent.click(within(row).getByTestId('backup-restore'))
    expect(awsControlApi.backupRestore).not.toHaveBeenCalled()
    expect(within(row).getByTestId('backup-restore-confirm').textContent).toContain(
      i18nT('apps.awsControl.console.backup_restore_unverified_confirm'),
    )

    // Confirming sends the override. Sitting under this install's own prefix is
    // not proof of anything -- a prefix is a folder name any bucket writer can
    // create, which is exactly what this origin exists to say.
    fireEvent.click(within(row).getByTestId('backup-restore-confirm-yes'))
    await waitFor(() =>
      expect(awsControlApi.backupRestore).toHaveBeenCalledWith(ACCOUNT_ID, `snapshots/${INSTALL_ID}/planted.tar`, true),
    )
  })

  it('does not consult the label when gating a restore: a labelled foreign row still confirms', async () => {
    // The label is written by another install into a shared bucket, so it must
    // never decide what a restore may do. A foreign row with a friendly label is
    // still gated on its origin: it asks first and sends foreignOk true.
    stubDrivePresent()
    stubBackup(remoteWith({ otherLabel: 'Totally Trusted' }))
    vi.mocked(awsControlApi.backupRestore).mockResolvedValue({
      downloaded: true, path: '/tmp/x', bytes: 10, origin: 'other', install: OTHER_ID,
    })
    const archive = await openArchive()
    const rows = archive.getAllByTestId('backup-archive-row')

    fireEvent.click(within(rows[1]).getByTestId('backup-restore'))
    // A label, however friendly, does not skip the confirm.
    expect(within(rows[1]).getByTestId('backup-restore-confirm')).toBeTruthy()
    expect(awsControlApi.backupRestore).not.toHaveBeenCalled()
    fireEvent.click(within(rows[1]).getByTestId('backup-restore-confirm-yes'))
    await waitFor(() =>
      expect(awsControlApi.backupRestore).toHaveBeenCalledWith(ACCOUNT_ID, `snapshots/${OTHER_ID}/other.tar`, true),
    )
  })

  it('surfaces the foreign-refused sentence when the mutation still returns the refusal code', async () => {
    stubDrivePresent()
    stubBackup(remoteWith({ otherLabel: 'Alice laptop' }))
    vi.mocked(awsControlApi.backupRestore).mockRejectedValue(
      new AwsControlError('foreign_install_archive', 409),
    )
    const archive = await openArchive()
    const rows = archive.getAllByTestId('backup-archive-row')
    fireEvent.click(within(rows[1]).getByTestId('backup-restore'))
    fireEvent.click(within(rows[1]).getByTestId('backup-restore-confirm-yes'))

    expect(await screen.findByTestId('backup-restore-error')).toHaveTextContent(
      i18nT('apps.awsControl.console.backup_restore_foreign_refused'),
    )
  })

  it('shows the shared-drive notes and the truncation note when other installs exist', async () => {
    stubDrivePresent()
    stubBackup(remoteWith({ otherLabel: 'Alice laptop', others: 3, truncated: true }))
    await openArchive()
    expect(screen.getByTestId('backup-shared-note')).toBeTruthy()
    expect(screen.getByTestId('backup-others-on-drive')).toBeTruthy()
    expect(screen.getByTestId('backup-others-truncated')).toBeTruthy()
  })

  it('the others-toggle drives `others` on the backup request and re-queries', async () => {
    stubDrivePresent()
    stubBackup(remoteWith({ otherLabel: 'Alice laptop' }))
    await renderDrive('backup')
    fireEvent.click(await screen.findByTestId('backup-remote-toggle'))
    await screen.findByTestId('backup-archive')

    // Before the toggle, others is off in the request.
    expect(awsControlApi.backup).toHaveBeenCalledWith(ACCOUNT_ID, { remote: true, others: false })
    vi.mocked(awsControlApi.backup).mockClear()

    const toggle = within(screen.getByTestId('backup-others-toggle')).getByRole('switch')
    fireEvent.click(toggle)
    await waitFor(() =>
      expect(awsControlApi.backup).toHaveBeenCalledWith(ACCOUNT_ID, { remote: true, others: true }),
    )
  })

  it('renames this install through the api and does not touch the label until it succeeds', async () => {
    stubDrivePresent()
    stubBackup(remoteWith({ otherLabel: 'Alice laptop' }))
    vi.mocked(awsControlApi.installLabel).mockResolvedValue({
      install: { id: INSTALL_ID, label: 'Renamed Mac' },
    })
    await renderDrive('backup')

    const row = await screen.findByTestId('backup-install')
    expect(within(row).getByTestId('backup-install-label')).toHaveTextContent('This Mac')
    fireEvent.click(within(row).getByTestId('backup-install-rename'))
    const input = await screen.findByTestId('backup-install-name')
    fireEvent.change(input, { target: { value: 'Renamed Mac' } })
    fireEvent.click(screen.getByTestId('backup-install-save'))
    await waitFor(() => expect(awsControlApi.installLabel).toHaveBeenCalledWith('Renamed Mac'))
  })

  it('Enter saves the install name and Escape abandons it, like the drive rename editors', async () => {
    // Three rename editors on this page commit on Enter and close on Escape. A
    // reader habituated by those presses Enter here, so a name lost to a key that
    // works everywhere else on the page is the worst version of the divergence.
    stubDrivePresent()
    stubBackup(remoteWith({ otherLabel: 'Alice laptop' }))
    vi.mocked(awsControlApi.installLabel).mockResolvedValue({
      install: { id: INSTALL_ID, label: 'Typed name' },
    })
    await renderDrive('backup')

    fireEvent.click(await screen.findByTestId('backup-install-rename'))
    const input = await screen.findByTestId('backup-install-name')
    fireEvent.change(input, { target: { value: 'Typed name' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(awsControlApi.installLabel).toHaveBeenCalledWith('Typed name'))

    vi.mocked(awsControlApi.installLabel).mockClear()
    fireEvent.click(await screen.findByTestId('backup-install-rename'))
    const reopened = await screen.findByTestId('backup-install-name')
    fireEvent.change(reopened, { target: { value: 'abandoned' } })
    fireEvent.keyDown(reopened, { key: 'Escape' })
    await waitFor(() =>
      expect(screen.queryByTestId('backup-install-name')).not.toBeInTheDocument(),
    )
    expect(awsControlApi.installLabel).not.toHaveBeenCalled()
  })

  it('connects the co-tenant count to the toggle when this install has no rows of its own', async () => {
    // The replacement-machine case this feature exists for: a fresh install owns
    // nothing, so every archive on the drive is behind the off-by-default toggle. A
    // count above an empty list is the failure -- the reader is told others are here
    // and given nothing that reveals them.
    stubBackup({ ...remoteWith(), snapshot: [], sessions: [], others: 1 })
    const card = await openArchive()

    expect(card.getByTestId('backup-others-on-drive')).toBeInTheDocument()
    expect(card.getByTestId('backup-no-own-archives')).toHaveTextContent(
      i18nT('apps.awsControl.console.backup_none_from_this_install'),
    )
  })

  it('drops the no-rows line once this install has archives of its own', async () => {
    stubBackup(remoteWith())
    const card = await openArchive()

    expect(card.getAllByTestId('backup-archive-row').length).toBeGreaterThan(0)
    expect(card.queryByTestId('backup-no-own-archives')).not.toBeInTheDocument()
  })

  it('drops the no-rows line when the toggle is already on, since it would name a done action', async () => {
    // The line's sentence is an instruction to use the toggle. With the toggle on
    // there is nothing left to instruct, so repeating it would read as the control
    // having failed.
    stubBackup({ ...remoteWith(), snapshot: [], sessions: [], others: 1 })
    const card = await openArchive()
    expect(card.getByTestId('backup-no-own-archives')).toBeInTheDocument()

    fireEvent.click(within(card.getByTestId('backup-others-toggle')).getByRole('switch'))
    await waitFor(() =>
      expect(card.queryByTestId('backup-no-own-archives')).not.toBeInTheDocument(),
    )
  })

  it('the rename commit button reads Save name, not Rename', async () => {
    // Rename OPENS the editor; the commit button SAVES it. One word for both was
    // the UX defect this fixes, so the commit control must not reuse the rename
    // label.
    stubDrivePresent()
    stubBackup(remoteWith({ otherLabel: 'Alice laptop' }))
    await renderDrive('backup')

    fireEvent.click(await screen.findByTestId('backup-install-rename'))
    const save = await screen.findByTestId('backup-install-save')
    expect(save).toHaveTextContent(i18nT('apps.awsControl.console.backup_install_save'))
    expect(save).not.toHaveTextContent(i18nT('apps.awsControl.console.backup_install_rename'))
  })

  it('reports a failed rename and leaves the stored name in place', async () => {
    stubDrivePresent()
    stubBackup(remoteWith({ otherLabel: 'Alice laptop' }))
    vi.mocked(awsControlApi.installLabel).mockRejectedValue(new AwsControlError('invalid_label', 400))
    await renderDrive('backup')

    fireEvent.click(await screen.findByTestId('backup-install-rename'))
    fireEvent.change(await screen.findByTestId('backup-install-name'), { target: { value: '' } })
    fireEvent.click(screen.getByTestId('backup-install-save'))
    expect(await screen.findByTestId('backup-install-error')).toHaveTextContent(
      i18nT('apps.awsControl.console.backup_install_rename_failed'),
    )
  })

  it('keeps the archive rows across a toggle when nothing namespaces the key', async () => {
    // The ONE case in this file that mounts without an app identity, because the
    // un-namespaced key IS the subject. The section keeps the rows on screen
    // while a toggle refetches by finding the account in the PREVIOUS query's
    // key, and the host decides how many segments sit in front of that account.
    // An index counted from the front reads the remote-list flag as the account
    // here, the placeholder is dropped, and the panel holding the toggle
    // vanishes under the cursor mid-click.
    //
    // The co-tenant read is held IN FLIGHT rather than resolved, so the rows can
    // only still be on screen because the placeholder put them there. Resolving
    // it would let the refetch land and repaint the same rows for a reason this
    // case is not testing.
    stubDrivePresent()
    const archive = {
      nightly: false,
      runs: {},
      install: { id: INSTALL_ID, label: 'This Mac' },
      remote: remoteWith({ otherLabel: 'Alice laptop' }),
    } as BackupStatus
    vi.mocked(awsControlApi.backup)
      .mockReset()
      .mockResolvedValueOnce(archive)
      .mockResolvedValueOnce(archive)
      .mockReturnValue(new Promise<BackupStatus>(() => {}))
    renderWithProviders(<BackupSection account={ACCOUNT_ID} />)

    fireEvent.click(await screen.findByTestId('backup-remote-toggle'))
    const opened = await screen.findByTestId('backup-archive')
    const before = within(opened).getAllByTestId('backup-archive-row').length
    expect(before).toBeGreaterThan(0)

    // A new key: the co-tenant roster costs its own AWS call, so this refetches.
    fireEvent.click(within(opened).getByRole('switch'))
    await waitFor(() => expect(awsControlApi.backup).toHaveBeenCalledTimes(3))

    // Same account, so the rows already fetched stay while that call is in
    // flight -- the rows, not a skeleton, and not an unmounted panel.
    expect(screen.getByTestId('backup-archive')).toBeTruthy()
    expect(screen.getAllByTestId('backup-archive-row')).toHaveLength(before)
  })
})
