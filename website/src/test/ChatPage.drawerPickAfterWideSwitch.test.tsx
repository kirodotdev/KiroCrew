/**
 * A drawer pick on the phone layout, after the same tab switched sessions in
 * the wide layout.
 *
 * Closing the sessions drawer spends the history entry the drawer pushed with
 * `navigate(-1)`, after arming a claim that tells the POP reader the Back is
 * the drawer's own. A browser delivers that Back as a later task, so React
 * commits the pick first: `activeSlot` moves, and URL sync replaces the entry
 * the drawer is still standing on. Both runs re-enter the POP reader before the
 * pop lands. If either spends the claim, the real pop is read as the user's
 * Back onto the entry under the drawer's, which still names the outgoing
 * session. A wide-layout switch had PUSHED that entry, so the phone layout
 * honours it and the pane walks straight back to the session just left.
 *
 * MemoryRouter applies `navigate(-1)` synchronously and cannot show the gap, so
 * this file drives a real browser history: jsdom queues `history.go()` as a
 * task, exactly as a browser does.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { useLayoutEffect } from 'react'
import { render, act, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { Routes, Route, UNSAFE_createBrowserHistory, unstable_HistoryRouter as HistoryRouter, useLocation } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { sseSlots, sseConnected } from '../store/dashboardSlice'
import { setActiveSlot, switchSlot } from '../store/chatSlice'
import { api } from '../api/client'

/** Completion callbacks handed to framer's `animate`, fired manually so a close
 *  can be run to completion (the panel unmounts on the settle). */
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
  default: () => null,
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
/** Mutable so a test can fold the device mid-session: wide first, phone after. */
const viewport = vi.hoisted(() => ({ isMobile: false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => viewport.isMobile }))
vi.mock('../api/client', () => ({
  api: Object.fromEntries(
    ['sessions', 'chatSlotDetail', 'createChatSlot', 'deleteChatSlot', 'resumeChatSlot',
      'deleteSession', 'agentDetail', 'approveChatSlot', 'chatSlotAgent', 'chatSlotModel',
      'chatSlotWorkspace', 'models', 'planFromChat', 'renameSlot',
      'resolveApproval', 'screenshot', 'slackChannels', 'slackLink', 'spawnList',
      'stopChatSlot', 'uploadFiles', 'voiceSynthesize', 'workspaces', 'chatSlots',
      'notifications', 'status', 'generateTitle', 'kirocrewConfig', 'agentResolvedModel',
      'chatSlotsUnrestored', 'sessionsNamed'].map(k => [k, vi.fn().mockResolvedValue(
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

/** Resolves when the browser delivers the next Back, as its own task. */
const nextPopstate = () => new Promise<void>(resolve =>
  window.addEventListener('popstate', () => resolve(), { once: true }))

/** The `?sid=` of the location the router has COMMITTED. `window.location`
 *  moves the moment a write is issued; this moves when React commits it. */
let committedSid: string | null = null
function CommittedLocation() {
  const { search } = useLocation()
  useLayoutEffect(() => { committedSid = new URLSearchParams(search).get('sid') }, [search])
  return null
}

type BrowserHistory = ReturnType<typeof UNSAFE_createBrowserHistory>

/** A real browser history whose Back/Forward traversals are held until released,
 *  standing in for the task a browser delivers the resulting popstate in: after
 *  React has committed whatever the same handler changed. jsdom queues the
 *  traversal too, but as a timer act() may run before the router commits. */
function heldTraversalHistory() {
  const inner = UNSAFE_createBrowserHistory({ window, v5Compat: true })
  const held: number[] = []
  const history = Object.create(inner) as BrowserHistory
  history.go = (delta: number) => { held.push(delta) }
  return {
    history,
    /** Deliver every held traversal and resolve once its popstate has fired. */
    async release() {
      for (const delta of held.splice(0)) {
        const popped = nextPopstate()
        window.history.go(delta)
        await popped
      }
    },
  }
}

const SLOTS = [
  { key: 'slot-a', title: 'Session A' },
  { key: 'slot-b', title: 'Session B' },
  { key: 'slot-c', title: 'Session C' },
]

async function renderChat() {
  const store = createTestStore()
  act(() => {
    // Connected before the slots: the `?sid=` reader returns early while offline,
    // and the switch this file is about is exactly what it would skip.
    store.dispatch(sseConnected())
    store.dispatch(sseSlots(SLOTS as never))
    store.dispatch(setActiveSlot('slot-a'))
  })
  const router = heldTraversalHistory()
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  await act(async () => { render(
    <QueryClientProvider client={queryClient}>
      <Provider store={store}>
        <ThemeProvider>
          <HistoryRouter history={router.history}>
            <CommittedLocation />
            <Routes>
              <Route path="/chat/:slug?" element={<ChatPage />} />
            </Routes>
          </HistoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  ) })
  return { store, router }
}

const liveSid = () => new URLSearchParams(window.location.search).get('sid')
/** The scrim exists exactly while the phone drawer is mounted; the sessions
 *  pane itself is kept mounted off screen between opens. */
const drawerMounted = () => screen.queryByTestId('sessions-backdrop') !== null
/** Two controls carry this label on the phone layout; either opens the drawer. */
const openDrawer = () => fireEvent.click(screen.getAllByLabelText('Toggle sessions')[0])
/** Run every queued settle to completion. */
const finishSlide = () => act(() => { pendingSettles.splice(0).forEach(fn => fn()) })

describe('ChatPage — phone drawer pick after a wide-layout switch', () => {
  beforeEach(() => {
    pendingSettles.length = 0
    viewport.isMobile = false
    localStorage.clear()
    committedSid = null
    window.history.replaceState(null, '', '/chat?sid=slot-a')
    Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: 390 })
    Object.defineProperty(window, 'innerHeight', { writable: true, configurable: true, value: 844 })
  })
  afterEach(() => { vi.clearAllMocks(); cleanup() })

  it('keeps the picked session when the drawer pop lands after the pick committed', async () => {
    const { store, router } = await renderChat()
    await waitFor(() => expect(liveSid()).toBe('slot-a'))

    // Wide layout: a sidebar switch PUSHES, leaving the slot-a entry behind and
    // standing on an entry naming slot-b.
    await act(async () => { await store.dispatch(switchSlot('slot-b')) })
    await waitFor(() => expect(liveSid()).toBe('slot-b'))

    // The device folds: phone layout, same tab, same stack.
    viewport.isMobile = true
    await act(async () => { store.dispatch(sseSlots([...SLOTS] as never)) })
    openDrawer()
    expect(drawerMounted()).toBe(true)

    // Pick slot-c. The drawer closes and issues its `navigate(-1)`; the browser
    // delivers that popstate as a later task, after React has committed the
    // pick and URL sync has replaced the entry the drawer pushed.
    await act(async () => { store.dispatch(setActiveSlot('slot-c')) })
    finishSlide()
    expect(drawerMounted()).toBe(false)
    expect(store.getState().chat.activeSlot).toBe('slot-c')
    await waitFor(() => expect(committedSid).toBe('slot-c'))
    await act(async () => { await router.release() })
    // Let any switch the pop dispatched answer before judging.
    await act(async () => { await vi.mocked(api.chatSlotDetail).mock.results.at(-1)?.value })

    // The pop is the drawer's bookkeeping, not a Back onto slot-b.
    expect(store.getState().chat.activeSlot).toBe('slot-c')
    await waitFor(() => expect(liveSid()).toBe('slot-c'))
    expect((window.history.state as { idx: number }).idx).toBe(1)
  })

  it('a plain drawer open and close still pops exactly its own entry', async () => {
    const { store, router } = await renderChat()
    await waitFor(() => expect(liveSid()).toBe('slot-a'))
    await act(async () => { await store.dispatch(switchSlot('slot-b')) })
    await waitFor(() => expect(liveSid()).toBe('slot-b'))

    viewport.isMobile = true
    await act(async () => { store.dispatch(sseSlots([...SLOTS] as never)) })
    const depth = (window.history.state as { idx: number }).idx
    openDrawer()
    expect((window.history.state as { idx: number }).idx).toBe(depth + 1)

    fireEvent.click(screen.getByTestId('sessions-backdrop'))
    finishSlide()
    await act(async () => { await router.release() })

    expect((window.history.state as { idx: number }).idx).toBe(depth)
    await waitFor(() => expect(liveSid()).toBe('slot-b'))
    expect(store.getState().chat.activeSlot).toBe('slot-b')
  })
})
