/**
 * "Move to folder" move contract — shared by the sidebar row menus (mobile
 * dropdown, desktop dropdown, right-click context), the session-header dropdown,
 * AND sidebar drag-to-folder. All four now route through the useMoveSlotToFolder
 * hook + the FolderMoveSubmenu's onPick.
 *
 * The Radix submenu *interaction* (open → arrow → pick) needs PointerEvent
 * support jsdom lacks (see ChatSidebar.boardNewChatInFolder.test.tsx + the
 * "New chat in folder" DropdownMenuSub, which is tested the same way — never
 * opened in jsdom). So this file locks the parts that ARE reliably testable:
 *   (1) FolderMoveSubmenu mounts its trigger inside the parent menu, and
 *   (2) useMoveSlotToFolder performs the optimistic Redux move + api call, and
 *       rolls back to the prior folder on failure.
 * The tree ordering / indentation / path-id mapping is covered by folderTree.test.ts.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, waitFor, renderHook, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import type { ChatFolder, ChatSlot } from '../types'

const mocks = vi.hoisted(() => ({ setSlotFolder: vi.fn() }))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (t, p: string) => (p in t ? t[p] : vi.fn().mockResolvedValue([])),
  }),
}))

import FolderMoveSubmenu from '../components/FolderMoveSubmenu'
import { DropdownMenu, DropdownMenuContent } from '../components/ui/dropdown-menu'

const folders: ChatFolder[] = [
  { id: 'f1', name: 'Alpha', order: 0 },
  { id: 'f2', name: 'Beta', order: 1 },
]

describe('FolderMoveSubmenu', () => {
  it('mounts its trigger with the given label inside an open menu', () => {
    render(
      <DropdownMenu open>
        <DropdownMenuContent forceMount>
          <FolderMoveSubmenu variant="dropdown" folders={folders} onPick={vi.fn()} label="Move to folder…" />
        </DropdownMenuContent>
      </DropdownMenu>,
    )
    // Content is portaled to document.body — query the whole document via screen.
    expect(screen.getByText('Move to folder…')).toBeTruthy()
  })
})

// ── The shared move hook (the real assign contract for all surfaces) ──
// The hook reads the previous folder_id from the app's singleton store at call
// time (store.getState()) and dispatches through useAppDispatch — both must hit
// the SAME store, so the test wraps renderHook in a Provider over that very
// singleton (matching how the real app is wired) and seeds it per test.
import { store } from '../store'
import { sseSlots } from '../store/dashboardSlice'
import { useMoveSlotToFolder } from '../hooks/useMoveSlotToFolder'

const SLOT = 'chat-move-1'

function seedStore(initialFolderId = '') {
  const slot: ChatSlot = { key: SLOT, title: SLOT, messages: 0, running: false, folder_id: initialFolderId }
  store.dispatch(sseSlots([slot]))
}
const folderOf = () => store.getState().dashboard.slots.find(s => s.key === SLOT)?.folder_id

function renderMove() {
  // useMoveSlotToFolder uses useMutation, so it needs a QueryClientProvider
  // in addition to the singleton store Provider.
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const wrapper = ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={qc}><Provider store={store}>{children}</Provider></QueryClientProvider>
  )
  return renderHook(() => useMoveSlotToFolder(), { wrapper }).result
}

beforeEach(() => mocks.setSlotFolder.mockResolvedValue({}))
afterEach(() => {
  vi.clearAllMocks()
  store.dispatch(sseSlots([]))  // reset slots between tests
})

describe('useMoveSlotToFolder', () => {
  it('moving to a folder optimistically updates Redux and calls setSlotFolder(key, id)', async () => {
    seedStore('')
    const move = renderMove()
    act(() => move.current(SLOT, 'f2'))
    expect(folderOf()).toBe('f2')
    await waitFor(() => expect(mocks.setSlotFolder).toHaveBeenCalledWith(SLOT, 'f2'))
  })

  it('moving to root calls setSlotFolder(key, null) and clears folder_id', async () => {
    seedStore('f1')
    const move = renderMove()
    act(() => move.current(SLOT, null))
    // The reducer normalizes the root folder ('' ) to undefined.
    expect(folderOf()).toBeUndefined()
    await waitFor(() => expect(mocks.setSlotFolder).toHaveBeenCalledWith(SLOT, null))
  })

  it('rolls back to the prior folder when the api call fails', async () => {
    mocks.setSlotFolder.mockRejectedValueOnce(new Error('boom'))
    seedStore('f1')                      // session starts in Alpha
    const move = renderMove()
    act(() => move.current(SLOT, 'f2'))  // optimistically moves to Beta
    expect(folderOf()).toBe('f2')
    await waitFor(() => expect(folderOf()).toBe('f1'))  // rolled back to Alpha
  })
})
