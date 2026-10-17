/**
 * Sidebar column resize via Pointer Events (mouse + touch + pen).
 *
 * The handle uses usePointerDrag, so the same gesture works for any pointer
 * type — including a touch drag on a tablet at desktop width, where the sidebar
 * is a side-by-side panel with a visible handle.
 *
 * Locks the contract:
 *  (1) A pointer drag on the handle changes the panel width by the pointer delta.
 *  (2) Width is clamped to [SIDEBAR_MIN, SIDEBAR_MAX].
 *  (3) The final width persists to localStorage on pointer-up.
 *  (4) onDragChange brackets the gesture (true on down, false on up).
 *
 * fireEvent.pointer* is the input path a touch drag takes in the browser; the
 * handler is pointer-type-agnostic, so exercising it proves touch works too.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent, act, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'

// Render framer-motion elements as plain DOM (jsdom can't run projection).
vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'layoutScroll', 'initial', 'animate', 'exit',
    'transition', 'variants', 'whileHover', 'whileTap', 'whileInView',
    'drag', 'dragConstraints', 'dragElastic', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef((props: Record<string, unknown>, ref: React.Ref<unknown>) => {
      const clean: Record<string, unknown> = {}
      for (const k of Object.keys(props)) {
        if (k === 'children') continue
        if (k === 'layoutId') { clean['data-layout-id'] = props[k]; continue }
        if (FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children as React.ReactNode)
    })
  const motion = new Proxy({}, { get: (_t, tag: string) => make(tag) })
  return {
    motion,
    AnimatePresence: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
    LayoutGroup: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
  }
})

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))
// Board columns are off unless a test renders the board (renderSidebar({ board })).
const chatConfig = vi.hoisted(() => ({ tagColumnsEnabled: false }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: chatConfig.tagColumnsEnabled, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: () => vi.fn().mockResolvedValue([]),
  }),
}))

// Desktop viewport: not mobile, so the sidebar renders as a side-by-side panel
// with the resize handle visible (the mobile overlay CSS-hides the handle).
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})

import ChatSidebar, { SIDEBAR_MIN, SIDEBAR_MAX } from '../pages/ChatSidebar'
import { CHAT_PANE_MIN_W } from '../pages/chat/SidePanel'
import { __resetRailWidth, setRailWidth, useRailWidth } from '../hooks/useRailWidth'
import { renderHook } from '@testing-library/react'

const SLOTS = [
  { key: 'k1', title: 'One', messages: 1, running: false, modified: 1000 },
]

// One board lane: enough for the sidebar to show board view.
const LANE = { id: 'lane-1', name: 'Lane', tag_ids: [], mode: 'any', order: 0, include_untagged: true }

function renderSidebar({ fillsHost, board }: { fillsHost?: boolean; board?: boolean } = {}) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots: SLOTS, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null, slotStatusDetail: {}, subagents: {}, slotActivity: {} } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], [])
  // Board columns stay as seeded (no refetch to the mocked empty list), so a
  // test switches views by setting this query (see showBoard).
  qc.setQueryDefaults(['tag-columns'], { staleTime: Infinity })
  chatConfig.tagColumnsEnabled = true
  qc.setQueryData(['tag-columns'], board ? [LANE] : [])
  const onWidthChange = vi.fn()
  const onDragChange = vi.fn()
  const tree = () => (
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={SLOTS} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
              onWidthChange={onWidthChange} onDragChange={onDragChange} fillsHost={fillsHost}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>
  )
  const utils = render(tree())
  const handle = utils.container.querySelector('.sidebar-resize-handle') as HTMLElement
  const panel = utils.container.querySelector('.sidebar-inner') as HTMLElement
  // The sidebar reads the live window width itself (useWindowWidth), so a
  // window resize is what moves it: set the width and fire the event.
  const rerenderAtWidth = (winW: number) => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: winW })
    window.dispatchEvent(new Event('resize'))
  }
  // The lane swap lands a tick or more after the write, so wait for it.
  const showBoard = async (on: boolean) => {
    act(() => { qc.setQueryData(['tag-columns'], on ? [LANE] : []) })
    await waitFor(() => expect(utils.queryByTestId('tree-view-lane') === null).toBe(on))
  }
  return { ...utils, handle, panel, onWidthChange, onDragChange, rerenderAtWidth, showBoard }
}

// Default width when localStorage is empty (see ChatSidebar useState init).
const DEFAULT_W = 260

beforeEach(() => localStorage.clear())
afterEach(() => vi.clearAllMocks())

describe('chat sidebar — pointer/touch resize', () => {
  it('exposes the handle as an accessible vertical separator', () => {
    const { getByRole } = renderSidebar()
    const sep = getByRole('separator', { name: 'Resize sidebar' })
    expect(sep).toBeTruthy()
    expect(sep.getAttribute('aria-orientation')).toBe('vertical')
    // touch-action:none lets a touch drag resize instead of scrolling the page.
    expect(sep.style.touchAction).toBe('none')
  })

  it('a pointer drag widens the panel by the pointer delta and persists it', () => {
    const { handle, panel, onWidthChange, onDragChange } = renderSidebar()
    fireEvent.pointerDown(handle, { clientX: DEFAULT_W, pointerId: 1 })
    fireEvent.pointerMove(handle, { clientX: DEFAULT_W + 100, pointerId: 1 })
    fireEvent.pointerUp(handle, { clientX: DEFAULT_W + 100, pointerId: 1 })

    expect(panel.style.width).toBe(`${DEFAULT_W + 100}px`)
    expect(onWidthChange).toHaveBeenCalledWith(DEFAULT_W + 100)
    expect(localStorage.getItem('mc-sidebar-width')).toBe(String(DEFAULT_W + 100))
    // Drag state brackets the gesture.
    expect(onDragChange).toHaveBeenCalledWith(true)
    expect(onDragChange).toHaveBeenLastCalledWith(false)
  })

  it('clamps width to SIDEBAR_MIN when dragged far left', () => {
    const { handle, panel } = renderSidebar()
    fireEvent.pointerDown(handle, { clientX: DEFAULT_W, pointerId: 1 })
    fireEvent.pointerMove(handle, { clientX: DEFAULT_W - 5000, pointerId: 1 })
    fireEvent.pointerUp(handle, { clientX: DEFAULT_W - 5000, pointerId: 1 })
    expect(panel.style.width).toBe(`${SIDEBAR_MIN}px`)
    expect(localStorage.getItem('mc-sidebar-width')).toBe(String(SIDEBAR_MIN))
  })

  it('clamps width to SIDEBAR_MAX when dragged far right', () => {
    const realW = window.innerWidth
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 2400 })
    try {
      const { handle, panel } = renderSidebar()
      fireEvent.pointerDown(handle, { clientX: DEFAULT_W, pointerId: 1 })
      fireEvent.pointerMove(handle, { clientX: DEFAULT_W + 100000, pointerId: 1 })
      fireEvent.pointerUp(handle, { clientX: DEFAULT_W + 100000, pointerId: 1 })
      expect(panel.style.width).toBe(`${SIDEBAR_MAX}px`)
    } finally {
      Object.defineProperty(window, 'innerWidth', { configurable: true, value: realW })
    }
  })

  it('restores body styles and clears drag state if unmounted mid-drag', () => {
    const { handle, onDragChange, unmount } = renderSidebar()
    fireEvent.pointerDown(handle, { clientX: DEFAULT_W, pointerId: 1 })
    // Drag started: body is locked and the parent is told dragging=true.
    expect(document.body.style.cursor).toBe('col-resize')
    expect(onDragChange).toHaveBeenCalledWith(true)
    // Unmount mid-drag (collapse / route change) with no pointerup — the
    // teardown guard must restore the global body styles and clear drag state
    // so nothing is left stuck (onEnd can't fire once the element is gone).
    unmount()
    expect(document.body.style.cursor).toBe('')
    expect(document.body.style.userSelect).toBe('')
    expect(onDragChange).toHaveBeenLastCalledWith(false)
  })
})

// #14853: in board view on a wide window the drag ceiling is the window minus
// the nav rail minus the chat pane minimum, so an ultra-wide screen is no
// longer held at SIDEBAR_MAX. Normal-width windows keep SIDEBAR_MAX exactly.
describe('chat sidebar — board view window-relative drag ceiling', () => {
  const realW = window.innerWidth
  const setWin = (w: number) => Object.defineProperty(window, 'innerWidth', { configurable: true, value: w })
  const railW = () => renderHook(() => useRailWidth()).result.current
  beforeEach(() => __resetRailWidth())
  afterEach(() => setWin(realW))

  function dragFarRight() {
    const r = renderSidebar({ board: true })
    fireEvent.pointerDown(r.handle, { clientX: DEFAULT_W, pointerId: 1 })
    fireEvent.pointerMove(r.handle, { clientX: DEFAULT_W + 100000, pointerId: 1 })
    fireEvent.pointerUp(r.handle, { clientX: DEFAULT_W + 100000, pointerId: 1 })
    return r
  }

  it.each([3440, 5120])('at %ipx the sidebar drags past SIDEBAR_MAX and leaves the chat pane its minimum', (w) => {
    setWin(w)
    const { panel, getByRole } = dragFarRight()
    const width = parseInt(panel.style.width, 10)
    expect(width).toBeGreaterThan(SIDEBAR_MAX)
    expect(w - railW() - width).toBe(CHAT_PANE_MIN_W)
    expect(localStorage.getItem('mc-sidebar-width')).toBe(String(width))
    expect(getByRole('separator', { name: 'Resize sidebar' }).getAttribute('aria-valuemax')).toBe(String(width))
  })

  it('at 3440px arrow-key nudges stop at the same window-relative ceiling', () => {
    setWin(3440)
    localStorage.setItem('mc-sidebar-width', String(3440 - railW() - CHAT_PANE_MIN_W - 5))
    const { panel, getByRole } = renderSidebar({ board: true })
    const sep = getByRole('separator', { name: 'Resize sidebar' })
    fireEvent.keyDown(sep, { key: 'ArrowRight' })
    fireEvent.keyDown(sep, { key: 'ArrowRight' })
    expect(parseInt(panel.style.width, 10)).toBe(3440 - railW() - CHAT_PANE_MIN_W)
  })

  it('a width saved on a wide window loads intact there and narrows beside a minimum chat pane on a 1440px window', () => {
    setWin(3440)
    localStorage.setItem('mc-sidebar-width', '2400')
    const wide = renderSidebar({ board: true })
    expect(wide.panel.style.width).toBe('2400px')
    wide.unmount()
    setWin(1440)
    const narrow = renderSidebar({ board: true })
    expect(narrow.panel.style.width).toBe(`${1440 - railW() - CHAT_PANE_MIN_W}px`)
    // The preference itself is untouched until the user drags again.
    expect(localStorage.getItem('mc-sidebar-width')).toBe('2400')
  })

  it('a window narrowed after a wide drag paints the root inside the drawer and drags from there', () => {
    setWin(3440)
    const r = dragFarRight()
    const wide = 3440 - railW() - CHAT_PANE_MIN_W
    expect(r.panel.style.width).toBe(`${wide}px`)
    act(() => { setWin(1440); r.rerenderAtWidth(1440) })
    // Held beside a minimum chat pane so the handle stays on screen, not left
    // at the wide width or at SIDEBAR_MAX (which overhangs a 1440px window).
    const narrow = 1440 - railW() - CHAT_PANE_MIN_W
    expect(r.panel.style.width).toBe(`${narrow}px`)
    expect(localStorage.getItem('mc-sidebar-width')).toBe(String(wide))
    // A drag starts from the painted width, so pulling left moves the edge at once.
    fireEvent.pointerDown(r.handle, { clientX: 0, pointerId: 2 })
    fireEvent.pointerMove(r.handle, { clientX: -100, pointerId: 2 })
    fireEvent.pointerUp(r.handle, { clientX: -100, pointerId: 2 })
    expect(r.panel.style.width).toBe(`${narrow - 100}px`)
  })

  it('a press on the handle without moving keeps a wide saved width on a narrowed window', () => {
    setWin(1440)
    localStorage.setItem('mc-sidebar-width', '2884')
    const r = renderSidebar({ board: true })
    expect(r.panel.style.width).toBe(`${1440 - railW() - CHAT_PANE_MIN_W}px`)
    fireEvent.pointerDown(r.handle, { clientX: 500, pointerId: 3 })
    fireEvent.pointerUp(r.handle, { clientX: 500, pointerId: 3 })
    expect(localStorage.getItem('mc-sidebar-width')).toBe('2884')
    act(() => { setWin(3440); r.rerenderAtWidth(3440) })
    expect(r.panel.style.width).toBe('2884px')
  })

  it('a pen press that reports moves with no horizontal travel keeps a wide saved width', () => {
    setWin(1440)
    localStorage.setItem('mc-sidebar-width', '2884')
    const r = renderSidebar({ board: true })
    fireEvent.pointerDown(r.handle, { clientX: 500, clientY: 300, pointerId: 4, pointerType: 'pen' })
    // Pressure changes and vertical jitter: later moves, zero horizontal delta.
    fireEvent.pointerMove(r.handle, { clientX: 500, clientY: 300, pointerId: 4, pointerType: 'pen' })
    fireEvent.pointerMove(r.handle, { clientX: 500, clientY: 304, pointerId: 4, pointerType: 'pen' })
    fireEvent.pointerUp(r.handle, { clientX: 500, clientY: 304, pointerId: 4, pointerType: 'pen' })
    expect(localStorage.getItem('mc-sidebar-width')).toBe('2884')
    act(() => { setWin(3440); r.rerenderAtWidth(3440) })
    expect(r.panel.style.width).toBe('2884px')
  })

  it('a window narrowed to a still-wide width keeps the chat pane its minimum', () => {
    setWin(5120)
    const r = dragFarRight()
    expect(parseInt(r.panel.style.width, 10)).toBe(5120 - railW() - CHAT_PANE_MIN_W)
    act(() => { setWin(3440); r.rerenderAtWidth(3440) })
    // Held to the 3440px ceiling, not to the whole room beside the rail.
    expect(r.panel.style.width).toBe(`${3440 - railW() - CHAT_PANE_MIN_W}px`)
    expect(localStorage.getItem('mc-sidebar-width')).toBe(String(5120 - railW() - CHAT_PANE_MIN_W))
  })

  it('a wide width loaded on a narrow window comes back when the window widens', () => {
    setWin(1440)
    localStorage.setItem('mc-sidebar-width', '2400')
    const r = renderSidebar({ board: true })
    expect(r.panel.style.width).toBe(`${1440 - railW() - CHAT_PANE_MIN_W}px`)
    act(() => { setWin(3440); r.rerenderAtWidth(3440) })
    expect(r.panel.style.width).toBe('2400px')
    expect(localStorage.getItem('mc-sidebar-width')).toBe('2400')
  })

  // The host seats the drawer from this report, so on a narrow window it must
  // carry the painted width (beside a minimum chat pane), not the saved one,
  // and follow the window back out as it widens.
  it('tells the host the painted width on a narrow window and the saved one once it widens', () => {
    setWin(1200)
    localStorage.setItem('mc-sidebar-width', String(SIDEBAR_MAX))
    const r = renderSidebar({ board: true })
    expect(r.onWidthChange).toHaveBeenLastCalledWith(1200 - railW() - CHAT_PANE_MIN_W)
    act(() => { r.rerenderAtWidth(3440) })
    expect(r.onWidthChange).toHaveBeenLastCalledWith(SIDEBAR_MAX)
  })

  it('at 1440px the ceiling is the room beside the rail and a minimum chat pane', () => {
    setWin(1440)
    const { panel, getByRole } = dragFarRight()
    const room = 1440 - railW() - CHAT_PANE_MIN_W
    expect(panel.style.width).toBe(`${room}px`)
    // The handle's range ends where the paint does.
    expect(getByRole('separator', { name: 'Resize sidebar' }).getAttribute('aria-valuemax')).toBe(String(room))
  })

  // One rule on both sides of SIDEBAR_MAX and in every view: on a window too
  // narrow for either, a saved 1400 and a saved 1401 paint at the same width
  // and the chat pane keeps its minimum, in list view and in board view.
  it.each([false, true])('at 1200px a saved 1400 and a saved 1401 paint the same (board %s)', (board) => {
    setWin(1200)
    const room = 1200 - railW() - CHAT_PANE_MIN_W
    localStorage.setItem('mc-sidebar-width', String(SIDEBAR_MAX))
    const at = renderSidebar({ board })
    expect(at.panel.style.width).toBe(`${room}px`)
    at.unmount()
    localStorage.setItem('mc-sidebar-width', String(SIDEBAR_MAX + 1))
    const past = renderSidebar({ board })
    expect(past.panel.style.width).toBe(`${room}px`)
    expect(localStorage.getItem('mc-sidebar-width')).toBe(String(SIDEBAR_MAX + 1))
    // The drag ceiling and the handle's range end where the paint does, in
    // either view: the smaller of SIDEBAR_MAX and the room.
    const ceiling = Math.min(SIDEBAR_MAX, room)
    expect(past.getByRole('separator', { name: 'Resize sidebar' }).getAttribute('aria-valuemax')).toBe(String(ceiling))
    fireEvent.pointerDown(past.handle, { clientX: room, pointerId: 1 })
    fireEvent.pointerMove(past.handle, { clientX: room + 100000, pointerId: 1 })
    fireEvent.pointerUp(past.handle, { clientX: room + 100000, pointerId: 1 })
    expect(past.panel.style.width).toBe(`${ceiling}px`)
  })

  // A window wider than SIDEBAR_MAX plus the rail and chat minimum, but
  // narrower than the one a saved width came from: the drag ceiling and the
  // painted width are the same value, so pushing right moves nothing.
  const atCeiling = () => {
    setWin(1920)
    // A collapsed rail, published the way App publishes its track.
    setRailWidth(74)
    localStorage.setItem('mc-sidebar-width', '4726')
    const r = renderSidebar({ board: true })
    const room = 1920 - railW() - CHAT_PANE_MIN_W
    expect(room).toBeGreaterThan(SIDEBAR_MAX)
    expect(r.panel.style.width).toBe(`${room}px`)
    return { ...r, room }
  }

  it('an arrow-key nudge the ceiling swallows keeps a wider saved width', () => {
    const r = atCeiling()
    fireEvent.keyDown(r.getByRole('separator', { name: 'Resize sidebar' }), { key: 'ArrowRight' })
    expect(r.panel.style.width).toBe(`${r.room}px`)
    expect(localStorage.getItem('mc-sidebar-width')).toBe('4726')
    act(() => { setWin(5120); r.rerenderAtWidth(5120) })
    expect(r.panel.style.width).toBe('4726px')
  })

  it('a drag pushed against the ceiling keeps a wider saved width', () => {
    const r = atCeiling()
    fireEvent.pointerDown(r.handle, { clientX: 500, pointerId: 5 })
    fireEvent.pointerMove(r.handle, { clientX: 900, pointerId: 5 })
    fireEvent.pointerUp(r.handle, { clientX: 900, pointerId: 5 })
    expect(r.panel.style.width).toBe(`${r.room}px`)
    expect(localStorage.getItem('mc-sidebar-width')).toBe('4726')
    act(() => { setWin(5120); r.rerenderAtWidth(5120) })
    expect(r.panel.style.width).toBe('4726px')
  })

  it('a nudge that does move still saves the new width', () => {
    const r = atCeiling()
    fireEvent.keyDown(r.getByRole('separator', { name: 'Resize sidebar' }), { key: 'ArrowLeft' })
    const w = parseInt(r.panel.style.width, 10)
    expect(w).toBeLessThan(r.room)
    expect(localStorage.getItem('mc-sidebar-width')).toBe(String(w))
  })

  it('a host that stretches the sidebar to its own width reserves no chat pane in the paint', () => {
    // The mobile drawer and the sessions embed force the root to full width,
    // so the header steps must follow that width, not one beside a chat pane.
    setWin(800)
    localStorage.setItem('mc-sidebar-width', '1500')
    const beside = renderSidebar({ board: true })
    expect(beside.panel.style.width).toBe(`${Math.max(SIDEBAR_MIN, 800 - railW() - CHAT_PANE_MIN_W)}px`)
    beside.unmount()
    const filled = renderSidebar({ fillsHost: true, board: true })
    expect(filled.panel.style.width).toBe(`${800 - railW()}px`)
    expect(filled.queryByText('New')).not.toBeNull()
    expect(localStorage.getItem('mc-sidebar-width')).toBe('1500')
  })

  it('a wide saved width painted narrow gets the narrow header, not the wide one', () => {
    // 700 - rail - chat minimum is under SIDEBAR_MIN, so 2884 paints at SIDEBAR_MIN.
    // The header's responsive collapse must follow that painted width, not the
    // stored 2884, or the full "Sessions" label and "New" text overflow it.
    setWin(700)
    localStorage.setItem('mc-sidebar-width', '2884')
    const { panel, queryByText } = renderSidebar({ board: true })
    expect(panel.style.width).toBe(`${SIDEBAR_MIN}px`)
    expect(queryByText('Sessions')).toBeNull()
    expect(localStorage.getItem('mc-sidebar-width')).toBe('2884')
  })

})

// The list views gain nothing from a sidebar wider than SIDEBAR_MAX, so there
// the drag and nudge stop at SIDEBAR_MAX on any window. A wider width saved in
// board view paints at SIDEBAR_MAX in list view and is kept for the board.
describe('chat sidebar — list view ceiling', () => {
  const realW = window.innerWidth
  const setWin = (w: number) => Object.defineProperty(window, 'innerWidth', { configurable: true, value: w })
  beforeEach(() => __resetRailWidth())
  afterEach(() => setWin(realW))

  it('clamps a drag at SIDEBAR_MAX on a wide window', () => {
    setWin(3440)
    const r = renderSidebar()
    fireEvent.pointerDown(r.handle, { clientX: DEFAULT_W, pointerId: 1 })
    fireEvent.pointerMove(r.handle, { clientX: DEFAULT_W + 100000, pointerId: 1 })
    fireEvent.pointerUp(r.handle, { clientX: DEFAULT_W + 100000, pointerId: 1 })
    expect(r.panel.style.width).toBe(`${SIDEBAR_MAX}px`)
    expect(localStorage.getItem('mc-sidebar-width')).toBe(String(SIDEBAR_MAX))
    expect(r.getByRole('separator', { name: 'Resize sidebar' }).getAttribute('aria-valuemax')).toBe(String(SIDEBAR_MAX))
    expect(r.onWidthChange).toHaveBeenLastCalledWith(SIDEBAR_MAX)
  })

  it('a wider saved width paints at SIDEBAR_MAX and a drag or nudge against the ceiling keeps it', () => {
    setWin(3440)
    localStorage.setItem('mc-sidebar-width', '2884')
    const r = renderSidebar()
    expect(r.panel.style.width).toBe(`${SIDEBAR_MAX}px`)
    // The host seats the sidebar at the painted width, not the saved one.
    expect(r.onWidthChange).toHaveBeenLastCalledWith(SIDEBAR_MAX)
    fireEvent.keyDown(r.getByRole('separator', { name: 'Resize sidebar' }), { key: 'ArrowRight' })
    fireEvent.pointerDown(r.handle, { clientX: 500, pointerId: 2 })
    fireEvent.pointerMove(r.handle, { clientX: 900, pointerId: 2 })
    fireEvent.pointerUp(r.handle, { clientX: 900, pointerId: 2 })
    expect(r.panel.style.width).toBe(`${SIDEBAR_MAX}px`)
    expect(localStorage.getItem('mc-sidebar-width')).toBe('2884')
  })

  it('switching list to board to list keeps a saved 2884', async () => {
    setWin(3440)
    localStorage.setItem('mc-sidebar-width', '2884')
    const r = renderSidebar()
    expect(r.panel.style.width).toBe(`${SIDEBAR_MAX}px`)
    await r.showBoard(true)
    expect(r.panel.style.width).toBe('2884px')
    expect(r.onWidthChange).toHaveBeenLastCalledWith(2884)
    await r.showBoard(false)
    expect(r.panel.style.width).toBe(`${SIDEBAR_MAX}px`)
    expect(r.onWidthChange).toHaveBeenLastCalledWith(SIDEBAR_MAX)
    expect(localStorage.getItem('mc-sidebar-width')).toBe('2884')
    await r.showBoard(true)
    expect(r.panel.style.width).toBe('2884px')
  })

  it('board view still accepts a 2884 drag on the same window', () => {
    setWin(3440)
    localStorage.setItem('mc-sidebar-width', String(SIDEBAR_MAX))
    const r = renderSidebar({ board: true })
    fireEvent.pointerDown(r.handle, { clientX: 0, pointerId: 3 })
    fireEvent.pointerMove(r.handle, { clientX: 2884 - SIDEBAR_MAX, pointerId: 3 })
    fireEvent.pointerUp(r.handle, { clientX: 2884 - SIDEBAR_MAX, pointerId: 3 })
    expect(r.panel.style.width).toBe('2884px')
    expect(localStorage.getItem('mc-sidebar-width')).toBe('2884')
  })
})
