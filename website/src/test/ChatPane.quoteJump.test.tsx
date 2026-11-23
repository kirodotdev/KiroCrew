// A sent quote card in a split-view pane jumps inside the pane's OWN
// transcript (#36545): the card is a control, a click glides the pane's
// scroller to the quoted row through its virtualized landing path and rings
// that row for the main chat's 3 s, and a quote whose row the pane has not
// loaded says it is not among the rows loaded here, and that notice dismisses.
//
// happy-dom has no layout, so rects are hand-set on the rows the pane renders,
// and frames are queued by hand (as ChatPane.pinnedPrompt.test.tsx does).

import { describe, it, expect, vi, beforeEach, afterEach, onTestFinished } from 'vitest'
import { render, act, screen, fireEvent, within, waitFor } from '@testing-library/react'
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
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
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
import { modelsDegraded } from '../providers/modelListHealth'
import { i18nT } from '../i18n/t'
import type { ChatMessage } from '../types'

const SLOT = 'pane-quote-jump'
const quote = (ts: string, mid: string) => ({ role: 'assistant', text: 'the excerpt the card shows', ts, mid })

/** Five rows, each its own display row: the quoted reply is row 1, the quoting send row 4. */
const transcript = (q: ReturnType<typeof quote>): ChatMessage[] => [
  { role: 'user', content: 'first prompt', cls: '', ts: 't0', meta: { mid: 'm0' } },
  { role: 'assistant', content: 'the quoted reply', cls: '', ts: 't1', meta: { mid: 'm1' } },
  { role: 'user', content: 'second prompt', cls: '', ts: 't2', meta: { mid: 'm2' } },
  { role: 'assistant', content: 'second reply', cls: '', ts: 't3', meta: { mid: 'm3' } },
  { role: 'user', content: 'remind the team', cls: '', ts: 't4', meta: { mid: 'm4', quote: q } },
]

function renderPane(messages: ChatMessage[], foldBefore?: string) {
  const store = configureStore({
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
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const view = render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatPane slotKey={SLOT} foldBefore={foldBefore} />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>,
  )
  act(() => {
    store.dispatch(hydrateSlotMessages({ slot: SLOT, messages, hasMore: false, bounded: false, total: messages.length, running: false }))
  })
  return view
}

/** The pane's first mount reads the model list; the mocked empty answer flips
 *  the module-level degraded flag and re-renders the pane. Wait for it, and for
 *  the reads queued behind it, so no update lands outside act. */
async function renderSettledPane(messages: ChatMessage[], foldBefore?: string) {
  const view = renderPane(messages, foldBefore)
  await waitFor(() => expect(modelsDegraded('acp')).toBe(true))
  await act(async () => { await Promise.resolve() })
  return view
}

const rect = (top: number, height: number): DOMRect => ({
  top, bottom: top + height, left: 0, right: 0, width: 0, height, x: 0, y: top, toJSON: () => ({}),
}) as DOMRect

function setRect(el: Element, top: number, height: number) {
  Object.defineProperty(el, 'getBoundingClientRect', { configurable: true, value: () => rect(top, height) })
}

/** The reader sits at the send (row 4), the quoted reply (row 1) far above the
 *  fold, under a prompt (row 0) taller than the pane: landing the head of that
 *  prompt run instead of the reply would leave the reply below the fold. */
const VIEW_H = 400
const DOCK_H = 80
const docTops = [100, 900, 1300, 1700, 2100]
const heights = [700, 100, 100, 100, 100]
function layOut(container: HTMLElement) {
  const scroller = container.querySelector('.chat-container') as HTMLElement
  setRect(scroller, 0, VIEW_H)
  let scrollTop = 1500
  Object.defineProperties(scroller, {
    clientHeight: { configurable: true, get: () => VIEW_H },
    scrollHeight: { configurable: true, get: () => 3000 },
    scrollTop: { configurable: true, get: () => scrollTop, set: (v: number) => { scrollTop = v } },
  })
  const rows = Array.from(container.querySelectorAll('[data-display-index]')) as HTMLElement[]
  // Row tops track the scroll position, so the glide converges on live geometry.
  // The composer dock floats over the scroller's bottom 80 px.
  setRect(container.querySelector('[data-testid="composer-dock-root"]') as HTMLElement, VIEW_H - DOCK_H, DOCK_H)
  rows.forEach((row, i) => {
    Object.defineProperty(row, 'getBoundingClientRect', { configurable: true, value: () => rect(docTops[i] - scrollTop, heights[i]) })
  })
  return { scroller, rows, get scrollTop() { return scrollTop } }
}

interface QueuedFrame { id: number; cb: FrameRequestCallback }
let frames: QueuedFrame[] = []
let nextId = 1
let clock = 0
let originalRaf: typeof requestAnimationFrame
let originalCancel: typeof cancelAnimationFrame
let nowSpy: ReturnType<typeof vi.spyOn>

/** Run every queued frame at `at` ms, and any frame those queue, up to a bound. */
function flushFrames(at: number) {
  clock = at
  for (let round = 0; round < 50 && frames.length; round++) {
    const pending = frames.splice(0)
    act(() => { pending.forEach(f => f.cb(at)) })
  }
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

const sentCard = (container: HTMLElement) => container.querySelector('[data-testid="quote-card-sent"]') as HTMLElement
const replyRow = (rows: HTMLElement[]) => rows[1]
const ringed = (rows: HTMLElement[]) => within(replyRow(rows)).getByText('the quoted reply').closest('.animate-msg-highlight')

describe('ChatPane quote card jump', () => {
  it("glides the pane's own scroller to the quoted row and rings it for 3 s", async () => {
    const { container } = await renderSettledPane(transcript(quote('t1', 'm1')))
    flushFrames(16)
    const geom = layOut(container)
    const card = sentCard(container)
    expect(card.tagName).toBe('BUTTON')
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    onTestFinished(() => { vi.useRealTimers() })

    fireEvent.click(card)
    // Long past the glide's duration: it has converged on row 1.
    flushFrames(5000)

    // The QUOTED row itself is in the visible band: below the scroller's top
    // chrome line, above the composer dock.
    const landed = replyRow(geom.rows).getBoundingClientRect()
    const dockTop = VIEW_H - DOCK_H
    expect(landed.top).toBeGreaterThanOrEqual(0)
    expect(landed.bottom).toBeLessThanOrEqual(dockTop)
    expect(ringed(geom.rows)).not.toBeNull()
    expect(within(geom.rows[4]).getByText('remind the team').closest('.animate-msg-highlight')).toBeNull()
    act(() => { vi.advanceTimersByTime(2999) })
    expect(ringed(geom.rows)).not.toBeNull()
    act(() => { vi.advanceTimersByTime(1) })
    expect(ringed(geom.rows)).toBeNull()
    expect(screen.queryByTestId('chat-pane-quote-unavailable')).toBeNull()
  })

  it('opens a closed fresh-start fold over the quoted row, then lands and rings it', async () => {
    // Before the fresh start: the prompt (row 0) and the quoted reply (row 1);
    // after it: the quoting send. Opened, the divider is row 2, the send row 3.
    const at = (s: number) => `2026-10-10T06:00:0${s}+00:00`
    const { container } = await renderSettledPane([
      { role: 'user', content: 'first prompt', cls: '', ts: at(0), meta: { mid: 'm0' } },
      { role: 'assistant', content: 'the quoted reply', cls: '', ts: at(1), meta: { mid: 'm1' } },
      { role: 'user', content: 'remind the team', cls: '', ts: at(3), meta: { mid: 'm4', quote: quote(at(1), 'm1') } },
    ], at(2))
    flushFrames(16)
    layOut(container)
    expect(screen.queryByText('the quoted reply')).toBeNull()

    fireEvent.click(sentCard(container))
    expect(screen.getByTestId('earlier-conversation-toggle').getAttribute('aria-expanded')).toBe('true')
    const geom = layOut(container) // the unfolded rows are new nodes
    flushFrames(5000)

    const landed = replyRow(geom.rows).getBoundingClientRect()
    expect(landed.top).toBeGreaterThanOrEqual(0)
    expect(landed.bottom).toBeLessThanOrEqual(VIEW_H - DOCK_H)
    expect(ringed(geom.rows)).not.toBeNull()
    expect(screen.queryByTestId('chat-pane-quote-unavailable')).toBeNull()
  })

  it('opens the closed step fold over a quoted working step, then ends the glide and scrolls to that row', async () => {
    // Display rows: 0 the prompt, 1 a finished turn (the quoted step, a tool
    // call, the answer) whose steps the default Show Thinking Inline folds,
    // 2 the quoting send.
    const { container } = await renderSettledPane([
      { role: 'user', content: 'first prompt', cls: '', ts: 't0', meta: { mid: 'm0' } },
      { role: 'assistant', content: 'the quoted reply', cls: '', ts: 't1', meta: { mid: 'm1' } },
      { role: 'tool', content: '🔧 Running: cat policy.md', cls: '', ts: 't1a', meta: { tool_call_id: 'c1', tool_input: 'cat policy.md' } },
      { role: 'assistant', content: 'The deploy key rotates every ninety days; the next one is in March.', cls: '', ts: 't2', meta: { mid: 'm2' } },
      { role: 'user', content: 'remind the team', cls: '', ts: 't3', meta: { mid: 'm3', quote: quote('t1', 'm1') } },
    ])
    flushFrames(16)
    const geom = layOut(container)
    const turn = geom.rows[1]
    const foldRow = () => within(turn).getByText('the quoted reply').closest('[data-fold-row]')
    expect(foldRow()).toHaveAttribute('data-collapsed', 'true')
    // The row scroll lands far from where the glide aims (row 1, under the chrome).
    const scrolled: Element[] = []
    const original = Element.prototype.scrollIntoView
    Element.prototype.scrollIntoView = function (this: Element) { scrolled.push(this); geom.scroller.scrollTop = 200 }
    onTestFinished(() => { Element.prototype.scrollIntoView = original })
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    onTestFinished(() => { vi.useRealTimers() })

    fireEvent.click(sentCard(container))
    expect(foldRow()).not.toHaveAttribute('data-collapsed')
    const ringedRow = within(turn).getByText('the quoted reply').closest('.animate-msg-highlight')
    expect(ringedRow).not.toBeNull()
    // Past the fold's transition, the turn scrolls to the quoted row itself.
    act(() => { vi.advanceTimersByTime(250) })
    expect(scrolled).toContain(ringedRow)
    // The pane's glide was ended first, so it does not pull the turn back.
    flushFrames(5000)
    expect(geom.scrollTop).toBe(200)
  })

  it('says it cannot jump when the quoted row is not loaded, and the notice dismisses', async () => {
    const { container } = await renderSettledPane(transcript(quote('t-gone', 'm-gone')))
    flushFrames(16)
    const geom = layOut(container)

    fireEvent.click(sentCard(container))
    flushFrames(5000)

    expect(geom.scrollTop).toBe(1500)
    const notice = screen.getByTestId('chat-pane-quote-unavailable')
    expect(notice).toHaveTextContent(i18nT('pages.chat.quoteCard.message_not_loaded'))
    fireEvent.click(within(notice).getByRole('button', { name: i18nT('components.errorNotice.dismiss') }))
    expect(screen.queryByTestId('chat-pane-quote-unavailable')).toBeNull()
  })
})
