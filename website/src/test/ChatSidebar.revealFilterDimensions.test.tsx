/**
 * "Reveal in sidebar" must drop any filter hiding the row the user asked for,
 * or it silently does nothing and the button looks broken. Those filters are
 * registered in ONE list, and the reveal effect walks it instead of naming each.
 *
 * The behavioural cases pin the outcome across MORE THAN ONE filter, so the
 * registry cannot quietly drop a dimension; they pass both before and after
 * the refactor, which deliberately changed no filter's behaviour.
 *
 * The structural cases are the before/after guard, and their reach is narrow:
 * they fail while the effect names filter state directly, and they pin that
 * the registry adapts the single filterDimensions declaration rather than
 * declaring dimensions of its own. The declaration's shape — and that
 * filteredSlots and listNarrowed derive from the same source — is pinned by
 * ChatSidebar.filterDimensions.test.tsx.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { render, fireEvent, waitFor } from '@testing-library/react'
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

/** `k-alpha` is running and tagged Alpha; `k-beta` is neither, so it is the row
 *  every filter below excludes and therefore the reveal target throughout. */
const SLOTS: ChatSlot[] = [
  { key: 'k-alpha', title: 'alpha session', running: true, messages: 2, tags: ['t1'] },
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

/** jsdom has no scrollIntoView; the reveal effect calls it on the found row. */
function withScrollStub(fn: () => Promise<void>) {
  const original = Element.prototype.scrollIntoView
  Element.prototype.scrollIntoView = vi.fn()
  return fn().finally(() => { Element.prototype.scrollIntoView = original })
}

beforeEach(() => localStorage.clear())
afterEach(() => vi.clearAllMocks())

describe('reveal-in-sidebar drops every registered filter dimension', () => {
  it('clears a search that hides the reveal target', async () => {
    await withScrollStub(async () => {
      const utils = renderSidebar()
      await waitFor(() => expect(utils.queryByText('beta session')).not.toBeNull())
      const search = utils.getByPlaceholderText('Search sessions…')
      fireEvent.change(search, { target: { value: 'alpha' } })
      // Precondition: the target really is excluded before the reveal.
      await waitFor(() => expect(utils.queryByText('beta session')).toBeNull())

      utils.store.dispatch(requestSlotReveal('k-beta'))

      await waitFor(() => expect(utils.queryByText('beta session')).not.toBeNull())
      expect(search).toHaveValue('')
    })
  })

  it('clears a status filter that hides the reveal target', async () => {
    await withScrollStub(async () => {
      localStorage.setItem(RUNNING_ONLY_LS_KEY, '1')
      const utils = renderSidebar()
      await waitFor(() => expect(utils.queryByText('beta session')).toBeNull())
      expect(utils.queryByText('alpha session')).not.toBeNull()

      utils.store.dispatch(requestSlotReveal('k-beta'))

      await waitFor(() => expect(utils.queryByText('beta session')).not.toBeNull())
      // Persisted, not just state: the sidebar unmounts when the drawer
      // collapses and remount re-reads a stored '1'.
      expect(localStorage.getItem(RUNNING_ONLY_LS_KEY)).toBe('0')
    })
  })

  it('clears two dimensions at once when both hide the reveal target', async () => {
    await withScrollStub(async () => {
      localStorage.setItem(RUNNING_ONLY_LS_KEY, '1')
      localStorage.setItem(TAG_FILTER_LS_KEY, JSON.stringify(['t1']))
      const utils = renderSidebar()
      await waitFor(() => expect(utils.queryByText('beta session')).toBeNull())

      utils.store.dispatch(requestSlotReveal('k-beta'))

      await waitFor(() => expect(utils.queryByText('beta session')).not.toBeNull())
      expect(localStorage.getItem(RUNNING_ONLY_LS_KEY)).toBe('0')
      expect(JSON.parse(localStorage.getItem(TAG_FILTER_LS_KEY) || '[]')).toEqual([])
    })
  })
})

const SRC = join(__dirname, '..', 'pages', 'ChatSidebar.tsx')
// Flattened first: a line-by-line scan misses a construct the moment a
// reformat splits it across lines.
const flat = readFileSync(SRC, 'utf8').replace(/\s+/g, ' ')

/** The reveal effect's body, from its guard clause to its dependency array. */
function revealEffect(): string {
  const body = flat.match(/if \(!revealRequest\) return.*?\}, \[revealRequest[^\]]*\]\)/)?.[0]
  expect(body).toBeDefined()
  return body!
}

describe('reveal filter dimensions are registered, not enumerated in the effect', () => {
  it('the effect names no filter state of its own', () => {
    const effect = revealEffect()
    // Each WAS named here. This pins that the effect no longer reads filter
    // state; it cannot see a filter that skipped the registry entirely.
    for (const named of [
      'slotFilter', 'setSlotFilter',
      'activeFilters', 'setActiveFilters', 'SESSION_FILTERS',
      'filterTagIds', 'activeTagIds', 'clearTagFilter',
      'filterHiddenSubtree', 'setFilterHiddenFolders',
      'filteredSlots',
    ]) {
      expect(effect, `reveal effect must not name ${named} directly`).not.toContain(named)
    }
    // It consults the registry instead.
    expect(effect).toContain('revealBlockingFilters')
  })

  it('the registry derives from the single filterDimensions declaration', () => {
    const registry = flat.match(/const revealBlockingFilters = useMemo<RevealBlockingFilter\[\]>.*?\}, \[[^\]]*\]\)/)?.[0]
    expect(registry).toBeDefined()
    // Dimensions are declared ONCE, in filterDimensions (whose shape
    // ChatSidebar.filterDimensions.test.tsx pins); this registry only adapts
    // them, so it can no longer hold a dimension the other consumers miss.
    expect(registry).toContain('filterDimensions.map')
    for (const named of [
      'filterTagIds', 'clearTagFilter',
      'slotFilter', 'setSlotFilter',
      'activeFilters', 'setActiveFilters', 'SESSION_FILTERS',
      'filterHiddenSubtree', 'setFilterHiddenFolders',
    ]) {
      expect(registry, `reveal registry must not name ${named} directly`).not.toContain(named)
    }
  })
})
