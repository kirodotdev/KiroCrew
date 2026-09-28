import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders, createTestStore } from './helpers'
import { sseSlots } from '../store/dashboardSlice'
import {
  consumeChatHandoff,
  installSoftNavigate,
  __resetErrorJournalForTests,
  __resetNavSeamForTests,
} from '../utils/errorReport'
import type { ChatSlot } from '../types'
import SessionActionsMenu from '../components/SessionActionsMenu'
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuTrigger,
} from '../components/ui/context-menu'

const mocks = vi.hoisted(() => ({
  exportSession: vi.fn(),
  chatFolders: vi.fn(),
}))

vi.mock('../api/client', () => ({
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (target, property: string) => (
      property in target ? target[property] : vi.fn().mockResolvedValue([])
    ),
  }),
}))

vi.mock('../components/FolderMoveSubmenu', () => ({ default: () => null }))
vi.mock('../components/SendToInstanceSubmenu', () => ({ default: () => null }))
vi.mock('../components/SessionColorSwatches', () => ({ default: () => null }))
vi.mock('../components/LinkedSurfacesSection', () => ({ default: () => null }))
vi.mock('../hooks/useSessionActions', () => ({
  useSessionActions: () => ({
    toggleRead: vi.fn(),
    togglePin: vi.fn(),
    toggleMode: vi.fn(),
    copyLink: vi.fn(),
    move: vi.fn(),
    reload: vi.fn(),
    close: vi.fn(),
  }),
}))
vi.mock('../hooks/useChatPopouts', () => ({
  useChatPopouts: () => ({
    isPoppedOut: () => false,
    isSelfPopout: () => false,
    open: vi.fn(),
    focus: vi.fn(),
    bringBack: vi.fn(),
    returnSelfToMain: vi.fn(),
  }),
}))
vi.mock('../hooks/useTagPopover', () => ({
  useTagPopover: () => ({ open: vi.fn() }),
}))

function mount() {
  const store = createTestStore()
  store.dispatch(sseSlots([{
    key: 'context-slot',
    messages: 1,
    running: false,
    memory_mode: 'persistent',
  } as ChatSlot]))
  const view = renderWithProviders(
    <ContextMenu>
      <ContextMenuTrigger asChild>
        <button type="button" data-testid="context-trigger">Actions</button>
      </ContextMenuTrigger>
      <ContextMenuContent>
        <SessionActionsMenu variant="context" slotKey="context-slot" />
      </ContextMenuContent>
    </ContextMenu>,
    { store },
  )
  fireEvent.contextMenu(screen.getByTestId('context-trigger'))
  return view
}

beforeEach(() => {
  vi.clearAllMocks()
  mocks.chatFolders.mockResolvedValue([])
  mocks.exportSession.mockRejectedValue(new Error('context export refused'))
  __resetErrorJournalForTests()
  __resetNavSeamForTests()
  sessionStorage.clear()
  installSoftNavigate(() => {})
})

afterEach(() => {
  __resetNavSeamForTests()
  vi.restoreAllMocks()
})

describe('SessionActionsMenu context-menu error hand-off', () => {
  it('keeps export activation on its row and gives Space to the sibling item', async () => {
    const user = userEvent.setup()
    mount()

    const exportRow = await screen.findByRole('menuitem', { name: /export to a file/i })
    exportRow.focus()
    await user.keyboard('{Enter}')
    await screen.findByRole('alert')
    expect(mocks.exportSession).toHaveBeenCalledTimes(1)

    exportRow.focus()
    await user.keyboard('{Enter}')
    await waitFor(() => expect(mocks.exportSession).toHaveBeenCalledTimes(2))
    await screen.findByRole('alert')

    exportRow.focus()
    await user.keyboard('{ArrowDown}')
    const handoff = screen.getByRole('menuitem', { name: /^ask the agent$/i })
    expect(handoff).toHaveFocus()
    await user.keyboard(' ')

    expect(consumeChatHandoff()).toContain('context export refused')
    expect(mocks.exportSession).toHaveBeenCalledTimes(2)
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
  })
})
