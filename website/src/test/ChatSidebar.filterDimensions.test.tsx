/**
 * The sidebar's filter dimensions are declared ONCE, in the row model's
 * `buildSidebarRows`, and all three consumers derive from it: `filteredSlots`
 * (which rows render), `listNarrowed` (is anything filtering),
 * `revealBlockingFilters` (does THIS row fail a filter). Before the
 * consolidation each site enumerated the dimensions by hand, so a new filter
 * added to one and missed in the others failed silently — reveal-in-sidebar
 * looked broken, or an empty-state branch showed a leftover container.
 *
 * That the three consumers agree per dimension is pinned at the model's
 * interface (`pages/chat-sidebar/rows.test.ts`, one row per dimension). These
 * cases pin `listNarrowed` through the rendered sidebar — the one consumer the
 * reveal tests do not touch — including the deliberate resolved-vs-raw tag
 * distinction the declaration documents.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/react'
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
    get: (_t, prop: string) => {
      if (prop === 'chatTags') return vi.fn().mockResolvedValue([
        { id: 't1', name: 'Alpha', color: '#ff0000', order: 0 },
        { id: 't2', name: 'Beta', color: '#00ff00', order: 1 },
      ])
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
import type { RootState } from '../store'
import type { ChatSlot } from '../types'

const RUNNING_ONLY_LS_KEY = 'mc-session-running-only'
const TAG_FILTER_LS_KEY = 'mc-session-tag-filter'

/** Neither slot is running, so the Running status chip excludes both. */
const SLOTS: ChatSlot[] = [
  { key: 'k-alpha', title: 'alpha session', running: false, messages: 2, tags: ['t1'] },
  { key: 'k-beta', title: 'beta session', running: false, messages: 2, tags: ['t2'] },
] as unknown as ChatSlot[]

function renderSidebar(slots: ChatSlot[] = SLOTS) {
  // Spread the real slice defaults: RTK REPLACES a slice with preloadedState
  // rather than merging, so a partial drops keys the reducers assume exist.
  const defaults = createTestStore().getState()
  const store = createTestStore({
    dashboard: {
      ...defaults.dashboard,
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: {
      ...defaults.chat,
      activeSlot: null, slotStatusDetail: {},
      revealRequest: null, revealNonce: 0,
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], [])
  const view = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return { ...view, store }
}

beforeEach(() => localStorage.clear())
afterEach(() => vi.clearAllMocks())

describe('listNarrowed derives from the filter dimensions', () => {
  it('a status chip that excludes every row narrows the list to its empty state', async () => {
    localStorage.setItem(RUNNING_ONLY_LS_KEY, '1')
    const utils = renderSidebar()
    await waitFor(() => expect(utils.queryByText('No sessions match')).not.toBeNull())
    expect(utils.queryByText('alpha session')).toBeNull()
    expect(utils.queryByText('beta session')).toBeNull()
  })

  it('a search that matches nothing narrows the list to its empty state', async () => {
    const utils = renderSidebar()
    await waitFor(() => expect(utils.queryByText('alpha session')).not.toBeNull())
    fireEvent.change(utils.getByPlaceholderText('Search sessions…'), { target: { value: 'zzz-no-such' } })
    await waitFor(() => expect(utils.queryByText('No sessions match')).not.toBeNull())
  })

  it('no active dimension means no narrowing', async () => {
    const utils = renderSidebar()
    await waitFor(() => expect(utils.queryByText('alpha session')).not.toBeNull())
    expect(utils.queryByText('beta session')).not.toBeNull()
    expect(utils.queryByText('No sessions match')).toBeNull()
  })

  it('tags narrow by the RESOLVED vocabulary: a stored id it cannot resolve filters nothing', async () => {
    // The declaration's documented raw-vs-resolved distinction: `hides` reads
    // the raw stored ids (so a reveal mid-load still clears them — pinned by
    // the reveal tests), but `narrows`/`filtersRow` use only ids the loaded
    // vocabulary resolves. A stale id from a deleted tag must not narrow.
    localStorage.setItem(TAG_FILTER_LS_KEY, JSON.stringify(['ghost-deleted-tag']))
    const utils = renderSidebar()
    await waitFor(() => expect(utils.queryByText('alpha session')).not.toBeNull())
    expect(utils.queryByText('beta session')).not.toBeNull()
    expect(utils.queryByText('No sessions match')).toBeNull()
  })
})

/** `k-alpha` is pinned; neither row is running, and `k-gamma` shares alpha's
 *  tag without the pin, so it is the control that shows the filter still acts
 *  on an unpinned row. */
const PINNED_SLOTS: ChatSlot[] = [
  { key: 'k-alpha', title: 'alpha session', running: false, messages: 2, tags: ['t1'], pinned: true },
  { key: 'k-beta', title: 'beta session', running: false, messages: 2, tags: ['t2'] },
  { key: 'k-gamma', title: 'gamma session', running: false, messages: 2, tags: ['t1'] },
] as unknown as ChatSlot[]

describe('a pinned session is exempt from the property filters', () => {
  it('a status chip that excludes the pinned row by property still shows it, and hides the unpinned rows', async () => {
    localStorage.setItem(RUNNING_ONLY_LS_KEY, '1')
    const utils = renderSidebar(PINNED_SLOTS)
    await waitFor(() => expect(utils.queryByText('alpha session')).not.toBeNull())
    expect(utils.queryByText('beta session')).toBeNull()
    expect(utils.queryByText('gamma session')).toBeNull()
    // The list IS narrowed (the chip still acts), so this is not the empty
    // state either: the pinned band stands alone above nothing.
    expect(utils.queryByText('No sessions match')).toBeNull()
  })

  it('a tag filter the pinned row does not carry still shows it, and hides the unpinned row without the tag', async () => {
    localStorage.setItem(TAG_FILTER_LS_KEY, JSON.stringify(['t2']))
    const utils = renderSidebar(PINNED_SLOTS)
    await waitFor(() => expect(utils.queryByText('beta session')).not.toBeNull())
    expect(utils.queryByText('alpha session')).not.toBeNull()
    expect(utils.queryByText('gamma session')).toBeNull()
  })

  it('the search is not exempt: a pinned row the query does not name is dropped', async () => {
    const utils = renderSidebar(PINNED_SLOTS)
    await waitFor(() => expect(utils.queryByText('alpha session')).not.toBeNull())
    fireEvent.change(utils.getByPlaceholderText('Search sessions…'), { target: { value: 'beta' } })
    await waitFor(() => expect(utils.queryByText('alpha session')).toBeNull())
    expect(utils.queryByText('beta session')).not.toBeNull()
  })
})
