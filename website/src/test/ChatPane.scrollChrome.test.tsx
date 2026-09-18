// Feature: ChatPane wears the shared transcript scroll chrome.
//
// ChatPane (split-view panes AND the Crew Members thread that reuses it)
// delegates stick-to-bottom follow to the virtualized transcript behind
// ChatMessageList (chat-core P5-e) and mounts the shared EdgeFade /
// JumpToBottomButton chrome. These tests pin the wiring at the host level:
//   1. both edge fades render (top under the header, bottom above the bars),
//   2. the jump-to-bottom pill appears once the user scrolls up and jumping
//      lands back at the bottom,
//   3. the scroller's scroll events drive the pill state.
//
// The follow DECISIONS themselves (release/re-engage/shrink re-pin) are pinned
// by FollowController.test.ts and the virtualizer's own tests; duplicating them
// here would test the hook twice through a heavier harness.

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, act, screen } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    sendChat: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    uploadFiles: vi.fn().mockResolvedValue({ paths: [] }),
    screenshot: vi.fn().mockResolvedValue({ path: null }),
    fileSearch: vi.fn().mockResolvedValue({ root: '/repo', results: [] }),
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'
import { appendSlotMessage } from '../store/chatSlice'

const SLOT = 'pane-scroll-chrome'

function makeStore() {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: SLOT, messages: 0, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        slotsLoaded: true,
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
}

function renderPane() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = makeStore()
  const view = render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatPane slotKey={SLOT} />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>,
  )
  return { ...view, store }
}

function fakeGeom(el: HTMLElement, initial: { scrollTop: number; scrollHeight: number; clientHeight: number }) {
  const state = { ...initial }
  Object.defineProperty(el, 'scrollTop', {
    configurable: true,
    get: () => state.scrollTop,
    set: (v: number) => { state.scrollTop = v },
  })
  Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => state.scrollHeight })
  Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => state.clientHeight })
  // The virtualizer writes through scrollTo; route it to the faked position.
  el.scrollTo = ((opts: ScrollToOptions | number) => {
    state.scrollTop = typeof opts === 'number' ? opts : (opts.top ?? state.scrollTop)
  }) as HTMLElement['scrollTo']
  return state
}

// The virtualized transcript applies its bottom pin on the next animation
// frame (so the tail rows it just mounted have real heights first) and reads
// the resulting position back through the scroll event, as a browser would
// deliver it. The test drives both by hand.
interface QueuedFrame { id: number; cb: FrameRequestCallback }
let frames: QueuedFrame[] = []
let nextId = 1
let originalRaf: typeof requestAnimationFrame
let originalCancel: typeof cancelAnimationFrame

function flushFrames() {
  const pending = frames.splice(0)
  act(() => { pending.forEach(f => f.cb(16)) })
}

beforeEach(() => {
  vi.clearAllMocks()
  frames = []
  nextId = 1
  originalRaf = globalThis.requestAnimationFrame
  originalCancel = globalThis.cancelAnimationFrame
  globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => { const id = nextId++; frames.push({ id, cb }); return id }) as typeof requestAnimationFrame
  globalThis.cancelAnimationFrame = ((id: number) => { frames = frames.filter(f => f.id !== id) }) as typeof cancelAnimationFrame
})

afterEach(() => {
  globalThis.requestAnimationFrame = originalRaf
  globalThis.cancelAnimationFrame = originalCancel
})

describe('ChatPane shared scroll chrome', () => {
  it('renders both edge fades around the transcript scroller', () => {
    const { container } = renderPane()
    const topFade = container.querySelector('.bg-gradient-to-b.from-bg')
    const bottomFade = container.querySelector('.bg-gradient-to-t.from-bg')
    expect(topFade).not.toBeNull()
    expect(bottomFade).not.toBeNull()
    // Both are decorative: hidden from the a11y tree and pointer-inert.
    expect(topFade!.getAttribute('aria-hidden')).toBe('true')
    expect(bottomFade!.getAttribute('aria-hidden')).toBe('true')
  })

  it('shows the jump pill after a user scroll up, and jumping returns to the bottom', () => {
    const { container, store } = renderPane()
    act(() => {
      store.dispatch(appendSlotMessage({ slot: SLOT, message: { role: 'assistant', content: 'hello', cls: '', ts: '2026-01-01T00:00:00Z' } }))
    })
    const scroller = container.querySelector('.chat-container') as HTMLElement
    expect(scroller).not.toBeNull()
    const state = fakeGeom(scroller, { scrollTop: 600, scrollHeight: 1000, clientHeight: 400 })

    // At the bottom: no pill.
    act(() => { scroller.dispatchEvent(new Event('scroll')) })
    expect(screen.queryByLabelText('Scroll to bottom')).toBeNull()

    // Scrolled up: pill appears; clicking it lands at the bottom and hides it.
    act(() => { state.scrollTop = 100; scroller.dispatchEvent(new Event('scroll')) })
    const pill = screen.getByLabelText('Scroll to bottom')
    act(() => { pill.click() })
    flushFrames()
    expect(state.scrollTop).toBe(600)
    // The browser reports the programmatic scroll back as a scroll event.
    act(() => { scroller.dispatchEvent(new Event('scroll')) })
    expect(screen.queryByLabelText('Scroll to bottom')).toBeNull()
  })
})
