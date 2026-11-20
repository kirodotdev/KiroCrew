// Feature: the opt-in top-down chat layout in ChatPane (Settings > Chat >
// `topDownLayout`).
//
// The composer dock keeps the scroll-under-glass decision
// (docs/decisions/2026-10-02-chat-transcript-scrolls-under-the-composer-glass.md)
// at the other edge: the dock root stays `absolute` over a full-height scroller,
// anchored to the scroller's TOP edge, and the scroller pays for the covered
// strip with `padding-top`. A crewmate's chat (the Members page DM) keeps the
// bottom layout: its unread landing reads the bottom geometry.
//
// happy-dom has no layout, so the two measured inputs are stubbed on the
// prototype: the dock root's `offsetHeight` (the shared dock measurement) and
// the scroller's `offsetTop` (the pane's title row above it).

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, act, within, fireEvent, waitFor } from '@testing-library/react'
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
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <button type="button" data-testid="transcript-focusable">{content}</button> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

// Desktop width: the top-down layout is off under the 768px breakpoint.
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'
import { api } from '../api/client'

const detail = api.chatSlotDetail as ReturnType<typeof vi.fn>
const SLOT = 'pane-top-down-dock'
const CHAT_CONFIG_KEY = 'mc-chat-config'
const DOCK_H = 120
const TITLE_ROW_H = 41
// DOCK_CLEARANCE_PX (pages/chat/composerDockMetrics.ts).
const CLEARANCE = 16

function makeStore(slotMode = '') {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: SLOT, messages: 0, running: false, mode: slotMode, pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        slotsLoaded: true,
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
}

async function renderPane(props: Partial<React.ComponentProps<typeof ChatPane>> = {}, slotMode = '') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  let container!: HTMLElement
  await act(async () => {
    container = render(
      <Provider store={makeStore(slotMode)}>
        <QueryClientProvider client={qc}>
          <ThemeProvider>
            <MemoryRouter>
              <ChatPane slotKey={SLOT} {...props} />
            </MemoryRouter>
          </ThemeProvider>
        </QueryClientProvider>
      </Provider>,
    ).container
  })
  const scroller = container.querySelector('.chat-container') as HTMLElement
  const dock = container.querySelector('[data-testid="composer-dock-root"]') as HTMLElement
  expect(scroller).not.toBeNull()
  expect(dock).not.toBeNull()
  return { container, scroller, dock }
}

type Saved = { name: 'offsetHeight' | 'offsetTop'; desc: PropertyDescriptor | undefined }
let saved: Saved[] = []

function stubGeometry() {
  saved = (['offsetHeight', 'offsetTop'] as const).map(name => ({ name, desc: Object.getOwnPropertyDescriptor(HTMLElement.prototype, name) }))
  Object.defineProperty(HTMLElement.prototype, 'offsetHeight', {
    configurable: true,
    get(this: HTMLElement) { return this.getAttribute('data-testid') === 'composer-dock-root' ? DOCK_H : 0 },
  })
  Object.defineProperty(HTMLElement.prototype, 'offsetTop', {
    configurable: true,
    get(this: HTMLElement) { return this.classList.contains('chat-container') ? TITLE_ROW_H : 0 },
  })
}

function restoreGeometry() {
  for (const { name, desc } of saved) {
    if (desc) Object.defineProperty(HTMLElement.prototype, name, desc)
    else delete (HTMLElement.prototype as unknown as Record<string, unknown>)[name]
  }
  saved = []
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.setItem(CHAT_CONFIG_KEY, JSON.stringify({ topDownLayout: true }))
  stubGeometry()
})

afterEach(() => {
  restoreGeometry()
  localStorage.removeItem(CHAT_CONFIG_KEY)
})

describe('ChatPane top-down layout: the dock floats over the scroller top', () => {
  it('anchors the dock root to the scroller top edge and pays for it with top padding', async () => {
    const { container, scroller, dock } = await renderPane()
    // Out of flow over the scroller, anchored to the top: the scroller's top in
    // the pane (under the title row) plus no host band.
    expect(dock.className.split(/\s+/)).toEqual(expect.arrayContaining(['absolute', 'pointer-events-none']))
    expect(dock.className.split(/\s+/)).not.toContain('bottom-0')
    expect(dock.style.top).toBe(`${TITLE_ROW_H}px`)
    // The covered strip is top padding; the bottom keeps the clearance only.
    expect(scroller.style.paddingTop).toBe(`${DOCK_H + CLEARANCE}px`)
    expect(scroller.style.paddingBottom).toBe(`${CLEARANCE}px`)
    // No in-flow slot reserves height above the scroller. The dock is nested
    // in a zero-height sibling BEFORE the scroller for keyboard focus. The
    // sibling's `order-last` class preserves the old post-scroller flex paint
    // order without a z-index.
    expect(container.querySelector('[data-testid="chat-pane-top-dock-slot"]')).toBeNull()
    const dockSlot = dock.parentElement as HTMLElement
    expect(dockSlot.parentElement).toBe(scroller.parentElement)
    expect(dockSlot.className.split(/\s+/)).toContain('order-last')
    expect(dockSlot.offsetHeight).toBe(0)
    expect(dockSlot.compareDocumentPosition(scroller) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('puts the composer before transcript controls only in top-down mode without remounting it', async () => {
    detail.mockResolvedValue({
      messages: [{ role: 'assistant', content: 'transcript action', ts: '2026-08-13T09:00:00Z', meta: { mid: 'focus-row' } }],
      running: false, has_more: false, total: 1,
    })
    const { container, scroller, dock } = await renderPane()
    const transcriptControl = await within(scroller).findByTestId('transcript-focusable')
    const firstFocusable = scroller.querySelector<HTMLElement>('button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])')
    const composer = dock.querySelector('textarea')

    expect(firstFocusable).toBe(transcriptControl)
    expect(composer).not.toBeNull()
    expect(composer!.compareDocumentPosition(firstFocusable!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()

    composer!.focus()
    localStorage.setItem(CHAT_CONFIG_KEY, JSON.stringify({ topDownLayout: false }))
    await act(async () => { window.dispatchEvent(new Event('mc-config-changed')) })
    await waitFor(() => expect(dock.className.split(/\s+/)).toContain('bottom-0'))

    const bottomDock = container.querySelector('[data-testid="composer-dock-root"]') as HTMLElement
    const bottomComposer = bottomDock.querySelector('textarea')
    const bottomFirstFocusable = scroller.querySelector<HTMLElement>('button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])')
    expect(bottomComposer).toBe(composer)
    expect(document.activeElement).toBe(composer)
    expect(bottomFirstFocusable).toBe(transcriptControl)
    expect(composer!.compareDocumentPosition(bottomFirstFocusable!) & Node.DOCUMENT_POSITION_PRECEDING).toBeTruthy()
  })

  // The dock grows downward from its top offset, so an expanded queue, the
  // cards above the composer and the composer can be taller than the pane. The
  // dock is capped to the height left under its top. The status stack yields
  // first, then the cards scroll, and the composer row holds.
  it('caps the dock to the pane height under its top and lets the status stack shrink and scroll first', async () => {
    const { dock } = await renderPane({ topInset: 48 })
    const stack = dock.querySelector('[data-testid="composer-status-stack"]') as HTMLElement
    const inputArea = stack.nextElementSibling as HTMLElement
    expect(dock.className.split(/\s+/)).toEqual(expect.arrayContaining(['flex', 'flex-col']))
    expect(dock.style.maxHeight).toBe(`calc(100% - ${TITLE_ROW_H + 48}px)`)
    expect(stack.className.split(/\s+/)).toEqual(expect.arrayContaining(['overflow-y-auto', 'overscroll-contain', 'pb-[11px]', 'mb-[-11px]']))
    // The flex-shrink weight that makes the stack reach 0 before the input
    // area, whose flex minimum is 0, gives up any height.
    expect(stack.className.split(/\s+/)).toContain('shrink-[1000]')
    expect(stack.contains(inputArea)).toBe(false)
  })

  it('scrolls the cards above the composer at the cap and never shrinks the composer row', async () => {
    const { dock } = await renderPane({}, 'member')
    const precards = within(dock).getByTestId('composer-precards')
    const inputArea = precards.parentElement as HTMLElement
    const card = within(dock).getByTestId('mate-worker-prompts')
    const composer = dock.querySelector('textarea') as HTMLTextAreaElement
    const row = within(dock).getByTestId('composer-row')
    expect(composer).not.toBeNull()
    expect(inputArea.className.split(/\s+/)).toEqual(expect.arrayContaining(['relative', 'z-10', 'dock-inert', 'flex', 'flex-col', 'min-h-0']))
    expect(precards.className.split(/\s+/)).toEqual(expect.arrayContaining(['min-h-0', 'overflow-y-auto', 'overscroll-contain', 'dock-inert']))
    expect(precards.contains(card)).toBe(true)
    expect(precards.contains(composer)).toBe(false)
    expect(row.parentElement).toBe(inputArea)
    expect(row.contains(composer)).toBe(true)
    expect(row.className.split(/\s+/)).toContain('shrink-0')
  })

  it('keeps the bottom input area boxes: the card and composer wrappers make no box', async () => {
    localStorage.setItem(CHAT_CONFIG_KEY, JSON.stringify({ topDownLayout: false }))
    const { dock } = await renderPane({}, 'member')
    const precards = within(dock).getByTestId('composer-precards')
    const row = within(dock).getByTestId('composer-row')
    const inputArea = precards.parentElement as HTMLElement
    expect(dock.className.split(/\s+/)).toContain('bottom-0')
    expect(inputArea.className).toBe('relative z-10 dock-inert')
    expect(precards.className).toBe('contents')
    expect(row.className).toBe('contents')
    expect(precards.contains(within(dock).getByTestId('mate-worker-prompts'))).toBe(true)
    expect(within(dock).getByTestId('composer-status-stack').className).toBe('dock-inert')
  })

  it('leaves the bottom dock uncapped and its status stack unscrolled', async () => {
    const { dock } = await renderPane({ crewmate: { name: 'Radar' } })
    const stack = dock.querySelector('[data-testid="composer-status-stack"]') as HTMLElement
    expect(dock.className.split(/\s+/)).not.toContain('flex-col')
    expect(dock.style.maxHeight).toBe('')
    expect(stack.className).toBe('dock-inert')
  })

  it('pays a host band over the pane top once: the dock sits under it and the scroller pads it', async () => {
    const { scroller, dock } = await renderPane({ topInset: 48 })
    expect(dock.style.top).toBe(`${TITLE_ROW_H + 48}px`)
    expect(scroller.style.paddingTop).toBe(`${48 + DOCK_H + CLEARANCE}px`)
  })

  it('keeps a crewmate pane on the bottom layout', async () => {
    const { scroller, dock } = await renderPane({ crewmate: { name: 'Radar' } })
    expect(dock.className.split(/\s+/)).toEqual(expect.arrayContaining(['absolute', 'bottom-0', 'pointer-events-none']))
    expect(dock.style.top).toBe('')
    expect(scroller.style.paddingTop).toBe('12px')
    expect(scroller.style.paddingBottom).toBe(`${DOCK_H + CLEARANCE}px`)
  })

  it('keeps a phone-width pane on the bottom layout', async () => {
    // Under the 768px breakpoint useIsMobile reports a phone, and the opt-in
    // applies at desktop width only. A new matchMedia function makes the hook
    // read the query again; the finally block puts the desktop one back.
    const desktop = window.matchMedia
    Object.defineProperty(window, 'matchMedia', {
      writable: true,
      value: vi.fn().mockReturnValue({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
    })
    try {
      const { scroller, dock } = await renderPane()
      expect(dock.className.split(/\s+/)).toEqual(expect.arrayContaining(['absolute', 'bottom-0', 'pointer-events-none']))
      expect(dock.style.top).toBe('')
      expect(scroller.style.paddingTop).toBe('12px')
      expect(scroller.style.paddingBottom).toBe(`${DOCK_H + CLEARANCE}px`)
    } finally {
      Object.defineProperty(window, 'matchMedia', { writable: true, value: desktop })
    }
  })

  it('keeps the bottom layout with the setting off', async () => {
    localStorage.setItem(CHAT_CONFIG_KEY, JSON.stringify({ topDownLayout: false }))
    const { scroller, dock } = await renderPane()
    expect(dock.className.split(/\s+/)).toEqual(expect.arrayContaining(['absolute', 'bottom-0', 'pointer-events-none']))
    expect(dock.style.top).toBe('')
    expect(scroller.style.paddingTop).toBe('12px')
    expect(scroller.style.paddingBottom).toBe(`${DOCK_H + CLEARANCE}px`)
  })
})

// The pane's bounded page has older history the pane cannot page itself (no
// cursor), so it links to the full session instead. That link points at older
// history, so it sits at the history end: before the oldest row in the bottom
// layout (as on main), after it in the top-down layout, where history is at
// the bottom and the leading slot holds only live-edge chrome.
describe('ChatPane top-down layout: the open-session link sits at the history end', () => {
  // Two turns, so newest-first has an order to change: the turn order flips
  // and each turn keeps its prompt above its reply.
  const OLDEST_PROMPT = 'oldest prompt row'
  const OLDEST_REPLY = 'oldest reply row'
  const NEWEST_PROMPT = 'newest prompt row'
  const NEWEST_REPLY = 'newest reply row'
  const EMPTY_PAGE = { messages: [], running: false, has_more: false, total: 0 }
  const follows = (a: Node, b: Node) => (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0

  beforeEach(() => {
    detail.mockResolvedValue({
      messages: [
        { role: 'user', content: OLDEST_PROMPT, ts: '2026-08-13T09:00:00Z', meta: { mid: 'oldest-1' } },
        { role: 'assistant', content: OLDEST_REPLY, ts: '2026-08-13T09:01:00Z', meta: { mid: 'oldest-2' } },
        { role: 'user', content: NEWEST_PROMPT, ts: '2026-08-13T09:05:00Z', meta: { mid: 'newest-1' } },
        { role: 'assistant', content: NEWEST_REPLY, ts: '2026-08-13T09:06:00Z', meta: { mid: 'newest-2' } },
      ],
      running: false, has_more: true, total: 120,
    })
  })

  afterEach(() => {
    // clearAllMocks keeps implementations, so put back the file's empty page.
    detail.mockResolvedValue(EMPTY_PAGE)
  })

  async function renderRows(onOpenFull: () => void) {
    const { container } = await renderPane({ onOpenFull })
    const link = await within(container).findByText(/earlier messages/i)
    const at = (text: string) => within(container).getByText(text)
    return { link, oldestPrompt: at(OLDEST_PROMPT), oldestReply: at(OLDEST_REPLY), newestPrompt: at(NEWEST_PROMPT), newestReply: at(NEWEST_REPLY) }
  }

  it('renders the link after the oldest turn in the top-down layout', async () => {
    const onOpenFull = vi.fn()
    const { link, oldestPrompt, oldestReply, newestPrompt, newestReply } = await renderRows(onOpenFull)
    // Newest turn first, then the oldest turn, then the link to what is older still.
    expect(follows(newestReply, oldestPrompt)).toBe(true)
    expect(follows(newestPrompt, link)).toBe(true)
    expect(follows(oldestReply, link)).toBe(true)
    expect(link.closest('[data-vc-trailing]')).not.toBeNull()
    expect(link.closest('[data-vc-leading]')).toBeNull()
    // The click is unchanged: it hands over the pane's oldest row as the anchor.
    fireEvent.click(link)
    expect(onOpenFull).toHaveBeenCalledWith(SLOT, '2026-08-13T09:00:00Z', 'oldest-1')
  })

  it('keeps the link before the oldest turn in the bottom layout', async () => {
    localStorage.setItem(CHAT_CONFIG_KEY, JSON.stringify({ topDownLayout: false }))
    const onOpenFull = vi.fn()
    const { link, oldestPrompt, newestReply } = await renderRows(onOpenFull)
    expect(follows(oldestPrompt, newestReply)).toBe(true)
    expect(follows(link, oldestPrompt)).toBe(true)
    expect(link.closest('[data-vc-trailing]')).toBeNull()
    fireEvent.click(link)
    expect(onOpenFull).toHaveBeenCalledWith(SLOT, '2026-08-13T09:00:00Z', 'oldest-1')
  })
})

// happy-dom has no layout, so a real ResizeObserver never fires. This one
// records what it observes, and a test fires it for one target.
class FakeResizeObserver {
  static all: FakeResizeObserver[] = []
  readonly targets = new Set<Element>()
  constructor(readonly cb: ResizeObserverCallback) { FakeResizeObserver.all.push(this) }
  observe(t: Element) { this.targets.add(t) }
  unobserve(t: Element) { this.targets.delete(t) }
  disconnect() { this.targets.clear() }
}
const observed = (t: Element) => FakeResizeObserver.all.some(o => o.targets.has(t))
const fireResize = (t: Element) => { for (const o of FakeResizeObserver.all) if (o.targets.has(t)) o.cb([], o as unknown as ResizeObserver) }
/** The box's content height (`scroll`) and box height (`client`), read live. */
function stubHeights(el: HTMLElement, h: { scroll: number; client: number }) {
  Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => h.scroll })
  Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => h.client })
}

// Top-down, the status stack and the card box above the composer are
// `dock-inert` scroll boxes: they pass input through, so each one's own
// scrollbar is a hit target only while it carries the overflow mark
// (`.dock-inert[data-overflowing="true"]`).
describe('ChatPane top-down layout: the dock scroll boxes mark their overflow', () => {
  let realResizeObserver: typeof ResizeObserver
  beforeEach(() => {
    realResizeObserver = globalThis.ResizeObserver
    FakeResizeObserver.all = []
    globalThis.ResizeObserver = FakeResizeObserver as unknown as typeof ResizeObserver
  })
  afterEach(() => { globalThis.ResizeObserver = realResizeObserver })

  it.each(['composer-status-stack', 'composer-precards'])('marks %s while it overflows, also when only its content grows at the cap', async (testId) => {
    const { dock } = await renderPane({}, 'member')
    const box = within(dock).getByTestId(testId)
    const child = box.firstElementChild as HTMLElement
    expect(child).not.toBeNull()
    const h = { scroll: 100, client: 100 }
    stubHeights(box, h)
    fireResize(box)
    expect(box.dataset.overflowing).toBe('false')
    // At the cap the box keeps its height, so only the child reports the growth.
    h.scroll = 180
    fireResize(child)
    expect(box.dataset.overflowing).toBe('true')
    h.scroll = 100
    fireResize(child)
    expect(box.dataset.overflowing).toBe('false')
  })

  it('drops the mark when the layout flips to the bottom, where the status stack stays inert', async () => {
    const { dock } = await renderPane({}, 'member')
    const stack = within(dock).getByTestId('composer-status-stack')
    stubHeights(stack, { scroll: 180, client: 100 })
    fireResize(stack)
    expect(stack.dataset.overflowing).toBe('true')
    localStorage.setItem(CHAT_CONFIG_KEY, JSON.stringify({ topDownLayout: false }))
    await act(async () => { window.dispatchEvent(new Event('mc-config-changed')) })
    await waitFor(() => expect(dock.className.split(/\s+/)).toContain('bottom-0'))
    // The same element, now the bottom layout's plain `dock-inert` wrapper: a
    // stale mark would make it catch input across the full pane width.
    expect(within(dock).getByTestId('composer-status-stack')).toBe(stack)
    expect(stack.className).toBe('dock-inert')
    expect(stack).not.toHaveAttribute('data-overflowing')
    expect(observed(stack)).toBe(false)
    expect(observed(within(dock).getByTestId('composer-precards'))).toBe(false)
  })

  it('leaves the bottom dock boxes unobserved and unmarked', async () => {
    localStorage.setItem(CHAT_CONFIG_KEY, JSON.stringify({ topDownLayout: false }))
    const { dock } = await renderPane({}, 'member')
    for (const testId of ['composer-status-stack', 'composer-precards']) {
      const box = within(dock).getByTestId(testId)
      expect(observed(box)).toBe(false)
      expect(box).not.toHaveAttribute('data-overflowing')
    }
  })
})
