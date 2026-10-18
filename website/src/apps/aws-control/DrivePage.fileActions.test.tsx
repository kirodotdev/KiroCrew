/**
 * DrivePage suites: Files row actions (the download tab, drag and drop, the keyboard move
 * picker), keyboard paths and honest copy, and StorageMeter. DrivePage.test.tsx names the
 * suites split from it; shared fixtures are in src/test/DrivePageTestUtils.tsx.
 */
import { describe, it, expect, vi, beforeEach, onTestFinished } from 'vitest'
import { screen, fireEvent, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import { i18nT } from '../../i18n/t'
import { fmtBytes } from '../../i18n/format'
import type {
  DriveUsage, Share,
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
  StorageMeter,
} from './DrivePage'
import {
  ACCOUNT_ID, chooseFromMenu, renderDrive, stubDrivePresent,
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

/**
 * Wait until a useDialogFocusTrap dialog can HEAR a key, not just until it is
 * mounted. The share and move dialogs open from a `setTimeout(0)` (openShare /
 * openMove), so their render is a default-priority update whose passive effects
 * may run a scheduler task after the commit `findByTestId` resolved on. The
 * trap's window keydown (Escape) listener is one of those effects; an Escape
 * sent before it exists is lost, and the dialog never closes. The trap moves
 * focus into the dialog in the same effect flush (the dialog has no autoFocus of
 * its own), so focus inside it proves the listener is attached.
 */
async function findTrappedDialog(testId: string): Promise<HTMLElement> {
  const dialog = await screen.findByTestId(testId)
  await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true))
  return dialog
}

/**
 * Record what the page showed each time a useDialogFocusTrap re-armed its window
 * keydown listener. The trap's Escape handler is the dialog's `close`, which
 * closes over the render's state (ShareDialog's `shareMut.isPending`), so the
 * trap re-adds its capture-phase listener in the passive effect of every render.
 * A key sent between the commit a test waited on and that re-add reaches the
 * previous render's handler, or none. Waiting for a re-arm recorded WHILE the
 * awaited state was on screen proves the handler a key now reaches has seen it.
 */
function recordTrapRearms<T>(snapshot: () => T): T[] {
  const seen: T[] = []
  const realAdd = window.addEventListener.bind(window)
  const adds = vi.spyOn(window, 'addEventListener').mockImplementation(
    (...args: Parameters<typeof window.addEventListener>) => {
      if (args[0] === 'keydown' && args[2] === true) seen.push(snapshot())
      realAdd(...args)
    },
  )
  onTestFinished(() => adds.mockRestore())
  return seen
}

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
    // Restored even if an assertion below throws, so no later test opens a dead tab.
    onTestFinished(() => openSpy.mockRestore())

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
    await findTrappedDialog('share-dialog')

    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('share-dialog')).toBeNull())
    // The opener owns focus restoration, not the previously focused button.
    await waitFor(() => expect(document.activeElement).toBe(trigger))
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
    // The panel is the dialog and holds focus; the scrim is presentational.
    const dialog = await findTrappedDialog('share-dialog')
    expect(dialog.getAttribute('role')).toBe('dialog')
    const rearms = recordTrapRearms(() => ({
      minting: screen.queryByTestId('share-create')?.textContent === i18nT('apps.awsControl.console.share_creating'),
      minted: screen.queryByTestId('share-result') !== null,
    }))

    // Escape while the link is being created is refused.
    fireEvent.click(screen.getByTestId('share-create'))
    await waitFor(() =>
      expect(screen.getByTestId('share-create')).toHaveTextContent(i18nT('apps.awsControl.console.share_creating')),
    )
    // Pressed against the handler that knows a mint is in flight, so the refusal
    // is the dialog's answer, not a key nobody heard.
    await waitFor(() => expect(rearms.some((r) => r.minting)).toBe(true))
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
    // The result's commit is not enough: the handler that still says "minting"
    // stays armed until the trap re-arms for it.
    await waitFor(() => expect(rearms.some((r) => r.minted)).toBe(true))
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('share-dialog')).toBeNull())
    // Focus went back to where the reader was, not to <body>.
    // Focus returns in a passive effect one tick after the dialog unmounts, so
    // a synchronous read races it under shard load; wait for it to settle.
    await waitFor(() => expect(document.activeElement).toBe(trigger))
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
    await waitFor(() => expect(document.activeElement).toBe(input))

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
    await findTrappedDialog('share-dialog')

    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('share-dialog')).toBeNull())
    // Focus returns in a passive effect one tick after the dialog unmounts, so
    // a synchronous read races it under shard load; wait for it to settle.
    await waitFor(() => expect(document.activeElement).toBe(trigger))
  })
})
