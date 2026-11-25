// Feature: chat-virtualizer — a FAR jump whose target mounts slowly still lands
// on the first navigation, driven through ChatPage.
//
// This pins the CALL SITE in ChatPage, not just `pollRowSettled`'s own unit
// tests: capping the poll at a ~30-frame rAF ceiling leaves every helper test
// green while the first click on a far target silently no-ops (the row has not
// committed to the DOM yet) and only a second click works, because the row is
// cached by then.
//
// This drives the real production path: a `?msg=` deep link on cold load calls
// ChatPage's own `navToDisplayIndex`, whose poll must survive the target row
// being absent for far longer than 30 frames and then scroll to it.
//
// jsdom has no layout, so the scroller geometry and per-row rects are faked at
// the prototype level; what is NOT faked is any part of the poll, the call site,
// or `scrollToDisplayIndex`, which is DOM-driven and runs for real.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, act, fireEvent, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { loadOlderMessages } from '../store/chatSlice'

// How many leading rows the virtualizer stub currently has committed. The test
// raises this to simulate the window-replacement commit landing late.
let mountedTo = 5

vi.mock('../pages/chat', async () => {
  const React = await import('react')
  const { useCurrentOcc } = await import('../hooks/SearchHighlightContext')
  return {
    ChatFooter: () => null,
    McpInfoButton: () => null,
    // A user row says which message it draws and whether it holds the find's
    // current match, which is all a test reads off it.
    UserMessage: ({ messageTs }: { messageTs?: string }) => React.createElement('div', {
      'data-testid': 'user-msg', 'data-ts': messageTs, 'data-current': useCurrentOcc() >= 0 ? 'yes' : 'no',
    }),
    AssistantMessage: () => React.createElement('div', { 'data-testid': 'assistant-msg' }),
  }
})
// The transcript draws a deferred snapshot of the messages. `hold` keeps it at
// the last frame drawn before it was set, the way a background render that React
// has not finished yet does; otherwise the real hook runs.
const deferral = vi.hoisted(() => ({ hold: false, held: undefined as unknown }))
vi.mock('../hooks/useSlotDeferredValue', async (importOriginal) => {
  const real = await importOriginal<typeof import('../hooks/useSlotDeferredValue')>()
  return {
    useSlotDeferredValue: (...args: Parameters<typeof real.useSlotDeferredValue>) => {
      const value = real.useSlotDeferredValue(...args)
      if (!deferral.hold) deferral.held = value
      return deferral.hold ? deferral.held : value
    },
  }
})
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../components/DiffPanel', () => ({ default: () => null }))
vi.mock('react-virtuoso', () => ({ Virtuoso: () => null }))
// Mounts only the first `mountedTo` items, so a far target is genuinely absent
// from the DOM — the condition the poll exists to survive. mountIndex reports a
// FAR jump (true) exactly as the real hook does when it replaces its window.
let capturedVirtOpts: { eagerFirstMeasure?: boolean } | null = null

vi.mock('../hooks/virtualizer/useVirtualChat', () => ({
  useVirtualChat: (opts: { items?: unknown[]; getKey?: (it: unknown, i: number) => string; eagerFirstMeasure?: boolean }) => {
    capturedVirtOpts = opts
    const items = opts.items ?? []
    return {
      virtualItems: items.slice(0, mountedTo).map((data, index) => ({
        key: opts.getKey ? opts.getKey(data, index) : String(index),
        index,
        mounted: true,
        data,
      farmIsMeasured: () => true,
      farmRecord: () => true,
      })),
      isAtBottom: false,
      getFollow: () => true,
      scrollToBottom: vi.fn(),
      scrollToIndexSmooth: vi.fn(),
      mountIndex: vi.fn(() => true),
      retainRange: vi.fn(),
      measureRef: () => () => {},
      topSentinelRef: { current: null },
      bottomSentinelRef: { current: null },
      offsetBefore: 0,
      offsetAfter: 0,
      totalHeight: 4000,
    }
  },
}))
vi.mock('../pages/ChatSidebar', () => ({ default: () => null, SIDEBAR_MIN: 200, SIDEBAR_MAX: 500 }))
vi.mock('../components/ChatInput', () => ({ default: () => null }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/MarkdownRenderer', () => ({ default: () => null }))
vi.mock('../components/TypewriterText', () => ({ default: () => null }))
vi.mock('../components/OverlayDrawer', () => ({ default: ({ children }: { children?: ReactNode }) => children }))
vi.mock('../components/AgentDropdownList', () => ({ default: () => null }))
vi.mock('../components/ModelDropdownList', () => ({ default: () => null }))
vi.mock('../components/InfoTip', () => ({ default: () => null }))
vi.mock('../components/SegmentedControl', () => ({ default: () => null }))
vi.mock('../pages/chat/CollapsibleToolGroup', () => ({ default: ({ children }: { children?: ReactNode }) => children }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../pages/chat/SessionColorPicker', () => ({ default: () => null }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ contentWidth: 'compact' }),
  CONTENT_WIDTH: { compact: { messages: '900px', input: '916px' }, comfortable: { messages: '84%', input: '85%' }, full: { messages: '92%', input: '93%' } },
}))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: null }) }))
vi.mock('../hooks/useFilteredDropdown', () => ({ useFilteredDropdown: () => ({ filtered: [], query: '', setQuery: vi.fn(), selectedIndex: 0, setSelectedIndex: vi.fn(), onKeyDown: vi.fn() }) }))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))

const apiMocks: Record<string, ReturnType<typeof vi.fn>> = {}
vi.mock('../api/client', () => ({
  api: new Proxy({}, {
    get: (_t, prop: string) => {
      if (!(prop in apiMocks)) {
        apiMocks[prop] = vi.fn().mockResolvedValue(
          prop === 'chatSlotDetail' ? { messages: [], has_more: false, total: 0 }
            : prop === 'pendingQuestions' || prop === 'approvals' ? [] : {},
        )
      }
      return apiMocks[prop]
    },
  }),
  fileReadUrl: (p: string) => `/api/file?path=${encodeURIComponent(p)}`,
}))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})
globalThis.fetch = vi.fn().mockResolvedValue({
  ok: true, status: 200, text: () => Promise.resolve(''), json: () => Promise.resolve({}),
}) as never

import ChatPage from '../pages/ChatPage'

const N = 40
const TARGET_DISPLAY_INDEX = 35
const ROW_H = 100
const CLIENT_H = 400
const SCROLL_H = N * ROW_H
const ts = (i: number) => `2026-06-23T20:${String(i).padStart(2, '0')}:00.000Z`
const mkMessages = () =>
  Array.from({ length: N }, (_, i) => ({ role: 'user', content: `m${i}`, cls: '', ts: ts(i) }))

interface Recorded { top?: number; behavior?: string }

/**
 * Fake enough layout for the DOM-driven scroll path: the scroller reports a
 * viewport and a tall content box, and each row sits at `index * ROW_H`.
 */
function installLayout(recorded: Recorded[]) {
  const proto = HTMLElement.prototype
  const origRect = proto.getBoundingClientRect
  const origScrollTo = (proto as unknown as { scrollTo?: unknown }).scrollTo
  const origClient = Object.getOwnPropertyDescriptor(proto, 'clientHeight')
  const origScrollH = Object.getOwnPropertyDescriptor(proto, 'scrollHeight')
  const origScrollT = Object.getOwnPropertyDescriptor(proto, 'scrollTop')

  proto.getBoundingClientRect = function (this: HTMLElement): DOMRect {
    const di = this.getAttribute?.('data-display-index')
    const top = di !== null && di !== undefined ? Number(di) * ROW_H : 0
    const height = di !== null && di !== undefined ? ROW_H : CLIENT_H
    return {
      top, bottom: top + height, height, left: 0, right: 0, width: 0, x: 0, y: top,
      toJSON() { return {} },
    } as DOMRect
  }
  Object.defineProperty(proto, 'clientHeight', { configurable: true, get: () => CLIENT_H })
  Object.defineProperty(proto, 'scrollHeight', { configurable: true, get: () => SCROLL_H })
  Object.defineProperty(proto, 'scrollTop', { configurable: true, get: () => 0, set: () => {} })
  ;(proto as unknown as { scrollTo: (o: Recorded) => void }).scrollTo = function (o: Recorded) {
    recorded.push({ top: o?.top, behavior: o?.behavior })
  }

  return () => {
    proto.getBoundingClientRect = origRect
    ;(proto as unknown as { scrollTo?: unknown }).scrollTo = origScrollTo
    if (origClient) Object.defineProperty(proto, 'clientHeight', origClient)
    if (origScrollH) Object.defineProperty(proto, 'scrollHeight', origScrollH)
    if (origScrollT) Object.defineProperty(proto, 'scrollTop', origScrollT)
  }
}

/** Queue every animation frame instead of running it, so a test steps the frames itself. */
function installFrames() {
  let frames: FrameRequestCallback[] = []
  const origRaf = globalThis.requestAnimationFrame
  globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => {
    frames.push(cb)
    return frames.length
  }) as typeof globalThis.requestAnimationFrame
  return {
    /** Run every queued frame once (each may enqueue its successor), `n` times. */
    flush(n: number) {
      for (let i = 0; i < n; i++) {
        const batch = frames
        frames = []
        if (batch.length === 0) return
        act(() => { batch.forEach((cb) => cb(0)) })
      }
    },
    restore() { globalThis.requestAnimationFrame = origRaf },
  }
}

/** `msgTs` opens the chat through a `?msg=` deep link to that message. */
const renderChatPage = async (messages: unknown[], msgTs?: string) => {
  const slot = { key: 'chat-1', title: 'chat-1', messages: N, running: false, mode: '', created: '', last_ts: '' }
  apiMocks.chatSlots = vi.fn().mockResolvedValue([slot])
  apiMocks.chatSlotDetail = vi.fn().mockResolvedValue({ messages, has_more: false, total: messages.length })
  const store = createTestStore({
    dashboard: {
      status: { platform: 'darwin' }, connected: false,
      slots: [slot], approvalMode: 'normal', channelTrusted: false, refreshTrigger: 0,
      unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as never,
    chat: {
      activeSlot: 'chat-1',
      messages, slotRunning: false, slotStopping: false, slotState: 'idle',
      slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
      lastChunkSeq: undefined, history: [], historyHasMore: false, historyOffset: 0,
      pendingInput: null, slotContextPct: {}, voicePlaying: false, voiceAudio: null,
      subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools', slotActivity: {}, slotHistory: [],
    } as never,
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  let container: HTMLElement | undefined
  // Async act: the ChatPage and ThemeProvider boot queries resolve on microtasks
  // after the first render, so they must land inside this barrier.
  await act(async () => {
    container = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter initialEntries={[msgTs ? `/chat/chat-1?msg=${encodeURIComponent(msgTs)}` : '/chat/chat-1']}>
            <Routes>
              <Route path="/chat/:slug?" element={<ChatPage mode="" />} />
            </Routes>
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
    ).container
  })
  await act(async () => { store.dispatch({ type: 'chat/replaceMessages', payload: messages }) })
  return { store, container: container as HTMLElement }
}

describe('ChatPage — a far jump whose row mounts late still lands on the first navigation', () => {
  let raf: ReturnType<typeof installFrames>
  let restoreLayout: (() => void) | null = null
  const recorded: Recorded[] = []

  beforeEach(() => {
    Object.keys(apiMocks).forEach(k => delete apiMocks[k])
    mountedTo = 5
    recorded.length = 0
    raf = installFrames()
    // Only timers are faked: performance.now must stay real so the poll's
    // wall-clock backstop measures the (near-zero) real duration of the flush
    // loop rather than the simulated 500ms deep-link delay.
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    restoreLayout = installLayout(recorded)
  })

  afterEach(() => {
    vi.useRealTimers()
    raf.restore()
    restoreLayout?.()
    restoreLayout = null
  })

  const flushFrames = (n: number) => raf.flush(n)

  it('keeps polling past 30 frames and scrolls once the row commits', async () => {
    const messages = mkMessages()
    const { store, container } = await renderChatPage(messages, ts(TARGET_DISPLAY_INDEX))

    // The deep-link effect defers the navigation by 500ms.
    act(() => { vi.advanceTimersByTime(600) })
    // Only the first 5 rows are committed, so the target genuinely is not there.
    expect(container.querySelector(`[data-display-index="${TARGET_DISPLAY_INDEX}"]`)).toBeNull()

    recorded.length = 0
    // Far longer than the ~30-frame ceiling.
    flushFrames(45)
    // Nothing may have been scrolled while the row was missing — in particular
    // no teleport to top:0 (the "far jump jumps to the top" bug).
    expect(recorded.length).toBe(0)

    // The virtualizer's window replacement finally commits.
    act(() => {
      mountedTo = N
      store.dispatch({ type: 'chat/replaceMessages', payload: [...messages] })
    })
    const row = container.querySelector(`[data-display-index="${TARGET_DISPLAY_INDEX}"]`)
    expect(row).not.toBeNull()

    flushFrames(5)

    // The poll found the row and scrolled to it — near its faked offset, not 0.
    expect(recorded.length).toBeGreaterThan(0)
    const tops = recorded.map((r) => r.top ?? -1)
    expect(Math.max(...tops)).toBeGreaterThan(3000)
  })

  it("RATCHET: the one-shot smooth-jump API stays off the virtualizer's surface", async () => {
    // It computed its destination from the offset tree, where a not-yet-measured
    // row still counts at estimatedHeight, and it issued a NATIVE smooth scroll —
    // which any concurrent scrollTop write cancels, and the anchor compensations
    // perform exactly such writes mid-glide. A caller was therefore stranded
    // wherever the compensation landed. ChatPage's converging rAF glide replaced
    // it and this PR deleted it, so the guard is now that it stays deleted:
    // re-exporting it would hand the next caller the same stranding bug.
    //
    // Asserted against the TYPE surface, not ChatPage's source text: a source
    // grep only fenced this one file, while the defect belonged to the API.
    const fs = await import('node:fs')
    const path = await import('node:path')
    const types = fs.readFileSync(path.join(__dirname, '..', 'hooks', 'virtualizer', 'types.ts'), 'utf8')
    const hook = fs.readFileSync(path.join(__dirname, '..', 'hooks', 'virtualizer', 'useVirtualChat.ts'), 'utf8')
    expect(types).not.toContain('scrollToIndexSmooth')
    expect(hook).not.toContain('scrollToIndexSmooth')
  })

  it('opts the transcript into eagerFirstMeasure (first row measurements land in the offset tree immediately)', async () => {
    // Without this, a FAR jump or fast scroll mounts rows whose real heights
    // wait out the height-sync debounce before entering the spacer math; the
    // late reconciliation shifts content under the viewport. Chrome's scroll
    // anchoring hides the shift, iOS Safari has none (13-25px measured drift).
    await renderChatPage([{ role: 'user', content: 'q', ts: '2026-01-01T00:00:00Z', meta: { mid: 'm-1' } }], '2026-01-01T00:00:00Z')
    expect(capturedVirtOpts?.eagerFirstMeasure).toBe(true)
  })
})

// A jump's settle poll re-scrolls every frame until the target's height has held
// still for its quiet window. A page of older history that lands in that window
// renumbers every loaded row, so the poll must aim at the jump's MESSAGE: the row
// number it started with names an older message once the page is in. The reader
// reaches this by wheeling (which lets the history walk page for the next 20
// seconds) and then jumping near the top, where the walk lands the next page.
describe('ChatPage — a page that lands while a jump settles leaves the jump on its message', () => {
  let raf: ReturnType<typeof installFrames>
  let restoreLayout: (() => void) | null = null
  const recorded: Recorded[] = []

  beforeEach(() => {
    Object.keys(apiMocks).forEach(k => delete apiMocks[k])
    // Every row is committed, so only the renumbering can move the target.
    mountedTo = 1000
    recorded.length = 0
    deferral.hold = false
    raf = installFrames()
    // The clock is frozen as well, so the poll is still inside its quiet window
    // when the page lands, however slowly the machine renders.
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'performance'] })
    restoreLayout = installLayout(recorded)
  })

  afterEach(() => {
    deferral.hold = false
    vi.useRealTimers()
    raf.restore()
    restoreLayout?.()
    restoreLayout = null
  })

  const OLDER = 10
  /** The walk's next page lands: OLDER messages from before the loaded ones,
   *  the word in message `withMatch` of the page when one is named. */
  const landOlderPage = (store: ReturnType<typeof createTestStore>, withMatch = -1) => {
    const older = Array.from({ length: OLDER }, (_, i) => ({
      role: 'user', content: i === withMatch ? `o${i} also has the needle` : `o${i}`, cls: '', ts: `2026-06-23T19:${String(i).padStart(2, '0')}:00.000Z`,
    }))
    act(() => {
      store.dispatch(loadOlderMessages.fulfilled(
        { slot: 'chat-1', nextBefore: 0, messages: older, hasMore: false, total: N + OLDER } as never,
        'req-older',
        undefined as never,
      ))
    })
  }
  /** The scroll position the faked layout gives a centered display row `di`. */
  const centered = (di: number) => di * ROW_H - CLIENT_H / 2
  /** The scroll positions `steps` wrote. */
  const scrollsDuring = (steps: () => void) => {
    recorded.length = 0
    steps()
    return recorded.map((r) => r.top)
  }

  // The reader is on the match in L19 when a page of older history lands with
  // another match in it (o2). The current match is the message, so L19 stays
  // current, and rows arriving move nobody.
  it('rows arriving: a page that brings an older match leaves the current one current and moves nobody', async () => {
    const messages = mkMessages().map((m, i) => (i === 19 ? { ...m, content: 'L19 asks about the needle' } : m))
    const { store } = await renderChatPage(messages)
    act(() => { fireEvent.keyDown(document, { key: 'f', ctrlKey: true }) })
    act(() => { fireEvent.change(screen.getByPlaceholderText('Find in chat…'), { target: { value: 'needle' } }) })
    act(() => { vi.advanceTimersByTime(60) })
    const current = () => [...document.querySelectorAll('[data-current="yes"]')].map((el) => el.getAttribute('data-ts'))
    expect(current()).toEqual([ts(19)])
    // The jump to L19 runs out its backstop before the page lands.
    raf.flush(1)
    act(() => { vi.advanceTimersByTime(2500) })
    raf.flush(2)

    landOlderPage(store, 2)
    act(() => { vi.advanceTimersByTime(60) })
    expect(scrollsDuring(() => raf.flush(3))).toEqual([])
    expect(current()).toEqual([ts(19)])
  })

  it('a find action: the poll keeps centering the match, not the row that takes its number', async () => {
    const messages = mkMessages().map((m, i) => (i === 3 ? { ...m, content: 'the needle' } : m))
    const { store } = await renderChatPage(messages)
    act(() => { fireEvent.keyDown(document, { key: 'f', ctrlKey: true }) })
    act(() => { fireEvent.change(screen.getByPlaceholderText('Find in chat…'), { target: { value: 'needle' } }) })
    // The search's debounce; the term is a find action, so the poll starts.
    act(() => { vi.advanceTimersByTime(60) })
    expect(scrollsDuring(() => raf.flush(2)).at(-1)).toBe(centered(3))

    landOlderPage(store)
    expect(document.querySelector(`[data-display-index="${3 + OLDER}"]`)).not.toBeNull()
    const after = scrollsDuring(() => raf.flush(3))
    expect(after.length).toBeGreaterThan(0)
    expect(after).toEqual(after.map(() => centered(3 + OLDER)))
  })

  it('a ?msg= link: the poll keeps centering the linked message after the page lands', async () => {
    const { store } = await renderChatPage(mkMessages(), ts(5))
    // The deep link scrolls 500ms after the rows load.
    act(() => { vi.advanceTimersByTime(600) })
    expect(scrollsDuring(() => raf.flush(2)).at(-1)).toBe(centered(5))

    landOlderPage(store)
    const after = scrollsDuring(() => raf.flush(3))
    expect(after.length).toBeGreaterThan(0)
    expect(after).toEqual(after.map(() => centered(5 + OLDER)))
  })

  // The search counts a landed page at once, while the rows on screen still
  // draw the list from before it: React renders the transcript's new snapshot
  // when it has room. In that window the current match must stay on its own
  // row, and a find action must move the reader to it.
  it('while the landed page waits to render, the current match stays on its row and a step goes to the next one', async () => {
    const messages = mkMessages().map((m, i) => (i === 3 || i === 13 ? { ...m, content: `the needle ${i}` } : m))
    const { store } = await renderChatPage(messages)
    act(() => { fireEvent.keyDown(document, { key: 'f', ctrlKey: true }) })
    const input = screen.getByPlaceholderText('Find in chat…')
    act(() => { fireEvent.change(input, { target: { value: 'needle' } }) })
    act(() => { vi.advanceTimersByTime(60) })
    const current = () => [...document.querySelectorAll('[data-current="yes"]')].map((el) => el.getAttribute('data-ts'))
    expect(current()).toEqual([ts(3)])
    // The jump to the first needle runs out its backstop.
    raf.flush(1)
    act(() => { vi.advanceTimersByTime(2500) })
    raf.flush(2)

    deferral.hold = true
    landOlderPage(store)
    // The rows on screen are still the loaded ones; the search has counted the
    // page (its debounce ran) and nobody was moved.
    expect(document.querySelector(`[data-display-index="${N}"]`)).toBeNull()
    act(() => { vi.advanceTimersByTime(60) })
    expect(scrollsDuring(() => raf.flush(2))).toEqual([])
    expect(current()).toEqual([ts(3)])

    // Enter: the next match is the second needle, still row 13 on screen.
    act(() => { fireEvent.keyDown(input, { key: 'Enter' }) })
    expect(current()).toEqual([ts(13)])
    const steps = scrollsDuring(() => raf.flush(2))
    expect(steps.length).toBeGreaterThan(0)
    expect(steps).toEqual(steps.map(() => centered(13)))
  })
})
