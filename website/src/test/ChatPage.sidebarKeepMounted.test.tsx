/**
 * Closing the desktop sessions sidebar keeps the session list mounted.
 *
 * Mounting the list is the expensive part of opening the sidebar, so the
 * desktop drawer only adopts and releases the list's host element: closing
 * takes it off screen, reopening puts the same instance back.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { useEffect } from 'react'
import { render, act, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { createTestStore } from './helpers'
import type { ChatSlot } from '../types'
import { ThemeProvider } from '../hooks/useTheme'
import { __resetPanelTabs } from '../hooks/usePanelTabs'
import { sseSlots } from '../store/dashboardSlice'
import { anotherEscapeOwnerIsOpen } from '../components/chat-input/escapeOwners'

const life = { mounts: 0, unmounts: 0, menuOpenOnMount: false }

vi.mock('react-virtuoso', () => ({ Virtuoso: () => null }))
vi.mock('../components/ChatInput', () => ({ default: () => null }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../components/MarkdownRenderer', () => ({ default: () => null }))
vi.mock('../components/TypewriterText', () => ({ default: () => null }))
// The drawer unmounts its children while closed, as the real one does once its
// exit animation ends.
vi.mock('../components/OverlayDrawer', () => ({
  default: ({ open, children }: { open?: boolean; children?: React.ReactNode }) =>
    (open ? <div data-testid="sessions-drawer">{children}</div> : null),
}))
vi.mock('../components/AgentDropdownList', () => ({ default: () => null }))
vi.mock('../components/ModelDropdownList', () => ({ default: () => null }))
vi.mock('../components/InfoTip', () => ({ default: () => null }))
vi.mock('../components/SegmentedControl', () => ({ default: () => null }))
vi.mock('../pages/chat/CollapsibleToolGroup', () => ({ default: () => null }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../pages/chat/SessionColorPicker', () => ({ default: () => null }))
vi.mock('../pages/chat', () => ({ ChatFooter: () => null, AssistantMessage: () => null, McpInfoButton: () => null }))
vi.mock('../pages/ChatSidebar', async () => {
  // A real shared menu, so the test sees what a row's menu does on close.
  const { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem } = await import('../components/ui/dropdown-menu')
  const { OverlayPortal } = await import('../components/overlayRoot')
  return {
    default: function SidebarStub() {
      useEffect(() => {
        life.mounts++
        return () => { life.unmounts++ }
      }, [])
      return (
        <div data-testid="session-list">
          <DropdownMenu defaultOpen={life.menuOpenOnMount}>
            <DropdownMenuTrigger>Row menu</DropdownMenuTrigger>
            <DropdownMenuContent><DropdownMenuItem>Rename</DropdownMenuItem></DropdownMenuContent>
          </DropdownMenu>
          {/* A hand-rolled floating layer, like the needs-me card or a folder dialog. */}
          <OverlayPortal><div role="dialog" aria-label="Folder settings" data-testid="floating-layer" /></OverlayPortal>
        </div>
      )
    },
    SIDEBAR_MIN: 200,
    SIDEBAR_MAX: 500,
  }
})
vi.mock('../pages/chat/ChatSettings', () => ({ loadChatConfig: () => ({ contentWidth: 'compact' }), CONTENT_WIDTH: { compact: { messages: '800px', input: '816px' }, comfortable: { messages: '84%', input: '85%' }, full: { messages: '92%', input: '93%' } } }))
vi.mock('../pages/chat/SidePanel', () => ({
  default: () => <div data-testid="side-panel" />,
  SIDE_PANEL_MIN_W: 320,
  SIDE_PANEL_RESERVED_W: 560,
  CHAT_PANE_MIN_W: 320,
  sidePanelFillWidth: () => undefined,
}))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: null }) }))
vi.mock('../hooks/useFilteredDropdown', () => ({ useFilteredDropdown: () => ({ filtered: [], query: '', setQuery: vi.fn(), selectedIndex: 0, setSelectedIndex: vi.fn(), onKeyDown: vi.fn() }) }))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
// useIsMobile reads matchMedia at module load; a mutable flag moves it per test.
let mockIsMobile = false
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => mockIsMobile }))
vi.mock('../api/client', () => ({
  api: new Proxy({}, { get: () => vi.fn(() => new Promise(() => {})) }),
}))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})
globalThis.fetch = vi.fn(() => new Promise(() => {})) as unknown as typeof fetch
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver

import ChatPage from '../pages/ChatPage'

function renderChat() {
  const store = createTestStore()
  const rows = [{ key: 'slot-0', title: 'Session 0' }] as unknown as ChatSlot[]
  act(() => { store.dispatch(sseSlots(rows)) })
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter initialEntries={['/chat']}>
            <Routes><Route path="/chat/:slug?" element={<ChatPage />} /></Routes>
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

describe('ChatPage — closing the sessions sidebar', () => {
  beforeEach(() => {
    Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: 1400 })
    localStorage.clear()
    __resetPanelTabs()
    life.mounts = 0
    life.menuOpenOnMount = false
    life.unmounts = 0
  })

  it('takes the session list off screen without unmounting it, and reopening shows the same one', () => {
    renderChat()
    const drawer = screen.getByTestId('sessions-drawer')
    const list = screen.getByTestId('session-list')
    expect(drawer.contains(list)).toBe(true)
    expect(life.mounts).toBe(1)

    fireEvent.click(screen.getByRole('button', { name: 'Hide sessions sidebar' }))
    expect(screen.queryByTestId('sessions-drawer')).toBeNull()
    expect(list.isConnected).toBe(false)
    expect(life.unmounts).toBe(0)

    fireEvent.click(screen.getByRole('button', { name: 'Show sessions sidebar' }))
    expect(screen.getByTestId('sessions-drawer').contains(list)).toBe(true)
    expect(life.mounts).toBe(1)
    expect(life.unmounts).toBe(0)
  })

  it('closes a menu the session list had open when the sidebar closes', async () => {
    life.menuOpenOnMount = true
    renderChat()
    expect(await screen.findByRole('menuitem', { name: 'Rename' })).toBeInTheDocument()

    // The keyboard shortcut's path: no click outside the menu closes it first.
    act(() => { window.dispatchEvent(new Event('toggle-pin-chat-sidebar')) })
    await waitFor(() => expect(screen.queryByTestId('sessions-drawer')).toBeNull())
    await waitFor(() => expect(screen.queryByRole('menuitem', { name: 'Rename' })).toBeNull())
    expect(life.unmounts).toBe(0)
  })

  it('takes a floating layer the list opened out of the page while the sidebar is closed, and brings the same one back', () => {
    renderChat()
    const layer = screen.getByTestId('floating-layer')
    expect(layer.isConnected).toBe(true)
    expect(anotherEscapeOwnerIsOpen()).toBe(true)

    fireEvent.click(screen.getByRole('button', { name: 'Hide sessions sidebar' }))
    // Gone from the document, so a dialog in it no longer claims the
    // composer's Escape (`anotherEscapeOwnerIsOpen`).
    expect(layer.isConnected).toBe(false)
    expect(anotherEscapeOwnerIsOpen()).toBe(false)

    fireEvent.click(screen.getByRole('button', { name: 'Show sessions sidebar' }))
    expect(screen.getByTestId('floating-layer')).toBe(layer)
  })

  it('on a phone, closing the drawer keeps the same list for the next open', async () => {
    mockIsMobile = true
    try {
      renderChat()
      const open = () => fireEvent.click(screen.getAllByRole('button', { name: 'Toggle sessions' })[0])
      const close = () => fireEvent.click(screen.getByTestId('sessions-backdrop'))
      open()
      const list = await screen.findByTestId('session-list')
      expect(screen.getByTestId('sessions-drawer').contains(list)).toBe(true)
      expect(life.mounts).toBe(1)

      close()
      await waitFor(() => expect(screen.queryByTestId('sessions-drawer')).toBeNull())
      expect(list.isConnected).toBe(false)
      expect(life.unmounts).toBe(0)

      open()
      await waitFor(() => expect(screen.getByTestId('sessions-drawer').contains(list)).toBe(true))
      expect(life.mounts).toBe(1)
      expect(life.unmounts).toBe(0)
    } finally {
      mockIsMobile = false
    }
  })
})
