/**
 * AWS Control — the drive's persisted folder.
 *
 * The assertions here are about the FIRST render and the FIRST request, not the settled
 * screen. A restored coordinate that arrives one render late still reaches the DOM, so a
 * settled-DOM assertion cannot tell a correct restore from a late one; what it cannot
 * hide is that the late version has already fetched the wrong folder.
 *
 * Separate from `DrivePage.test.tsx` because these cases need the app-identity provider
 * the rest of that file deliberately does without — a section mounted with no identity
 * gets no host namespace, which is exactly why the existing tests still start at the root.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import { AppIdentityProvider } from '../../app-sdk/identity'
import type { LibraryResponse } from './types'

vi.mock('./api', async () => {
  const actual = await vi.importActual<typeof import('./api')>('./api')
  return {
    ...actual,
    awsControlApi: {
      driveList: vi.fn(),
      library: vi.fn(),
      driveDownload: vi.fn(),
      driveShare: vi.fn(),
      driveDelete: vi.fn(),
      driveFolderCreate: vi.fn(),
      driveFolderDelete: vi.fn(),
      driveMove: vi.fn(),
      driveUpload: vi.fn(),
    },
  }
})

vi.mock('../../api/client', () => ({
  api: { artifact: vi.fn(), awsConsent: vi.fn() },
}))

import { awsControlApi } from './api'
import { DriveSectionView } from './DrivePage'

const ACCOUNT_ID = '111122223333'
const OTHER_ACCOUNT = '999988887777'
const BUCKET = 'kirocrew-drive-abc123'
// The key spelled out rather than recomputed: this is the on-disk contract.
const KEY = `kc:app:aws-control:view:drive:${ACCOUNT_ID}`

const emptyLibrary: LibraryResponse = { artifacts: [] }

function seedView(scope: string, state: Record<string, unknown>, revision = 1): void {
  localStorage.setItem(`kc:app:aws-control:view:drive:${scope}`, JSON.stringify({ revision, state }))
}

function storedRecord(): { revision: number; scope: string; state: Record<string, unknown> } | null {
  const raw = localStorage.getItem(KEY)
  return raw === null ? null : JSON.parse(raw)
}

/** Mount the drive pane as a builtin page, the way `BuiltinAppRoute` does. */
function renderDrive(account = ACCOUNT_ID) {
  return renderWithProviders(
    <AppIdentityProvider appId="aws-control" origin="builtin">
      <DriveSectionView account={account} bucket={BUCKET} />
    </AppIdentityProvider>,
  )
}

/** The paths `driveList` was asked for, in call order. */
function requestedPaths(): string[] {
  return vi.mocked(awsControlApi.driveList).mock.calls.map((c) => c[2] as string)
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  vi.mocked(awsControlApi.library).mockResolvedValue(emptyLibrary)
  vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: [] })
})

describe('drive folder restore', () => {
  it('asks for the restored folder on the FIRST request, never the root', async () => {
    // The point of the whole feature: a return lands where the user was, and the network
    // is not spent listing a folder nobody asked for. If the record were read in an
    // effect, the first key would be the root and this would fail on the first call while
    // still passing on the last.
    seedView(ACCOUNT_ID, { path: 'docs/reports' })
    renderDrive()

    await waitFor(() => expect(awsControlApi.driveList).toHaveBeenCalled())
    expect(vi.mocked(awsControlApi.driveList).mock.calls[0]).toEqual([
      ACCOUNT_ID,
      'drive',
      'docs/reports',
      '',
    ])
    expect(requestedPaths()).not.toContain('')
  })

  it('shows the restored folder in the breadcrumbs', async () => {
    // One segment on purpose: a deeper path collapses its middle crumbs into the
    // overflow menu, so asserting on a two-level path would be testing that menu rather
    // than the restore.
    seedView(ACCOUNT_ID, { path: 'docs' })
    renderDrive()
    const crumbs = await screen.findByTestId('drive-crumbs')
    expect(crumbs.textContent).toContain('docs')
  })

  it('restores a path several folders deep', async () => {
    seedView(ACCOUNT_ID, { path: 'docs/reports/q3' })
    renderDrive()
    await waitFor(() => expect(awsControlApi.driveList).toHaveBeenCalled())
    expect(requestedPaths()[0]).toBe('docs/reports/q3')
    expect((await screen.findByTestId('drive-crumbs')).textContent).toContain('q3')
  })

  it('starts at the root when there is no record', async () => {
    renderDrive()
    await waitFor(() => expect(awsControlApi.driveList).toHaveBeenCalled())
    expect(vi.mocked(awsControlApi.driveList).mock.calls[0]).toEqual([ACCOUNT_ID, 'drive', '', ''])
  })

  it('starts at the root when the record belongs to another account', async () => {
    // A prefix means nothing outside the bucket it was taken in, so the scope refusal
    // has to hold here and not just in the store's own tests.
    seedView(OTHER_ACCOUNT, { path: 'docs/reports' })
    renderDrive()
    await waitFor(() => expect(awsControlApi.driveList).toHaveBeenCalled())
    expect(requestedPaths()[0]).toBe('')
  })

  it('starts at the root when the record was written under another revision', async () => {
    seedView(ACCOUNT_ID, { path: 'docs/reports' }, 2)
    renderDrive()
    await waitFor(() => expect(awsControlApi.driveList).toHaveBeenCalled())
    expect(requestedPaths()[0]).toBe('')
  })

  it('mounts at the root rather than failing when the record is corrupt', async () => {
    // A changed or broken schema must never be able to stop the page from mounting.
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    localStorage.setItem(KEY, '{not json')
    renderDrive()
    await waitFor(() => expect(awsControlApi.driveList).toHaveBeenCalled())
    expect(requestedPaths()[0]).toBe('')
    expect(await screen.findByTestId('drive-empty')).toBeTruthy()
  })
})

describe('drive folder persistence', () => {
  it('records the folder the user opens, scoped to the account', async () => {
    vi.mocked(awsControlApi.driveList)
      .mockResolvedValueOnce({ files: [], folders: ['docs'] })
      .mockResolvedValue({ files: [], folders: [] })
    renderDrive()

    fireEvent.click(await screen.findByTestId('drive-folder'))

    await waitFor(() => expect(storedRecord()).not.toBeNull())
    // The account is the KEY the record lives under, not a field inside it.
    expect(storedRecord()).toEqual({ revision: 1, state: { path: 'docs' } })
    expect(localStorage.getItem(`kc:app:aws-control:view:drive:${OTHER_ACCOUNT}`)).toBeNull()
  })

  it('never writes the listing, only the coordinate', async () => {
    // `contents` is not a declared field, so the store drops it on write. This is the
    // design's "do not persist drive contents" as an enforced property rather than a
    // rule the page is trusted to follow.
    vi.mocked(awsControlApi.driveList)
      .mockResolvedValueOnce({
        files: [{ key: 'secret-budget.xlsx', size: 2048, modified: '2026-08-20T00:00:00Z' }],
        folders: ['docs'],
      })
      .mockResolvedValue({
        files: [{ key: 'docs/private-notes.txt', size: 1024, modified: '2026-08-20T00:00:00Z' }],
        folders: [],
      })
    renderDrive()

    fireEvent.click(await screen.findByTestId('drive-folder'))
    await waitFor(() => expect(storedRecord()).not.toBeNull())

    const raw = localStorage.getItem(KEY) as string
    expect(raw).not.toContain('secret-budget')
    expect(raw).not.toContain('private-notes')
    expect(raw).not.toContain('files')
    expect(Object.keys(storedRecord()?.state ?? {})).toEqual(['path'])
  })

  it('clears the record when the user walks back to the root', async () => {
    // Nothing to restore means no row left behind.
    seedView(ACCOUNT_ID, { path: 'docs' })
    renderDrive()

    const crumbs = await screen.findByTestId('drive-crumbs')
    fireEvent.click(within(crumbs).getAllByRole('button')[0])

    await waitFor(() => expect(storedRecord()).toBeNull())
  })
})
