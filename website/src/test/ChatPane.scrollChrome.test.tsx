// Feature: ChatPane wears the shared transcript scroll chrome.
//
// ChatPane (split-view panes AND the Crew Members thread that reuses it)
// delegates stick-to-bottom follow to the virtualized transcript behind
// ChatMessageList (chat-core P5-e) and mounts the shared EdgeFade /
// JumpToBottomButton chrome. These tests pin the wiring at the host level:
//   1. the top edge fade renders under the header, and there is NO bottom fade:
//      the composer dock floats over the scroller (ChatPage's layout, #18279)
//      and the scroller pays for the covered strip with its padding instead,
//   2. the jump-to-bottom pill appears once the user scrolls up and jumping
//      lands back at the bottom,
//   3. the scroller's scroll events drive the pill state,
//   4. sending a message force-pins the transcript and re-arms follow, as
//      ChatPage does — a Crewmate DM sent from mid-history lands on the bubble.
//
// The follow DECISIONS themselves (release/re-engage/shrink re-pin) are pinned
// by FollowController.test.ts and the virtualizer's own tests; duplicating them
// here would test the hook twice through a heavier harness.

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, act, screen, fireEvent } from '@testing-library/react'
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

function renderPane(props: { topInset?: number } = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = makeStore()
  const view = render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatPane slotKey={SLOT} {...props} />
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
  it('pays for a host band floating over its top edge (`topInset`, #18325)', () => {
    const { container } = renderPane({ topInset: 48 })
    // The scroller pads its rows below the band on top of its own 12px, so
    // at scroll top nothing hides under it while rows still scroll under it.
    const scroller = container.querySelector('.chat-container') as HTMLElement
    expect(scroller.style.paddingTop).toBe('60px')
    // The pinned-prompt band (its fold sentinel's parent) hangs under the band
    // too: a relative offset, so it costs no flow height.
    const pinBand = container.querySelector('.relative.z-\\[2\\]') as HTMLElement
    expect(pinBand).not.toBeNull()
    expect(pinBand.style.top).toBe('48px')
    // Default: no band, nothing paid.
    const plain = renderPane()
    expect((plain.container.querySelector('.chat-container') as HTMLElement).style.paddingTop).toBe('12px')
    expect((plain.container.querySelector('.relative.z-\\[2\\]') as HTMLElement).style.top).toBe('')
  })

  it('renders the top edge fade only; the composer dock floats over the scroller', () => {
    const { container } = renderPane()
    const topFade = container.querySelector('.bg-gradient-to-b.from-bg')
    expect(topFade).not.toBeNull()
    // Decorative: hidden from the a11y tree and pointer-inert.
    expect(topFade!.getAttribute('aria-hidden')).toBe('true')
    // No opaque band between transcript and composer: the transcript scrolls
    // under the glass, so a bottom fade would hide exactly what the layout
    // exists to show (docs/decisions/2026-10-02-chat-transcript-scrolls-under-the-composer-glass.md).
    expect(container.querySelector('.bg-gradient-to-t.from-bg')).toBeNull()
    // The dock is out of flow over the scroller's bottom edge and holds the
    // composer, so the scroller runs the full height of the pane.
    const dock = container.querySelector('[data-testid="composer-dock-root"]') as HTMLElement
    expect(dock).not.toBeNull()
    expect(dock.className.split(/\s+/)).toEqual(expect.arrayContaining(['absolute', 'bottom-0', 'pointer-events-none']))
    // Inert on the inner boxes, never the root (see the dockClearance pins):
    // the jump pill's full-width wrapper is a direct child of the root and
    // must stay pointer-inert, or it swallows input over the rows behind it.
    expect(dock.className.split(/\s+/)).not.toContain('dock-inert')
    const inputArea = screen.getAllByRole('textbox')[0].closest('.dock-inert') as HTMLElement
    expect(inputArea).not.toBeNull()
    expect(inputArea.parentElement).toBe(dock)
    expect(dock.querySelector('[data-testid="composer-status-stack"]')!.className.split(/\s+/)).toContain('dock-inert')
    const scroller = container.querySelector('.chat-container') as HTMLElement
    expect(dock.compareDocumentPosition(scroller) & Node.DOCUMENT_POSITION_PRECEDING).toBeTruthy()
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
    // The pill's full-width wrapper is a direct child of the dock root and
    // stays pointer-inert (only the button catches input), so the strip above
    // the composer never swallows clicks or wheel meant for the rows under it.
    const pillWrap = pill.parentElement as HTMLElement
    expect(pillWrap.parentElement).toBe(container.querySelector('[data-testid="composer-dock-root"]'))
    expect(pillWrap.className.split(/\s+/)).toContain('pointer-events-none')
    expect(pillWrap.className.split(/\s+/)).not.toContain('dock-inert')
    expect(pill.className.split(/\s+/)).toContain('pointer-events-auto')
    act(() => { pill.click() })
    flushFrames()
    expect(state.scrollTop).toBe(600)
    // The browser reports the programmatic scroll back as a scroll event.
    act(() => { scroller.dispatchEvent(new Event('scroll')) })
    expect(screen.queryByLabelText('Scroll to bottom')).toBeNull()
  })

  it('sending from a scrolled-up reader lands at the bottom and re-arms follow', async () => {
    const { container, store } = renderPane()
    act(() => {
      store.dispatch(appendSlotMessage({ slot: SLOT, message: { role: 'assistant', content: 'earlier', cls: '', ts: '2026-01-01T00:00:00Z' } }))
    })
    const scroller = container.querySelector('.chat-container') as HTMLElement
    const state = fakeGeom(scroller, { scrollTop: 600, scrollHeight: 1000, clientHeight: 400 })
    act(() => { scroller.dispatchEvent(new Event('scroll')) })
    // Reading history: follow released, pill shown.
    act(() => { state.scrollTop = 100; scroller.dispatchEvent(new Event('scroll')) })
    expect(screen.getByLabelText('Scroll to bottom')).not.toBeNull()

    const box = screen.getAllByRole('textbox')[0]
    await act(async () => {
      fireEvent.change(box, { target: { value: 'are you there?' } })
      fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })
    })
    // The pin is deferred past the bubble's commit (SCROLL_AFTER_RENDER_MS),
    // then applied on the next frame by the virtualizer.
    await act(async () => { await new Promise((r) => setTimeout(r, 150)) })
    flushFrames()
    expect(state.scrollTop).toBe(600)
    act(() => { scroller.dispatchEvent(new Event('scroll')) })
    expect(screen.queryByLabelText('Scroll to bottom')).toBeNull()
  })
})
