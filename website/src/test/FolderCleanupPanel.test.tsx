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
import { act, render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { StrictMode, type ComponentProps } from 'react'
import FolderCleanupPanel, { folderPath } from '../pages/chat-sidebar/FolderCleanupPanel'
import { api } from '../api/client'
import dashboardReducer, { sseSlots } from '../store/dashboardSlice'
import chatReducer from '../store/chatSlice'
import { createTestStore } from './helpers'
import type { ChatFolder, ChatSlot } from '../types'
import type { Slot } from '../pages/chat-sidebar/types'

const FOLDERS = [
  { id: 'a', name: 'oss', parent_id: null },
  { id: 'b', name: 'ci-blockers', parent_id: 'a' },
  { id: 'c', name: 'kirocrew-worker', parent_id: 'b' },
] as unknown as ChatFolder[]

function mount(onClose = vi.fn(), props: Partial<ComponentProps<typeof FolderCleanupPanel>> = {}, state: { activeSlot?: string; strict?: boolean } = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  // A fresh store per mount: a close leaves holds behind that must not reach the next test.
  const store = createTestStore({
    dashboard: { ...dashboardReducer(undefined, { type: '@@init' }), slots: (props.slots ?? []) as ChatSlot[] },
    chat: { ...chatReducer(undefined, { type: '@@init' }), activeSlot: state.activeSlot ?? null },
  })
  const tree = (
    <Provider store={store}>
      <QueryClientProvider client={client}>
        <FolderCleanupPanel folders={FOLDERS} onClose={onClose} {...props} />
      </QueryClientProvider>
    </Provider>
  )
  // `main.tsx` mounts the dashboard under StrictMode, which runs each effect's setup twice.
  render(state.strict ? <StrictMode>{tree}</StrictMode> : tree)
  return onClose
}

/** `mount`, handing back the store so a test can change the live slot list. */
function mountWithStore(props: Partial<ComponentProps<typeof FolderCleanupPanel>>) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = createTestStore({
    dashboard: { ...dashboardReducer(undefined, { type: '@@init' }), slots: (props.slots ?? []) as ChatSlot[] },
    chat: { ...chatReducer(undefined, { type: '@@init' }), activeSlot: null },
  })
  render(
    <Provider store={store}>
      <QueryClientProvider client={client}>
        <FolderCleanupPanel folders={FOLDERS} onClose={vi.fn()} {...props} />
      </QueryClientProvider>
    </Provider>,
  )
  return store
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

  it('says a conductor in the tree closes its open workers with it, by the server count (#31629)', async () => {
    const read = vi.spyOn(api, 'chatSlotClosePreview').mockResolvedValue({ workers: 2 })
    vi.spyOn(api, 'cleanupChatFolders').mockImplementation(async (opts) => (
      opts.dryRun ? { ids: ['a'], archived: {} } : { deleted: ['a'] }
    ) as Cleanup)
    // The workers sit in no folder; their root is filed in the tree.
    const slots = [
      { key: 'cond', title: 'Conductor', running: false, folder_id: 'a', crew_mode_root: true },
      { key: 'w1', title: 'W1', running: false, crew_root: 'cond' },
      { key: 'w2', title: 'W2', running: false, crew_root: 'cond' },
    ] as Slot[]
    mount(vi.fn(), { root: FOLDERS[0], slots })
    expect(await screen.findByText('oss / Conductor')).toBeTruthy()
    await waitFor(() => expect(screen.getAllByTestId('folder-cleanup-busy').map(n => n.textContent))
      .toEqual(['It has 2 open workers, which close with it.']))
    expect(read).toHaveBeenCalledWith('cond')
  })

  it('says workers close and holds Close while the worker count is still being read', async () => {
    let answer: (v: { workers: number }) => void = () => undefined
    vi.spyOn(api, 'chatSlotClosePreview').mockReturnValue(new Promise(resolve => { answer = resolve }))
    vi.spyOn(api, 'cleanupChatFolders').mockImplementation(async (opts) => (
      opts.dryRun ? { ids: ['a'], archived: {} } : { deleted: ['a'] }
    ) as Cleanup)
    const slots = [
      { key: 'cond', title: 'Conductor', running: false, folder_id: 'a', crew_mode_root: true },
      { key: 'w1', title: 'W1', running: false, crew_root: 'cond' },
    ] as Slot[]
    mount(vi.fn(), { root: FOLDERS[0], slots })
    const confirm = await screen.findByTestId('folder-cleanup-confirm') as HTMLButtonElement
    expect(screen.getAllByTestId('folder-cleanup-busy').map(n => n.textContent))
      .toEqual(['It has open workers, which close with it.'])
    expect(confirm.disabled).toBe(true)
    await act(async () => { answer({ workers: 1 }) })
    await waitFor(() => expect(screen.getAllByTestId('folder-cleanup-busy').map(n => n.textContent))
      .toEqual(['It has 1 open worker, which closes with it.']))
    expect((screen.getByTestId('folder-cleanup-confirm') as HTMLButtonElement).disabled).toBe(false)
  })

  it('reads the count again when the conductor dispatches another worker', async () => {
    const read = vi.spyOn(api, 'chatSlotClosePreview')
      .mockResolvedValueOnce({ workers: 1 })
      .mockResolvedValueOnce({ workers: 2 })
    vi.spyOn(api, 'cleanupChatFolders').mockImplementation(async (opts) => (
      opts.dryRun ? { ids: ['a'], archived: {} } : { deleted: ['a'] }
    ) as Cleanup)
    const slots = [
      { key: 'cond', title: 'Conductor', running: false, folder_id: 'a', crew_mode_root: true },
      { key: 'w1', title: 'W1', running: false, crew_root: 'cond' },
    ] as Slot[]
    const store = mountWithStore({ root: FOLDERS[0], slots })
    await waitFor(() => expect(screen.getAllByTestId('folder-cleanup-busy').map(n => n.textContent))
      .toEqual(['It has 1 open worker, which closes with it.']))
    act(() => {
      store.dispatch(sseSlots([...slots, { key: 'w2', title: 'W2', running: false, crew_root: 'cond' }] as ChatSlot[]))
    })
    await waitFor(() => expect(screen.getAllByTestId('folder-cleanup-busy').map(n => n.textContent))
      .toEqual(['It has 2 open workers, which close with it.']))
    expect(read).toHaveBeenCalledTimes(2)
  })

  it('reads the count again on Close and holds the close when it changed (a handoff moves no stamp)', async () => {
    const closed: string[] = []
    vi.spyOn(api, 'deleteChatSlot').mockImplementation(async (key: string) => { closed.push(key); return {} })
    vi.spyOn(api, 'chatSlotClosePreview')
      .mockResolvedValueOnce({ workers: 0 })
      .mockResolvedValueOnce({ workers: 1 })
      .mockResolvedValue({ workers: 1 })
    vi.spyOn(api, 'cleanupChatFolders').mockImplementation(async (opts) => (
      opts.dryRun ? { ids: ['a'], archived: {} } : { deleted: ['a'] }
    ) as Cleanup)
    const slots = [
      { key: 'cond', title: 'Conductor', running: false, folder_id: 'a', crew_mode_root: true },
      { key: 'w1', title: 'W1', running: false, crew_root: 'cond' },
    ] as Slot[]
    mount(vi.fn(), { root: FOLDERS[0], slots })
    const confirm = await screen.findByTestId('folder-cleanup-confirm') as HTMLButtonElement
    await waitFor(() => expect(confirm.disabled).toBe(false))
    expect(screen.queryAllByTestId('folder-cleanup-busy')).toEqual([])
    fireEvent.click(confirm)
    expect((await screen.findByTestId('folder-cleanup-notice')).textContent)
      .toBe('The number of workers that close changed. Check the list, then press Close again.')
    expect(closed).toEqual([])
    await waitFor(() => expect(screen.getAllByTestId('folder-cleanup-busy').map(n => n.textContent))
      .toEqual(['It has 1 open worker, which closes with it.']))
    await waitFor(() => expect(confirm.disabled).toBe(false))
    fireEvent.click(confirm)
    await waitFor(() => expect(closed).toEqual(['cond']))
  })

  it('closes a conductor with open workers under StrictMode, whose double effect must not mark the panel unmounted', async () => {
    const closed: string[] = []
    vi.spyOn(api, 'deleteChatSlot').mockImplementation(async (key: string) => { closed.push(key); return {} })
    vi.spyOn(api, 'chatSlotClosePreview').mockResolvedValue({ workers: 1 })
    vi.spyOn(api, 'cleanupChatFolders').mockImplementation(async (opts) => (
      opts.dryRun ? { ids: ['a'], archived: {} } : { deleted: ['a'] }
    ) as Cleanup)
    const slots = [
      { key: 'cond', title: 'Conductor', running: false, folder_id: 'a', crew_mode_root: true },
      { key: 'w1', title: 'W1', running: false, crew_root: 'cond' },
    ] as Slot[]
    mount(vi.fn(), { root: FOLDERS[0], slots }, { strict: true })
    await waitFor(() => expect(screen.getAllByTestId('folder-cleanup-busy').map(n => n.textContent))
      .toEqual(['It has 1 open worker, which closes with it.']))
    const confirm = screen.getByTestId('folder-cleanup-confirm') as HTMLButtonElement
    await waitFor(() => expect(confirm.disabled).toBe(false))
    fireEvent.click(confirm)
    await waitFor(() => expect(closed).toEqual(['cond']))
  })

  it('holds Cancel during the recount on Close, and a dismissal mid-recount closes nothing', async () => {
    const closed: string[] = []
    vi.spyOn(api, 'deleteChatSlot').mockImplementation(async (key: string) => { closed.push(key); return {} })
    const cleanupCall = vi.spyOn(api, 'cleanupChatFolders').mockImplementation(async (opts) => (
      opts.dryRun ? { ids: ['a'], archived: {} } : { deleted: ['a'] }
    ) as Cleanup)
    let answer: (v: { workers: number }) => void = () => undefined
    vi.spyOn(api, 'chatSlotClosePreview')
      .mockResolvedValueOnce({ workers: 1 })
      .mockImplementationOnce(() => new Promise(resolve => { answer = resolve }))
    const slots = [
      { key: 'cond', title: 'Conductor', running: false, folder_id: 'a', crew_mode_root: true },
      { key: 'w1', title: 'W1', running: false, crew_root: 'cond' },
    ] as Slot[]
    mount(vi.fn(), { root: FOLDERS[0], slots })
    const confirm = await screen.findByTestId('folder-cleanup-confirm') as HTMLButtonElement
    await waitFor(() => expect(confirm.disabled).toBe(false))
    fireEvent.click(confirm)
    const cancel = screen.getByRole('button', { name: 'Cancel' }) as HTMLButtonElement
    await waitFor(() => expect(cancel.disabled).toBe(true))
    // The panel goes away (Escape, a click outside) before the recount answers.
    cleanup()
    await act(async () => { answer({ workers: 1 }) })
    expect(closed).toEqual([])
    expect(cleanupCall).not.toHaveBeenCalledWith(expect.objectContaining({ ids: ['a'], dryRun: undefined }))
  })

  it('names no number for a conductor whose close preview cannot be read', async () => {
    vi.spyOn(api, 'chatSlotClosePreview').mockRejectedValue(new Error('offline'))
    vi.spyOn(api, 'cleanupChatFolders').mockImplementation(async (opts) => (
      opts.dryRun ? { ids: ['a'], archived: {} } : { deleted: ['a'] }
    ) as Cleanup)
    const slots = [
      { key: 'cond', title: 'Conductor', running: false, folder_id: 'a', crew_mode_root: true },
      { key: 'w1', title: 'W1', running: false, crew_root: 'cond' },
    ] as Slot[]
    mount(vi.fn(), { root: FOLDERS[0], slots })
    await waitFor(() => expect(screen.getAllByTestId('folder-cleanup-busy').map(n => n.textContent))
      .toEqual(['It has open workers, which close with it.']))
    // The failure itself is surfaced through ErrorNotice, not only folded into the line.
    expect((await screen.findByTestId('folder-cleanup-workers-error')).textContent)
      .toContain("Couldn't read how many workers close with it.")
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
