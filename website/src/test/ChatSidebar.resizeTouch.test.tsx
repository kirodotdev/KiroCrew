/**
 * Sidebar column resize via Pointer Events (mouse + touch + pen).
 *
 * The handle used to be `onMouseDown` + window mousemove/mouseup, which never
 * fired for a touch drag — so a tablet at desktop width (where the sidebar is a
 * side-by-side panel with a visible handle) could not resize it. It now uses
 * usePointerDrag, so the same gesture works for any pointer type.
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
import { render, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

// Render framer-motion elements as plain DOM (jsdom can't run projection).
vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'layoutScroll', 'initial', 'animate', 'exit',
    'transition', 'variants', 'whileHover', 'whileTap', 'whileInView',
    'drag', 'dragConstraints', 'dragElastic', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef((props: any, ref: any) => {
      const clean: any = {}
      for (const k of Object.keys(props)) {
        if (k === 'children') continue
        if (k === 'layoutId') { clean['data-layout-id'] = props[k]; continue }
        if (FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children)
    })
  const motion = new Proxy({}, { get: (_t, tag: string) => make(tag) })
  return {
    motion,
    AnimatePresence: ({ children }: any) => React.createElement(React.Fragment, null, children),
    LayoutGroup: ({ children }: any) => React.createElement(React.Fragment, null, children),
  }
})

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false }),
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

const SLOTS = [
  { key: 'k1', title: 'One', messages: 1, running: false, modified: 1000 },
]

function renderSidebar() {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots: SLOTS, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as any,
    chat: { activeSlot: null, slotStatusDetail: {}, subagents: {}, slotActivity: {} } as any,
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], [])
  const onWidthChange = vi.fn()
  const onDragChange = vi.fn()
  const utils = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={SLOTS} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
              onWidthChange={onWidthChange} onDragChange={onDragChange}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  const handle = utils.container.querySelector('.sidebar-resize-handle') as HTMLElement
  const panel = utils.container.querySelector('.sidebar-inner') as HTMLElement
  return { ...utils, handle, panel, onWidthChange, onDragChange }
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
    const { handle, panel } = renderSidebar()
    fireEvent.pointerDown(handle, { clientX: DEFAULT_W, pointerId: 1 })
    fireEvent.pointerMove(handle, { clientX: DEFAULT_W + 100000, pointerId: 1 })
    fireEvent.pointerUp(handle, { clientX: DEFAULT_W + 100000, pointerId: 1 })
    expect(panel.style.width).toBe(`${SIDEBAR_MAX}px`)
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
