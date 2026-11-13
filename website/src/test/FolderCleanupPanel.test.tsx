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
// - on one folder's tree (folder ⋮ → "Close sessions and delete…") it lists the
//   sessions to close with the live work their ✕ would ask about, closes each
//   (the open chat last), then sends the previewed ids scoped to that root; a
//   session that stays open says so
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import type { ComponentProps } from 'react'
import FolderCleanupPanel, { folderPath } from '../pages/chat-sidebar/FolderCleanupPanel'
import { api } from '../api/client'
import dashboardReducer from '../store/dashboardSlice'
import chatReducer from '../store/chatSlice'
import { createTestStore } from './helpers'
import type { ChatFolder, ChatSlot } from '../types'
import type { Slot } from '../pages/chat-sidebar/types'

const FOLDERS = [
  { id: 'a', name: 'oss', parent_id: null },
  { id: 'b', name: 'ci-blockers', parent_id: 'a' },
  { id: 'c', name: 'kirocrew-worker', parent_id: 'b' },
] as unknown as ChatFolder[]

function mount(onClose = vi.fn(), props: Partial<ComponentProps<typeof FolderCleanupPanel>> = {}, state: { activeSlot?: string } = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  // A fresh store per mount: a close leaves holds behind that must not reach the next test.
  const store = createTestStore({
    dashboard: { ...dashboardReducer(undefined, { type: '@@init' }), slots: (props.slots ?? []) as ChatSlot[] },
    chat: { ...chatReducer(undefined, { type: '@@init' }), activeSlot: state.activeSlot ?? null },
  })
  render(
    <Provider store={store}>
      <QueryClientProvider client={client}>
        <FolderCleanupPanel folders={FOLDERS} onClose={onClose} {...props} />
      </QueryClientProvider>
    </Provider>,
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

describe('FolderCleanupPanel on one folder', () => {
  // oss (2 sub-folders) holds three sessions; `elsewhere` sits outside its tree.
  const SLOTS: Slot[] = [
    { key: 's1', title: 'Plan', running: true, folder_id: 'a' },
    { key: 's2', title: 'Fix', running: false, folder_id: 'b' },
    { key: 's3', title: 'Ship', running: false, folder_id: 'c' },
    { key: 'elsewhere', title: 'Other', running: false, folder_id: 'z' },
  ]
  type Cleanup = Awaited<ReturnType<typeof api.cleanupChatFolders>>

  it('closes every session in the tree, then deletes the previewed folders', async () => {
    const closed: string[] = []
    vi.spyOn(api, 'deleteChatSlot').mockImplementation(async (key: string) => { closed.push(key); return {} })
    const spy = vi.spyOn(api, 'cleanupChatFolders').mockImplementation(async (opts) => (
      opts.dryRun ? { ids: ['a', 'b', 'c'], archived: {} } : { deleted: ['a', 'b', 'c'] }
    ) as Cleanup)
    const onClose = mount(vi.fn(), { root: FOLDERS[0], slots: SLOTS })
    expect(await screen.findByText('oss / ci-blockers / kirocrew-worker')).toBeTruthy()
    expect(spy).toHaveBeenCalledWith({ dryRun: true, includeTopLevel: false, root: 'a' })
    expect(screen.getByTestId('folder-cleanup-sessions').textContent).toContain('oss / ci-blockers / Fix')
    expect(screen.getByTestId('folder-cleanup-sessions').textContent).not.toContain('Other')
    // The running turn is named in the ✕ prompt's own words; idle rows say nothing.
    expect(screen.getAllByTestId('folder-cleanup-busy').map(n => n.textContent)).toEqual(['It is still working on a reply. Closing stops that reply.'])
    expect(screen.queryByTestId('folder-cleanup-top-level')).toBeNull()
    expect(screen.getByTestId('folder-cleanup-confirm').textContent).toBe('Close and delete')

    fireEvent.click(screen.getByTestId('folder-cleanup-confirm'))
    await waitFor(() => expect(spy).toHaveBeenCalledWith({ includeTopLevel: false, root: 'a', ids: ['a', 'b', 'c'] }))
    expect(closed).toEqual(['s1', 's2', 's3'])
    await waitFor(() => expect(onClose).toHaveBeenCalled())
  })

  it('on a root the server keeps, says why it stays and offers only Close sessions', async () => {
    const closed: string[] = []
    vi.spyOn(api, 'deleteChatSlot').mockImplementation(async (key: string) => { closed.push(key); return {} })
    vi.spyOn(api, 'cleanupChatFolders').mockImplementation(async (opts) => (
      opts.dryRun ? { ids: ['c'], archived: {}, root_kept: 'cron' } : { deleted: ['c'] }
    ) as Cleanup)
    const onClose = mount(vi.fn(), { root: FOLDERS[0], slots: SLOTS })
    expect((await screen.findByTestId('folder-cleanup-root-kept')).textContent).toBe('oss stays: a cron job files its chats here.')
    expect(screen.getByText('Close sessions in oss')).toBeTruthy()
    expect(screen.getByTestId('folder-cleanup-confirm').textContent).toBe('Close sessions')
    fireEvent.click(screen.getByTestId('folder-cleanup-confirm'))
    await waitFor(() => expect(onClose).toHaveBeenCalled())
    expect(closed).toEqual(['s1', 's2', 's3'])
  })

  it('closes the open chat last, and stays open saying so when a session would not close', async () => {
    const closed: string[] = []
    vi.spyOn(api, 'deleteChatSlot').mockImplementation(async (key: string) => {
      closed.push(key)
      if (key === 's3') throw new Error('refused')
      return {}
    })
    // A refused close refetches the slot list to recover the row.
    vi.spyOn(api, 'chatSlots').mockResolvedValue({ slots: [] } as unknown as Awaited<ReturnType<typeof api.chatSlots>>)
    vi.spyOn(api, 'cleanupChatFolders').mockImplementation(async (opts) => (
      opts.dryRun ? { ids: ['a', 'b', 'c'], archived: {} } : { deleted: [] }
    ) as Cleanup)
    const onClose = mount(vi.fn(), { root: FOLDERS[0], slots: SLOTS }, { activeSlot: 's1' })
    expect(await screen.findByText('oss / ci-blockers / kirocrew-worker')).toBeTruthy()
    fireEvent.click(screen.getByTestId('folder-cleanup-confirm'))
    // s3 is refused and s1's later close succeeds, clearing the sidebar's
    // refused-close notice: the panel's own error surface still says so.
    expect((await screen.findByTestId('folder-cleanup-error')).textContent).toContain('Some sessions stayed open, so their folders were kept.')
    expect(closed).toEqual(['s2', 's3', 's1'])
    expect(onClose).not.toHaveBeenCalled()
  })
})
