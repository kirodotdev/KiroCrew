/**
 * A Crew Mode worker reads like a normal session: opening one on a screen too
 * narrow for the sessions list, the Workers panel and a readable chat hides the
 * sessions list, and leaving the worker brings it back. A wide screen keeps it.
 * Like preview expand, the auto-hide never writes the stored preference.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { StrictMode } from 'react'
import { render, act, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { createTestStore } from './helpers'
import type { ChatSlot } from '../types'
import { ThemeProvider } from '../hooks/useTheme'
import { __resetPanelTabs } from '../hooks/usePanelTabs'
import { sseSlots } from '../store/dashboardSlice'
import { switchSlot } from '../store/chatSlice'

vi.mock('react-virtuoso', () => ({ Virtuoso: () => null }))
vi.mock('../components/ChatInput', () => ({ default: () => null }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../components/MarkdownRenderer', () => ({ default: () => null }))
vi.mock('../components/TypewriterText', () => ({ default: () => null }))
// The drawer stands in for `sidebarOpen` itself, so a test can assert the
// session list's real visibility rather than inferring it from the toggle label
// (which is not rendered at all on mobile, or with an empty list).
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
vi.mock('../pages/ChatSidebar', () => ({ default: () => null, SIDEBAR_MIN: 200, SIDEBAR_MAX: 500 }))
vi.mock('../pages/chat/ChatSettings', () => ({ loadChatConfig: () => ({ contentWidth: 'compact' }), CONTENT_WIDTH: { compact: { messages: '800px', input: '816px' }, comfortable: { messages: '84%', input: '85%' }, full: { messages: '92%', input: '93%' } } }))
vi.mock('../pages/chat/SidePanel', () => ({
  // `expanded` is surfaced so a test can assert the panel stops claiming its
  // maximum once the user reopens the session list.
  default: ({ expanded }: { expanded?: boolean }) =>
    <div data-testid="side-panel" data-expanded={String(!!expanded)} />,
  SIDE_PANEL_MIN_W: 320,
  SIDE_PANEL_RESERVED_W: 560,
  CHAT_PANE_MIN_W: 320,
  sidePanelFillWidth: () => undefined,
}))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: null }) }))
vi.mock('../hooks/useFilteredDropdown', () => ({ useFilteredDropdown: () => ({ filtered: [], query: '', setQuery: vi.fn(), selectedIndex: 0, setSelectedIndex: vi.fn(), onKeyDown: vi.fn() }) }))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
// useIsMobile reads matchMedia at module load, so a per-test matchMedia stub
// cannot move it. Mock the hook with a mutable flag instead.
let mockIsMobile = false
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => mockIsMobile }))
vi.mock('../api/client', () => ({
  api: Object.fromEntries(
    ['sessions', 'chatSlotDetail', 'createChatSlot', 'deleteChatSlot', 'resumeChatSlot',
      'deleteSession', 'agentDetail', 'approveChatSlot', 'chatSlotAgent', 'chatSlotModel',
      'chatSlotWorkspace', 'models', 'planFromChat', 'renameSlot',
      'resolveApproval', 'screenshot', 'slackChannels', 'slackLink', 'spawnList',
      'stopChatSlot', 'uploadFiles', 'voiceSynthesize', 'workspaces', 'chatSlots',
      'notifications', 'status', 'generateTitle'].map(k => [k, vi.fn().mockResolvedValue(
      k === 'chatSlotDetail' ? { messages: [], has_more: false, total: 0 } : {},
    )]),
  ),
}))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})
globalThis.fetch = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }) as unknown as typeof fetch
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver

import ChatPage from '../pages/ChatPage'
import { crewModePreviewOn, turnOnCrewModePreview } from './crewModePreview'


const ROWS = [
  { key: 'root', title: 'Root', crew_mode_root: true },
  { key: 'w1', title: 'Worker', crew_root: 'root' },
  { key: 'plain', title: 'Plain' },
] as unknown as ChatSlot[]

function renderChat({ strict = false }: { strict?: boolean } = {}) {
  const store = createTestStore()
  act(() => { store.dispatch(sseSlots(ROWS)) })
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const tree = (
    <QueryClientProvider client={queryClient}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter initialEntries={['/chat']}>
            <Routes><Route path="/chat/:slug?" element={<ChatPage />} /></Routes>
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>
  )
  render(strict ? <StrictMode>{tree}</StrictMode> : tree)
  return store
}

const open = (store: ReturnType<typeof renderChat>, key: string) =>
  act(() => { store.dispatch(switchSlot({ key })) })

describe('ChatPage — the sessions list steps aside for a Crew Mode worker', () => {
  beforeEach(() => {
    Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: 1280 })
    localStorage.clear()
    turnOnCrewModePreview()
    __resetPanelTabs()
  })

  it('hides the list on a worker at 1280 and brings it back on the root', async () => {
    const store = renderChat()
    open(store, 'plain')
    expect(screen.getByRole('button', { name: 'Hide sessions sidebar' })).toBeInTheDocument()
    open(store, 'w1')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Show sessions sidebar' })).toBeInTheDocument())
    open(store, 'root')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Hide sessions sidebar' })).toBeInTheDocument())
    expect(localStorage.getItem('mc-sidebar-pinned')).toBeNull()
  })

  it('keeps the list on a wide screen', async () => {
    Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: 1700 })
    const store = renderChat()
    open(store, 'w1')
    await act(async () => {})
    expect(screen.getByRole('button', { name: 'Hide sessions sidebar' })).toBeInTheDocument()
  })

  it('a person who reopens the list on a worker keeps it', async () => {
    const store = renderChat({ strict: true })
    open(store, 'w1')
    fireEvent.click(await screen.findByRole('button', { name: 'Show sessions sidebar' }))
    expect(screen.getByRole('button', { name: 'Hide sessions sidebar' })).toBeInTheDocument()
    open(store, 'root')
    await act(async () => {})
    expect(screen.getByRole('button', { name: 'Hide sessions sidebar' })).toBeInTheDocument()
  })
})

// Last, after the file's own hooks that clear localStorage: Crew Mode is a feature preview.
crewModePreviewOn()
