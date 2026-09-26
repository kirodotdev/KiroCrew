/**
 * Mobile chat header: one-tap "new session here".
 *
 * On a phone the sessions list lives in a full-screen drawer, so starting a
 * sibling chat used to take three taps (open drawer, find the folder, press its
 * "+"). The title row now carries a "+" beside the sessions toggle that creates
 * a session in the SAME folder as the one on screen, with the folder's
 * inherited default agent and project directory, exactly as the drawer's folder
 * "+" resolves them.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, act, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { sseSlots, sseConnected } from '../store/dashboardSlice'
import { setActiveSlot } from '../store/chatSlice'
import { api } from '../api/client'


vi.mock('react-virtuoso', () => ({ Virtuoso: () => null }))
vi.mock('../components/ChatInput', () => ({ default: () => null }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../components/MarkdownRenderer', () => ({ default: () => null }))
vi.mock('../components/TypewriterText', () => ({ default: () => null }))
vi.mock('../components/AgentDropdownList', () => ({ default: () => null }))
vi.mock('../components/ModelDropdownList', () => ({ default: () => null }))
vi.mock('../components/InfoTip', () => ({ default: () => null }))
vi.mock('../components/SegmentedControl', () => ({ default: () => null }))
vi.mock('../pages/chat/CollapsibleToolGroup', () => ({ default: () => null }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../pages/chat/SessionColorPicker', () => ({ default: () => null }))
vi.mock('../pages/chat', () => ({ ChatFooter: () => null, AssistantMessage: () => null, McpInfoButton: () => null }))
vi.mock('../pages/ChatSidebar', () => ({
  default: () => <div data-testid="sidebar-stub" />,
  SIDEBAR_MIN: 200,
  SIDEBAR_MAX: 500,
}))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ contentWidth: 'compact' }),
  CONTENT_WIDTH: { compact: { messages: '800px', input: '816px' }, comfortable: { messages: '84%', input: '85%' }, full: { messages: '92%', input: '93%' } },
}))
vi.mock('../pages/chat/SidePanel', () => ({
  default: () => null,
  SIDE_PANEL_MIN_W: 320,
  SIDE_PANEL_RESERVED_W: 560,
  CHAT_PANE_MIN_W: 320,
  sidePanelFillWidth: () => undefined,
}))
vi.mock('../hooks/usePanelState', () => ({ usePanelState: () => ({ isOpen: false, openPanel: vi.fn(), closePanel: vi.fn() }), useDiffPanel: () => ({ isOpen: false, filePath: '', original: '', modified: '', openDiff: vi.fn(), closeDiff: vi.fn() }) }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => {
  const AGENTS = { agents: [], defaultAgent: 'global-agent' }
  return { useAgents: () => AGENTS }
})
vi.mock('../hooks/useFilteredDropdown', () => ({ useFilteredDropdown: () => ({ filtered: [], query: '', setQuery: vi.fn(), selectedIndex: 0, setSelectedIndex: vi.fn(), onKeyDown: vi.fn() }) }))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
const viewport = vi.hoisted(() => ({ isMobile: true }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => viewport.isMobile }))
vi.mock('../api/client', () => ({
  api: Object.fromEntries(
    ['sessions', 'chatSlotDetail', 'createChatSlot', 'deleteChatSlot', 'resumeChatSlot',
      'deleteSession', 'agentDetail', 'approveChatSlot', 'chatSlotAgent', 'chatSlotModel',
      'chatSlotWorkspace', 'models', 'planAction', 'planFromChat', 'renameSlot',
      'resolveApproval', 'screenshot', 'slackChannels', 'slackLink', 'spawnList',
      'stopChatSlot', 'uploadFiles', 'voiceSynthesize', 'workspaces', 'chatSlots',
      'notifications', 'status', 'generateTitle', 'chatFolders', 'setSlotColor', 'setSlotColorHex', 'chatSlotProject'].map(k => [k, vi.fn().mockResolvedValue(
      k === 'chatSlotDetail' ? { messages: [], has_more: false, total: 0 }
        : k === 'chatFolders' ? [
          { id: 'f-parent', name: 'Parent', parent_id: null, default_agent: 'folder-agent', project_dir: '/work/repo' },
          { id: 'f-child', name: 'Child', parent_id: 'f-parent', default_agent: '', project_dir: '' },
        ]
        : k === 'createChatSlot' ? { key: 'slot-new', title: '' }
        : {},
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
globalThis.fetch = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }) as never
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as never

import ChatPage from '../pages/ChatPage'

function renderChat(slot: { key: string; title: string; folder_id?: string }) {
  const store = createTestStore()
  act(() => {
    store.dispatch(sseConnected())
    store.dispatch(sseSlots([slot] as never))
    store.dispatch(setActiveSlot(slot.key))
  })
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter initialEntries={[`/chat?sid=${slot.key}`]}>
            <Routes>
              <Route path="/chat/:slug?" element={<ChatPage />} />
            </Routes>
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return store
}

const createCall = () => (api.createChatSlot as unknown as ReturnType<typeof vi.fn>).mock.calls[0]

describe('ChatPage mobile header: new session in the same folder', () => {
  beforeEach(() => {
    viewport.isMobile = true
    Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: 390 })
  })
  afterEach(() => { vi.clearAllMocks(); cleanup() })

  it('sits right after the sessions toggle in the title row', () => {
    renderChat({ key: 'slot-0', title: 'Session 0', folder_id: 'f-child' })
    const plus = screen.getByTestId('mobile-new-session-here')
    const toggle = screen.getAllByLabelText('Toggle sessions').find(b => b.nextElementSibling === plus)
    expect(toggle).toBeTruthy()
  })

  it('creates the session in the current folder with the inherited agent and project', async () => {
    renderChat({ key: 'slot-0', title: 'Session 0', folder_id: 'f-child' })
    // The folder list is a query: wait for it so the label reflects the folder.
    const plus = await screen.findByLabelText('New session in this folder')
    await waitFor(() => expect(api.chatFolders).toHaveBeenCalled())
    await act(async () => { await Promise.resolve() })
    fireEvent.click(plus)
    await waitFor(() => expect(api.createChatSlot).toHaveBeenCalledTimes(1))
    const args = createCall()
    expect(args[1]).toBe('folder-agent') // agent: nearest ancestor default_agent
    expect(args[7]).toBe('f-child') // folder_id: the on-screen session's folder
    await waitFor(() => expect(api.chatSlotProject).toHaveBeenCalledWith('slot-new', '/work/repo'))
  })

  it('an unfiled session creates an unfiled sibling on the global default agent', async () => {
    renderChat({ key: 'slot-0', title: 'Session 0' })
    fireEvent.click(screen.getByLabelText('New session'))
    await waitFor(() => expect(api.createChatSlot).toHaveBeenCalledTimes(1))
    const args = createCall()
    expect(args[1]).toBe('global-agent')
    expect(args[7]).toBeUndefined()
    expect(api.chatSlotProject).not.toHaveBeenCalled()
  })

  it('is not rendered on desktop', () => {
    viewport.isMobile = false
    renderChat({ key: 'slot-0', title: 'Session 0', folder_id: 'f-child' })
    expect(screen.queryByTestId('mobile-new-session-here')).toBeNull()
  })
})
