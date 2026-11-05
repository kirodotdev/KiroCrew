/**
 * "Move to folder… → New folder…" in the session menus: the submenu ends with a
 * New folder… entry, and its dialog names a folder, creates it, and moves the
 * session into it in one step.
 *
 * The Radix SUBmenu cannot be opened in jsdom (see ChatSidebar.moveToFolder),
 * so the entry is checked through `FolderPickerItems`, the list the submenu
 * renders, inside an open root menu.
 */
import { describe, it, expect, vi, beforeEach, onTestFinished } from 'vitest'
import { act, fireEvent, render, screen, waitFor, renderHook } from '@testing-library/react'
import { QueryClient, QueryClientProvider, onlineManager } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import type { ReactNode } from 'react'
import type { ChatFolder, ChatSlot } from '../types'

const mocks = vi.hoisted(() => ({
  createChatFolder: vi.fn(),
  setSlotFolder: vi.fn(),
  chatFolders: vi.fn(),
}))
vi.mock('../api/client', () => ({
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (t, p: string) => (p in t ? t[p] : vi.fn().mockResolvedValue([])),
  }),
  ApiError: class ApiError extends Error {},
}))

import NewFolderMoveDialog from '../components/NewFolderMoveDialog'
import { FolderPickerItems } from '../components/FolderMoveSubmenu'
import { NewFolderMoveProvider, useNewFolderMove } from '../hooks/useNewFolderMove'
import { seedCreatedChatFolder } from '../api/chatFoldersWrite'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator } from '../components/ui/dropdown-menu'
import { store } from '../store'
import { sseSlots, sseConnected, sseDisconnected, sseSlotsPartial } from '../store/dashboardSlice'
import { clearSlotReveal } from '../store/chatSlice'

const SLOT = 'chat-nfm-1'
const folders: ChatFolder[] = [
  { id: 'f1', name: 'Alpha', order: 0 },
  { id: 'f2', name: 'Beta', order: 1 },
]

function seedSlot(folderId = '') {
  const slot: ChatSlot = { key: SLOT, title: SLOT, messages: 0, running: false, folder_id: folderId }
  store.dispatch(sseSlots([slot]))
}
const folderOf = () => store.getState().dashboard.slots.find(s => s.key === SLOT)?.folder_id

function newClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
}

/** Probe that reports the provider's open slot, so a test can see the dialog
 *  close, and opens it the way a session menu does. */
function OpenSlot() {
  const ctx = useNewFolderMove()
  return (
    <>
      <span data-testid="open-slot">{ctx?.slotKey ?? 'closed'}</span>
      <button type="button" onClick={() => ctx?.open(SLOT)}>open-for-slot</button>
      <button type="button" onClick={() => ctx?.open('chat-other')}>open-for-other</button>
    </>
  )
}

function renderDialog(qc = newClient()) {
  qc.setQueryData<ChatFolder[]>(['chat-folders'], folders)
  render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <NewFolderMoveProvider>
          <OpenSlot />
          <NewFolderMoveDialog />
        </NewFolderMoveProvider>
      </Provider>
    </QueryClientProvider>,
  )
  fireEvent.click(screen.getByText('open-for-slot'))
  return qc
}

/** A promise the test settles by hand, for a request still in flight. */
function deferred<T>() {
  let resolve!: (v: T) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

const nameInput = () => screen.getByLabelText('Folder name') as HTMLInputElement
// A regex: offline, the button's name gains the reason it is held.
const createBtn = () => screen.getByRole('button', { name: /^Create and move/ }) as HTMLButtonElement

beforeEach(() => {
  mocks.createChatFolder.mockReset()
  mocks.setSlotFolder.mockReset().mockResolvedValue({})
  mocks.chatFolders.mockReset().mockResolvedValue(folders)
  store.dispatch(sseConnected())
})

describe('FolderPickerItems New folder… entry', () => {
  function renderItems(onNewFolder?: () => void) {
    render(
      <Provider store={store}>
        <DropdownMenu open>
          <DropdownMenuContent forceMount>
            <FolderPickerItems
              folders={folders}
              onPick={vi.fn()}
              Item={DropdownMenuItem}
              Separator={DropdownMenuSeparator}
              onNewFolder={onNewFolder}
              newFolderLabel="New folder…"
            />
          </DropdownMenuContent>
        </DropdownMenu>
      </Provider>,
    )
  }

  it('ends the list with the entry, below a separator, and calls back on select', () => {
    const onNewFolder = vi.fn()
    renderItems(onNewFolder)
    const items = screen.getAllByRole('menuitem')
    expect(items.map(i => i.textContent)).toEqual(['No folder (root)', 'Alpha', 'Beta', 'New folder…'])
    expect(screen.getByRole('separator')).toBeTruthy()
    fireEvent.click(items[3])
    expect(onNewFolder).toHaveBeenCalledTimes(1)
  })

  it('dims and refuses the entry offline, naming why, like the sidebar\'s New folder row', () => {
    store.dispatch(sseDisconnected())
    const onNewFolder = vi.fn()
    renderItems(onNewFolder)
    const entry = screen.getAllByRole('menuitem')[3]
    expect(entry.getAttribute('aria-disabled')).toBe('true')
    expect(entry.getAttribute('title')).toContain('create folders')
    fireEvent.click(entry)
    expect(onNewFolder).not.toHaveBeenCalled()
    expect(screen.getByTestId('folder-move-new-folder-offline')).toBeTruthy()
  })

  it('lists existing folders only when no callback is given', () => {
    renderItems(undefined)
    expect(screen.queryByText('New folder…')).toBeNull()
    expect(screen.queryByRole('separator')).toBeNull()
  })
})

describe('NewFolderMoveDialog', () => {
  it('creates the folder at the top level and moves the session into it', async () => {
    seedSlot('')
    mocks.createChatFolder.mockResolvedValue({ id: 'new1', name: 'Inbox', order: 2 })
    // The refetch the create schedules never answers here, so what the cache
    // holds afterwards is the seed alone.
    mocks.chatFolders.mockReturnValue(new Promise(() => {}))
    const qc = renderDialog()
    expect(createBtn().disabled).toBe(true)
    // The plain path offers no "inside" choice for a session at the top level.
    expect(screen.queryByRole('checkbox')).toBeNull()
    fireEvent.change(nameInput(), { target: { value: '  Inbox  ' } })
    fireEvent.click(createBtn())
    await waitFor(() => expect(screen.getByTestId('open-slot').textContent).toBe('closed'))
    expect(mocks.createChatFolder).toHaveBeenCalledWith('Inbox', undefined)
    await waitFor(() => expect(mocks.setSlotFolder).toHaveBeenCalledWith(SLOT, 'new1'))
    expect(folderOf()).toBe('new1')
    // Seeded so the sidebar draws the new folder before the refetch lands.
    expect(qc.getQueryData<ChatFolder[]>(['chat-folders'])?.map(f => f.id)).toContain('new1')
  })

  it('submits on Enter', async () => {
    seedSlot('')
    mocks.createChatFolder.mockResolvedValue({ id: 'new2', name: 'Later', order: 2 })
    renderDialog()
    fireEvent.change(nameInput(), { target: { value: 'Later' } })
    fireEvent.keyDown(nameInput(), { key: 'Enter' })
    await waitFor(() => expect(mocks.setSlotFolder).toHaveBeenCalledWith(SLOT, 'new2'))
    expect(mocks.createChatFolder).toHaveBeenCalledWith('Later', undefined)
  })

  it('does not submit a blank name', () => {
    seedSlot('')
    renderDialog()
    fireEvent.change(nameInput(), { target: { value: '   ' } })
    fireEvent.keyDown(nameInput(), { key: 'Enter' })
    expect(createBtn().disabled).toBe(true)
    expect(mocks.createChatFolder).not.toHaveBeenCalled()
  })

  it('offers the session\'s current folder as the parent, and creates inside it when ticked', async () => {
    seedSlot('f1')
    mocks.createChatFolder.mockResolvedValue({ id: 'sub1', name: 'Sub', parent_id: 'f1', order: 0 })
    renderDialog()
    const box = screen.getByRole('checkbox', { name: 'Create inside “Alpha”' }) as HTMLInputElement
    expect(box.checked).toBe(false)
    fireEvent.click(box)
    fireEvent.change(nameInput(), { target: { value: 'Sub' } })
    fireEvent.click(createBtn())
    await waitFor(() => expect(mocks.setSlotFolder).toHaveBeenCalledWith(SLOT, 'sub1'))
    expect(mocks.createChatFolder).toHaveBeenCalledWith('Sub', 'f1')
    // A collapsed parent would hide the moved session, so its path is opened,
    // keeping the sidebar's view, search and filters (openFoldersOnly).
    expect(store.getState().chat.revealRequest).toMatchObject({ kind: 'session', target: SLOT, openFoldersOnly: true, folderId: 'sub1' })
  })

  it('creates at the top level when the parent offer is left unticked', async () => {
    seedSlot('f1')
    mocks.createChatFolder.mockResolvedValue({ id: 'top1', name: 'Top', order: 2 })
    renderDialog()
    fireEvent.change(nameInput(), { target: { value: 'Top' } })
    fireEvent.click(createBtn())
    await waitFor(() => expect(mocks.setSlotFolder).toHaveBeenCalledWith(SLOT, 'top1'))
    expect(mocks.createChatFolder).toHaveBeenCalledWith('Top', undefined)
  })

  it('holds Create while the gateway is offline, saying why in the dialog and keeping the typed name', () => {
    seedSlot('')
    store.dispatch(sseDisconnected())
    renderDialog()
    fireEvent.change(nameInput(), { target: { value: 'Inbox' } })
    expect(createBtn().disabled).toBe(true)
    // In the dialog, not only in the button's tooltip.
    expect(screen.getByTestId('new-folder-move-offline').textContent).toBe('Gateway offline — reconnect to create folders')
    fireEvent.keyDown(nameInput(), { key: 'Enter' })
    expect(mocks.createChatFolder).not.toHaveBeenCalled()
    expect(nameInput().value).toBe('Inbox')
    act(() => { store.dispatch(sseConnected()) })
    expect(screen.queryByTestId('new-folder-move-offline')).toBeNull()
  })

  it('keeps the dialog and the name when the create fails, and moves nothing', async () => {
    seedSlot('')
    mocks.createChatFolder.mockRejectedValue(new Error('Folder limit reached'))
    renderDialog()
    fireEvent.change(nameInput(), { target: { value: 'Inbox' } })
    fireEvent.click(createBtn())
    expect(await screen.findByText('Folder limit reached')).toBeTruthy()
    expect(screen.getByTestId('open-slot').textContent).toBe(SLOT)
    expect(nameInput().value).toBe('Inbox')
    expect(createBtn().disabled).toBe(false)
    expect(mocks.setSlotFolder).not.toHaveBeenCalled()
    expect(folderOf()).toBe('')
  })

  it('reports a failed move after the create, keeps the folder, and retries the move only', async () => {
    seedSlot('')
    const row = { id: 'new1', name: 'Inbox', order: 2 }
    mocks.createChatFolder.mockResolvedValue(row)
    // The retry reads the list to check the folder still exists; it does.
    mocks.chatFolders.mockResolvedValue([...folders, row])
    mocks.setSlotFolder.mockRejectedValueOnce(new Error('Gateway unreachable'))
    renderDialog()
    fireEvent.change(nameInput(), { target: { value: 'Inbox' } })
    fireEvent.click(createBtn())
    const notice = await screen.findByTestId('new-folder-move-error')
    expect(notice.textContent).toContain('Created “Inbox”, but the session was not moved into it: Gateway unreachable')
    // Open, rolled back, and the name fixed to the folder that now exists.
    expect(screen.getByTestId('open-slot').textContent).toBe(SLOT)
    // Rolled back to the top level (the store clears the field for it).
    expect(folderOf() || '').toBe('')
    expect(nameInput().readOnly).toBe(true)
    // Focus is back in the field, where Enter retries.
    expect(document.activeElement).toBe(nameInput())
    const retry = screen.getByRole('button', { name: /^Retry move/ }) as HTMLButtonElement
    fireEvent.click(retry)
    await waitFor(() => expect(screen.getByTestId('open-slot').textContent).toBe('closed'))
    expect(mocks.createChatFolder).toHaveBeenCalledTimes(1)
    expect(mocks.setSlotFolder).toHaveBeenCalledTimes(2)
    expect(mocks.setSlotFolder).toHaveBeenLastCalledWith(SLOT, 'new1')
    expect(folderOf()).toBe('new1')
  })

  it('stays open, with every dismissal refused, until the server acknowledges the move', async () => {
    seedSlot('')
    mocks.createChatFolder.mockResolvedValue({ id: 'new1', name: 'Inbox', order: 2 })
    const patch = deferred<unknown>()
    mocks.setSlotFolder.mockReturnValue(patch.promise)
    renderDialog()
    fireEvent.change(nameInput(), { target: { value: 'Inbox' } })
    fireEvent.click(createBtn())
    await waitFor(() => expect(mocks.setSlotFolder).toHaveBeenCalledWith(SLOT, 'new1'))
    const cancel = screen.getByRole('button', { name: 'Cancel' }) as HTMLButtonElement
    expect(cancel.disabled).toBe(true)
    fireEvent.click(cancel)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.getByTestId('open-slot').textContent).toBe(SLOT)
    await act(async () => { patch.resolve({}) })
    await waitFor(() => expect(screen.getByTestId('open-slot').textContent).toBe('closed'))
  })

  it('keeps offering the folder the session was in while the move is pending, not the one it creates', async () => {
    seedSlot('')
    const row = { id: 'new1', name: 'Inbox', order: 2 }
    mocks.createChatFolder.mockResolvedValue(row)
    // The server's list after the create carries the new folder, as it would.
    mocks.chatFolders.mockResolvedValue([...folders, row])
    const patch = deferred<unknown>()
    mocks.setSlotFolder.mockReturnValue(patch.promise)
    renderDialog()
    fireEvent.change(nameInput(), { target: { value: 'Inbox' } })
    fireEvent.click(createBtn())
    // The optimistic move has put the session in the new folder already, and
    // the folder is in the cache.
    await waitFor(() => expect(folderOf()).toBe('new1'))
    expect(screen.queryByRole('checkbox')).toBeNull()
    expect(screen.queryByText(/Create inside/)).toBeNull()
    await act(async () => { patch.resolve({}) })
    await waitFor(() => expect(screen.getByTestId('open-slot').textContent).toBe('closed'))
  })

  it('creates one folder when Enter and Create land before the first create answers', async () => {
    seedSlot('')
    const post = deferred<unknown>()
    mocks.createChatFolder.mockReturnValue(post.promise)
    renderDialog()
    fireEvent.change(nameInput(), { target: { value: 'Inbox' } })
    // Same tick, before any re-render could disable the controls.
    act(() => {
      fireEvent.keyDown(nameInput(), { key: 'Enter' })
      fireEvent.keyDown(nameInput(), { key: 'Enter' })
      fireEvent.click(createBtn())
    })
    await act(async () => { post.resolve({ id: 'new1', name: 'Inbox', order: 2 }) })
    await waitFor(() => expect(screen.getByTestId('open-slot').textContent).toBe('closed'))
    expect(mocks.createChatFolder).toHaveBeenCalledTimes(1)
    expect(mocks.setSlotFolder).toHaveBeenCalledTimes(1)
  })

  it('locks the name and the parent offer while the create is in flight', async () => {
    seedSlot('f1')
    const post = deferred<unknown>()
    mocks.createChatFolder.mockReturnValue(post.promise)
    renderDialog()
    fireEvent.change(nameInput(), { target: { value: 'Sub' } })
    const box = screen.getByRole('checkbox') as HTMLInputElement
    fireEvent.click(createBtn())
    await waitFor(() => expect(nameInput().readOnly).toBe(true))
    expect(box.disabled).toBe(true)
    // Read-only, not disabled: keyboard focus stays in the dialog during the write.
    expect(nameInput().disabled).toBe(false)
    expect(document.activeElement).toBe(nameInput())
    await act(async () => { post.resolve({ id: 'top1', name: 'Sub', order: 2 }) })
    await waitFor(() => expect(screen.getByTestId('open-slot').textContent).toBe('closed'))
    expect(mocks.createChatFolder).toHaveBeenCalledWith('Sub', undefined)
  })

  it('opens the subfolder\'s path after a retried move, and names the folder as the server stored it', async () => {
    seedSlot('f1')
    const row = { id: 'sub1', name: 'Storyboards (shortened)', parent_id: 'f1', order: 0 }
    mocks.createChatFolder.mockResolvedValue(row)
    mocks.chatFolders.mockResolvedValue([...folders, row])
    mocks.setSlotFolder.mockRejectedValueOnce(new Error('Gateway unreachable'))
    renderDialog()
    fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.change(nameInput(), { target: { value: 'Storyboards' } })
    fireEvent.click(createBtn())
    const notice = await screen.findByTestId('new-folder-move-error')
    // Names the parent too, so the folder can be found if the dialog is left.
    expect(notice.textContent).toContain('Created “Storyboards (shortened)” inside “Alpha”, but the session was not moved into it: Gateway unreachable')
    store.dispatch(clearSlotReveal())
    fireEvent.click(screen.getByRole('button', { name: /^Retry move/ }))
    await waitFor(() => expect(screen.getByTestId('open-slot').textContent).toBe('closed'))
    expect(mocks.createChatFolder).toHaveBeenCalledTimes(1)
    expect(store.getState().chat.revealRequest).toMatchObject({ kind: 'session', target: SLOT, openFoldersOnly: true, folderId: 'sub1' })
  })

  it('names no parent in a failed move when the parent offer was left unticked', async () => {
    seedSlot('f1')
    mocks.createChatFolder.mockResolvedValue({ id: 'top1', name: 'Top', order: 2 })
    mocks.setSlotFolder.mockRejectedValueOnce(new Error('Gateway unreachable'))
    renderDialog()
    fireEvent.change(nameInput(), { target: { value: 'Top' } })
    fireEvent.click(createBtn())
    const notice = await screen.findByTestId('new-folder-move-error')
    expect(notice.textContent).toBe('Created “Top”, but the session was not moved into it: Gateway unreachable')
  })

  it('names the parent as it is now when it was renamed before a retried move fails again', async () => {
    seedSlot('f1')
    const row = { id: 'sub1', name: 'Sub', parent_id: 'f1', order: 0 }
    mocks.createChatFolder.mockResolvedValue(row)
    // Renamed in another tab between the two tries.
    mocks.chatFolders.mockResolvedValue([{ ...folders[0], name: 'Alpha renamed' }, folders[1], row])
    mocks.setSlotFolder.mockRejectedValueOnce(new Error('Gateway unreachable')).mockRejectedValueOnce(new Error('Still unreachable'))
    renderDialog()
    fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.change(nameInput(), { target: { value: 'Sub' } })
    fireEvent.click(createBtn())
    expect((await screen.findByTestId('new-folder-move-error')).textContent).toContain('inside “Alpha”')
    fireEvent.click(screen.getByRole('button', { name: /^Retry move/ }))
    await waitFor(() => expect(screen.getByTestId('new-folder-move-error').textContent)
      .toBe('Created “Sub” inside “Alpha renamed”, but the session was not moved into it: Still unreachable'))
    expect(mocks.createChatFolder).toHaveBeenCalledTimes(1)
  })

  it('follows a parent deleted between retries: no parent named, and a re-create at the top level', async () => {
    seedSlot('f1')
    const row = { id: 'sub1', name: 'Sub', parent_id: 'f1', order: 0 }
    mocks.createChatFolder.mockResolvedValue(row)
    // Deleting a parent lifts its children to the top level.
    let listNow: ChatFolder[] = [folders[1], { ...row, parent_id: '' }]
    mocks.chatFolders.mockImplementation(() => Promise.resolve(listNow))
    mocks.setSlotFolder.mockRejectedValueOnce(new Error('Gateway unreachable')).mockRejectedValueOnce(new Error('Still unreachable'))
    renderDialog()
    fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.change(nameInput(), { target: { value: 'Sub' } })
    fireEvent.click(createBtn())
    expect((await screen.findByTestId('new-folder-move-error')).textContent).toContain('inside “Alpha”')
    fireEvent.click(screen.getByRole('button', { name: /^Retry move/ }))
    await waitFor(() => expect(screen.getByTestId('new-folder-move-error').textContent)
      .toBe('Created “Sub”, but the session was not moved into it: Still unreachable'))
    // Then the folder itself is deleted: the retry creates it again where it
    // last was, the top level, not inside the parent that is gone.
    listNow = [folders[1]]
    mocks.createChatFolder.mockResolvedValue({ id: 'sub2', name: 'Sub', order: 1 })
    fireEvent.click(screen.getByRole('button', { name: /^Retry move/ }))
    await waitFor(() => expect(screen.getByTestId('open-slot').textContent).toBe('closed'))
    expect(mocks.createChatFolder).toHaveBeenLastCalledWith('Sub', undefined)
    expect(mocks.setSlotFolder).toHaveBeenLastCalledWith(SLOT, 'sub2')
  })

  it('says the move is what waits for the gateway once the folder exists', async () => {
    seedSlot('')
    mocks.createChatFolder.mockResolvedValue({ id: 'new1', name: 'Inbox', order: 2 })
    mocks.setSlotFolder.mockRejectedValueOnce(new Error('Gateway unreachable'))
    renderDialog()
    fireEvent.change(nameInput(), { target: { value: 'Inbox' } })
    fireEvent.click(createBtn())
    await screen.findByTestId('new-folder-move-error')
    act(() => { store.dispatch(sseDisconnected()) })
    expect(screen.getByTestId('new-folder-move-offline').textContent).toBe('Gateway offline — reconnect to move sessions')
    expect(screen.getByRole('button', { name: /^Retry move/ }).getAttribute('aria-describedby')).toBe(screen.getByTestId('new-folder-move-offline').id)
  })

  it('keeps a ticked parent offer, and creates inside that folder, when a refetch briefly lacks it', async () => {
    seedSlot('f1')
    mocks.createChatFolder.mockResolvedValue({ id: 'sub1', name: 'Sub', parent_id: 'f1', order: 0 })
    const qc = renderDialog()
    fireEvent.click(screen.getByRole('checkbox'))
    act(() => { qc.setQueryData<ChatFolder[]>(['chat-folders'], [folders[1]]) })
    const box = screen.getByRole('checkbox', { name: 'Create inside “Alpha”' }) as HTMLInputElement
    expect(box.checked).toBe(true)
    fireEvent.change(nameInput(), { target: { value: 'Sub' } })
    fireEvent.click(createBtn())
    await waitFor(() => expect(screen.getByTestId('open-slot').textContent).toBe('closed'))
    expect(mocks.createChatFolder).toHaveBeenCalledWith('Sub', 'f1')
  })

  it('creates the folder again on Retry when the first one was deleted in the meantime', async () => {
    seedSlot('f1')
    mocks.createChatFolder
      .mockResolvedValueOnce({ id: 'sub1', name: 'Sub', parent_id: 'f1', order: 0 })
      .mockResolvedValueOnce({ id: 'sub2', name: 'Sub', parent_id: 'f1', order: 0 })
    mocks.setSlotFolder.mockRejectedValueOnce(new Error('Gateway unreachable'))
    renderDialog()
    fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.change(nameInput(), { target: { value: 'Sub' } })
    fireEvent.click(createBtn())
    await screen.findByTestId('new-folder-move-error')
    // The list the retry reads lacks sub1: it was deleted elsewhere.
    mocks.chatFolders.mockResolvedValue(folders)
    fireEvent.click(screen.getByRole('button', { name: /^Retry move/ }))
    await waitFor(() => expect(screen.getByTestId('open-slot').textContent).toBe('closed'))
    expect(mocks.createChatFolder).toHaveBeenCalledTimes(2)
    expect(mocks.createChatFolder).toHaveBeenLastCalledWith('Sub', 'f1')
    expect(mocks.setSlotFolder).toHaveBeenLastCalledWith(SLOT, 'sub2')
  })

  it('keeps keyboard focus in the dialog when Create is clicked', async () => {
    seedSlot('')
    const post = deferred<unknown>()
    mocks.createChatFolder.mockReturnValue(post.promise)
    renderDialog()
    fireEvent.change(nameInput(), { target: { value: 'Inbox' } })
    // Wait out the on-open focus first, so only submit can put focus back in the field.
    await waitFor(() => expect(document.activeElement).toBe(nameInput()))
    createBtn().focus()
    expect(document.activeElement).toBe(createBtn())
    fireEvent.click(createBtn())
    await waitFor(() => expect(createBtn().disabled).toBe(true))
    expect(document.activeElement).toBe(nameInput())
    await act(async () => { post.resolve({ id: 'new1', name: 'Inbox', order: 2 }) })
    await waitFor(() => expect(screen.getByTestId('open-slot').textContent).toBe('closed'))
  })

  it('holds Create until the session list has loaded since the last reconnect', () => {
    seedSlot('')
    renderDialog()
    fireEvent.change(nameInput(), { target: { value: 'Inbox' } })
    // A reconnect: the list is stale until the next slots frame.
    act(() => { store.dispatch(sseDisconnected()); store.dispatch(sseConnected()) })
    expect(createBtn().disabled).toBe(true)
    fireEvent.keyDown(nameInput(), { key: 'Enter' })
    expect(mocks.createChatFolder).not.toHaveBeenCalled()
  })

  it('holds Create on the partial list a gateway restart sends first, and clears a stale closed note', async () => {
    seedSlot('')
    renderDialog()
    fireEvent.change(nameInput(), { target: { value: 'Inbox' } })
    // The session is closed elsewhere: Create says so.
    act(() => { store.dispatch(sseSlots([{ key: 'chat-someone-else', title: 'x', messages: 0, running: false }])) })
    fireEvent.click(createBtn())
    expect(await screen.findByTestId('new-folder-move-closed')).toBeTruthy()
    // A restart's first list is partial and still lacks the session: Create waits.
    act(() => { store.dispatch(sseSlotsPartial(true)); store.dispatch(sseSlots([{ key: 'chat-someone-else', title: 'x', messages: 0, running: false }])) })
    expect(createBtn().disabled).toBe(true)
    // The complete list brings the session back: the note goes, Create works.
    act(() => {
      store.dispatch(sseSlotsPartial(false))
      store.dispatch(sseSlots([{ key: SLOT, title: SLOT, messages: 0, running: false, folder_id: '' }]))
    })
    expect(screen.queryByTestId('new-folder-move-closed')).toBeNull()
    mocks.createChatFolder.mockResolvedValue({ id: 'new1', name: 'Inbox', order: 2 })
    fireEvent.click(createBtn())
    await waitFor(() => expect(screen.getByTestId('open-slot').textContent).toBe('closed'))
    expect(mocks.createChatFolder).toHaveBeenCalledTimes(1)
  })

  it('moves the session while the browser reports offline but the gateway is connected', async () => {
    seedSlot('')
    mocks.createChatFolder.mockResolvedValue({ id: 'new1', name: 'Inbox', order: 2 })
    // A local gateway with no internet: the browser says offline, the socket is up.
    onlineManager.setOnline(false)
    onTestFinished(() => { onlineManager.setOnline(true) })
    renderDialog()
    fireEvent.change(nameInput(), { target: { value: 'Inbox' } })
    fireEvent.click(createBtn())
    await waitFor(() => expect(screen.getByTestId('open-slot').textContent).toBe('closed'))
    expect(mocks.setSlotFolder).toHaveBeenCalledWith(SLOT, 'new1')
    expect(folderOf()).toBe('new1')
  })

  it('says so when the folder list cannot be read', async () => {
    seedSlot('f1')
    mocks.chatFolders.mockRejectedValue(new Error('Folder list unavailable'))
    const qc = newClient()
    render(
      <QueryClientProvider client={qc}>
        <Provider store={store}>
          <NewFolderMoveProvider>
            <OpenSlot />
            <NewFolderMoveDialog />
          </NewFolderMoveProvider>
        </Provider>
      </QueryClientProvider>,
    )
    fireEvent.click(screen.getByText('open-for-slot'))
    expect((await screen.findByTestId('new-folder-move-folders-error')).textContent).toContain('Folder list unavailable')
  })

  it('creates nothing for a session that was closed while the dialog was open', async () => {
    seedSlot('')
    renderDialog()
    fireEvent.change(nameInput(), { target: { value: 'Inbox' } })
    act(() => { store.dispatch(sseSlots([{ key: 'chat-someone-else', title: 'x', messages: 0, running: false }])) })
    fireEvent.click(createBtn())
    const hint = await screen.findByTestId('new-folder-move-closed')
    expect(hint.textContent).toContain('This session was closed, so there is nothing to move.')
    // Status text, not an error: nothing was asked of the server.
    expect(hint.getAttribute('role')).toBe('status')
    // Nothing is left to do but Cancel.
    expect(createBtn().disabled).toBe(true)
    expect(screen.queryByTestId('new-folder-move-error')).toBeNull()
    expect(mocks.createChatFolder).not.toHaveBeenCalled()
    expect(screen.getByTestId('open-slot').textContent).toBe(SLOT)
    // Offline the list cannot say either way, so only the offline reason shows.
    act(() => { store.dispatch(sseDisconnected()) })
    expect(screen.queryByTestId('new-folder-move-closed')).toBeNull()
    expect(screen.getByTestId('new-folder-move-offline')).toBeTruthy()
    act(() => { store.dispatch(sseConnected()) })
    expect(screen.getByTestId('new-folder-move-closed')).toBeTruthy()
  })

  it('does not close a dialog opened for another session when an earlier write settles', async () => {
    seedSlot('')
    const post = deferred<unknown>()
    mocks.createChatFolder.mockReturnValue(post.promise)
    renderDialog()
    fireEvent.change(nameInput(), { target: { value: 'Inbox' } })
    fireEvent.click(createBtn())
    // The page swaps the dialog to another session while the create is in flight.
    fireEvent.click(screen.getByText('open-for-other'))
    expect(screen.getByTestId('open-slot').textContent).toBe('chat-other')
    fireEvent.change(nameInput(), { target: { value: 'Typed for the other one' } })
    await act(async () => { post.resolve({ id: 'new1', name: 'Inbox', order: 2 }) })
    await waitFor(() => expect(mocks.setSlotFolder).toHaveBeenCalledWith(SLOT, 'new1'))
    expect(screen.getByTestId('open-slot').textContent).toBe('chat-other')
    expect(nameInput().value).toBe('Typed for the other one')
  })

  it('reports a create reply without an id instead of moving the session nowhere', async () => {
    seedSlot('')
    mocks.createChatFolder.mockResolvedValue({})
    renderDialog()
    fireEvent.change(nameInput(), { target: { value: 'Inbox' } })
    fireEvent.click(createBtn())
    expect(await screen.findByTestId('new-folder-move-error')).toBeTruthy()
    expect(screen.getByTestId('open-slot').textContent).toBe(SLOT)
    expect(mocks.setSlotFolder).not.toHaveBeenCalled()
  })

  it('closes on Cancel without writing anything', async () => {
    seedSlot('')
    renderDialog()
    fireEvent.change(nameInput(), { target: { value: 'Inbox' } })
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.getByTestId('open-slot').textContent).toBe('closed'))
    expect(screen.queryByLabelText('Folder name')).toBeNull()
    expect(mocks.createChatFolder).not.toHaveBeenCalled()
  })

  it('renders nothing, and the hook reports no host, outside the provider', () => {
    const qc = newClient()
    const { container } = render(
      <QueryClientProvider client={qc}><Provider store={store}><NewFolderMoveDialog /></Provider></QueryClientProvider>,
    )
    expect(container.innerHTML).toBe('')
    const { result } = renderHook(() => useNewFolderMove())
    expect(result.current).toBeNull()
  })

  it('opens and closes through the provider', () => {
    const wrapper = ({ children }: { children: ReactNode }) => <NewFolderMoveProvider>{children}</NewFolderMoveProvider>
    const { result } = renderHook(() => useNewFolderMove(), { wrapper })
    expect(result.current?.slotKey).toBeNull()
    act(() => result.current?.open('chat-x'))
    expect(result.current?.slotKey).toBe('chat-x')
    act(() => result.current?.close())
    expect(result.current?.slotKey).toBeNull()
  })
})

describe('seedCreatedChatFolder', () => {
  it('adds the created folder once to a list that already loaded', () => {
    const qc = newClient()
    qc.setQueryData<ChatFolder[]>(['chat-folders'], folders)
    const row = { id: 'n1', name: 'New', order: 2 }
    seedCreatedChatFolder(qc, row)
    seedCreatedChatFolder(qc, row)
    expect(qc.getQueryData<ChatFolder[]>(['chat-folders'])?.map(f => f.id)).toEqual(['f1', 'f2', 'n1'])
  })

  it('leaves an unloaded cache empty, so a failed first load still reads as failed', () => {
    const qc = newClient()
    seedCreatedChatFolder(qc, { id: 'n1', name: 'New', order: 0 })
    expect(qc.getQueryData(['chat-folders'])).toBeUndefined()
  })
})
