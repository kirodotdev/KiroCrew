// Feature: chat-core P5-d — the pinned-prompt banner sticks in ChatPane too.
//
// The main chat pins the current turn's user prompt under its title row as the
// reader scrolls (PinnedPrompt). ChatPane — split-view panes AND the Crew
// Members DM thread that reuses it — now wears the same banner through the
// shared usePinnedPrompt hook. These tests pin the HOST wiring:
//   1. the pane's transcript rows carry `data-display-index` (the hook's DOM
//      contract) and the fold sentinel is mounted,
//   2. scrolling a prompt behind the fold mounts the banner with that prompt's
//      text and hides the bubble it stands in for (visibility, not display),
//   3. the banner honours the pin-last-prompt setting,
//   4. frameless mode (Members DM) mounts the same banner with no pane title bar,
//   5. clicking the banner glides the scroller back to the prompt.
//
// The geometry DECISIONS (which prompt, how far pushed, the jump inset) are
// pinned by usePinnedPrompt.test.tsx; here happy-dom has no layout, so rects
// are hand-set on the rows the pane renders.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
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
import { hydrateSlotMessages } from '../store/chatSlice'
import type { ChatMessage } from '../types'

const SLOT = 'pane-pinned-prompt'

const MESSAGES: ChatMessage[] = [
  { role: 'user', content: 'first prompt', cls: '', ts: '2026-09-08T00:00:00Z' },
  { role: 'assistant', content: 'first reply', cls: '', ts: '2026-09-08T00:00:01Z' },
  { role: 'user', content: 'second prompt about the pinned banner', cls: '', ts: '2026-09-08T00:00:02Z' },
  { role: 'assistant', content: 'second reply', cls: '', ts: '2026-09-08T00:00:03Z' },
  { role: 'user', content: 'third prompt', cls: '', ts: '2026-09-08T00:00:04Z' },
]

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

function renderPane(props: { frameless?: boolean } = {}) {
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
  act(() => {
    store.dispatch(hydrateSlotMessages({ slot: SLOT, messages: MESSAGES, hasMore: false, bounded: false, total: MESSAGES.length, running: false }))
  })
  return { ...view, store }
}

const rect = (top: number, height: number): DOMRect => ({
  top, bottom: top + height, left: 0, right: 0, width: 0, height, x: 0, y: top, toJSON: () => ({}),
}) as DOMRect

function setRect(el: Element, top: number, height: number) {
  Object.defineProperty(el, 'getBoundingClientRect', { configurable: true, value: () => rect(top, height) })
}

/** Lay the pane out as if the reader had scrolled the second prompt (row 2)
 *  and its reply entirely behind the fold: rows 0-3 above the hand-off line
 *  (happy-dom's zero-rect fold sits at y=0), the next prompt (row 4) far below
 *  so it neither takes the pin nor pushes the banner. */
function layOut(container: HTMLElement) {
  const scroller = container.querySelector('.chat-container') as HTMLElement
  setRect(scroller, 0, 400)
  let scrollTop = 200
  Object.defineProperties(scroller, {
    clientHeight: { configurable: true, get: () => 200 },
    scrollHeight: { configurable: true, get: () => 2000 },
    scrollTop: { configurable: true, get: () => scrollTop, set: (v: number) => { scrollTop = v } },
  })
  const rows = Array.from(container.querySelectorAll('[data-display-index]')) as HTMLElement[]
  const tops = [-200, -150, -100, -40, 300]
  rows.forEach((row, i) => setRect(row, tops[i] ?? 300 + i * 100, 40))
  return { scroller, rows, get scrollTop() { return scrollTop } }
}

interface QueuedFrame { id: number; cb: FrameRequestCallback }
let frames: QueuedFrame[] = []
let nextId = 1
let clock = 0
let originalRaf: typeof requestAnimationFrame
let originalCancel: typeof cancelAnimationFrame
let nowSpy: ReturnType<typeof vi.spyOn>

function flushFrames(at = 16) {
  clock = at
  const pending = frames.splice(0)
  act(() => { pending.forEach(f => f.cb(at)) })
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  frames = []
  nextId = 1
  clock = 0
  originalRaf = globalThis.requestAnimationFrame
  originalCancel = globalThis.cancelAnimationFrame
  globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => { const id = nextId++; frames.push({ id, cb }); return id }) as typeof requestAnimationFrame
  globalThis.cancelAnimationFrame = ((id: number) => { frames = frames.filter(f => f.id !== id) }) as typeof cancelAnimationFrame
  nowSpy = vi.spyOn(performance, 'now').mockImplementation(() => clock)
})

afterEach(() => {
  nowSpy.mockRestore()
  globalThis.requestAnimationFrame = originalRaf
  globalThis.cancelAnimationFrame = originalCancel
})

describe('ChatPane pinned prompt (chat-core P5-d)', () => {
  it('numbers its transcript rows and mounts the fold sentinel', () => {
    const { container } = renderPane()
    const rows = container.querySelectorAll('[data-display-index]')
    expect(rows.length).toBe(MESSAGES.length)
    expect(Array.from(rows).map(r => r.getAttribute('data-display-index'))).toEqual(['0', '1', '2', '3', '4'])
    // Nothing pinned at rest: the sentinel is there, the card is not.
    expect(screen.queryByTestId('pinned-prompt')).toBeNull()
  })

  it('pins the prompt that scrolled behind the fold and hides its bubble row', () => {
    const { container } = renderPane()
    const geom = layOut(container)
    act(() => { geom.scroller.dispatchEvent(new Event('scroll')) })
    flushFrames()
    const card = screen.getByTestId('pinned-prompt')
    expect(card.textContent).toContain('second prompt about the pinned banner')
    // The banner stands in for row 2's bubble: that row is hidden by
    // visibility (it keeps its height), every other row stays visible.
    expect(geom.rows[2].style.visibility).toBe('hidden')
    expect(geom.rows[1].style.visibility).toBe('')
    expect(geom.rows[3].style.visibility).toBe('')
    // The jump affordance carries the same title the main chat uses.
    expect(screen.getByTitle('Jump to this turn')).toBeTruthy()
  })

  it('does not pin while the pin-last-prompt setting is off', () => {
    localStorage.setItem('mc-chat-config', JSON.stringify({ pinLastPrompt: false }))
    const { container } = renderPane()
    const geom = layOut(container)
    act(() => { geom.scroller.dispatchEvent(new Event('scroll')) })
    flushFrames()
    expect(screen.queryByTestId('pinned-prompt')).toBeNull()
    expect(geom.rows[2].style.visibility).toBe('')
  })

  it('frameless (Members DM) mounts the same banner with no pane title bar', () => {
    const { container } = renderPane({ frameless: true })
    // No title bar: the host renders the identity header, so the pane's own
    // title (slot key fallback) is not in the DOM.
    expect(screen.queryByText(SLOT)).toBeNull()
    const geom = layOut(container)
    act(() => { geom.scroller.dispatchEvent(new Event('scroll')) })
    flushFrames()
    const card = screen.getByTestId('pinned-prompt')
    expect(card.textContent).toContain('second prompt about the pinned banner')
    // The band is anchored inside the pane root, before the scroller, so it
    // can never paint over a host header that sits above the pane.
    const root = container.querySelector('[data-chat-pane]') as HTMLElement
    expect(root.contains(card)).toBe(true)
    expect(card.compareDocumentPosition(geom.scroller) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('clicking the banner glides the scroller back to the pinned prompt', () => {
    const { container } = renderPane()
    const geom = layOut(container)
    act(() => { geom.scroller.dispatchEvent(new Event('scroll')) })
    flushFrames()
    // Re-lay row 2 where a jump can land it: at viewport y=300 with the
    // scroller at y=0 and scrollTop=200 the raw landing is 500, minus the
    // chrome (fold 0 + pinPushTravel + 24px slack) — so the glide must end
    // strictly above where it started and below the raw landing.
    setRect(geom.rows[2], 300, 40)
    const jump = screen.getByTitle('Jump to this turn')
    act(() => { jump.click() })
    flushFrames(0)
    flushFrames(600)
    expect(geom.scrollTop).toBeGreaterThan(200)
    expect(geom.scrollTop).toBeLessThan(500)
  })
})
