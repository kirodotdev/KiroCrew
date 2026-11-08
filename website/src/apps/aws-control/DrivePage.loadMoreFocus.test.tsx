/**
 * Load more keeps its focus while the next page is fetched.
 *
 * Files and Library each end in a Load more button that stays on screen while
 * the next page loads, and again after it lands when more pages remain. A
 * native `disabled` on the button a reader just pressed blurs it to <body>, so a
 * keyboard user lost their place at the bottom of the list. The button is held
 * with aria-disabled instead, and a second press fetches nothing.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

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
  renderDrive, stubDrivePresent,
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

type Page = { files: Array<{ key: string; size: number; modified: string }>; folders: string[]; nextToken?: string }

/** A first page with a continuation token, then a second page the test settles by hand. */
function holdSecondPage(first: Page, second: Page) {
  let settle: () => void = () => {}
  vi.mocked(awsControlApi.driveList)
    .mockResolvedValueOnce(first)
    .mockImplementationOnce(() => new Promise((resolve) => { settle = () => resolve(second) }))
  return () => settle()
}

describe('Load more focus while the next page is fetched', () => {
  const cases = [
    {
      section: 'drive' as const,
      testId: 'drive-load-more',
      first: { files: [], folders: ['first'], nextToken: 'tok-2' },
      second: { files: [], folders: ['second'], nextToken: 'tok-3' },
    },
    {
      section: 'library' as const,
      testId: 'library-load-more',
      first: { files: [], folders: ['first'], nextToken: 'tok-2' },
      second: { files: [], folders: ['second'], nextToken: 'tok-3' },
    },
  ]
  for (const c of cases) {
    it(`keeps focus on ${c.testId}, refuses a second press, and keeps it after the page lands`, async () => {
      vi.mocked(awsControlApi.library).mockResolvedValue({ artifacts: [] })
      const settle = holdSecondPage(c.first, c.second)
      await renderDrive(c.section)
      const button = await screen.findByTestId(c.testId)

      button.focus()
      await userEvent.keyboard('{Enter}')
      await waitFor(() => expect(awsControlApi.driveList).toHaveBeenCalledTimes(2))
      await waitFor(() => expect(button.getAttribute('aria-disabled')).toBe('true'))
      expect(button.hasAttribute('disabled')).toBe(false)
      expect(document.activeElement).toBe(button)

      await userEvent.keyboard('{Enter}')
      expect(awsControlApi.driveList).toHaveBeenCalledTimes(2)

      settle()
      await waitFor(() => expect(button.hasAttribute('aria-disabled')).toBe(false))
      // More pages remain, so the same button is still there, and still focused.
      expect(screen.getByTestId(c.testId)).toBe(button)
      expect(document.activeElement).toBe(button)
    })
  }
})

describe('Load more after leaving a folder mid-fetch', () => {
  it('lets the parent listing load more while the folder page is still out', async () => {
    // By arguments, not call order: the root listing refetches when it is shown again.
    vi.mocked(awsControlApi.driveList).mockImplementation((_account, _section, path, token) => {
      if (path === 'docs' && token === 'docs-2') return new Promise(() => {})
      if (path === 'docs') return Promise.resolve({ files: [], folders: ['inner'], nextToken: 'docs-2' })
      if (token === 'root-2') return Promise.resolve({ files: [], folders: ['more'] })
      return Promise.resolve({ files: [], folders: ['docs'], nextToken: 'root-2' })
    })
    await renderDrive('drive')
    await userEvent.click(await screen.findByTestId('drive-folder'))
    await waitFor(() => expect(awsControlApi.driveList).toHaveBeenLastCalledWith(expect.anything(), 'drive', 'docs', ''))

    // Start the folder's next page and leave it out.
    await userEvent.click(await screen.findByTestId('drive-load-more'))
    await waitFor(() => expect(awsControlApi.driveList).toHaveBeenLastCalledWith(expect.anything(), 'drive', 'docs', 'docs-2'))

    // Back to the root, whose own Load more must answer.
    await userEvent.click(within(screen.getByTestId('drive-crumbs')).getAllByRole('button')[0])
    const rootMore = await screen.findByTestId('drive-load-more')
    await userEvent.click(rootMore)
    await waitFor(() => expect(awsControlApi.driveList).toHaveBeenLastCalledWith(expect.anything(), 'drive', '', 'root-2'))
  })
})

describe('LoadMoreButton press latch', () => {
  it('refuses a second press before the re-render, and fetches with the default cancelRefetch', async () => {
    const { LoadMoreButton } = await import('./shared')
    const { render, fireEvent } = await import('@testing-library/react')
    let settle: () => void = () => {}
    // isFetchingNextPage stays false: the second press lands before any
    // re-render, or while only a background refetch of the loaded pages runs.
    const listQ = {
      isFetchingNextPage: false,
      fetchNextPage: vi.fn(() => new Promise<void>((resolve) => { settle = resolve })),
    }
    const { getByTestId } = render(<LoadMoreButton listQ={listQ} testId="lm" />)
    fireEvent.click(getByTestId('lm'))
    fireEvent.click(getByTestId('lm'))
    expect(listQ.fetchNextPage).toHaveBeenCalledTimes(1)
    // No options: the default cancelRefetch lets a press cancel a background
    // refetch instead of being handed that refetch's promise.
    expect(listQ.fetchNextPage).toHaveBeenCalledWith()

    settle()
    await waitFor(() => {
      fireEvent.click(getByTestId('lm'))
      expect(listQ.fetchNextPage).toHaveBeenCalledTimes(2)
    })
  })
})
