/**
 * The scroll peek never changes the session rows' width.
 *
 * `website/AUTOSDE.yaml` rule `session-row-fixed-height` forbids widening a
 * session row (the sidebar width is user-controlled). The peek shows clipped
 * titles in full in a separate floating layer instead; this pins that the
 * sidebar card and its rows keep the stored width while that layer is up.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, act, waitFor } from '@testing-library/react'
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
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false, fullTitlesOnScroll: true }),
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

import ChatSidebar from '../pages/ChatSidebar'

const LONG = 'Investigate flaky Windows shard in backend tests after the sandbox refactor'
// Recent activity: rows older than the stale-collapse threshold fold away.
const SLOTS = Array.from({ length: 12 }, (_, i) => ({
  key: `k${i}`, title: `${LONG} ${i}`, messages: 1, running: false, mode: '', created: '', pinned: false,
  last_ts: new Date(Date.now() - (i + 1) * 60_000).toISOString(), folder_id: null,
}))

function renderSidebar() {
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
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={SLOTS} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
              scrollPeek
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

const rect = (top: number, bottom: number, left: number, right: number) =>
  ({ top, bottom, left, right, width: right - left, height: bottom - top, x: left, y: top, toJSON: () => ({}) })

beforeEach(() => { localStorage.clear() })
afterEach(() => { vi.useRealTimers(); vi.clearAllMocks() })

describe('chat sidebar scroll peek', () => {
  it('shows clipped titles in the floating layer while every row keeps the stored width', async () => {
    const { container } = renderSidebar()
    await waitFor(() => expect(container.querySelectorAll('[data-session-title]').length).toBeGreaterThan(0))
    vi.useFakeTimers()
    const panel = container.querySelector('.sidebar-inner') as HTMLElement
    const lane = container.querySelector<HTMLElement>('[data-testid$="-view-lane"]')
    expect(lane).toBeTruthy()
    const rowWidthsBefore = Array.from(container.querySelectorAll<HTMLElement>('[data-session-row]'), r => r.style.width)
    // Layout the test DOM cannot compute: a lane that scrolls, titles the rows clip.
    Object.defineProperty(lane, 'scrollHeight', { configurable: true, value: 2000 })
    Object.defineProperty(lane, 'clientHeight', { configurable: true, value: 600 })
    lane!.getBoundingClientRect = () => rect(0, 600, 0, 260)
    container.querySelectorAll<HTMLElement>('[data-session-title]').forEach((t, i) => {
      Object.defineProperty(t, 'clientWidth', { configurable: true, value: 200 })
      Object.defineProperty(t, 'scrollWidth', { configurable: true, value: 480 })
      t.getBoundingClientRect = () => rect(20 + i * 48, 40 + i * 48, 20, 220)
    })

    act(() => {
      panel.dispatchEvent(new Event('pointerenter'))
      lane!.dispatchEvent(new WheelEvent('wheel', { deltaY: 60, bubbles: true }))
      vi.advanceTimersByTime(20)
    })

    const layer = document.querySelector<HTMLElement>('[data-testid="scroll-peek-layer"]')
    expect(layer).toBeTruthy()
    expect(layer!.querySelectorAll('[data-scroll-peek-title]').length).toBeGreaterThan(0)
    // The rule as written: the card and its rows are not widened.
    expect(panel.style.width).toBe('260px')
    expect(Array.from(container.querySelectorAll<HTMLElement>('[data-session-row]'), r => r.style.width)).toEqual(rowWidthsBefore)
  })
})
