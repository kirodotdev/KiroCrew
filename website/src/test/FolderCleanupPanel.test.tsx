// The header ⋮ → "Clean up empty folders" panel.
//
// Pinned here:
// - it opens on a dry run and lists each removable folder by its full path
// - the top-level box re-runs the dry run asking for top-level folders
// - Delete sends the real (non-dry) call with the same top-level choice and
//   the ids the preview showed
// - an archived-session count carries a readable label
// - a delete that kept some previewed folders stays open and says so
// - nothing to remove offers no Delete
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import FolderCleanupPanel, { folderPath } from '../pages/chat-sidebar/FolderCleanupPanel'
import { api } from '../api/client'
import type { ChatFolder } from '../types'

const FOLDERS = [
  { id: 'a', name: 'oss', parent_id: null },
  { id: 'b', name: 'ci-blockers', parent_id: 'a' },
  { id: 'c', name: 'kirocrew-worker', parent_id: 'b' },
] as unknown as ChatFolder[]

function mount(onClose = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <FolderCleanupPanel folders={FOLDERS} onClose={onClose} />
    </QueryClientProvider>,
  )
  return onClose
}

afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('folderPath', () => {
  it('joins the parent chain and survives a cycle', () => {
    expect(folderPath(FOLDERS, 'c')).toBe('oss / ci-blockers / kirocrew-worker')
    const loop = [
      { id: 'x', name: 'x', parent_id: 'y' },
      { id: 'y', name: 'y', parent_id: 'x' },
    ] as unknown as ChatFolder[]
    expect(folderPath(loop, 'x')).toBe('y / x')
  })
})

describe('FolderCleanupPanel', () => {
  it('previews by full path, then deletes with the same scope', async () => {
    const spy = vi.spyOn(api, 'cleanupChatFolders').mockImplementation(async (opts) => (
      opts.dryRun ? { ids: ['b', 'c'], archived: { c: 2 } } : { deleted: ['b', 'c'] }
    ) as Awaited<ReturnType<typeof api.cleanupChatFolders>>)
    const onClose = mount()
    expect(await screen.findByText('oss / ci-blockers / kirocrew-worker')).toBeTruthy()
    expect(spy).toHaveBeenCalledWith({ dryRun: true, includeTopLevel: false })
    expect(screen.getByTestId('folder-cleanup-archived').getAttribute('aria-label')).toBe('2 archived sessions stay in Older Sessions')
    // The count reads as words, not a bare number beside a clock icon.
    expect(screen.getByTestId('folder-cleanup-archived').textContent).toBe('2 archived')
    // A truncated deep path can still be read in full on hover.
    expect(screen.getByText('oss / ci-blockers / kirocrew-worker').getAttribute('title')).toBe('oss / ci-blockers / kirocrew-worker')
    expect(screen.getByTestId('folder-cleanup-confirm').textContent).toBe('Delete 2 empty folders')

    fireEvent.click(screen.getByTestId('folder-cleanup-top-level'))
    await waitFor(() => expect(spy).toHaveBeenCalledWith({ dryRun: true, includeTopLevel: true }))
    await waitFor(() => expect((screen.getByTestId('folder-cleanup-confirm') as HTMLButtonElement).disabled).toBe(false))

    fireEvent.click(screen.getByTestId('folder-cleanup-confirm'))
    await waitFor(() => expect(spy).toHaveBeenCalledWith({ includeTopLevel: true, ids: ['b', 'c'] }))
    await waitFor(() => expect(onClose).toHaveBeenCalled())
  })

  it('stays open and says so when the server kept some previewed folders', async () => {
    let previews = 0
    vi.spyOn(api, 'cleanupChatFolders').mockImplementation(async (opts) => {
      if (!opts.dryRun) return { deleted: ['c'] } as Awaited<ReturnType<typeof api.cleanupChatFolders>>
      previews += 1
      return { ids: previews === 1 ? ['b', 'c'] : ['b'], archived: {} } as Awaited<ReturnType<typeof api.cleanupChatFolders>>
    })
    const onClose = mount()
    expect(await screen.findByText('oss / ci-blockers / kirocrew-worker')).toBeTruthy()
    fireEvent.click(screen.getByTestId('folder-cleanup-confirm'))
    expect(await screen.findByTestId('folder-cleanup-notice')).toBeTruthy()
    expect(screen.getByTestId('folder-cleanup-notice').textContent).toBe('Some folders were kept: they are no longer empty.')
    await waitFor(() => expect(screen.getByTestId('folder-cleanup-confirm').textContent).toBe('Delete 1 empty folder'))
    expect(onClose).not.toHaveBeenCalled()
  })

  it('offers no Delete when nothing is removable', async () => {
    vi.spyOn(api, 'cleanupChatFolders').mockResolvedValue(
      { ids: [], archived: {} } as Awaited<ReturnType<typeof api.cleanupChatFolders>>,
    )
    mount()
    expect(await screen.findByText('No empty folders to delete.')).toBeTruthy()
    expect(screen.queryByTestId('folder-cleanup-confirm')).toBeNull()
    expect(screen.queryByTestId('folder-cleanup-list')).toBeNull()
  })
})
