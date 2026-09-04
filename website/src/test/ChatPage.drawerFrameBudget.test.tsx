/**
 * Frame budget of the mobile sessions drawer.
 *
 * The drawer slides on a main-thread rAF (a framer MotionValue the scrim also
 * reads), so anything the main thread does during those ~0.32s is subtracted
 * directly from the animation. Two things used to help themselves to it, and
 * both got worse exactly when the user has a lot going on:
 *
 *  (1) ChatPage re-renders once per frame while anything streams — chunks are
 *      batched per rAF by useWebSocket and this page subscribes to the whole
 *      `chat.messages`. `ChatSidebar` is wrapped in `memo`, but ONE inline
 *      arrow prop makes that memo bail, so every one of those frames re-rendered
 *      the entire sidebar. Locked here as a property of ALL props, not of the
 *      one that regressed: the stub is `memo`-wrapped and counts its own
 *      renders, so any newly-unstable prop reddens this.
 *
 *  (2) The scrim's full-viewport `backdrop-filter` must re-sample its backdrop
 *      whenever the backdrop changes — and a streaming message list under it
 *      repaints every frame. Kept anyway, deliberately: both cheaper variants
 *      looked worse on a real device. Locked here so the decision survives the
 *      next perf pass, with the smoothness paid for on the compositor instead
 *      (see animateDrawer.compositor.test.ts).
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { memo } from 'react'
import { render, act, screen, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { sseSlots } from '../store/dashboardSlice'
import { sseChatMessage } from '../store/chatSlice'

/** Renders of the memo-wrapped sidebar stub. A parent render that does NOT
 *  bump this is a parent render the sidebar was insulated from. */
const sidebarRenders = { n: 0 }

/** Completion callbacks handed to framer's `animate`, fired manually so a test
 *  can observe the window while the panel is still in motion. */
const pendingSettles: (() => void)[] = []

// Only `animate` is replaced. Everything else stays real: the scrim's class is
// what this file asserts on, and a hand-rolled `motion` proxy would be one more
// thing that can diverge from the component actually under test.
vi.mock('framer-motion', async (importOriginal) => {
  const actual = await importOriginal<typeof import('framer-motion')>()
  return {
    ...actual,
    animate: (_v: unknown, _to: unknown, opts?: { onComplete?: () => void }) => {
      if (opts?.onComplete) pendingSettles.push(opts.onComplete)
      return { stop: () => {} }
    },
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
// memo-wrapped on purpose: it performs the SAME shallow prop comparison the
// real `memo(ChatSidebar)` does, so its render count answers "would the real
// sidebar have re-rendered here" without paying for the real sidebar.
vi.mock('../pages/ChatSidebar', () => ({
  default: memo(function ChatSidebarStub(props: { staticRows?: boolean }) {
    sidebarRenders.n += 1
    return <div data-testid="sidebar-stub" data-static-rows={String(!!props.staticRows)} />
  }),
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
  measureSidePanelReservedW: () => 560,
  sidePanelFillWidth: () => undefined,
}))
vi.mock('../hooks/usePanelState', () => ({ usePanelState: () => ({ isOpen: false, openPanel: vi.fn(), closePanel: vi.fn() }), useDiffPanel: () => ({ isOpen: false, filePath: '', original: '', modified: '', openDiff: vi.fn(), closeDiff: vi.fn() }) }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
// A stable object: the real hook holds `agents` in `useState`, so a mock that
// rebuilt `[]` per render would fail this file for a reason production does not
// have — and would mask the props that really are unstable.
vi.mock('../hooks/useAgents', () => {
  const AGENTS = { agents: [], defaultAgent: null }
  return { useAgents: () => AGENTS }
})
vi.mock('../hooks/useFilteredDropdown', () => ({ useFilteredDropdown: () => ({ filtered: [], query: '', setQuery: vi.fn(), selectedIndex: 0, setSelectedIndex: vi.fn(), onKeyDown: vi.fn() }) }))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => true }))
vi.mock('../api/client', () => ({
  api: Object.fromEntries(
    ['sessions', 'chatSlotDetail', 'createChatSlot', 'deleteChatSlot', 'resumeChatSlot',
      'deleteSession', 'agentDetail', 'approveChatSlot', 'chatSlotAgent', 'chatSlotModel',
      'chatSlotWorkspace', 'models', 'planAction', 'planFromChat', 'renameSlot',
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
globalThis.fetch = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }) as never
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as never

import ChatPage from '../pages/ChatPage'

function renderChat() {
  const store = createTestStore()
  act(() => { store.dispatch(sseSlots([{ key: 'slot-0', title: 'Session 0' } as never])) })
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
  return store
}

/** One batched streaming flush, i.e. one animation frame's worth of store churn
 *  from a session that is producing tokens. */
const streamFrame = (store: ReturnType<typeof createTestStore>, slot: string, text: string) =>
  act(() => { store.dispatch(sseChatMessage({ slot, role: 'chunk', content: text, batched: true } as never)) })

/** Open the drawer and let its slide finish, so the sidebar is mounted and at
 *  rest. Two controls carry this label on mobile (the floating opener and the
 *  in-header toggle); either one opens a closed drawer. */
function openDrawer() {
  fireEvent.click(screen.getAllByLabelText('Toggle sessions')[0])
}
const finishSlide = () => act(() => {
  const queued = pendingSettles.splice(0)
  queued.forEach(fn => fn())
})

describe('ChatPage — mobile sessions drawer frame budget', () => {
  beforeEach(() => {
    sidebarRenders.n = 0
    pendingSettles.length = 0
    Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: 390 })
    Object.defineProperty(window, 'innerHeight', { writable: true, configurable: true, value: 844 })
  })
  afterEach(() => { vi.clearAllMocks() })

  it('insulates the sidebar from per-frame streaming re-renders', () => {
    const store = renderChat()
    // The mobile drawer unmounts the sidebar while closed, so the window this
    // guards is the one where it IS mounted: the slide and everything after.
    openDrawer()
    expect(screen.getByTestId('sidebar-stub')).toBeTruthy()
    // The compositor pairing: the drawer-hosted sidebar must render staticRows,
    // or its rows are projection nodes under a WAAPI transform — the fly-in bug.
    expect(screen.getByTestId('sidebar-stub').getAttribute('data-static-rows')).toBe('true')
    const afterMount = sidebarRenders.n
    expect(afterMount).toBeGreaterThan(0)

    // 20 frames ~= one 0.32s drawer slide with a session streaming through it.
    for (let i = 0; i < 20; i++) streamFrame(store, 'slot-0', `tok${i} `)

    // Every prop ChatPage hands the sidebar must be referentially stable, so
    // none of those frames reaches it.
    expect(sidebarRenders.n).toBe(afterMount)
  })

  /**
   * The frosted scrim is a DECISION, not an oversight, so it gets a guard: it
   * matches every other scrim in the app, and the two cheaper-looking variants
   * (no blur at all; blur deferred to the settled state) were both tried on a
   * real device and rejected on appearance. A future pass reading "full-viewport
   * backdrop-filter" as free perf budget should have to delete this test.
   */
  it('keeps the frosted scrim, in motion and at rest', () => {
    renderChat()
    openDrawer()
    expect(screen.getByTestId('sessions-backdrop').className).toContain('backdrop-blur-sm')
    finishSlide()
    expect(screen.getByTestId('sessions-backdrop').className).toContain('backdrop-blur-sm')
  })
})
