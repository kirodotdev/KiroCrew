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
import { render, act } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

// How many leading rows the virtualizer stub currently has committed. The test
// raises this to simulate the window-replacement commit landing late.
let mountedTo = 5

vi.mock('../pages/chat', async () => {
  const React = await import('react')
  return {
    ChatFooter: () => null,
    McpInfoButton: () => null,
    UserMessage: () => React.createElement('div', { 'data-testid': 'user-msg' }),
    AssistantMessage: () => React.createElement('div', { 'data-testid': 'assistant-msg' }),
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
          prop === 'chatSlotDetail' ? { messages: [], has_more: false, total: 0 } : {},
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

const renderChatPage = (messages: unknown[], msgTs: string) => {
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
  const { container } = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter initialEntries={[`/chat/chat-1?msg=${encodeURIComponent(msgTs)}`]}>
            <Routes>
              <Route path="/chat/:slug?" element={<ChatPage mode="" />} />
            </Routes>
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  act(() => { store.dispatch({ type: 'chat/replaceMessages', payload: messages }) })
  return { store, container }
}

describe('ChatPage — a far jump whose row mounts late still lands on the first navigation', () => {
  let frames: FrameRequestCallback[] = []
  let origRaf: typeof globalThis.requestAnimationFrame
  let restoreLayout: (() => void) | null = null
  const recorded: Recorded[] = []

  beforeEach(() => {
    Object.keys(apiMocks).forEach(k => delete apiMocks[k])
    mountedTo = 5
    recorded.length = 0
    frames = []
    origRaf = globalThis.requestAnimationFrame
    globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => {
      frames.push(cb)
      return frames.length
    }) as typeof globalThis.requestAnimationFrame
    // Only timers are faked: performance.now must stay real so the poll's
    // wall-clock backstop measures the (near-zero) real duration of the flush
    // loop rather than the simulated 500ms deep-link delay.
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    restoreLayout = installLayout(recorded)
  })

  afterEach(() => {
    vi.useRealTimers()
    globalThis.requestAnimationFrame = origRaf
    restoreLayout?.()
    restoreLayout = null
  })

  /** Run every queued frame once (each may enqueue its successor). */
  const flushFrames = (n: number) => {
    for (let i = 0; i < n; i++) {
      const batch = frames
      frames = []
      if (batch.length === 0) return
      act(() => { batch.forEach((cb) => cb(0)) })
    }
  }

  it('keeps polling past 30 frames and scrolls once the row commits', () => {
    const messages = mkMessages()
    const { store, container } = renderChatPage(messages, ts(TARGET_DISPLAY_INDEX))

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

  it('opts the transcript into eagerFirstMeasure (first row measurements land in the offset tree immediately)', () => {
    // Without this, a FAR jump or fast scroll mounts rows whose real heights
    // wait out the height-sync debounce before entering the spacer math; the
    // late reconciliation shifts content under the viewport. Chrome's scroll
    // anchoring hides the shift, iOS Safari has none (13-25px measured drift).
    renderChatPage([{ role: 'user', content: 'q', ts: '2026-01-01T00:00:00Z', meta: { mid: 'm-1' } }], '2026-01-01T00:00:00Z')
    expect(capturedVirtOpts?.eagerFirstMeasure).toBe(true)
  })
})
