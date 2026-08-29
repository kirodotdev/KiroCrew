/**
 * SIDEBAR_ANIM_CAP gate: above the cap, session rows must render WITHOUT
 * framer layout projection (no layoutId, layout=false) so a 200+ session
 * sidebar stops paying a group-wide getBoundingClientRect pass per commit.
 * At or below the cap the animation contract is unchanged.
 *
 * The shared framer mock maps `layoutId` -> `data-layout-id`, which is what
 * these assertions read.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render } from '@testing-library/react'
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
    React.forwardRef((props: Record<string, unknown> & { children?: unknown }, ref: unknown) => {
      const clean: Record<string, unknown> = {}
      for (const k of Object.keys(props)) {
        if (k === 'children') continue
        if (k === 'layoutId') { clean['data-layout-id'] = props[k]; continue }
        if (k === 'layout') { clean['data-layout'] = String(props[k]); continue }
        if (FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref: ref as never }, props.children as never)
    })
  const motion = new Proxy({}, { get: (_t, tag: string) => make(tag) })
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

function mkSlots(n: number) {
  return Array.from({ length: n }, (_, i) => ({
    key: `slot-${i}`, title: `session ${i}`, running: false, messages: 1,
  }))
}

type FixtureSlot = ReturnType<typeof mkSlots>[number]

function renderSidebar(slots: FixtureSlot[]) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as never,
    chat: { activeSlot: null, slotStatusDetail: {} } as never,
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], [])
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots as never} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

beforeEach(() => localStorage.clear())
afterEach(() => vi.clearAllMocks())

const rowWrappers = () => Array.from(document.querySelectorAll<HTMLElement>('[data-slot-key]'))

describe('SIDEBAR_ANIM_CAP layout-animation gate', () => {
  it('keeps layout projection at or below the cap', () => {
    renderSidebar(mkSlots(5))
    const rows = rowWrappers()
    expect(rows.length).toBe(5)
    for (const el of rows) {
      expect(el.getAttribute('data-layout-id')).toMatch(/^slot-/)
      expect(el.getAttribute('data-layout')).toBe('position')
    }
  })

  it('drops layout projection above the cap', () => {
    renderSidebar(mkSlots(201))
    const rows = rowWrappers()
    expect(rows.length).toBe(201)
    for (const el of rows) {
      expect(el.getAttribute('data-layout-id')).toBeNull()
      expect(el.getAttribute('data-layout')).toBe('false')
    }
  })
})

describe('lastActivityEpoch parse cache', () => {
  it('returns stable values for repeated ISO strings and still ranks garbage last', async () => {
    const { lastActivityEpoch } = await import('../pages/chat/sessionOrder')
    const a = { key: 'a', last_ts: '2026-08-29T00:00:00Z' }
    const first = lastActivityEpoch(a)
    expect(first).toBeGreaterThan(0)
    // Cache hit must return the identical value.
    expect(lastActivityEpoch(a)).toBe(first)
    // NaN handling survives the cache: unparseable -> 0, consistently.
    const bad = { key: 'b', last_ts: 'not-a-date' }
    expect(lastActivityEpoch(bad)).toBe(0)
    expect(lastActivityEpoch(bad)).toBe(0)
    // `modified` still bypasses the ladder entirely.
    expect(lastActivityEpoch({ key: 'c', modified: 123 })).toBe(123)
  })
})
