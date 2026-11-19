/**
 * The real ChatSidebar beside a reported fold: the fold owns the list's width
 * only on the dashboard's own chat page, so an embedded list (the App Kit
 * ChatPanel) keeps its width and resize handle. Pins ChatSidebar's own
 * pass-through of `embedded` to useSidebarResize; the page-level plumbing above
 * it is pinned in ChatPage.foldResizeFrame.test.tsx.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { cleanup, render } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

const segmentsMock = vi.hoisted(() => ({ value: null as null | { x: number; width: number }[] }))
vi.mock('../hooks/useViewportSegments', async (orig) => ({
  ...(await orig<typeof import('../hooks/useViewportSegments')>()),
  useViewportSegments: () => segmentsMock.value,
}))

// Render framer-motion elements as plain DOM (jsdom can't run projection).
vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'layoutScroll', 'initial', 'animate', 'exit',
    'transition', 'variants', 'whileHover', 'whileTap', 'whileInView',
    'drag', 'dragConstraints', 'dragElastic', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef((props: Record<string, unknown> & { children?: unknown }, ref: unknown) => {
      // Framer copies `layout`/`layoutId` into the projection node at mount and
      // never re-reads them, so what an element mounted with is what it keeps.
      const atMount = React.useRef({ layout: props.layout, layoutId: props.layoutId, layoutScroll: props.layoutScroll, initial: props.initial })
      const clean: Record<string, unknown> = {
        'data-mount-layout': String(atMount.current.layout),
        'data-mount-layout-id': atMount.current.layoutId,
        'data-mount-layout-scroll': String(atMount.current.layoutScroll),
        'data-mount-initial': JSON.stringify(atMount.current.initial),
      }
      for (const k of Object.keys(props)) {
        if (k === 'children') continue
        if (k === 'layoutId') { clean['data-layout-id'] = props[k]; continue }
        if (k === 'layout') { clean['data-layout'] = String(props[k]); continue }
        if (k === 'transition') { clean['data-layout-transition'] = JSON.stringify((props[k] as { layout?: unknown } | undefined)?.layout); continue }
        if (FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref: ref as never }, props.children as never)
    })
  // One component per tag, as the real `motion.div` is: a fresh one per access
  // would remount every motion element whenever its parent re-renders.
  const components = new Map<string, ReturnType<typeof make>>()
  const motion = new Proxy({}, {
    get: (_t, tag: string) => {
      if (!components.has(tag)) components.set(tag, make(tag))
      return components.get(tag)
    },
  })
  return {
    motion,
    AnimatePresence: ({ children }: { children?: unknown }) => React.createElement(React.Fragment, null, children as never),
    LayoutGroup: ({ children }: { children?: unknown }) => React.createElement(React.Fragment, null, children as never),
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

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})

import ChatSidebar from '../pages/ChatSidebar'

function renderSidebar(embedded: boolean) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots: [], approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as never,
    chat: { activeSlot: null, slotStatusDetail: {} } as never,
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity, refetchOnMount: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], [])
  const onWidthChange = vi.fn()
  render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={[]} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
              embedded={embedded} onWidthChange={onWidthChange}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return { onWidthChange }
}

beforeEach(() => {
  localStorage.clear()
  // A window wide enough for the 260px default beside the rail and a chat pane.
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1024 })
  segmentsMock.value = [{ x: 0, width: 475.5 }, { x: 475.5, width: 475.5 }]
})
afterEach(() => { cleanup(); segmentsMock.value = null; vi.clearAllMocks() })

describe('the real ChatSidebar beside a reported fold', () => {
  it('embedded, keeps its stored width (the 260px default) and shows the resize handle', () => {
    const { onWidthChange } = renderSidebar(true)
    expect(document.querySelector('.sidebar-resize-handle')).not.toBeNull()
    expect(onWidthChange).toHaveBeenLastCalledWith(260)
  })

  it("on the dashboard's own chat page, ends at the fold with no handle", () => {
    const { onWidthChange } = renderSidebar(false)
    expect(document.querySelector('.sidebar-resize-handle')).toBeNull()
    // 475.5px first segment less the 236px expanded rail (useRailWidth default).
    expect(onWidthChange).toHaveBeenLastCalledWith(475.5 - 236)
  })
})
