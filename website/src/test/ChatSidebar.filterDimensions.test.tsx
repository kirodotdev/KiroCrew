/**
 * The sidebar's filter dimensions are declared ONCE, in `filterDimensions`,
 * and all three consumers derive from it: `filteredSlots` (which rows render),
 * `listNarrowed` (is anything filtering), `revealBlockingFilters` (does THIS
 * row fail a filter). Before the consolidation each site enumerated the
 * dimensions by hand, so a new filter added to one and missed in the others
 * failed silently — reveal-in-sidebar looked broken (#4141), or an empty-state
 * branch showed a leftover container.
 *
 * The structural cases pin the derivation: each consumer names the single
 * source and no filter state of its own, so a dimension cannot be added to a
 * consumer directly. Adding one to `filterDimensions` itself is compiler-
 * checked — every `FilterDimension` field is required, so an entry cannot skip
 * a consumer's answer.
 *
 * The behavioural cases pin `listNarrowed` through the derivation — the one
 * consumer the reveal tests do not touch — including the deliberate
 * resolved-vs-raw tag distinction the declaration documents.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { readSource } from './readSource'
import { join } from 'node:path'
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

const RUNNING_ONLY_LS_KEY = 'mc-session-running-only'
const TAG_FILTER_LS_KEY = 'mc-session-tag-filter'

/** Neither slot is running, so the Running status chip excludes both. */
const SLOTS = [
  { key: 'k-alpha', title: 'alpha session', running: false, messages: 2, tags: ['t1'] },
  { key: 'k-beta', title: 'beta session', running: false, messages: 2, tags: ['t2'] },
]

function renderSidebar(slots: any[] = SLOTS) {
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
    } as any,
    chat: {
      ...defaults.chat,
      activeSlot: null, slotStatusDetail: {},
      revealRequest: null, revealNonce: 0,
    } as any,
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

const SRC = join(__dirname, '..', 'pages', 'ChatSidebar.tsx')
// Flattened first: a line-by-line scan misses a construct the moment a
// reformat splits it across lines.
const raw = readSource(SRC)
const flat = raw.replace(/\s+/g, ' ')

describe('all three consumers derive from the single filterDimensions declaration', () => {
  it('the declaration holds every dimension', () => {
    const decl = flat.match(/const filterDimensions = useMemo<FilterDimension\[\]>.*?\}, \[[^\]]*\]\)/)?.[0]
    expect(decl).toBeDefined()
    // Tags, search, status, folder. One count suffices: the compiler already
    // forces every entry to carry all four fields, so one field's occurrence
    // count pins the number of dimensions declared here. Bump it in the same
    // commit as a fifth entry so the addition is a decision, not drift.
    expect([...decl!.matchAll(/\bfiltersRow:/g)]).toHaveLength(4)
  })

  // The consumer pins below hold the COMPLETE normalized expression, not
  // fragments: a fragment check ("contains filterDimensions.every") is
  // satisfied by an expression that ALSO smuggles in an undeclared operand
  // (`&& ownerFilter(slot)`, `|| ownerFilterActive`, a second registry
  // entry). Changing a consumer means changing its pinned string in the same
  // commit — that is the point.

  it('filteredSlots filters only through the declaration', () => {
    const memo = flat.match(/const filteredSlots = useMemo\(.*?\}, \[[^\]]*\]\s*\)/)?.[0]
    expect(memo).toBeDefined()
    expect(memo).toContain(
      '.filter(slot => filterDimensions.every(d => d.filtersRow === null || d.filtersRow(slot)))',
    )
    // Exactly one .filter pass, and it is the pinned one — a second pass is a
    // dimension the other consumers cannot see.
    expect([...memo!.matchAll(/\.filter\(/g)]).toHaveLength(1)
  })

  it('listNarrowed consults only the declaration', () => {
    const line = raw.match(/const listNarrowed = [^\n]+/)?.[0]
    expect(line).toBe(
      'const listNarrowed = filterDimensions.some(d => d.narrows !== null && d.narrows())',
    )
  })

  it('revealBlockingFilters adapts the declaration, declaring nothing of its own', () => {
    const registry = flat.match(/const revealBlockingFilters = useMemo<RevealBlockingFilter\[\]>.*?\}, \[[^\]]*\]\)/)?.[0]
    expect(registry).toBeDefined()
    expect(registry).toContain(
      'return filterDimensions.map(d => ({ hides: (slot: Slot) => d.hides(slot, excluded), clear: d.clear, }))',
    )
  })
})
