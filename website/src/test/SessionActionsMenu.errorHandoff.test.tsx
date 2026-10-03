import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
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
import { markSubagentApprovalGone, reconcileSubagentApprovalGone, sseSubagentPending } from '../store/chatSlice'
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

function mount(seed?: (store: ReturnType<typeof createTestStore>) => void) {
  const store = createTestStore()
  store.dispatch(sseSlots([{
    key: 'context-slot',
    messages: 1,
    running: false,
    memory_mode: 'persistent',
  } as ChatSlot]))
  seed?.(store)
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
  return { ...view, store }
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
  it('keeps Reload blocked while gone-approval liveness is unresolved, then releases it when absent', async () => {
    const { store } = mount(s => {
      s.dispatch(sseSubagentPending({ slot: 'context-slot', id: 'p1', task: 'wait', approval_id: 'ap-1' }))
      s.dispatch(markSubagentApprovalGone({ id: 'p1', approval_id: 'ap-1' }))
    })
    const reload = await screen.findByRole('menuitem', { name: /reload session/i })
    expect(reload).toHaveAttribute('data-disabled')
    expect(reload).toHaveTextContent('sub-agents working')

    act(() => {
      store.dispatch(reconcileSubagentApprovalGone({
        slot: 'context-slot', id: 'p1', approval_id: 'ap-1', agent: null,
      }))
    })

    await waitFor(() => expect(reload).not.toHaveAttribute('data-disabled'))
    expect(reload).not.toHaveTextContent('sub-agents working')
  })

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
