/**
 * Mobile sessions drawer — closing a session from the list keeps the list.
 *
 * On a phone the session list IS the drawer. Every session-row Close lands in
 * `deleteSlot`, which hands `activeSlot` to the neighbour when the closed
 * session was the active one, and the drawer dismisses itself whenever
 * `activeSlot` moves. So closing the active row slid the list away and opened
 * the neighbour's conversation, a session the user never picked, while they
 * were still working through rows.
 *
 * The assertions are on what the user SEES (the drawer mounted and open, its
 * toggle still expanded), not only on which session is selected: the neighbour
 * landing behind the drawer is intended and unchanged, so a selection-only test
 * passes on the broken tree.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, act, screen, fireEvent, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { sseSlots, sseConnected, setSidebarOrder, addSlotOptimistic } from '../store/dashboardSlice'
import { createSlot, deleteSlot, setActiveSlot, switchSlot } from '../store/chatSlice'
import { api } from '../api/client'

/** Completion callbacks handed to framer's `animate`, fired manually: the
 *  panel unmounts on the settle of a close, not on the state flip. */
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
/** The stubbed sidebar's `onSelectSlot`, which a real row tap calls after its `switchSlot`. */
const sidebar = vi.hoisted(() => ({ onSelectSlot: undefined as undefined | ((key: string) => void) }))
vi.mock('../pages/ChatSidebar', () => ({
  default: (p: { onSelectSlot?: (key: string) => void }) => { sidebar.onSelectSlot = p.onSelectSlot; return <div data-testid="sidebar-stub" /> },
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
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => {
  const AGENTS = { agents: [], defaultAgent: null }
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
globalThis.fetch = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }) as never
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as never

import ChatPage from '../pages/ChatPage'

const KEYS = ['slot-0', 'slot-1', 'slot-2']
/** What the server answers for the session the page creates for an empty list. */
const AUTO_SLOT = { key: 'slot-auto', title: 'New Session', messages: 0, running: false } as never

function renderChat() {
  const store = createTestStore()
  act(() => {
    // Connected first: `sseConnected` also clears `slotsLoaded`.
    store.dispatch(sseConnected())
    store.dispatch(sseSlots(KEYS.map((key, i) => ({ key, title: `Session ${i}` })) as never))
    store.dispatch(setSidebarOrder(KEYS))
    store.dispatch(setActiveSlot('slot-0'))
  })
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter initialEntries={['/chat?sid=slot-0']}>
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

/** Run every queued slide settle; a dismissed drawer unmounts here. */
const finishSlide = () => act(() => { pendingSettles.splice(0).forEach(fn => fn()) })
const drawerMounted = () => screen.queryAllByTestId('sidebar-stub').length > 0
/** The list is on screen: after every queued slide has settled, the drawer
 *  panel and the backdrop over the conversation are both still mounted. A
 *  dismissal unmounts both on its settle. */
const listShown = () => {
  finishSlide()
  return drawerMounted() && screen.queryByTestId('sessions-backdrop') !== null
}
const openDrawer = () => { fireEvent.click(screen.getAllByLabelText('Toggle sessions')[0]); finishSlide() }
/** What every session-row Close does once any confirm has been answered. */
const closeRow = async (store: ReturnType<typeof createTestStore>, key: string) => {
  await act(async () => { await store.dispatch(deleteSlot(key)) })
  finishSlide()
}

describe('ChatPage — closing a session on mobile keeps the session list on screen', () => {
  beforeEach(() => {
    pendingSettles.length = 0
    viewport.isMobile = true
    Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: 390 })
    Object.defineProperty(window, 'innerHeight', { writable: true, configurable: true, value: 844 })
  })
  // `mockReset` as well as clearing: a once-value a test queued but did not
  // consume must not answer the next test's create.
  afterEach(() => { vi.mocked(api.createChatSlot).mockReset().mockResolvedValue({} as never); vi.clearAllMocks(); cleanup() })

  it('closing the ACTIVE session leaves the drawer open, with the neighbour selected behind it', async () => {
    const store = renderChat()
    openDrawer()
    expect(listShown()).toBe(true)

    await closeRow(store, 'slot-0')

    expect(store.getState().dashboard.slots.map(s => s.key)).toEqual(['slot-1', 'slot-2'])
    // The landing rule is unchanged: the row below takes over the pane.
    expect(store.getState().chat.activeSlot).toBe('slot-1')
    // The defect: this close slid the list away and showed slot-1's chat.
    expect(drawerMounted()).toBe(true)
    expect(listShown()).toBe(true)
  })

  it('closing a DIFFERENT session leaves the drawer and the selection alone', async () => {
    const store = renderChat()
    openDrawer()

    await closeRow(store, 'slot-2')

    expect(store.getState().chat.activeSlot).toBe('slot-0')
    expect(drawerMounted()).toBe(true)
    expect(listShown()).toBe(true)
  })

  it('repeated closes down to the last session keep the list, ending on the empty list', async () => {
    const store = renderChat()
    openDrawer()

    for (const key of KEYS) {
      await closeRow(store, key)
      expect(drawerMounted(), `after closing ${key}`).toBe(true)
      expect(listShown(), `after closing ${key}`).toBe(true)
    }
    // Keys only: the stubbed list refetch after each close carries no rows.
    expect(store.getState().dashboard.slots.map(s => s.key).filter(Boolean)).toEqual([])
    expect(store.getState().chat.activeSlot ?? null).toBeNull()
  })

  it('the blank session the page creates after the last close joins the list without dismissing it', async () => {
    vi.mocked(api.createChatSlot).mockResolvedValueOnce(AUTO_SLOT)
    const store = renderChat()
    openDrawer()

    for (const key of KEYS) await closeRow(store, key)
    // The empty list is the page's cue to create a session on its own.
    await act(async () => { await Promise.resolve() })
    finishSlide()

    expect(api.createChatSlot).toHaveBeenCalled()
    expect(store.getState().chat.activeSlot).toBe('slot-auto')
    expect(listShown()).toBe(true)
  })

  it('after the blank session lands, a session the user then opens still dismisses the drawer', async () => {
    vi.mocked(api.createChatSlot).mockResolvedValueOnce(AUTO_SLOT)
    const store = renderChat()
    openDrawer()
    for (const key of KEYS) await closeRow(store, key)
    await act(async () => { await Promise.resolve() })
    expect(store.getState().chat.activeSlot).toBe('slot-auto')
    expect(listShown()).toBe(true)

    // The page's own create was let through once; the user's next pick is a
    // selection again.
    act(() => { store.dispatch(setActiveSlot('slot-user')) })
    expect(listShown()).toBe(false)
  })

  it('when the blank session fails to create, a session the user then starts still dismisses the drawer', async () => {
    vi.mocked(api.createChatSlot).mockRejectedValueOnce(new Error('create failed'))
    const store = renderChat()
    openDrawer()
    for (const key of KEYS) await closeRow(store, key)
    await act(async () => { await Promise.resolve() })
    expect(api.createChatSlot).toHaveBeenCalled()
    expect(store.getState().chat.activeSlot ?? null).toBeNull()
    expect(listShown()).toBe(true)

    act(() => { store.dispatch(setActiveSlot('slot-user')) })
    expect(listShown()).toBe(false)
  })

  it('a session the user opens while the blank session is still being created dismisses the drawer', async () => {
    let finishCreate: (v: unknown) => void = () => {}
    vi.mocked(api.createChatSlot).mockImplementationOnce(() => new Promise(resolve => { finishCreate = resolve }) as never)
    const store = renderChat()
    openDrawer()
    for (const key of KEYS) await closeRow(store, key)
    expect(api.createChatSlot).toHaveBeenCalled()
    expect(store.getState().chat.creatingSlot).toBe(true)
    expect(listShown()).toBe(true)

    // What opening an Older Sessions row does while the create is in flight:
    // the resumed session joins the list and becomes active.
    act(() => {
      store.dispatch(addSlotOptimistic({ key: 'slot-older', title: 'Older', messages: 3, running: false } as never))
      store.dispatch(setActiveSlot('slot-older'))
    })
    expect(listShown()).toBe(false)

    // The late create neither takes the view back nor reopens anything.
    await act(async () => { finishCreate(AUTO_SLOT); await Promise.resolve() })
    expect(store.getState().chat.activeSlot).toBe('slot-older')
    expect(listShown()).toBe(false)
  })

  it('tapping the row of the session a close landed on dismisses the drawer', async () => {
    const store = renderChat()
    openDrawer()
    await closeRow(store, 'slot-0')
    expect(store.getState().chat.activeSlot).toBe('slot-1')
    expect(listShown()).toBe(true)

    // A row tap on the session already active behind the list: `switchSlot`
    // leaves `activeSlot` as it is, so only the row's own pick can dismiss.
    await act(async () => {
      await store.dispatch(switchSlot({ key: 'slot-1', announceOnMissing: true }))
      sidebar.onSelectSlot?.('slot-1')
    })
    expect(store.getState().chat.activeSlot).toBe('slot-1')
    expect(listShown()).toBe(false)
  })

  it('the auto-created session, closed and later reopened, dismisses the drawer as a plain selection', async () => {
    vi.mocked(api.createChatSlot).mockResolvedValueOnce(AUTO_SLOT)
    const store = renderChat()
    openDrawer()
    for (const key of KEYS) await closeRow(store, key)
    await act(async () => { await Promise.resolve() })
    expect(store.getState().chat.activeSlot).toBe('slot-auto')
    expect(listShown()).toBe(true)

    // Close the blank session too, then reopen it the way an Older Sessions
    // row does: it re-enters the list and becomes active again.
    await closeRow(store, 'slot-auto')
    expect(listShown()).toBe(true)
    act(() => {
      store.dispatch(addSlotOptimistic({ key: 'slot-auto', title: 'New Session', messages: 1, running: false } as never))
      store.dispatch(setActiveSlot('slot-auto'))
    })
    expect(listShown()).toBe(false)
  })

  it('a New the user starts while the blank session is pending still opens and dismisses the drawer', async () => {
    let finishAuto: (v: unknown) => void = () => {}
    let finishUser: (v: unknown) => void = () => {}
    vi.mocked(api.createChatSlot)
      .mockImplementationOnce(() => new Promise(resolve => { finishAuto = resolve }) as never)
      .mockImplementationOnce(() => new Promise(resolve => { finishUser = resolve }) as never)
    const store = renderChat()
    openDrawer()
    for (const key of KEYS) await closeRow(store, key)
    expect(api.createChatSlot).toHaveBeenCalledTimes(1)

    // The user taps New before the page's own create has come back.
    let userCreate: Promise<unknown> = Promise.resolve()
    act(() => { userCreate = store.dispatch(createSlot({ mode: '' })) })
    await act(async () => { finishAuto(AUTO_SLOT); await Promise.resolve() })
    expect(store.getState().chat.activeSlot).toBe('slot-auto')
    expect(listShown()).toBe(true)

    await act(async () => { finishUser({ key: 'slot-new', title: 'New Session', messages: 0, running: false }); await userCreate })
    expect(store.getState().chat.activeSlot).toBe('slot-new')
    expect(listShown()).toBe(false)
  })

  it('picking a session after a close still dismisses the drawer', async () => {
    const store = renderChat()
    openDrawer()
    await closeRow(store, 'slot-0')
    expect(listShown()).toBe(true)

    // A row tap is a selection: the list gives way to that conversation.
    act(() => { store.dispatch(setActiveSlot('slot-2')) })
    expect(listShown()).toBe(false)
    expect(drawerMounted()).toBe(false)
  })
})
