/**
 * DrivePage Library picker: focus handling.
 *
 * The "Add from artifacts" dialog is modal, so dismissing it must return focus
 * to the control that opened it rather than dropping focus to the page body.
 * The search input carries `autoFocus`, which runs before the shared focus
 * trap reads the restore target, so the section captures the opener itself and
 * restores to it on every dismiss path.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'

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

vi.mock('../../api/client', () => ({
  api: {
    artifact: vi.fn(),
    awsConsent: vi.fn(),
    grantAwsConsent: vi.fn(),
    revokeAwsConsent: vi.fn(),
  },
}))

vi.mock('../../pierre', () => ({
  PierreCode: ({ file }: { file: { contents: string } }) => (
    <div data-testid="pierre-mounted">{file.contents}</div>
  ),
}))

import { awsControlApi } from './api'
import { api } from '../../api/client'
import { renderDrive, stubDrivePresent } from '../../test/DrivePageTestUtils'

beforeEach(() => {
  vi.clearAllMocks()
  for (const group of [awsControlApi, api] as unknown as Array<Record<string, unknown>>) {
    for (const fn of Object.values(group)) {
      if (vi.isMockFunction(fn)) fn.mockReset()
    }
  }
  stubDrivePresent()
  localStorage.clear()
  vi.mocked(api.artifact).mockReturnValue(new Promise(() => {}) as ReturnType<typeof api.artifact>)
})

describe('DrivePage Library picker: restores focus to its opener', () => {
  async function openPickerFromAddButton() {
    stubDrivePresent()
    vi.mocked(awsControlApi.driveList).mockResolvedValue({ files: [], folders: ['notes'] })
    vi.mocked(awsControlApi.library).mockResolvedValue({
      artifacts: [
        { slug: 'notes', name: 'Notes', kind: 'markdown', version: 1, updatedAt: '2026-08-20T00:00:00Z', pushedVersion: null, pushedAt: null },
      ],
    })

    await renderDrive('library')
    const opener = await screen.findByTestId('library-add-open')
    opener.focus()
    fireEvent.click(opener)
    await screen.findByTestId('library-add-dialog')
    return opener as HTMLElement
  }

  it('leaves focus on open where the shared trap puts it (the close button)', async () => {
    // Open focus is deliberately NOT changed by this fix: the shared trap moves
    // focus to the first focusable control in the dialog, which is the close
    // button. Pinning it here guards against the restore change drifting the
    // open-focus behaviour.
    await openPickerFromAddButton()
    await waitFor(() =>
      expect(document.activeElement).toBe(screen.getByTestId('library-add-close')))
  })

  it('returns focus to the opener on Escape', async () => {
    const opener = await openPickerFromAddButton()

    fireEvent.keyDown(document.activeElement ?? document, { key: 'Escape' })

    await waitFor(() => expect(screen.queryByTestId('library-add-dialog')).toBeNull())
    await waitFor(() => expect(document.activeElement).toBe(opener))
  })

  it('returns focus to the opener via the close button', async () => {
    const opener = await openPickerFromAddButton()

    fireEvent.click(screen.getByTestId('library-add-close'))

    await waitFor(() => expect(screen.queryByTestId('library-add-dialog')).toBeNull())
    await waitFor(() => expect(document.activeElement).toBe(opener))
  })

  it('falls back to the header Add button when the empty-state opener has unmounted', async () => {
    // Open from the empty-state button, then add an artifact. That fills the
    // bucket, the listing refetches and the empty state (with its button)
    // unmounts, so the captured opener is detached by close time. Focus must
    // then land on the header Add button, which renders once the listing is
    // non-empty, rather than falling to the page body.
    stubDrivePresent()
    // Empty bucket until an artifact is pushed, then one folder shows up.
    let pushed = false
    vi.mocked(awsControlApi.driveList).mockImplementation(async () =>
      pushed ? { files: [], folders: ['notes'] } : { files: [], folders: [] })
    vi.mocked(awsControlApi.library).mockResolvedValue({
      artifacts: [
        { slug: 'notes', name: 'Notes', kind: 'markdown', version: 1, updatedAt: '2026-08-20T00:00:00Z', pushedVersion: null, pushedAt: null },
      ],
    })
    vi.mocked(awsControlApi.libraryPush).mockImplementation(async () => { pushed = true })

    await renderDrive('library')
    const emptyAdd = await screen.findByTestId('library-empty-add')
    emptyAdd.focus()
    fireEvent.click(emptyAdd)
    await screen.findByTestId('library-add-dialog')

    // Add the artifact from inside the dialog; the listing refetch empties the
    // empty state behind it.
    fireEvent.click(await screen.findByTestId('library-push'))
    await waitFor(() => expect(screen.queryByTestId('library-empty-add')).toBeNull())
    const headerAdd = await screen.findByTestId('library-add-open')

    fireEvent.keyDown(document.activeElement ?? document, { key: 'Escape' })

    await waitFor(() => expect(screen.queryByTestId('library-add-dialog')).toBeNull())
    await waitFor(() => expect(document.activeElement).toBe(headerAdd))
    expect(document.activeElement).not.toBe(document.body)
  })
})
