/**
 * Mobile sessions drawer — history mechanics (#5795).
 *
 * The drawer covers the screen, so Back is the gesture a user reaches for to
 * dismiss it. It was pure component state, present in no history entry, so a
 * back swipe with the drawer open left `/chat` entirely and the drawer was
 * still open when they came back.
 *
 * The fix mints ONE entry per open and the invariant is that the entry exists
 * exactly while the drawer is open:
 *
 *   - open PUSHES one entry at the SAME url (the drawer is view state, not a
 *     location, so the url must not move -- and a bare duplicate is enough,
 *     because nothing can deep-link a drawer open),
 *   - Back pops it: the drawer closes and the route does not change,
 *   - every OTHER close consumes it (`navigate(-1)`), so the next Back is not an
 *     invisible no-op on a duplicate entry — the twin-entry defect
 *     `SidePanelLayout`'s back control documents,
 *   - consuming it on a SESSION SWITCH must not resurrect the outgoing session:
 *     the entry below still carries the outgoing `?sid=`, and the `?sid=` to
 *     `activeSlot` effect treats any POP as the user retracing sessions. This is
 *     the conflict that kept the fix out of #5794.
 *
 * Two of these are RED on the pre-fix tree (no entry is pushed, and Back leaves
 * the route). The other two are GREEN there only because there was no entry to
 * mismanage — they exist to hold the bookkeeping the fix introduces, and both
 * caught a real defect in it: the consuming pop lands on an entry with the SAME
 * pathname, which the URL-sync effect could not see, so `?sid=` was left naming
 * the outgoing session.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, act, screen, fireEvent, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route, useLocation, useNavigate, useNavigationType } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { sseSlots, sseConnected } from '../store/dashboardSlice'
import { createSlot, setActiveSlot } from '../store/chatSlice'
import { api } from '../api/client'

/** Completion callbacks handed to framer's `animate`, fired manually so a close
 *  can be run to completion (the panel unmounts on the settle, not on the
 *  state flip). */
const pendingSettles: (() => void)[] = []

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
  measureSidePanelReservedW: () => 560,
  sidePanelFillWidth: () => undefined,
}))
vi.mock('../hooks/usePanelState', () => ({ usePanelState: () => ({ isOpen: false, openPanel: vi.fn(), closePanel: vi.fn() }), useDiffPanel: () => ({ isOpen: false, filePath: '', original: '', modified: '', openDiff: vi.fn(), closeDiff: vi.fn() }) }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
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

/** Reads the live location, and owns the only Back in this file. There is no
 *  `window.history` under MemoryRouter, so `navigate(-1)` IS the platform back
 *  gesture here — the same stand-in `SidePanelLayout.mobileNav` uses. */
function NavProbe() {
  const location = useLocation()
  const navigate = useNavigate()
  const navType = useNavigationType()
  // The same Back, reachable without a DOM click. A real back swipe is not a
  // click, and after a drag the gesture hook deliberately swallows the next one
  // (a drag must not also read as a tap), so a click-driven Back cannot express
  // "swipe open, then immediately press Back" — the very window the drag test
  // below is about.
  backNav = () => navigate(-1)
  return (
    <div>
      <div
        data-testid="nav-probe"
        data-pathname={location.pathname}
        data-sid={new URLSearchParams(location.search).get('sid') || ''}
        data-navtype={navType}
      />
      <button data-testid="platform-back" onClick={() => navigate(-1)}>back</button>
    </div>
  )
}

let backNav: (() => void) | null = null

/**
 * Two slots and a route BELOW `/chat`, so "Back left the chat route" is an
 * observable rather than an inert no-op at the bottom of the stack — that is
 * the whole defect in #5795.
 */
function renderChat() {
  const store = createTestStore()
  act(() => {
    // BEFORE the slots: `sseConnected` also clears `slotsLoaded`. Connected is
    // load-bearing here, not scenery — the `?sid=` to `activeSlot` effect returns
    // early while offline, so an unconnected harness would never exercise the
    // revert this file is about and would pass with the guard deleted.
    store.dispatch(sseConnected())
    store.dispatch(sseSlots([
      { key: 'slot-0', title: 'Session 0' },
      { key: 'slot-1', title: 'Session 1' },
    ] as never))
    store.dispatch(setActiveSlot('slot-0'))
  })
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter initialEntries={['/before-chat', '/chat?sid=slot-0']}>
            <NavProbe />
            <Routes>
              <Route path="/before-chat" element={<div data-testid="off-chat" />} />
              <Route path="/chat/:slug?" element={<ChatPage />} />
            </Routes>
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return store
}

const probe = () => screen.getByTestId('nav-probe')
const onChat = () => probe().dataset.pathname?.startsWith('/chat')
const drawerMounted = () => screen.queryAllByTestId('sidebar-stub').length > 0
/** Two controls carry this label on mobile; either opens a closed drawer. */
const openDrawer = () => fireEvent.click(screen.getAllByLabelText('Toggle sessions')[0])
const platformBack = () => fireEvent.click(screen.getByTestId('platform-back'))
/** Back WITHOUT a DOM click — see `NavProbe`. Required after a drag, whose
 *  release arms the hook's click swallower. */
const platformBackNoClick = () => act(() => { backNav?.() })
/**
 * Open the drawer through the real touch gesture rather than the toggle, because
 * the two mint the entry at different moments: the toggle pushes inside
 * `openSidebar`, while a drag only commits on RELEASE.
 *
 * Starts well inside the pane (both platform edge bands are 24px and a touch
 * landing in one is left to the OS), travels rightward past
 * `COMMIT_DRAG_SHARE` of the 350px travel (`innerWidth 390 - DRAWER_UNCOVERED_PX
 * 40`) so the release commits to open, and stays on one row so the direction lock
 * does not read it as a scroll. Leaves the settle QUEUED — the caller decides
 * whether to run it, which is what makes the mid-slide window testable.
 */
const dragDrawerOpen = () => {
  const el = document.querySelector('[data-owns-swipe]') as HTMLElement
  const at = (x: number) => ({ clientX: x, clientY: 400 })
  fireEvent.touchStart(el, { touches: [at(200)] })
  fireEvent.touchMove(el, { touches: [at(250)] })
  fireEvent.touchMove(el, { touches: [at(360)] })
  fireEvent.touchEnd(el, { changedTouches: [at(360)] })
}
/** Run every queued settle to completion — the panel unmounts there. */
const finishSlide = () => act(() => { pendingSettles.splice(0).forEach(fn => fn()) })

describe('ChatPage — mobile sessions drawer answers Back (#5795)', () => {
  beforeEach(() => {
    pendingSettles.length = 0
    Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: 390 })
    Object.defineProperty(window, 'innerHeight', { writable: true, configurable: true, value: 844 })
  })
  afterEach(() => { vi.clearAllMocks(); cleanup() })

  it('opening the drawer PUSHES one entry at the same url', () => {
    renderChat()
    // Whatever the sync effect settled on, slug and all -- the assertion is that
    // the push does not MOVE it.
    const before = { pathname: probe().dataset.pathname, sid: probe().dataset.sid }
    expect(before.sid).toBe('slot-0')
    openDrawer()
    // A real entry, or Back has nothing to pop and leaves the route. This also
    // pins that react-router pushes for a navigate to the CURRENT url: the entry
    // is a bare duplicate, so a dedupe here would silently remove the whole fix.
    expect(probe().dataset.navtype).toBe('PUSH')
    // The drawer is view state: the entry must be a DUPLICATE of the one below
    // it. A url that moved would make the sid effect switch sessions on the pop.
    expect(probe().dataset.pathname).toBe(before.pathname)
    expect(probe().dataset.sid).toBe(before.sid)
  })

  it('Back closes the drawer and stays on /chat', () => {
    renderChat()
    openDrawer()
    expect(drawerMounted()).toBe(true)

    platformBack()
    finishSlide()

    // The defect: this Back used to leave /chat outright, with the drawer still
    // open underneath for when the user came back.
    expect(onChat()).toBe(true)
    expect(screen.queryByTestId('off-chat')).toBeNull()
    expect(drawerMounted()).toBe(false)
    // The pop landed on the entry below, which carries the same session.
    expect(probe().dataset.sid).toBe('slot-0')
  })

  it('switching sessions from the drawer consumes the entry — Back does not resurrect the outgoing session', () => {
    const store = renderChat()
    openDrawer()

    // What picking a row in the drawer does: `activeSlot` moves, and the
    // drawer's own effect closes it. Driven through the store rather than the
    // stubbed sidebar's props so the test pins the page's reaction, not the
    // sidebar's call signature.
    act(() => { store.dispatch(setActiveSlot('slot-1')) })
    finishSlide()
    expect(drawerMounted()).toBe(false)
    expect(probe().dataset.sid).toBe('slot-1')

    platformBack()

    // The entry below the drawer's still carries `?sid=slot-0`. Consuming the
    // drawer's entry on the switch is what keeps this Back from landing there
    // and switching the user back to the session they just left.
    expect(screen.getByTestId('off-chat')).toBeTruthy()
    expect(probe().dataset.sid).toBe('')
    expect(store.getState().chat.activeSlot).toBe('slot-1')
  })

  it('creating a session from the drawer consumes the entry without restoring the outgoing chat', async () => {
    const store = renderChat()
    openDrawer()
    vi.mocked(api.createChatSlot).mockResolvedValueOnce({
      key: 'slot-new', title: 'New Session', messages: 0, running: false,
    })

    await act(async () => {
      await store.dispatch(createSlot({ mode: '' })).unwrap()
    })
    finishSlide()

    expect(drawerMounted()).toBe(false)
    expect(store.getState().chat.activeSlot).toBe('slot-new')
    expect(probe().dataset.sid).toBe('slot-new')

    platformBack()
    expect(screen.getByTestId('off-chat')).toBeTruthy()
    expect(store.getState().chat.activeSlot).toBe('slot-new')
  })

  it('a backdrop tap consumes the entry too, so the next Back is not an invisible no-op', () => {
    renderChat()
    openDrawer()
    fireEvent.click(screen.getByTestId('sessions-backdrop'))
    finishSlide()
    expect(drawerMounted()).toBe(false)

    // With the drawer gone, Back means what it means everywhere else on mobile:
    // leave the chat route. An unconsumed duplicate entry would swallow it and
    // the press would do nothing visible.
    platformBack()
    expect(screen.getByTestId('off-chat')).toBeTruthy()
  })

  it('a DRAG open mints the entry at RELEASE, so Back during the opening slide closes the drawer', () => {
    renderChat()
    const before = probe().dataset.pathname
    dragDrawerOpen()

    // The settle is QUEUED, not run — this is the drawer mid-slide, covering the
    // screen. Minting on ARRIVAL instead left this whole window (120ms reduced,
    // 450ms normal) with no entry to pop, so a Back here leaked past the drawer
    // and left /chat: #5795 again, reachable by drag rather than by tap.
    expect(pendingSettles.length).toBeGreaterThan(0)
    expect(drawerMounted()).toBe(true)
    expect(probe().dataset.navtype).toBe('PUSH')
    // Still a bare duplicate: committing must not MOVE the url either.
    expect(probe().dataset.pathname).toBe(before)

    platformBackNoClick()
    finishSlide()

    expect(onChat()).toBe(true)
    expect(screen.queryByTestId('off-chat')).toBeNull()
    expect(drawerMounted()).toBe(false)
  })
})
