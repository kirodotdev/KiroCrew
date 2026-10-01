/**
 * Mobile chat header: "new session here" from the session menu.
 *
 * On a phone the sessions list lives in a full-screen drawer, so starting a
 * sibling chat used to take three taps (open drawer, find the folder, press its
 * "+"). The session (chevron) menu now leads with "New chat in {folder}", which
 * creates a session in the SAME folder as the one on screen, with the folder's
 * inherited default agent and project directory, exactly as the drawer's folder
 * "+" resolves them. It is a menu item, not a third control in the bar's centre
 * cell (AUTOSDE max-two-buttons-per-row).
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
  return { store, queryClient }
}

const createCall = () => (api.createChatSlot as unknown as ReturnType<typeof vi.fn>).mock.calls[0]

// The item lives in the session (chevron) menu, which Radix mounts only while
// open, so every read of it opens the menu first.
async function openSessionMenu() {
  const trigger = screen.getByRole('button', { name: /Session options/ })
  fireEvent.pointerDown(trigger, { button: 0, ctrlKey: false })
  fireEvent.click(trigger)
  return screen.findByRole('menu')
}
async function newSessionItem() {
  await openSessionMenu()
  return screen.findByTestId('mobile-new-session-here')
}
const isDisabled = (el: HTMLElement) => el.hasAttribute('data-disabled')

describe('ChatPage mobile header: new session in the same folder', () => {
  beforeEach(() => {
    viewport.isMobile = true
    Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: 390 })
  })
  afterEach(() => { vi.clearAllMocks(); cleanup() })

  it('leads the session menu and adds no control beside the sessions toggle', async () => {
    renderChat({ key: 'slot-0', title: 'Session 0', folder_id: 'f-child' })
    // The row keeps its two controls: the toggle sits right before the menu
    // trigger, with no new-chat button between them.
    const toggle = screen.getAllByLabelText('Toggle sessions')[0]
    expect(toggle.parentElement?.querySelector('[data-testid="mobile-new-session-here"]')).toBeNull()
    const menu = await openSessionMenu()
    const items = menu.querySelectorAll('[role="menuitem"]')
    expect(items[0]).toBe(screen.getByTestId('mobile-new-session-here'))
  })

  it('creates the session in the current folder with the inherited agent and project', async () => {
    renderChat({ key: 'slot-0', title: 'Session 0', folder_id: 'f-child' })
    // The folder list is a query: wait for it so the label names the folder.
    await waitFor(() => expect(api.chatFolders).toHaveBeenCalled())
    await act(async () => {})
    const item = await newSessionItem()
    expect(item.textContent).toContain('New chat in Child')
    expect(isDisabled(item)).toBe(false)
    fireEvent.click(item)
    await waitFor(() => expect(api.createChatSlot).toHaveBeenCalledTimes(1))
    const args = createCall()
    expect(args[1]).toBe('folder-agent') // agent: nearest ancestor default_agent
    expect(args[7]).toBe('f-child') // folder_id: the on-screen session's folder
    await waitFor(() => expect(api.chatSlotProject).toHaveBeenCalledWith('slot-new', '/work/repo'))
  })

  it('an unfiled session creates an unfiled sibling on the global default agent', async () => {
    renderChat({ key: 'slot-0', title: 'Session 0' })
    const item = await newSessionItem()
    expect(item.textContent).toContain('New chat')
    fireEvent.click(item)
    await waitFor(() => expect(api.createChatSlot).toHaveBeenCalledTimes(1))
    const args = createCall()
    expect(args[1]).toBe('global-agent')
    expect(args[7]).toBeUndefined()
    expect(api.chatSlotProject).not.toHaveBeenCalled()
  })

  it('stays disabled while the folder list has not loaded, so no create drops the folder agent', async () => {
    let release: (v: unknown) => void = () => {}
    ;(api.chatFolders as unknown as ReturnType<typeof vi.fn>).mockImplementationOnce(() => new Promise(r => { release = r }))
    renderChat({ key: 'slot-0', title: 'Session 0', folder_id: 'f-child' })
    const item = await newSessionItem()
    expect(isDisabled(item)).toBe(true)
    fireEvent.click(item)
    expect(api.createChatSlot).not.toHaveBeenCalled()
    await act(async () => { release([{ id: 'f-child', name: 'Child', parent_id: null, default_agent: '', project_dir: '' }]) })
    await waitFor(() => expect(isDisabled(screen.getByTestId('mobile-new-session-here'))).toBe(false))
  })

  it('surfaces a failed create instead of swallowing it', async () => {
    ;(api.createChatSlot as unknown as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('gateway said no'))
    renderChat({ key: 'slot-0', title: 'Session 0' })
    fireEvent.click(await newSessionItem())
    expect(await screen.findByText(/gateway said no/)).toBeTruthy()
  })

  it('reports a failed folder list on tap instead of leaving a dead item', async () => {
    // Twice: opening the menu mounts its folder submenu's observer of the same
    // query, which retries a failed read once.
    ;(api.chatFolders as unknown as ReturnType<typeof vi.fn>)
      .mockRejectedValueOnce(new Error('folders unavailable'))
      .mockRejectedValueOnce(new Error('folders unavailable'))
    renderChat({ key: 'slot-0', title: 'Session 0', folder_id: 'f-child' })
    await waitFor(() => expect(api.chatFolders).toHaveBeenCalled())
    await act(async () => {})
    const item = await newSessionItem()
    await waitFor(() => expect(isDisabled(screen.getByTestId('mobile-new-session-here'))).toBe(false))
    fireEvent.click(item)
    expect(await screen.findByText(/folders unavailable/)).toBeTruthy()
    expect(api.createChatSlot).not.toHaveBeenCalled()
  })

  it('creates an unfiled sibling when the session names a folder that no longer exists', async () => {
    renderChat({ key: 'slot-0', title: 'Session 0', folder_id: 'f-gone' })
    await waitFor(() => expect(api.chatFolders).toHaveBeenCalled())
    await act(async () => {})
    const item = await newSessionItem()
    await waitFor(() => expect(isDisabled(screen.getByTestId('mobile-new-session-here'))).toBe(false))
    fireEvent.click(item)
    await waitFor(() => expect(api.createChatSlot).toHaveBeenCalled())
    expect(createCall()[1]).toBe('global-agent')
    expect(createCall()[7]).toBeFalsy()
  })

  it('makes one session from a repeated select while the create is in flight', async () => {
    let finish: (v: unknown) => void = () => {}
    ;(api.createChatSlot as unknown as ReturnType<typeof vi.fn>).mockImplementationOnce(() => new Promise(r => { finish = r }))
    renderChat({ key: 'slot-0', title: 'Session 0' })
    fireEvent.click(await newSessionItem())
    await waitFor(() => expect(api.createChatSlot).toHaveBeenCalledTimes(1))
    // Reopen while the create is pending: the item is disabled and inert.
    const again = await newSessionItem()
    expect(isDisabled(again)).toBe(true)
    fireEvent.click(again)
    await act(async () => { finish({ key: 'slot-new', title: '' }) })
    expect(api.createChatSlot).toHaveBeenCalledTimes(1)
  })

  it('keeps using cached folders after a failed background refetch', async () => {
    const { queryClient } = renderChat({ key: 'slot-0', title: 'Session 0', folder_id: 'f-child' })
    await waitFor(() => expect(api.chatFolders).toHaveBeenCalled())
    await act(async () => {})
    ;(api.chatFolders as unknown as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('refetch failed'))
    await act(async () => { await queryClient.refetchQueries({ queryKey: ['chat-folders'] }) })
    await waitFor(() => expect(queryClient.getQueryState(['chat-folders'])?.status).toBe('error'))
    const item = await newSessionItem()
    expect(item.textContent).toContain('New chat in Child')
    fireEvent.click(item)
    await waitFor(() => expect(api.createChatSlot).toHaveBeenCalledTimes(1))
    expect(createCall()[1]).toBe('folder-agent')
    expect(createCall()[7]).toBe('f-child')
    expect(screen.queryByText(/refetch failed/)).toBeNull()
  })

  it('names the stale folder project directory like the drawer does', async () => {
    const stale = Object.assign(new Error('Not a directory'), { name: 'ApiError' })
    // A fresh slot object: the shared mock value is frozen by an earlier test's store.
    ;(api.createChatSlot as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ key: 'slot-new', title: '' })
    ;(api.chatSlotProject as unknown as ReturnType<typeof vi.fn>).mockRejectedValueOnce(stale)
    renderChat({ key: 'slot-0', title: 'Session 0', folder_id: 'f-child' })
    await waitFor(() => expect(api.chatFolders).toHaveBeenCalled())
    await act(async () => {})
    fireEvent.click(await newSessionItem())
    expect(await screen.findByText(/project directory no longer exists: \/work\/repo/)).toBeTruthy()
    // The body keeps the raw error text: the notice's ask-agent hand-off looks
    // the failure up in the error journal by that exact string.
    expect(screen.getByText(/^\s*Not a directory\s*$/)).toBeTruthy()
  })

  it('is not offered on desktop', async () => {
    viewport.isMobile = false
    renderChat({ key: 'slot-0', title: 'Session 0', folder_id: 'f-child' })
    await openSessionMenu()
    expect(screen.queryByTestId('mobile-new-session-here')).toBeNull()
  })
})
