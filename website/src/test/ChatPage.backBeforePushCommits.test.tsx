/**
 * Back pressed while a session switch's URL push is still uncommitted.
 *
 * The dashboard mounts `<BrowserRouter>`, which commits every location change
 * inside `React.startTransition`. A push therefore moves `window.history` at
 * once, while the rendered location (key, search) keeps naming the entry it
 * left until the transition commits. MemoryRouter settles in the same act()
 * and cannot show that window, so these tests drive a real browser history
 * through `unstable_HistoryRouter` and hold its listener notifications: the
 * held updates are exactly the router's pending transition. Releasing them in
 * order reproduces what React commits when Back lands before the push does.
 */
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest'
import { act, render, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { Routes, Route, UNSAFE_createBrowserHistory, unstable_HistoryRouter as HistoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { switchSlot } from '../store/chatSlice'
import { sseSlots } from '../store/dashboardSlice'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'
import type { ChatSlot } from '../types'

vi.mock('react-virtuoso', () => ({ Virtuoso: () => null }))
vi.mock('../components/ChatInput', () => ({ default: () => null }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../components/MarkdownRenderer', () => ({ default: () => null }))
vi.mock('../components/TypewriterText', () => ({ default: () => null }))
vi.mock('../components/OverlayDrawer', () => ({ default: () => null }))
vi.mock('../components/AgentDropdownList', () => ({ default: () => null }))
vi.mock('../components/ModelDropdownList', () => ({ default: () => null }))
vi.mock('../components/InfoTip', () => ({ default: () => null }))
vi.mock('../components/SegmentedControl', () => ({ default: () => null }))
vi.mock('../pages/chat/CollapsibleToolGroup', () => ({ default: () => null }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../pages/chat/SessionColorPicker', () => ({ default: () => null }))
vi.mock('../pages/chat', () => ({ ChatFooter: () => null, AssistantMessage: () => null, McpInfoButton: () => null, UserMessage: () => null, CronAckBar: () => null, NotificationItem: () => null, PinnedPrompt: () => null }))
vi.mock('../pages/ChatSidebar', () => ({ default: () => null, SIDEBAR_MIN: 200, SIDEBAR_MAX: 500 }))
vi.mock('../pages/chat/ChatSettings', () => ({ loadChatConfig: () => ({ contentWidth: 'compact' }), CONTENT_WIDTH: { compact: { messages: '900px', input: '916px' }, comfortable: { messages: '84%', input: '85%' }, full: { messages: '92%', input: '93%' } } }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: null }) }))
vi.mock('../hooks/useFilteredDropdown', () => ({ useFilteredDropdown: () => ({ filtered: [], query: '', setQuery: vi.fn(), selectedIndex: 0, setSelectedIndex: vi.fn(), onKeyDown: vi.fn() }) }))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../api/client', () => ({
  api: Object.fromEntries(
    ['sessions', 'chatSlotDetail', 'createChatSlot', 'deleteChatSlot', 'resumeChatSlot',
     'deleteSession', 'agentDetail', 'approveChatSlot', 'chatSlotAgent', 'chatSlotModel',
     'chatSlotWorkspace', 'models', 'planFromChat', 'renameSlot',
     'resolveApproval', 'screenshot', 'slackChannels', 'slackLink', 'spawnList',
     'stopChatSlot', 'uploadFiles', 'voiceSynthesize', 'workspaces', 'chatSlots',
     'notifications', 'status', 'generateTitle', 'kirocrewConfig', 'agentResolvedModel',
     'chatSlotsUnrestored', 'sessionsNamed'].map(k => [k, vi.fn().mockResolvedValue(
      k === 'chatSlotDetail' ? { messages: [], has_more: false, total: 0 } : {}
    )])
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

import ChatPage from '../pages/ChatPage'

const slot = (key: string, title: string): ChatSlot => ({
  key, title, messages: 0, running: false, mode: '', created: '', last_ts: '',
})
const ORIGIN = slot('chat-1-100', 'Alpha')
const NEW = slot('chat-2-200', 'Beta')
const THIRD = slot('chat-3-300', 'Gamma')

type BrowserHistory = ReturnType<typeof UNSAFE_createBrowserHistory>

/** A real browser history whose listener calls can be held back, standing in
 *  for the router's not-yet-committed transition. */
function heldBrowserHistory() {
  const inner = UNSAFE_createBrowserHistory({ window, v5Compat: true })
  const held: Array<() => void> = []
  let holding = false
  const history = Object.create(inner) as BrowserHistory
  history.listen = fn => inner.listen(update => {
    if (holding) held.push(() => fn(update))
    else fn(update)
  })
  return {
    history,
    hold() { holding = true },
    release() {
      holding = false
      for (const notify of held.splice(0)) notify()
    },
  }
}

function renderOnBrowserHistory() {
  const router = heldBrowserHistory()
  const store = createTestStore({
    dashboard: {
      status: { platform: 'darwin' }, connected: true, slots: [ORIGIN, NEW, THIRD], slotsLoaded: true, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    },
    chat: {
      activeSlot: null, messages: [], slotRunning: false, slotStopping: false, slotState: 'idle',
      slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
      lastChunkSeq: undefined, history: [], historyHasMore: false, historyOffset: 0,
      pendingInput: null, slotContextPct: {}, voicePlaying: false, voiceAudio: null,
      subagents: {}, toolLog: [], activityOpen: false, activityTab: 'logs', slotActivity: {}, slotHistory: [],
      slotMessages: {}, slotLoading: false,
    },
  } as unknown as Partial<RootState>)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <HistoryRouter history={router.history}>
            <Routes>
              <Route path="/chat/:slug?" element={<ChatPage />} />
            </Routes>
          </HistoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return { store, router }
}

const liveSid = () => new URLSearchParams(window.location.search).get('sid')

function nextPopstate(): Promise<void> {
  return new Promise(resolve => window.addEventListener('popstate', () => resolve(), { once: true }))
}

/** Back or Forward, with the popstate and every update it starts inside one act. */
async function traverse(step: 'back' | 'forward') {
  await act(async () => {
    const popped = nextPopstate()
    window.history[step]()
    await popped
  })
}

/** Traverse once the router has committed, and wait for the view to follow. */
async function traverseTo(store: ReturnType<typeof createTestStore>, step: 'back' | 'forward', key: string) {
  await traverse(step)
  await waitFor(() => expect(store.getState().chat.activeSlot).toBe(key))
  expect(liveSid()).toBe(key)
}

/** Open the origin session and let its URL settle on a committed entry. */
async function openOrigin() {
  const env = renderOnBrowserHistory()
  await waitFor(() => expect(env.store.getState().chat.activeSlot).toBe(ORIGIN.key))
  await waitFor(() => expect(window.location.pathname).toBe('/chat/alpha'))
  return env
}

beforeEach(() => {
  localStorage.clear()
  window.history.replaceState(null, '', `/chat?sid=${ORIGIN.key}`)
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.clearAllMocks()
})

describe('Back while a session switch push is uncommitted', () => {
  it('returns the view to the session the URL names', async () => {
    const { store, router } = await openOrigin()

    router.hold()
    await act(async () => { await store.dispatch(switchSlot(NEW.key)) })
    // The push reached the browser; the router has not committed it.
    expect(liveSid()).toBe(NEW.key)

    await traverse('back')
    expect(liveSid()).toBe(ORIGIN.key)

    await act(async () => { router.release() })

    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(ORIGIN.key))
    expect(liveSid()).toBe(ORIGIN.key)
  })

  it('still honours Back when a slot frame re-renders before the router commits', async () => {
    const { store, router } = await openOrigin()

    router.hold()
    await act(async () => { await store.dispatch(switchSlot(NEW.key)) })
    await traverse('back')
    // A frame between the pop and its commit re-runs URL sync on the stale
    // rendered location; it must not push the URL forward over the Back.
    await act(async () => { store.dispatch(sseSlots([ORIGIN, { ...NEW, messages: 1 }])) })
    await act(async () => { router.release() })

    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(ORIGIN.key))
    expect(liveSid()).toBe(ORIGIN.key)
  })

  it('pushes one history entry per switch when a slot frame lands before the commit', async () => {
    const { store, router } = await openOrigin()
    const push = vi.spyOn(window.history, 'pushState')

    router.hold()
    await act(async () => { await store.dispatch(switchSlot(NEW.key)) })
    await act(async () => { store.dispatch(sseSlots([ORIGIN, { ...NEW, messages: 1 }])) })
    await act(async () => { router.release() })

    await waitFor(() => expect(liveSid()).toBe(NEW.key))
    expect(push).toHaveBeenCalledTimes(1)
    expect(store.getState().chat.activeSlot).toBe(NEW.key)
  })

  it('keeps an entry for each session when a second switch lands before the first commits', async () => {
    const { store, router } = await openOrigin()
    const push = vi.spyOn(window.history, 'pushState')

    router.hold()
    await act(async () => { await store.dispatch(switchSlot(NEW.key)) })
    await act(async () => { await store.dispatch(switchSlot(THIRD.key)) })
    // The second switch must push on top of the first, not replace it.
    expect(push).toHaveBeenCalledTimes(2)
    expect(liveSid()).toBe(THIRD.key)
    await act(async () => { router.release() })

    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(THIRD.key))
    expect(liveSid()).toBe(THIRD.key)
    await traverseTo(store, 'back', NEW.key)
    await traverseTo(store, 'back', ORIGIN.key)
    await traverseTo(store, 'forward', NEW.key)
    await traverseTo(store, 'forward', THIRD.key)
    expect(push).toHaveBeenCalledTimes(2)
  })

  it('pushes a switch that lands while a same-session slug replace is uncommitted', async () => {
    const { store, router } = await openOrigin()
    const push = vi.spyOn(window.history, 'pushState')
    const replace = vi.spyOn(window.history, 'replaceState')

    router.hold()
    // The title changes, so URL sync replaces the origin entry's slug.
    await act(async () => { store.dispatch(sseSlots([{ ...ORIGIN, title: 'Alpha Two' }, NEW, THIRD])) })
    expect(replace).toHaveBeenCalledTimes(1)
    expect(window.location.pathname).toBe('/chat/alpha-two')
    await act(async () => { await store.dispatch(switchSlot(NEW.key)) })
    expect(push).toHaveBeenCalledTimes(1)
    await act(async () => { router.release() })

    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(NEW.key))
    await traverseTo(store, 'back', ORIGIN.key)
    expect(window.location.pathname).toBe('/chat/alpha-two')
  })
})
