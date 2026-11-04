/**
 * Mobile chat header: "new session here" in a folder that pins a default crew.
 *
 * The phone's session menu item is the second door to a folder's New chat (the
 * drawer's folder `+` is the first), so it must honour the folder's Default crew
 * the same way: a connected crew gets the chat, an offline one is reported and
 * nothing is created, and the Remote crew sessions preview off means Local.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, act, screen, fireEvent, cleanup, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { sseSlots, sseConnected } from '../store/dashboardSlice'
import { setActiveSlot } from '../store/chatSlice'
import { setWarm } from '../store/instancesSlice'
import { api } from '../api/client'
import { PREVIEW_REMOTE_CREW_CHAT } from '../utils/previewFlags'
import { closeCrewWindow, currentCrewWindow } from '../pages/chat/crew-window/crewWindowStore'


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
      'chatSlotWorkspace', 'models', 'planFromChat', 'renameSlot', 'instancesCapabilities', 'crewPeerPost', 'dashboardConfig',
      'resolveApproval', 'screenshot', 'slackChannels', 'slackLink', 'spawnList',
      'stopChatSlot', 'uploadFiles', 'voiceSynthesize', 'workspaces', 'chatSlots',
      'notifications', 'status', 'generateTitle', 'chatFolders', 'setSlotColor', 'setSlotColorHex', 'chatSlotProject'].map(k => [k, vi.fn().mockResolvedValue(
      k === 'chatSlotDetail' ? { messages: [], has_more: false, total: 0 }
        : k === 'chatFolders' ? [
          { id: 'f-crew', name: 'Billing', parent_id: null, default_agent: '', project_dir: '', default_crew: 'i-nobita' },
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

let lastLocation = ''
function LocationProbe() {
  const loc = useLocation()
  lastLocation = `${loc.pathname}${loc.search}`
  return null
}

function renderChat(slot: { key: string; title: string; folder_id?: string }, warm?: Record<string, unknown>, opts: { embedded?: boolean } = {}) {
  const store = createTestStore(warm ? { instances: { warm } as never } : undefined)
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
          <MemoryRouter initialEntries={[opts.embedded ? `/embed/chat?sid=${slot.key}` : `/chat?sid=${slot.key}`]}>
            <LocationProbe />
            <Routes>
              <Route path="/chat/:slug?" element={<ChatPage />} />
              {/* The SDK's default ChatPanel: embedded, no embedMode. */}
              <Route path="/embed/chat/:slug?" element={<ChatPage embedded />} />
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

const WARM = { 'i-nobita': { port: 7879, token: 't' } }
async function pressNewSessionHere() {
  await waitFor(() => expect(api.chatFolders).toHaveBeenCalled())
  await act(async () => {})
  fireEvent.click(await newSessionItem())
}
const asMock = (fn: unknown) => fn as ReturnType<typeof vi.fn>

describe('ChatPage mobile header: new session in a folder with a default crew', () => {
  beforeEach(() => {
    viewport.isMobile = true
    Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: 390 })
    localStorage.clear()
    closeCrewWindow()
    asMock(api.instancesCapabilities).mockResolvedValue({ version_match: true, version: '0.9.0', local_version: '0.9.0' })
    asMock(api.dashboardConfig).mockResolvedValue({ default_memory_mode: 'persistent' })
    asMock(api.crewPeerPost).mockResolvedValue({ key: 'peer-new-1' })
  })
  afterEach(() => { vi.clearAllMocks(); cleanup(); closeCrewWindow() })

  it('starts the chat on the connected crew and creates nothing locally', async () => {
    localStorage.setItem(PREVIEW_REMOTE_CREW_CHAT, '1')
    renderChat({ key: 'slot-0', title: 'Session 0', folder_id: 'f-crew' }, WARM)
    await pressNewSessionHere()
    await waitFor(() => expect(api.crewPeerPost).toHaveBeenCalledWith('i-nobita', 'api/chat/slots', { memory_mode: 'persistent' }))
    await waitFor(() => expect(currentCrewWindow()).toEqual({ instanceId: 'i-nobita', key: 'peer-new-1' }))
    expect(api.createChatSlot).not.toHaveBeenCalled()
  })

  it('opens the new crew chat on an embedded page instead of the global crew window it ignores', async () => {
    // GPT r1 on 3f324dceba: an embedded page shows only its own `crew`/`key`
    // route, so the global window target left the panel on the old chat.
    localStorage.setItem(PREVIEW_REMOTE_CREW_CHAT, '1')
    renderChat({ key: 'slot-0', title: 'Session 0', folder_id: 'f-crew' }, WARM, { embedded: true })
    await pressNewSessionHere()
    await waitFor(() => expect(lastLocation).toBe('/embed/chat?crew=i-nobita&key=peer-new-1'))
    expect(currentCrewWindow()).toBeNull()
    expect(api.createChatSlot).not.toHaveBeenCalled()
  })

  it('reports an offline crew and creates nothing, locally or on the crew', async () => {
    localStorage.setItem(PREVIEW_REMOTE_CREW_CHAT, '1')
    renderChat({ key: 'slot-0', title: 'Session 0', folder_id: 'f-crew' }, {})
    await pressNewSessionHere()
    expect(await screen.findByText(/i-nobita is offline/)).toBeTruthy()
    // A state, not a failure: the warn-tone status slot, not the error one.
    const status = screen.getByTestId('action-status')
    expect(status.getAttribute('data-tone')).toBe('warn')
    expect(screen.queryByTestId('action-error')).toBeNull()
    expect(api.crewPeerPost).not.toHaveBeenCalled()
    expect(api.createChatSlot).not.toHaveBeenCalled()
    // The menu has closed, so the remedy sits INSIDE the notice, next to the
    // state it fixes, as the sidebar's does (UX r121).
    const local = within(status).getByTestId('action-status-action')
    expect(local.textContent).toMatch(/New chat in .+ on Local/)
    fireEvent.click(local)
    await waitFor(() => expect(api.createChatSlot).toHaveBeenCalledTimes(1))
    expect(createCall()[7]).toBe('f-crew')
    expect(api.crewPeerPost).not.toHaveBeenCalled()
    expect(screen.queryByTestId('action-status')).toBeNull()
  })

  it('clears the offline notice once a later tap starts the chat on the reconnected crew', async () => {
    // Opus r1 on af2e73d97b: the crew window opens without a slot switch, so
    // the offline notice and its Local button outlived the chat that started.
    localStorage.setItem(PREVIEW_REMOTE_CREW_CHAT, '1')
    // The full default slice (nothing warm), so setWarm finds its `mru` list.
    const { store } = renderChat({ key: 'slot-0', title: 'Session 0', folder_id: 'f-crew' })
    await pressNewSessionHere()
    expect(await screen.findByText(/i-nobita is offline/)).toBeTruthy()
    act(() => { store.dispatch(setWarm({ id: 'i-nobita', conn: WARM['i-nobita'] as never })) })
    fireEvent.click(await newSessionItem())
    await waitFor(() => expect(currentCrewWindow()).toEqual({ instanceId: 'i-nobita', key: 'peer-new-1' }))
    await waitFor(() => expect(screen.queryByTestId('action-status')).toBeNull())
    expect(api.createChatSlot).not.toHaveBeenCalled()
  })

  it('offers this one chat on Local while the crew is offline, and keeps the folder in place', async () => {
    // The phone's counterpart of the sidebar notice's "Start this chat on
    // Local instead": the menu carries it, and it starts a local chat in the
    // same folder without touching the crew.
    localStorage.setItem(PREVIEW_REMOTE_CREW_CHAT, '1')
    renderChat({ key: 'slot-0', title: 'Session 0', folder_id: 'f-crew' }, {})
    await waitFor(() => expect(api.chatFolders).toHaveBeenCalled())
    await act(async () => {})
    await openSessionMenu()
    const local = await screen.findByTestId('mobile-new-session-here-local')
    // It names a NEW chat in the folder: this menu belongs to an open session,
    // and "Start this chat on Local" read as moving it.
    expect(local.textContent).toMatch(/New chat in .+ on Local/)
    expect(local.textContent).not.toMatch(/this chat/)
    fireEvent.click(local)
    await waitFor(() => expect(api.createChatSlot).toHaveBeenCalledTimes(1))
    expect(createCall()[7]).toBe('f-crew')
    expect(api.crewPeerPost).not.toHaveBeenCalled()
  })

  it('offers no Local item while the crew is connected', async () => {
    localStorage.setItem(PREVIEW_REMOTE_CREW_CHAT, '1')
    renderChat({ key: 'slot-0', title: 'Session 0', folder_id: 'f-crew' }, WARM)
    await waitFor(() => expect(api.chatFolders).toHaveBeenCalled())
    await act(async () => {})
    await openSessionMenu()
    await screen.findByTestId('mobile-new-session-here')
    expect(screen.queryByTestId('mobile-new-session-here-local')).toBeNull()
  })

  it('creates a local chat in the folder while the Remote crew sessions preview is off', async () => {
    renderChat({ key: 'slot-0', title: 'Session 0', folder_id: 'f-crew' }, WARM)
    await pressNewSessionHere()
    await waitFor(() => expect(api.createChatSlot).toHaveBeenCalledTimes(1))
    expect(createCall()[7]).toBe('f-crew')
    expect(api.crewPeerPost).not.toHaveBeenCalled()
  })
})
