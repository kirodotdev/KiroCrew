/**
 * Phone chat page, the PAGE end of the single-top-bar hand-off.
 *
 * The App shell renders two portal targets on the phone chat route
 * (`#mobile-topbar-slot`, `#mobile-topbar-trail-slot`) and hands the page its
 * main-navigation rail through `MobileNavRailContext`. This pins what the page
 * does with them, against a stand-in shell (the two slot elements appended to
 * `document.body`, a spy rail renderer):
 *
 *  - the title row moves INTO the slot — sessions toggle, title, session menu —
 *    and the inline copy, the fixed corner button and the transcript spacer all
 *    stand down, so the row is in exactly one place;
 *  - the overflow menu lands in the trail slot and carries the controls the old
 *    row showed as icons (pop-out, activity panel);
 *  - the sessions drawer renders the rail beside the sessions pane, asking for
 *    a close callback, and covers the full safe-area height now that no title
 *    row sits under the bar;
 *  - with NO slot (the shell did not render one: popout, embed, desktop) the
 *    page keeps today's inline row, so a missing target never means no title.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, act, screen, fireEvent, within, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { sseSlots, sseConnected } from '../store/dashboardSlice'
import { setActiveSlot } from '../store/chatSlice'
import { MobileNavRailContext, type MobileNavRailOptions } from '../components/MobileNavRailContext'

vi.mock('framer-motion', async (importOriginal) => {
  const actual = await importOriginal<typeof import('framer-motion')>()
  return {
    ...actual,
    animate: () => ({ stop: () => {} }),
  }
})
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
vi.mock('../hooks/usePanelState', () => ({ usePanelState: () => ({ isOpen: false, openPanel: vi.fn(), closePanel: vi.fn() }), useDiffPanel: () => ({ isOpen: false, filePath: '', original: '', modified: '', openDiff: vi.fn(), closeDiff: vi.fn() }) }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: null }) }))
vi.mock('../hooks/useFilteredDropdown', () => ({ useFilteredDropdown: () => ({ filtered: [], query: '', setQuery: vi.fn(), selectedIndex: 0, setSelectedIndex: vi.fn(), onKeyDown: vi.fn() }) }))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
const viewport = vi.hoisted(() => ({ isMobile: true }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => viewport.isMobile }))
vi.mock('../api/client', () => ({
  api: Object.fromEntries(
    ['sessions', 'chatSlotDetail', 'createChatSlot', 'deleteChatSlot', 'resumeChatSlot',
      'deleteSession', 'agentDetail', 'approveChatSlot', 'chatSlotAgent', 'chatSlotModel',
      'chatSlotWorkspace', 'models', 'planFromChat', 'renameSlot',
      'resolveApproval', 'screenshot', 'slackChannels', 'slackLink', 'spawnList',
      'stopChatSlot', 'uploadFiles', 'voiceSynthesize', 'workspaces', 'chatSlots',
      'notifications', 'status', 'generateTitle', 'dashboardConfig', 'projectGit',
      'mcpActive', 'mcpServers', 'kirocrewConfig'].map(k => [k, vi.fn().mockResolvedValue(
      k === 'chatSlotDetail' ? { messages: [], has_more: false, total: 0 } : {},
    )]),
  ),
  isAuthBannerShown: vi.fn(() => false),
  ApiError: class ApiError extends Error {
    status: number
    constructor(status: number, message: string) { super(message); this.status = status }
  },
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

/** The shell's side of the hand-off: the two slot elements, as `App.tsx`
 *  renders them on the phone chat route. Appended to `document.body` so the
 *  portal target exists BEFORE the page mounts, the common route-change case. */
function mountShellSlots() {
  const slot = document.createElement('div')
  slot.id = 'mobile-topbar-slot'
  const trail = document.createElement('div')
  trail.id = 'mobile-topbar-trail-slot'
  document.body.append(slot, trail)
  return { slot, trail }
}

const railSpy = vi.fn((opts: MobileNavRailOptions) => (
  <nav data-testid="rail-stub">
    <button type="button" data-testid="rail-activate" onClick={opts.onActivate}>row</button>
  </nav>
))

function renderChat({ rail = true }: { rail?: boolean } = {}) {
  const store = createTestStore()
  act(() => {
    store.dispatch(sseConnected())
    store.dispatch(sseSlots([{ key: 'slot-0', title: 'Liquid Glass Library Recap' }] as never))
    store.dispatch(setActiveSlot('slot-0'))
  })
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <Provider store={store}>
        <ThemeProvider>
          <MobileNavRailContext.Provider value={rail ? railSpy : null}>
            <MemoryRouter initialEntries={['/chat?sid=slot-0']}>
              <Routes><Route path="/chat/:slug?" element={<ChatPage />} /></Routes>
            </MemoryRouter>
          </MobileNavRailContext.Provider>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return store
}

describe('ChatPage on the phone: its share of the single top bar', () => {
  beforeEach(() => {
    viewport.isMobile = true
    Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: 390 })
    localStorage.clear()
    railSpy.mockClear()
  })
  afterEach(() => {
    cleanup()
    document.getElementById('mobile-topbar-slot')?.remove()
    document.getElementById('mobile-topbar-trail-slot')?.remove()
  })

  it('moves the title row into the shell slot and retires every inline copy of it', async () => {
    const { slot } = mountShellSlots()
    renderChat()
    // Sessions toggle, title and session menu — in the slot, in that order.
    const toggle = await within(slot).findByTestId('mobile-topbar-sessions-toggle')
    const title = within(slot).getByTestId('mobile-topbar-title')
    expect(title).toHaveTextContent('Liquid Glass Library Recap')
    // Title and chevron are ONE control: the session menu's trigger carries
    // the title text, so the centre cell holds two tap targets (toggle, menu),
    // and there is no separate rename tap beside a menu tap.
    // Accessible name = the title, then the role: the visible title is the
    // only copy on the phone, so no aria-label may replace it.
    const menu = within(title).getByRole('button', { name: /Liquid Glass Library Recap.*Session options/ })
    expect(within(title).getAllByRole('button')).toHaveLength(1)
    expect(within(slot).getAllByRole('button')).toHaveLength(2)
    // Rename and Auto-title keep a touch home: both are items of that menu.
    fireEvent.pointerDown(menu, { button: 0, ctrlKey: false })
    fireEvent.click(menu)
    expect(await screen.findByRole('menuitem', { name: /Rename/ })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: /^Regenerate title/ })).toBeInTheDocument()
    // Pop out is NOT here: the bar's trailing ⋯ menu is the phone's window
    // menu and carries it, and the same row in two adjacent menus read as two
    // different actions (UX lane).
    expect(screen.queryByRole('menuitem', { name: /Pop out to window/ })).toBeNull()
    fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' })
    expect(toggle.compareDocumentPosition(title) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    // Exactly ONE sessions toggle on the page: not the inline row's, not the
    // fixed corner button an empty chat otherwise shows.
    expect(screen.getAllByLabelText('Toggle sessions')).toHaveLength(1)
    expect(document.querySelector('.session-header-title')?.closest('#mobile-topbar-slot')).not.toBeNull()
    // The transcript's 64px header spacer clears a row that is no longer there
    // (an empty chat renders the welcome view and no transcript at all, which
    // is also spacer-free).
    expect(document.querySelector('.chat-container > .h-16')).toBeNull()
  })

  it('puts the overflow menu after the bell and carries pop-out + activity panel in it', async () => {
    const { trail } = mountShellSlots()
    renderChat()
    const more = await within(trail).findByRole('button', { name: 'More actions' })
    fireEvent.pointerDown(more, { button: 0, ctrlKey: false })
    fireEvent.click(more)
    expect(await screen.findByRole('menuitem', { name: /Pop out to window/ })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: /Open activity panel/ })).toBeInTheDocument()
    // Those controls are no longer icons in the bar: the row holds two.
    expect(screen.queryByLabelText('Pop out session to its own window')).toBeNull()
    expect(screen.queryByLabelText('Open activity panel')).toBeNull()
  })

  it('renders the rail from context beside the sessions pane, asking rows to replace history', async () => {
    mountShellSlots()
    renderChat()
    fireEvent.click(await screen.findByTestId('mobile-topbar-sessions-toggle'))
    const drawer = await screen.findByTestId('mobile-split-drawer')
    // Full safe-area height: the title row is in the bar, so nothing above the
    // panel needs to stay visible.
    expect(document.querySelector('.mobile-sessions-overlay')).toHaveClass('top-safe')
    expect(within(drawer).getByTestId('sidebar-stub')).toBeInTheDocument()
    // Rail before pane: the rail is the drawer's left column.
    const rail = within(drawer).getByTestId('rail-stub')
    const pane = within(drawer).getByTestId('sidebar-stub')
    expect(rail.compareDocumentPosition(pane) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(railSpy).toHaveBeenCalledWith(expect.objectContaining({ onActivate: expect.any(Function) }))
  })

  it('shows the sessions pane alone when the shell hands over no rail', async () => {
    mountShellSlots()
    renderChat({ rail: false })
    fireEvent.click(await screen.findByTestId('mobile-topbar-sessions-toggle'))
    expect(await screen.findByTestId('sidebar-stub')).toBeInTheDocument()
    expect(screen.queryByTestId('mobile-split-drawer')).toBeNull()
  })

  it('gives the bar\'s small icon buttons a 44px touch hit area', async () => {
    // `mc-touch-hit` grows the tap target with an invisible ::after under
    // `(pointer: coarse)` only (touchHitArea.test.ts pins the CSS); the boxes
    // themselves keep their 32-34px size.
    const { slot, trail } = mountShellSlots()
    renderChat()
    expect(await within(slot).findByTestId('mobile-topbar-sessions-toggle')).toHaveClass('mc-touch-hit')
    expect(await within(trail).findByRole('button', { name: 'More actions' })).toHaveClass('mc-touch-hit')
  })

  it('keeps the inline title row when the shell rendered no slot', async () => {
    renderChat()
    // No portal target → today's layout: the row is inline, with its own toggle
    // (and, on an empty chat, the fixed corner button beside it).
    expect((await screen.findAllByLabelText('Toggle sessions')).length).toBeGreaterThanOrEqual(1)
    expect(screen.queryByTestId('mobile-topbar-sessions-toggle')).toBeNull()
    expect(screen.queryByTestId('mobile-topbar-title')).toBeNull()
    // The session menu is the inline row's bare chevron, not the bar's
    // title-as-trigger (the title text itself is a stubbed TypewriterText here).
    expect(screen.getByRole('button', { name: 'Session options' })).toBeInTheDocument()
    expect(screen.queryByTestId('session-title-menu')).toBeNull()
    expect(screen.queryByRole('button', { name: 'More actions' })).toBeNull()
    // The inline row's 24px toggle (and the empty-chat corner button, when it
    // shows) carry the same touch hit area.
    for (const toggle of screen.getAllByLabelText('Toggle sessions')) expect(toggle).toHaveClass('mc-touch-hit')
  })
})
