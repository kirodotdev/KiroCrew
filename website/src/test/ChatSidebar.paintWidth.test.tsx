/**
 * #16094: the sidebar root must paint the width its HOST sized the container to,
 * not the raw stored width.
 *
 * ChatPage clamps the stored width to the viewport once (`effectiveSidebarWidth`,
 * the width it also gives the OverlayDrawer) and hands it down as `paintWidth`.
 * The root paints THAT, so the inner edge and the drawer edge coincide; the
 * resize handle (absolutely positioned on the root's right border) then stays
 * inside the window and grabbable. Before the fix the root painted the raw
 * stored width and overflowed the clamped drawer.
 *
 * Covered here (jsdom, synchronously renderable):
 *  - a supplied `paintWidth` BELOW the stored width is what paints -> inner==outer.
 *  - no `paintWidth` (standalone / embed / mobile) falls back to the stored
 *    width -> the prior behavior the ~100 standalone ChatSidebar suites rely on.
 *
 * The viewport-clamp arithmetic itself is pinned by clampSidebarWidth.test.ts;
 * this file only pins that the root consumes the host's clamped width.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render } from '@testing-library/react'
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

// Desktop viewport: not mobile, so the sidebar renders as a side-by-side panel.
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})

import ChatSidebar from '../pages/ChatSidebar'

const SLOTS = [
  { key: 'k1', title: 'One', messages: 1, running: false, modified: 1000 },
]

function renderSidebar(paintWidth?: number) {
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
  const utils = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={SLOTS} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
              onWidthChange={vi.fn()} onDragChange={vi.fn()}
              paintWidth={paintWidth}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  const panel = utils.container.querySelector('.sidebar-inner') as HTMLElement
  return { ...utils, panel }
}

// A stored width wider than any clamp this test applies, so the stored value and
// the clamped value are unambiguously different.
const STORED_WIDE = 1400

beforeEach(() => {
  localStorage.clear()
  localStorage.setItem('mc-sidebar-width', String(STORED_WIDE))
})
afterEach(() => vi.clearAllMocks())

describe('chat sidebar — paintWidth (host-clamped width)', () => {
  it('paints the supplied paintWidth, not the (wider) stored width', () => {
    // The host clamped 1400 down to 884 for a narrow window; the root MUST paint
    // 884 so its right edge (and the resize handle on it) sits where the drawer
    // edge does, not 516px past it.
    const { panel } = renderSidebar(884)
    expect(panel.style.width).toBe('884px')
  })

  it('falls back to the raw stored width when no paintWidth is supplied', () => {
    // Standalone / embed / mobile render ChatSidebar without paintWidth; the
    // root keeps painting the stored width exactly as before the fix.
    const { panel } = renderSidebar(undefined)
    expect(panel.style.width).toBe(`${STORED_WIDE}px`)
  })
})
