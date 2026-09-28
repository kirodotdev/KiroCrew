/** The session menu's move-to submenu lists chat folders in the sidebar's folder
 *  order (`dashboard.folder_sort`). When the settings read behind that order
 *  fails, the submenu is drawn in the stored order -- a different list than the
 *  one the person chose. One screen says that once: while the sidebar (and its
 *  banner) is on screen the menu stays quiet; when it is not (mobile with the
 *  drawer closed, desktop with the panel collapsed, embed chat) the menu says it
 *  in the rule's in-menu form -- a passive alert with the server's own words and
 *  the plain subline, plus the hand-off as a sibling menu item described by it.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders, createTestStore } from './helpers'
import { sseSlots } from '../store/dashboardSlice'
import {
  consumeChatHandoff,
  installSoftNavigate,
  __resetErrorJournalForTests,
  __resetNavSeamForTests,
} from '../utils/errorReport'
import type { ChatFolder, ChatSlot } from '../types'
import SessionActionsMenu from '../components/SessionActionsMenu'
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuTrigger,
} from '../components/ui/context-menu'

const mocks = vi.hoisted(() => ({
  chatFolders: vi.fn(),
  kirocrewConfig: vi.fn(),
}))

vi.mock('../api/client', () => ({
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (target, property: string) => (
      property in target ? target[property] : vi.fn().mockResolvedValue([])
    ),
  }),
}))

// The submenu itself is a Radix Sub, flaky under jsdom; its presence is what
// this file gates on, so a stub that renders a marker is enough.
vi.mock('../components/FolderMoveSubmenu', () => ({ default: () => <div data-testid="move-submenu" /> }))
vi.mock('../components/SendToInstanceSubmenu', () => ({ default: () => null }))
vi.mock('../components/SessionColorSwatches', () => ({ default: () => null }))
vi.mock('../components/LinkedSurfacesSection', () => ({ default: () => null }))
vi.mock('../components/ExportSessionItem', () => ({ default: () => null }))
vi.mock('../components/ImportSessionItem', () => ({ default: () => null }))
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

const FOLDERS: ChatFolder[] = [{ id: 'work', name: 'Work', order: 0 }]

function mount(props: { sidebarOnScreen?: boolean } = {}) {
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
        <SessionActionsMenu variant="context" slotKey="context-slot" {...props} />
      </ContextMenuContent>
    </ContextMenu>,
    { store },
  )
  fireEvent.contextMenu(screen.getByTestId('context-trigger'))
  return view
}

beforeEach(() => {
  vi.clearAllMocks()
  mocks.chatFolders.mockResolvedValue(FOLDERS)
  mocks.kirocrewConfig.mockResolvedValue({ dashboard: { folder_sort: 'name' } })
  __resetErrorJournalForTests()
  __resetNavSeamForTests()
  sessionStorage.clear()
  installSoftNavigate(() => {})
})

afterEach(() => {
  __resetNavSeamForTests()
  vi.restoreAllMocks()
})

describe('SessionActionsMenu folder-order read failure', () => {
  it('draws the submenu and says nothing itself, whether the order reads fine or not', async () => {
    mount({ sidebarOnScreen: true })
    await screen.findByTestId('move-submenu')
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.queryByRole('menuitem', { name: /^ask the agent$/i })).toBeNull()
    expect(screen.queryByTestId('session-menu-folder-order-detail')).toBeNull()
  })

  it('stays quiet on a failed read while the sidebar is on screen: its banner says it once', async () => {
    mocks.kirocrewConfig.mockRejectedValue(new Error('gateway restarting'))
    mount({ sidebarOnScreen: true })
    await screen.findByTestId('move-submenu')
    // Long enough for the failed read to settle into the menu's tree.
    await screen.findByRole('menuitem', { name: /tags/i })
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.queryByText('gateway restarting')).toBeNull()
    expect(screen.queryByRole('menuitem', { name: /^ask the agent$/i })).toBeNull()
    expect(screen.queryByTestId('session-menu-folder-order-detail')).toBeNull()
  })

  it('with the sidebar OFF screen (drawer closed, panel collapsed) the folder row carries the in-menu pair', async () => {
    // The header's menu on mobile with the drawer closed, or on desktop with the
    // sidebar collapsed: the banner is not on the screen, so this is the one place
    // the person can learn why the folder list is not in the order they chose --
    // in the rule's in-menu form: a PASSIVE notice with the server's own words
    // (`askAgent` off inside menu content), the plain subline under it, and the
    // hand-off as a sibling menu item the roving focus reaches, described by the
    // notice's id.
    const user = userEvent.setup()
    mocks.kirocrewConfig.mockRejectedValue(new Error('gateway restarting'))
    mount({ sidebarOnScreen: false })
    await screen.findByTestId('move-submenu')
    const notice = await screen.findByTestId('session-menu-folder-order-unavailable')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(notice).toHaveTextContent('Folder order could not be read')
    expect(notice).toHaveTextContent('gateway restarting')
    expect(notice.querySelector('button')).toBeNull()
    const detail = screen.getByTestId('session-menu-folder-order-detail')
    // A picker's line, and no "open the sidebar" pointer: the hand-off is the
    // very next menu item.
    expect(detail).toHaveTextContent('All folders are shown, in your Custom order; retries automatically')
    expect(detail).not.toHaveTextContent('sidebar')
    expect(detail.getAttribute('role')).toBeNull()
    const handoff = screen.getByRole('menuitem', { name: /^ask the agent$/i })
    expect(notice.id).not.toBe('')
    expect(handoff).toHaveAttribute('aria-describedby', notice.id)
    // The block is closed after the hand-off: a rule between it and the regular
    // items below, so "Tags…" does not read as part of the failure.
    const after = handoff.nextElementSibling
    expect(after?.getAttribute('role')).toBe('separator')
    expect(after?.nextElementSibling).toHaveTextContent(/tags/i)
    handoff.focus()
    await user.keyboard('{Enter}')
    expect(consumeChatHandoff()).toContain('gateway restarting')
  })

  it('says nothing off screen while the read works, and nothing without folders to list', async () => {
    mount({ sidebarOnScreen: false })
    await screen.findByTestId('move-submenu')
    await screen.findByRole('menuitem', { name: /tags/i })
    expect(screen.queryByTestId('session-menu-folder-order-detail')).toBeNull()
    cleanup()
    mocks.chatFolders.mockResolvedValue([])
    mocks.kirocrewConfig.mockRejectedValue(new Error('gateway restarting'))
    mount({ sidebarOnScreen: false })
    await screen.findByRole('menuitem', { name: /tags/i })
    expect(screen.queryByTestId('move-submenu')).toBeNull()
    expect(screen.queryByTestId('session-menu-folder-order-detail')).toBeNull()
  })
})
