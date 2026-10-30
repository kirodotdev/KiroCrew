/**
 * DrivePage suites: the Files preview dialog and its per-type viewers, rename and delete
 * error placement, and whole-section search. DrivePage.test.tsx names the suites split from it;
 * shared fixtures are in src/test/DrivePageTestUtils.tsx.
 */
import { describe, it, expect, vi, beforeEach, onTestFinished } from 'vitest'
import { screen, fireEvent, waitFor, within } from '@testing-library/react'
import { i18nT } from '../../i18n/t'
import type {
  DriveSearchHit, DriveListing,
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
    expect((await screen.findByTestId('drive-preview-text')).textContent).toBe('hello')
    expect(screen.getByTestId('drive-preview-truncated')).toBeTruthy()
    expect(awsControlApi.drivePreview).toHaveBeenCalledWith(ACCOUNT_ID, 'drive', 'notes.md')
    expect(awsControlApi.driveDownload).not.toHaveBeenCalled()
  })

  it('a markdown file renders as markdown, the way the file side panel renders it', async () => {
    // The dialog used to print the source, so a design doc read as `#` and
    // `**` instead of headings and bold. It now goes through the dashboard's
    // shared ContentRenderer, so the same file reads the same in both surfaces.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue(listing)
    vi.mocked(awsControlApi.drivePreview).mockResolvedValue({
      content: '# Title\n\nsome **bold** text\n',
      truncated: false,
      redacted: false,
    })
    await renderDrive('drive')

    fireEvent.click((await screen.findAllByTestId('drive-preview-open'))[1])
    const body = await screen.findByTestId('drive-preview-text')
    expect(within(body).getByRole('heading', { level: 1 })).toHaveTextContent('Title')
    expect(within(body).getByText('bold').tagName).toBe('STRONG')
    // The syntax itself is gone from the reading surface, and it renders under
    // the same prose wrapper the side panel uses.
    expect(body.textContent).not.toContain('**')
    expect(body.querySelector('.msg-content')).toBeTruthy()
  })

  it('a code file gets the syntax viewer, not a wall of plain text', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'probe.py', size: 40, modified: '2026-09-01T00:00:00Z' }],
      folders: [],
    })
    vi.mocked(awsControlApi.drivePreview).mockResolvedValue({
      content: 'def probe():\n    return 1\n', truncated: false, redacted: false,
    })
    await renderDrive('drive')

    fireEvent.click((await screen.findAllByTestId('drive-preview-open'))[0])
    const body = await screen.findByTestId('drive-preview-text')
    // The code surface received the bytes verbatim (the stub stands in for
    // Pierre, whose real chunk does not resolve under vitest).
    expect(within(body).getByTestId('pierre-mounted')).toHaveTextContent('def probe():')
  })

  it('an html file renders as a page, in a fully sandboxed frame', async () => {
    // Bucket bytes are untrusted, so the shared viewer's `sandbox=""` is the
    // load-bearing part: no script, no same-origin, no top-level navigation.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'report.html', size: 400, modified: '2026-09-01T00:00:00Z' }],
      folders: [],
    })
    vi.mocked(awsControlApi.drivePreview).mockResolvedValue({
      content: '<h1>Quarter</h1>', truncated: false, redacted: false,
    })
    await renderDrive('drive')

    fireEvent.click((await screen.findAllByTestId('drive-preview-open'))[0])
    const frame = (await screen.findByTestId('drive-preview-text')).querySelector('iframe')
    expect(frame).toBeTruthy()
    expect(frame).toHaveAttribute('sandbox', '')
    expect(frame).toHaveAttribute('srcdoc', '<h1>Quarter</h1>')
  })

  it('a csv file renders as a table', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'spend.csv', size: 60, modified: '2026-09-01T00:00:00Z' }],
      folders: [],
    })
    vi.mocked(awsControlApi.drivePreview).mockResolvedValue({
      content: 'service,cost\ns3,2.25\n', truncated: false, redacted: false,
    })
    await renderDrive('drive')

    fireEvent.click((await screen.findAllByTestId('drive-preview-open'))[0])
    const body = await screen.findByTestId('drive-preview-text')
    expect(body.querySelector('table')).toBeTruthy()
    expect(body.textContent).toContain('service')
    expect(body.textContent).toContain('2.25')
  })

  it('a tsv splits on tabs, not on commas', async () => {
    // The csv viewer picks its delimiter from the extension, so the key has to
    // reach it: without one a tab-separated row splits on nothing and the whole
    // line lands in a single cell.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'spend.tsv', size: 60, modified: '2026-09-01T00:00:00Z' }],
      folders: [],
    })
    vi.mocked(awsControlApi.drivePreview).mockResolvedValue({
      content: 'service\tcost\ns3\t2.25\n', truncated: false, redacted: false,
    })
    await renderDrive('drive')

    fireEvent.click((await screen.findAllByTestId('drive-preview-open'))[0])
    const body = await screen.findByTestId('drive-preview-text')
    expect(body.querySelectorAll('th')).toHaveLength(2)
    expect(body.querySelectorAll('td')).toHaveLength(2)
    expect(body.querySelectorAll('th')[1]).toHaveTextContent('cost')
    expect(body.querySelectorAll('td')[1]).toHaveTextContent('2.25')
  })

  it('a json file renders as a tree', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'state.json', size: 60, modified: '2026-09-01T00:00:00Z' }],
      folders: [],
    })
    vi.mocked(awsControlApi.drivePreview).mockResolvedValue({
      content: '{"region": "us-west-2"}', truncated: false, redacted: false,
    })
    await renderDrive('drive')

    fireEvent.click((await screen.findAllByTestId('drive-preview-open'))[0])
    const body = await screen.findByTestId('drive-preview-text')
    expect(body.textContent).toContain('region')
    // The viewer parsed it, so it did not fall through to its own parse-error
    // state -- which is what a truncated read must land in instead.
    expect(screen.queryByTestId('json-viewer-error')).toBeNull()
  })

  it('a TRUNCATED json shows its source, so a capped read is not read as a broken file', async () => {
    // Half a JSON object is an unfinished read, not an invalid document. The
    // tree viewer would accuse the file of being broken; the source plus the
    // truncation notice says what actually happened.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'huge.json', size: 900_000, modified: '2026-09-01T00:00:00Z' }],
      folders: [],
    })
    vi.mocked(awsControlApi.drivePreview).mockResolvedValue({
      content: '{"region": "us-west-2", "objects": [1, 2', truncated: true, redacted: false,
    })
    await renderDrive('drive')

    fireEvent.click((await screen.findAllByTestId('drive-preview-open'))[0])
    const body = await screen.findByTestId('drive-preview-text')
    expect(body.tagName).toBe('PRE')
    expect(body.textContent).toBe('{"region": "us-west-2", "objects": [1, 2')
    expect(screen.getByTestId('drive-preview-truncated')).toBeTruthy()
    expect(screen.queryByTestId('json-viewer-error')).toBeNull()
  })

  it('a log file stays verbatim — its own bytes, not reflowed', async () => {
    // A log IS its own text and reaches the code surface, not the prose one:
    // rendering `---` as a rule or eating a leading `#` would change what the
    // reader is looking at.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'gateway.log', size: 40, modified: '2026-09-01T00:00:00Z' }],
      folders: [],
    })
    vi.mocked(awsControlApi.drivePreview).mockResolvedValue({
      content: '# not a heading\nINFO ready\n', truncated: false, redacted: false,
    })
    await renderDrive('drive')

    fireEvent.click((await screen.findAllByTestId('drive-preview-open'))[0])
    const body = await screen.findByTestId('drive-preview-text')
    expect(within(body).getByTestId('pierre-mounted')).toHaveTextContent('# not a heading')
    expect(body.querySelector('h1')).toBeNull()
  })

  it('a spreadsheet says so honestly: its parse needs the file on the gateway', async () => {
    // The sheet and Office viewers render a GATEWAY-SIDE parse of a file on
    // disk. A drive object is in S3, so there is nothing to open: it is a
    // download, and no text read is attempted.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'budget.xlsx', size: 8000, modified: '2026-09-01T00:00:00Z' }],
      folders: [],
    })
    await renderDrive('drive')

    fireEvent.click((await screen.findAllByTestId('drive-preview-open'))[0])
    expect(await screen.findByTestId('drive-preview-fallback')).toBeTruthy()
    expect(awsControlApi.drivePreview).not.toHaveBeenCalled()
    expect(awsControlApi.driveDownload).not.toHaveBeenCalled()
  })

  it('an avif image previews like every other image', async () => {
    // The pane no longer keeps its own extension table, so the format it knew
    // about had to move INTO the shared one rather than be dropped.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({
      files: [{ key: 'shot.avif', size: 900, modified: '2026-09-01T00:00:00Z' }],
      folders: [],
    })
    vi.mocked(awsControlApi.driveDownload).mockResolvedValue({ url: 'https://signed/shot', expiresSecs: 60 })
    await renderDrive('drive')

    fireEvent.click((await screen.findAllByTestId('drive-preview-open'))[0])
    expect(await screen.findByTestId('drive-preview-image')).toHaveAttribute('src', 'https://signed/shot')
    expect(awsControlApi.drivePreview).not.toHaveBeenCalled()
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

  it('a delete that fails in the same tick as the swap to search is still reported page-level', async () => {
    // The search request is sent from the same effect pass that closes the
    // strip, so failing the delete from inside it lands the rejection before
    // React has committed the closed strip. The routing must not depend on
    // that commit having happened.
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue(listing)
    let failDelete: (e: unknown) => void = () => {}
    vi.mocked(awsControlApi.driveDelete).mockImplementation(
      () => new Promise((_resolve, reject) => { failDelete = reject }),
    )
    vi.mocked(awsControlApi.driveSearch).mockImplementation(async () => {
      failDelete(new Error('AccessDenied'))
      return { results: [], capped: false, limit: 200 }
    })
    await renderDrive('drive')

    await chooseFromMenu((await screen.findAllByTestId('drive-more'))[0], 'drive-delete')
    fireEvent.click(await screen.findByTestId('drive-delete-confirm-action'))
    await waitFor(() => expect(awsControlApi.driveDelete).toHaveBeenCalledTimes(1))

    fireEvent.change(screen.getByTestId('drive-search-input'), { target: { value: 'zzz' } })
    await screen.findByTestId('drive-search-empty')
    const notice = await screen.findByTestId('drive-page-error')
    expect(notice).toHaveTextContent('photo.png')
  })

  it('a rename that fails in the same tick as the swap to search is still reported page-level', async () => {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue(listing)
    let failRename: (e: unknown) => void = () => {}
    vi.mocked(awsControlApi.driveMove).mockImplementation(
      () => new Promise((_resolve, reject) => { failRename = reject }),
    )
    vi.mocked(awsControlApi.driveSearch).mockImplementation(async () => {
      failRename(new AwsControlError('destination_exists', 409))
      return { results: [], capped: false, limit: 200 }
    })
    await renderDrive('drive')

    await chooseFromMenu((await screen.findAllByTestId('drive-more'))[0], 'drive-rename')
    fireEvent.change(await screen.findByTestId('drive-rename-input'), { target: { value: 'photo2.png' } })
    fireEvent.click(screen.getByTestId('drive-rename-save'))
    await waitFor(() => expect(awsControlApi.driveMove).toHaveBeenCalledTimes(1))

    fireEvent.change(screen.getByTestId('drive-search-input'), { target: { value: 'zzz' } })
    await screen.findByTestId('drive-search-empty')
    const notice = await screen.findByTestId('drive-page-error')
    expect(notice).toHaveTextContent('photo.png')
    expect(notice).toHaveTextContent(i18nT('apps.awsControl.console.rename_conflict'))
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
    await waitFor(() => expect(document.activeElement).toBe(marked[0]))
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
    // Restored even if an assertion below throws, so no later test opens a dead tab.
    onTestFinished(() => openSpy.mockRestore())
    await renderDrive('drive')

    fireEvent.click((await screen.findAllByTestId('drive-preview-open'))[1])
    await screen.findByTestId('drive-preview-text')
    fireEvent.click(screen.getByTestId('drive-preview-download'))
    await waitFor(() => expect(fakeTab.close).toHaveBeenCalled())
    const notice = await screen.findByTestId('drive-preview-download-error')
    expect(notice).toHaveTextContent(i18nT('apps.awsControl.console.download_failed'))
    // Inside the dialog, not only in the pane behind it.
    expect(screen.getByTestId('drive-preview-dialog').contains(notice)).toBe(true)
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
    // Restored even if an assertion below throws, so no later test opens a dead tab.
    onTestFinished(() => openSpy.mockRestore())
    await renderDrive('drive')

    // Fail a download from photo.png's row menu (first file row)...
    await chooseFromMenu((await screen.findAllByTestId('drive-more'))[0], 'drive-download')
    expect(await screen.findByTestId('drive-download-error')).toBeTruthy()
    // ...then preview notes.md: the pane still shows it, the dialog does not.
    fireEvent.click(screen.getAllByTestId('drive-preview-open')[1])
    await screen.findByTestId('drive-preview-text')
    expect(screen.queryByTestId('drive-preview-download-error')).toBeNull()
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
