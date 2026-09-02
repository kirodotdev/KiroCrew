/**
 * Stale-session collapse, component level: sessions idle past the threshold
 * collapse behind a per-container "Dormant sessions (N)" expander row — at the
 * ungrouped root AND inside each folder independently — while pinned, focused,
 * running and needs-input sessions are exempt. localStorage "0" turns the
 * feature off. The pure split predicate is pinned in staleCollapse.test.ts.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent, act } from '@testing-library/react'
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

const DAY = 24 * 60 * 60 * 1000
const hoursAgo = (h: number) => new Date(Date.now() - h * 3600_000).toISOString()

type FixtureSlot = Record<string, unknown>

function renderSidebar(slots: FixtureSlot[], { folders = [] as FixtureSlot[], activeSlot = null as string | null, unreadSlots = [] as string[], seedFolders = true } = {}) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots, updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as never,
    chat: { activeSlot, slotStatusDetail: {} } as never,
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  if (seedFolders) qc.setQueryData(['chat-folders'], folders)
  const view = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots as never} activeSlot={activeSlot} unreadSlots={unreadSlots}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return { ...view, store, qc }
}

const slot = (key: string, title: string, ageHours: number, extra: Record<string, unknown> = {}) => ({
  key, title, running: false, messages: 2,
  created: hoursAgo(ageHours + 1), last_turn_ts: hoursAgo(ageHours), ...extra,
})

beforeEach(() => localStorage.clear())
afterEach(() => vi.clearAllMocks())

describe('chat sidebar — stale-session collapse', () => {
  it('collapses root sessions older than the default 7 days behind an expander row', () => {
    const { getByTestId, queryByText, getByText } = renderSidebar([
      slot('fresh', 'fresh session', 2),
      slot('old-1', 'old session one', 10 * 24),
      slot('old-2', 'old session two', 12 * 24),
    ])
    expect(getByText('fresh session')).toBeInTheDocument()
    expect(queryByText('old session one')).toBeNull()
    expect(queryByText('old session two')).toBeNull()
    const expander = getByTestId('stale-expander-root')
    expect(expander).toHaveAttribute('aria-expanded', 'false')
    expect(expander).toHaveTextContent('2')
  })

  it('expands and re-collapses on click, flipping aria-expanded', () => {
    const { getByTestId, queryByText } = renderSidebar([
      slot('fresh', 'fresh session', 2),
      slot('old-1', 'old session one', 10 * 24),
    ])
    // Re-query after each click: the row re-renders and the node is replaced.
    fireEvent.click(getByTestId('stale-expander-root'))
    expect(getByTestId('stale-expander-root')).toHaveAttribute('aria-expanded', 'true')
    expect(queryByText('old session one')).not.toBeNull()
    fireEvent.click(getByTestId('stale-expander-root'))
    expect(getByTestId('stale-expander-root')).toHaveAttribute('aria-expanded', 'false')
    expect(queryByText('old session one')).toBeNull()
  })

  it('exempts pinned, focused, running, needs-input, pending-approval and unread sessions regardless of age', () => {
    const { getByText, queryByTestId } = renderSidebar([
      slot('pinned-old', 'pinned old', 10 * 24, { pinned: true }),
      slot('running-old', 'running old', 10 * 24, { running: true }),
      slot('needs-old', 'needs input old', 10 * 24, { needs_input: true }),
      slot('approval-old', 'approval old', 10 * 24, { pending_approval: true }),
      slot('unread-old', 'unread old', 10 * 24),
      slot('active-old', 'active old', 10 * 24),
    ], { activeSlot: 'active-old', unreadSlots: ['unread-old'] })
    expect(getByText('pinned old')).toBeInTheDocument()
    expect(getByText('running old')).toBeInTheDocument()
    expect(getByText('needs input old')).toBeInTheDocument()
    expect(getByText('approval old')).toBeInTheDocument()
    expect(getByText('unread old')).toBeInTheDocument()
    expect(getByText('active old')).toBeInTheDocument()
    // Every old row is exempt, so no expander renders at all.
    expect(queryByTestId('stale-expander-root')).toBeNull()
  })

  it('goes inert while the list is narrowed — a search match must never hide', () => {
    const { getByPlaceholderText, getByText, getByTestId, queryByTestId, queryByText } = renderSidebar([
      slot('fresh', 'fresh session', 2),
      slot('old-match', 'ancient treasure hunt', 10 * 24),
      slot('old-other', 'unrelated old work', 10 * 24),
    ])
    // Before the search: the old rows are collapsed.
    expect(getByTestId('stale-expander-root')).toBeInTheDocument()
    fireEvent.change(getByPlaceholderText(/search/i), { target: { value: 'treasure' } })
    // The matching old row renders; no expander swallows the result.
    expect(getByText('ancient treasure hunt')).toBeInTheDocument()
    expect(queryByTestId('stale-expander-root')).toBeNull()
    expect(queryByText('fresh session')).toBeNull()
  })

  it('goes inert under non-date sorts, where a trailing bucket would lie about position', () => {
    localStorage.setItem('mc-session-sort', 'name-asc')
    const { getByText, queryByTestId } = renderSidebar([
      slot('fresh', 'alpha fresh', 2),
      slot('old-1', 'beta old', 10 * 24),
    ])
    expect(getByText('beta old')).toBeInTheDocument()
    expect(queryByTestId('stale-expander-root')).toBeNull()
  })

  it('collapses per folder independently of the root', () => {
    const folders = [{ id: 'f1', name: 'Work', order: 0, collapsed: false }]
    const { getByTestId, queryByText, getByText } = renderSidebar([
      slot('in-fresh', 'foldered fresh', 1, { folder_id: 'f1' }),
      slot('in-old', 'foldered old', 10 * 24, { folder_id: 'f1' }),
      slot('root-old', 'root old', 10 * 24),
    ], { folders })
    expect(getByText('foldered fresh')).toBeInTheDocument()
    expect(queryByText('foldered old')).toBeNull()
    expect(getByTestId('stale-expander-f1')).toHaveTextContent('1')
    expect(getByTestId('stale-expander-root')).toHaveTextContent('1')
    // Expanding the folder's section must not expand the root's.
    fireEvent.click(getByTestId('stale-expander-f1'))
    expect(queryByText('foldered old')).not.toBeNull()
    expect(queryByText('root old')).toBeNull()
  })

  it('a persisted "0" turns the feature off and shows every session', () => {
    localStorage.setItem('mc-session-stale-collapse-ms', '0')
    const { getByText, queryByTestId } = renderSidebar([
      slot('fresh', 'fresh session', 2),
      slot('old-1', 'old session one', 30 * 24),
    ])
    expect(getByText('old session one')).toBeInTheDocument()
    expect(queryByTestId('stale-expander-root')).toBeNull()
  })

  it('honors a persisted custom threshold', () => {
    localStorage.setItem('mc-session-stale-collapse-ms', String(7 * DAY))
    const { getByText, queryByText, getByTestId } = renderSidebar([
      slot('mid', 'three days old', 3 * 24),
      slot('week-plus', 'eight days old', 8 * 24),
    ])
    // 3d < 7d threshold: visible. 8d > 7d: collapsed.
    expect(getByText('three days old')).toBeInTheDocument()
    expect(queryByText('eight days old')).toBeNull()
    expect(getByTestId('stale-expander-root')).toHaveTextContent('1')
  })

  it('renders the aria-controls target even while collapsed, so the relationship never dangles', () => {
    const { getByTestId, container } = renderSidebar([
      slot('fresh', 'fresh session', 2),
      slot('old-1', 'old session one', 10 * 24),
    ])
    const expander = getByTestId('stale-expander-root')
    const regionId = expander.getAttribute('aria-controls')!
    const region = container.querySelector(`[id="${regionId}"]`)
    expect(region).not.toBeNull()
    expect(region).toHaveAttribute('hidden')
  })

  it('exempts a row the user just moved, so it does not vanish behind the destination expander', () => {
    const folders = [{ id: 'f1', name: 'Work', order: 0, collapsed: false }]
    const before = [
      slot('in-old', 'foldered old', 10 * 24, { folder_id: 'f1' }),
      slot('moving-old', 'freshly moved old', 10 * 24),
    ]
    const view = renderSidebar(before, { folders })
    // At rest: both old rows are collapsed in their containers.
    expect(view.queryByText('freshly moved old')).toBeNull()
    // The session moves into f1 (any path: drag, row menu, header menu).
    const after = [
      slot('in-old', 'foldered old', 10 * 24, { folder_id: 'f1' }),
      slot('moving-old', 'freshly moved old', 10 * 24, { folder_id: 'f1' }),
    ]
    view.rerender(
      <QueryClientProvider client={view.qc}>
        <Provider store={view.store}>
          <ThemeProvider>
            <MemoryRouter>
              <ChatSidebar
                slots={after as never} activeSlot={null} unreadSlots={[]}
                history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
              />
            </MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    )
    // The moved row stays visible; its equally-old neighbor stays collapsed.
    expect(view.getByText('freshly moved old')).toBeInTheDocument()
    expect(view.queryByText('foldered old')).toBeNull()
  })

  it('does not mistake cold-load folder hydration for user movement', async () => {
    // The folders query resolves AFTER first render — the production cold
    // load. The slot→folder map going from empty to populated must not mark
    // every filed session "just moved" (which would exempt the whole tree).
    const folders = [{ id: 'f1', name: 'Work', order: 0, collapsed: false }]
    chatFoldersMock.mockImplementationOnce(
      () => new Promise(resolve => setTimeout(() => resolve(folders), 30)),
    )
    const { findByTestId, queryByText } = renderSidebar([
      slot('in-fresh', 'foldered fresh', 1, { folder_id: 'f1' }),
      slot('in-old', 'foldered old', 10 * 24, { folder_id: 'f1' }),
    ], { seedFolders: false })
    // Once folders hydrate, the old foldered row is collapsed, not exempted.
    expect(await findByTestId('stale-expander-f1')).toHaveTextContent('1')
    expect(queryByText('foldered old')).toBeNull()
  })

  it('a FAILED first folders fetch does not open the hydration hole either', async () => {
    // An errored fetch settles the query with no data; the websocket layer
    // later seeds the cache. That late arrival must not read as movement —
    // this is why the watcher gates on isSuccess, not isFetched.
    const folders = [{ id: 'f1', name: 'Work', order: 0, collapsed: false }]
    chatFoldersMock.mockRejectedValueOnce(new Error('gateway restarting'))
    const view = renderSidebar([
      slot('in-fresh', 'foldered fresh', 1, { folder_id: 'f1' }),
      slot('in-old', 'foldered old', 10 * 24, { folder_id: 'f1' }),
    ], { seedFolders: false })
    // Let the rejection settle, then recover via the cache seed path.
    await act(async () => { await new Promise(r => setTimeout(r, 20)) })
    act(() => { view.qc.setQueryData(['chat-folders'], folders) })
    expect(await view.findByTestId('stale-expander-f1')).toHaveTextContent('1')
    expect(view.queryByText('foldered old')).toBeNull()
  })

  it('pre-expands containers holding rows the user saw during a search, when the search clears', () => {
    const { getByPlaceholderText, getByText, getByTestId } = renderSidebar([
      slot('fresh', 'fresh session', 2),
      slot('old-match', 'ancient treasure hunt', 10 * 24),
    ])
    const search = getByPlaceholderText(/search/i)
    fireEvent.change(search, { target: { value: 'treasure' } })
    expect(getByText('ancient treasure hunt')).toBeInTheDocument()
    fireEvent.change(search, { target: { value: '' } })
    // The row the user was just reading is NOT swallowed: its container
    // arrives pre-expanded.
    expect(getByText('ancient treasure hunt')).toBeInTheDocument()
    expect(getByTestId('stale-expander-root')).toHaveAttribute('aria-expanded', 'true')
  })
})
