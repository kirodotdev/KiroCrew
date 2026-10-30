/**
 * The chat page's active session can leave the slot list while the user's
 * own New chat or Fork is still in flight: another tab deletes it, or a
 * stale slots frame omits it. The page then must not auto-open some other
 * session (the first listed one, the oldest) for the length of the request.
 * It waits, and the new chat opens when the request lands. A request that
 * fails ends the wait, and the usual pick runs.
 *
 * Renders the REAL ChatPage, with the same child stubs as ChatPage.sid.
 */
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest'
import { act, render, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { createTestStore } from './helpers'
import { createSlot, forkOpenSettled, forkOpenStarted, resumeFromHistory } from '../store/chatSlice'
import { sseSlots } from '../store/dashboardSlice'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'
import type { ChatSlot } from '../types'

type PreloadState = {
  dashboard?: Partial<RootState['dashboard']>
  chat?: Partial<RootState['chat']>
}

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
     'chatSlotsUnrestored', 'sessionsNamed', 'setSlotColor'].map(k => [k, vi.fn().mockResolvedValue(
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
import ChatPage from '../pages/ChatPage'
import { PENDING_OPEN_LEASE_MS } from '../pages/chat/useChatPageSessionController'
import { api } from '../api/client'

const slot = (key: string, title?: string): ChatSlot => ({
  key, title: title ?? key, messages: 0, running: false, mode: '', created: '', last_ts: '',
})
const OLDEST = 'chat-1-100'
const ORIGIN = 'chat-3-300'
const NEW = 'chat-9-900'
const listed = [slot(OLDEST, 'Oldest'), slot('chat-2-200', 'Second'), slot(ORIGIN, 'Origin')]
const withoutOrigin = listed.filter(s => s.key !== ORIGIN)

function renderChatPage() {
  const preload: PreloadState = {
    dashboard: {
      status: { platform: 'darwin' }, connected: true, slots: listed, slotsLoaded: true, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    },
    chat: {
      activeSlot: ORIGIN, messages: [], slotCursorKey: null, slotRunning: false, slotStopping: false, slotState: 'idle',
      slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
      lastChunkSeq: undefined, history: [], historyHasMore: false, historyOffset: 0,
      pendingInput: null, slotContextPct: {}, voicePlaying: false, voiceAudio: null,
      subagents: {}, toolLog: [], activityOpen: false, activityTab: 'logs', slotActivity: {}, slotHistory: [],
      slotMessages: {}, slotLoading: false, slotRun: {},
    },
  }
  const store = createTestStore(preload as Partial<RootState>)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter initialEntries={['/chat']}>
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

/** Every session the page made active, in order, from now on. */
function trackActive(store: ReturnType<typeof createTestStore>) {
  const seen: (string | null)[] = [store.getState().chat.activeSlot]
  const unsubscribe = store.subscribe(() => {
    const k = store.getState().chat.activeSlot
    if (seen.at(-1) !== k) seen.push(k)
  })
  return { seen, unsubscribe }
}

function deferredCreate() {
  let resolve!: (v: unknown) => void
  let reject!: (e: unknown) => void
  vi.mocked(api.createChatSlot).mockReturnValue(new Promise((res, rej) => { resolve = res; reject = rej }) as never)
  return {
    land: () => resolve({ ...slot(NEW, 'New Session'), memory_mode: 'persistent' }),
    fail: () => reject(new Error('create failed')),
  }
}

beforeEach(() => {
  localStorage.clear()
  // Per test and restored after it: a module-scope assignment would outlive
  // this file in the worker and hide MSW from later files.
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }))
})
afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

/** From here on the lease's timer and the clock are fake and pinned. Called
 *  after the first render settles, because waitFor polls on real timers. */
function pinClock() {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] })
  vi.setSystemTime(new Date('2026-10-21T12:00:00Z'))
}
const advance = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms) })

describe('the active chat leaves the list while a new chat is on its way', () => {
  it('a pending New chat opens with no other session in between', async () => {
    const store = renderChatPage()
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(ORIGIN))
    const create = deferredCreate()
    let created!: Promise<unknown>
    await act(async () => { created = store.dispatch(createSlot({ mode: '', memory_mode: 'persistent' })) })
    await waitFor(() => expect(api.createChatSlot).toHaveBeenCalled())
    vi.mocked(api.chatSlotDetail).mockClear()
    const track = trackActive(store)

    // Another tab deletes the origin, or a stale frame omits it.
    await act(async () => { store.dispatch(sseSlots(withoutOrigin)) })
    await act(async () => {})
    expect(store.getState().chat.activeSlot).toBeNull()
    expect(api.chatSlotDetail).not.toHaveBeenCalledWith(OLDEST, expect.anything())

    await act(async () => { create.land(); await created })
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(NEW))
    track.unsubscribe()
    expect(track.seen).toEqual([ORIGIN, null, NEW])
    expect(vi.mocked(api.chatSlotDetail).mock.calls.map(c => c[0])).not.toContain(OLDEST)
  })

  it('a New chat that fails ends the wait, and the first listed chat opens', async () => {
    const store = renderChatPage()
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(ORIGIN))
    const create = deferredCreate()
    let created!: Promise<unknown>
    await act(async () => { created = store.dispatch(createSlot({ mode: '', memory_mode: 'persistent' })) })
    await waitFor(() => expect(api.createChatSlot).toHaveBeenCalled())
    await act(async () => { store.dispatch(sseSlots(withoutOrigin)) })
    await act(async () => {})
    expect(store.getState().chat.activeSlot).toBeNull()

    await act(async () => { create.fail(); await created })
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(OLDEST))
  })

  it('a pending fork holds the pick until it settles', async () => {
    const store = renderChatPage()
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(ORIGIN))
    await act(async () => { store.dispatch(forkOpenStarted()) })
    await act(async () => { store.dispatch(sseSlots(withoutOrigin)) })
    await act(async () => {})
    expect(store.getState().chat.activeSlot).toBeNull()

    // A fork that opened nothing (failed, or the user's choice kept the view).
    await act(async () => { store.dispatch(forkOpenSettled()) })
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(OLDEST))
  })

  it('a New chat that never answers holds the pick only for the lease', async () => {
    const store = renderChatPage()
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(ORIGIN))
    const create = deferredCreate()
    let created!: Promise<unknown>
    await act(async () => { created = store.dispatch(createSlot({ mode: '', memory_mode: 'persistent' })) })
    await waitFor(() => expect(api.createChatSlot).toHaveBeenCalled())
    pinClock()
    await act(async () => { store.dispatch(sseSlots(withoutOrigin)) })

    await advance(PENDING_OPEN_LEASE_MS - 1)
    expect(store.getState().chat.activeSlot).toBeNull()
    await advance(1)
    expect(store.getState().chat.activeSlot).toBe(OLDEST)
    // The lease lapsing left the request itself alone.
    expect(store.getState().chat.foregroundCreateId).not.toBeNull()

    // The create answers late. The pick above was the page's own, so the new
    // chat still opens, and the request's state clears as usual.
    vi.useRealTimers()
    await act(async () => { create.land(); await created })
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(NEW))
    expect(store.getState().chat.foregroundCreateId).toBeNull()
  })

  it('a fork that never settles holds the pick only for the lease, and the lease leaves the count alone', async () => {
    const store = renderChatPage()
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(ORIGIN))
    pinClock()
    await act(async () => { store.dispatch(forkOpenStarted()); store.dispatch(forkOpenStarted()) })
    await act(async () => { store.dispatch(sseSlots(withoutOrigin)) })

    await advance(PENDING_OPEN_LEASE_MS - 1)
    expect(store.getState().chat.activeSlot).toBeNull()
    await advance(1)
    expect(store.getState().chat.activeSlot).toBe(OLDEST)
    expect(store.getState().chat.pendingForkOpens).toBe(2)

    // Late settlements lower exactly their own share.
    await act(async () => { store.dispatch(forkOpenSettled()) })
    expect(store.getState().chat.pendingForkOpens).toBe(1)
    await act(async () => { store.dispatch(forkOpenSettled()) })
    expect(store.getState().chat.pendingForkOpens).toBe(0)
  })

  it('each loss of the chat starts a fresh lease', async () => {
    const store = renderChatPage()
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(ORIGIN))
    pinClock()
    await act(async () => { store.dispatch(forkOpenStarted()) })
    await act(async () => { store.dispatch(sseSlots(withoutOrigin)) })
    await advance(PENDING_OPEN_LEASE_MS)
    expect(store.getState().chat.activeSlot).toBe(OLDEST)

    // The fork is still pending when the page loses its new pick too. The
    // wait starts over rather than inheriting the lapsed lease.
    await act(async () => { store.dispatch(sseSlots(withoutOrigin.filter(s => s.key !== OLDEST))) })
    await act(async () => {})
    expect(store.getState().chat.activeSlot).toBeNull()
    await advance(PENDING_OPEN_LEASE_MS - 1)
    expect(store.getState().chat.activeSlot).toBeNull()
    await advance(1)
    expect(store.getState().chat.activeSlot).toBe('chat-2-200')
  })

  // Only the pending action lands: the POST never answers, so the entry stays.
  const hangResume = (store: ReturnType<typeof renderChatPage>) => act(async () => {
    store.dispatch(resumeFromHistory.pending('resume-hung', { key: 'chat-hung', title: 'Hung' }, { startedAt: Date.now() }))
  })

  it('a resume within its lease holds the pick', async () => {
    const store = renderChatPage()
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(ORIGIN))
    pinClock()
    await hangResume(store)
    await advance(PENDING_OPEN_LEASE_MS - 1)
    await act(async () => { store.dispatch(sseSlots(withoutOrigin)) })
    await act(async () => {})
    expect(store.getState().chat.activeSlot).toBeNull()
  })

  it('a resume past its lease does not hold a later pick', async () => {
    const store = renderChatPage()
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(ORIGIN))
    pinClock()
    await hangResume(store)
    await advance(PENDING_OPEN_LEASE_MS)
    // The hung entry is still there: nothing pruned it.
    expect(Object.keys(store.getState().chat.pendingResumes ?? {})).toEqual(['resume-hung'])

    await act(async () => { store.dispatch(sseSlots(withoutOrigin)) })
    await act(async () => {})
    expect(store.getState().chat.activeSlot).toBe(OLDEST)
  })

  it('with nothing pending, the first listed chat opens at once (unchanged)', async () => {
    const store = renderChatPage()
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(ORIGIN))
    await act(async () => { store.dispatch(sseSlots(withoutOrigin)) })

    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(OLDEST))
  })
})
