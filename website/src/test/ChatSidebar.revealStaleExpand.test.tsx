/**
 * Reveal-in-sidebar must pre-expand the stale-session ("Dormant sessions")
 * section hiding its target. The stale collapse is a per-container disclosure,
 * not a registered filter dimension, so the reveal effect's filter-clearing
 * walk cannot see it: without the pre-expand, revealing a dormant NON-active
 * session scrolls to a row that never rendered (#6479).
 *
 * Scope pins: expansion opens ONLY the target's container (root or its
 * folder), and an exempt target (the active session) opens nothing.
 * The collapse itself is pinned in ChatSidebar.staleCollapse.test.tsx; the
 * filter-dimension walk in ChatSidebar.revealFilterDimensions.test.tsx.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { requestSlotReveal } from '../store/chatSlice'
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
        if (FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children as never)
    })
  const motion = new Proxy({}, { get: (_t, tag: string) => make(tag) })
  return {
    motion,
    AnimatePresence: ({ children }: { children?: unknown }) => React.createElement(React.Fragment, null, children as never),
    LayoutGroup: ({ children }: { children?: unknown }) => React.createElement(React.Fragment, null, children as never),
  }
})

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))

// The folders query REFETCHES on mount (seeded cache data is stale), so the
// mock must serve the test's folders or the refetch wipes them — which reads
// as every filed slot "moving" to root and move-exempts it from the collapse.
const chatFoldersMock = vi.hoisted(() => vi.fn().mockResolvedValue([]))

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, prop: string) => {
      if (prop === 'chatFolders') return chatFoldersMock
      return vi.fn().mockResolvedValue([])
    },
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

const hoursAgo = (h: number) => new Date(Date.now() - h * 3600_000).toISOString()

type FixtureSlot = Record<string, unknown>

const slot = (key: string, title: string, ageHours: number, extra: Record<string, unknown> = {}) => ({
  key, title, running: false, messages: 2,
  created: hoursAgo(ageHours + 1), last_turn_ts: hoursAgo(ageHours), ...extra,
})

function renderSidebar(slots: FixtureSlot[], { folders = [] as FixtureSlot[], activeSlot = null as string | null } = {}) {
  chatFoldersMock.mockResolvedValue(folders)
  // Spread the real slice defaults: RTK REPLACES a slice with preloadedState
  // rather than merging, so a partial drops keys the reducers assume exist
  // (revealRequest/revealNonce here).
  const defaults = createTestStore().getState()
  const store = createTestStore({
    dashboard: {
      ...defaults.dashboard,
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as never,
    chat: {
      ...defaults.chat,
      activeSlot, slotStatusDetail: {},
      revealRequest: null, revealNonce: 0,
    } as never,
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], folders)
  const view = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots as never} activeSlot={activeSlot} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return { ...view, store, qc }
}

/** jsdom has no scrollIntoView; the reveal effect calls it on the found row. */
function withScrollStub(fn: () => Promise<void>) {
  const original = Element.prototype.scrollIntoView
  Element.prototype.scrollIntoView = vi.fn()
  return fn().finally(() => { Element.prototype.scrollIntoView = original })
}

beforeEach(() => localStorage.clear())
afterEach(() => vi.clearAllMocks())

describe('reveal-in-sidebar pre-expands the stale-session section', () => {
  it('renders a dormant non-active root session when revealed', async () => {
    await withScrollStub(async () => {
      const utils = renderSidebar([
        slot('fresh', 'fresh session', 2),
        slot('dormant', 'dormant session', 10 * 24),
      ], { activeSlot: 'fresh' })
      // Precondition: the target really is collapsed before the reveal.
      await waitFor(() => expect(utils.getByTestId('stale-expander-root')).toBeInTheDocument())
      expect(utils.queryByText('dormant session')).toBeNull()

      utils.store.dispatch(requestSlotReveal('dormant'))

      await waitFor(() => expect(utils.queryByText('dormant session')).not.toBeNull())
    })
  })

  it('opens only the target folder\'s section, not every container\'s', async () => {
    await withScrollStub(async () => {
      const folders = [{ id: 'f1', name: 'Work', order: 0, collapsed: false }]
      const utils = renderSidebar([
        slot('fresh', 'fresh session', 2),
        slot('in-dormant', 'foldered dormant', 10 * 24, { folder_id: 'f1' }),
        slot('root-dormant', 'root dormant', 10 * 24),
      ], { folders, activeSlot: 'fresh' })
      await waitFor(() => expect(utils.getByTestId('stale-expander-f1')).toBeInTheDocument())
      expect(utils.queryByText('foldered dormant')).toBeNull()

      utils.store.dispatch(requestSlotReveal('in-dormant'))

      await waitFor(() => expect(utils.queryByText('foldered dormant')).not.toBeNull())
      // Per-container scope: the root's dormant section stays collapsed.
      expect(utils.queryByText('root dormant')).toBeNull()
    })
  })

  it('an exempt target opens no dormant section', async () => {
    await withScrollStub(async () => {
      const utils = renderSidebar([
        slot('fresh', 'fresh session', 2),
        slot('dormant', 'dormant session', 10 * 24),
      ], { activeSlot: 'fresh' })
      await waitFor(() => expect(utils.getByTestId('stale-expander-root')).toBeInTheDocument())

      // The active session is stale-exempt: it renders outside the dormant
      // section, so revealing it must not pop the section open.
      utils.store.dispatch(requestSlotReveal('fresh'))

      await waitFor(() => expect(Element.prototype.scrollIntoView).toHaveBeenCalled())
      expect(utils.queryByText('dormant session')).toBeNull()
    })
  })
})
