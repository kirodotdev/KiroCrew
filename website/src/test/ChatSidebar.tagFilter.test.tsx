/**
 * Filter-menu Tags section: narrows the session list to the selected tags.
 *
 * Board view is forced OFF here because this control exists to give the LIST
 * view a tag-aware lane — on a phone the board's columns scroll sideways, which
 * is what drove the feature. The section itself is deliberately not gated on the
 * lane (unlike Folders), so `board view on` is covered too.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
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

/** Three sessions: one Alpha, one Beta, one untagged. */
const SLOTS = [
  { key: 'k-alpha', title: 'alpha session', running: false, messages: 2, tags: ['t1'] },
  { key: 'k-beta', title: 'beta session', running: false, messages: 2, tags: ['t2'] },
  { key: 'k-none', title: 'untagged session', running: false, messages: 2 },
]

function renderSidebar(slots: any[] = SLOTS, revealRequest: { key: string; nonce: number } | null = null) {
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
      revealRequest, revealNonce: revealRequest?.nonce ?? 0,
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

/** Two folders, one session in each, each carrying a different tag. */
const FOLDERS = [
  { id: 'fWork', name: 'Work', collapsed: false, order: 0 },
  { id: 'fPersonal', name: 'Personal', collapsed: false, order: 1 },
]
const FOLDER_SLOTS = [
  { key: 'k-alpha', title: 'alpha session', running: false, messages: 2, tags: ['t1'], folder_id: 'fWork' },
  { key: 'k-beta', title: 'beta session', running: false, messages: 2, tags: ['t2'], folder_id: 'fPersonal' },
]

/** Render the folder-tree lane with folders seeded. `staleTime: Infinity` and
 *  `refetchOnMount: false` are load-bearing: the api mock resolves chatFolders()
 *  to [], so an on-mount refetch would drop the folders and every "this folder
 *  is gone" assertion below would pass without the guard doing anything. */
function renderWithFolders(slots: any[] = FOLDER_SLOTS, folders: any[] = FOLDERS) {
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
    chat: { ...defaults.chat, activeSlot: null, slotStatusDetail: {}, revealRequest: null, revealNonce: 0 } as any,
  })
  const qc = new QueryClient({ defaultOptions: {
    queries: { retry: false, staleTime: Infinity, refetchOnMount: false }, mutations: { retry: false },
  } })
  qc.setQueryData(['chat-folders'], folders)
  return render(
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
}

/** Open the header's sort/filter dropdown. */
function openFilterMenu(utils: ReturnType<typeof renderSidebar>) {
  fireEvent.keyDown(utils.getByLabelText('Sort and filter sessions'), { key: 'Enter' })
}

beforeEach(() => localStorage.clear())
afterEach(() => vi.clearAllMocks())

describe('chat sidebar — filter menu Tags section', () => {
  it('renders one checkbox row per tag in the vocabulary', async () => {
    const utils = renderSidebar()
    await waitFor(() => expect(utils.queryByText('alpha session')).not.toBeNull())
    openFilterMenu(utils)
    const row = await utils.findByTestId('tag-filter-t1')
    expect(row).toHaveAttribute('role', 'menuitemcheckbox')
    expect(row).toHaveAttribute('aria-checked', 'false')
    expect(row).toHaveTextContent('Alpha')
    expect(await utils.findByTestId('tag-filter-t2')).toHaveTextContent('Beta')
  })

  it('narrows the list to the selected tag', async () => {
    const utils = renderSidebar()
    await waitFor(() => expect(utils.queryByText('beta session')).not.toBeNull())
    openFilterMenu(utils)
    fireEvent.click(await utils.findByTestId('tag-filter-t1'))
    await waitFor(() => expect(utils.queryByText('beta session')).toBeNull())
    expect(utils.queryByText('alpha session')).not.toBeNull()
    expect(utils.queryByText('untagged session')).toBeNull()
  })

  it('treats a multi-tag selection as a union, not an intersection', async () => {
    const utils = renderSidebar()
    await waitFor(() => expect(utils.queryByText('alpha session')).not.toBeNull())
    openFilterMenu(utils)
    fireEvent.click(await utils.findByTestId('tag-filter-t1'))
    fireEvent.click(await utils.findByTestId('tag-filter-t2'))
    // Both tagged sessions survive; only the untagged one is filtered out. An
    // intersection would have emptied the list, since no session has both tags.
    await waitFor(() => expect(utils.queryByText('untagged session')).toBeNull())
    expect(utils.queryByText('alpha session')).not.toBeNull()
    expect(utils.queryByText('beta session')).not.toBeNull()
  })

  it('persists the selection so it survives a remount', async () => {
    localStorage.setItem('mc-session-tag-filter', JSON.stringify(['t1']))
    const utils = renderSidebar()
    await waitFor(() => expect(utils.queryByText('alpha session')).not.toBeNull())
    expect(utils.queryByText('beta session')).toBeNull()
  })

  it('ignores a persisted id whose tag no longer exists instead of hiding everything', async () => {
    // A deleted tag leaves its id in localStorage, and an unresolvable id matches
    // no session — so an unguarded filter would empty the sidebar silently.
    localStorage.setItem('mc-session-tag-filter', JSON.stringify(['t-deleted']))
    const utils = renderSidebar()
    await waitFor(() => expect(utils.queryByText('alpha session')).not.toBeNull())
    expect(utils.queryByText('beta session')).not.toBeNull()
    expect(utils.queryByText('untagged session')).not.toBeNull()
  })

  it('renders a 0 count rather than omitting it', async () => {
    const utils = renderSidebar([SLOTS[0]])
    await waitFor(() => expect(utils.queryByText('alpha session')).not.toBeNull())
    openFilterMenu(utils)
    // Beta matches nothing here — the number a user needs BEFORE selecting it.
    expect(await utils.findByTestId('tag-filter-t2')).toHaveTextContent('0')
  })

  it('shows one aggregate chip that clears the whole tag filter when clicked', async () => {
    localStorage.setItem('mc-session-tag-filter', JSON.stringify(['t1']))
    const utils = renderSidebar()
    await waitFor(() => expect(utils.queryByText('beta session')).toBeNull())
    const chip = await utils.findByTestId('tag-filter-chip')
    expect(chip).toHaveAccessibleName('Clear Alpha filter')
    expect(chip).toHaveTextContent('Alpha')
    fireEvent.click(chip)
    await waitFor(() => expect(utils.queryByText('beta session')).not.toBeNull())
    expect(JSON.parse(localStorage.getItem('mc-session-tag-filter') || '[]')).toEqual([])
  })

  it('keeps the tag filter to ONE control however many tags are selected', async () => {
    // AUTOSDE `max-two-buttons-per-row` grandfathers the session-filter chip row
    // but forbids growing it, so the tag filter must not add a chip per tag.
    localStorage.setItem('mc-session-tag-filter', JSON.stringify(['t1', 't2']))
    const utils = renderSidebar()
    const chip = await utils.findByTestId('tag-filter-chip')
    // Both names are conveyed, but by one control, and it sits in its own row.
    expect(chip).toHaveTextContent('Alpha')
    expect(chip).toHaveTextContent('Beta')
    // Named for screen readers as a union, not as an opaque "tag filter".
    expect(chip).toHaveAccessibleName('Clear Alpha or Beta filter')
    expect(chip.parentElement!.querySelectorAll('button')).toHaveLength(1)
  })

  it('renders both folders when nothing is filtered (control for the two below)', async () => {
    // If the seeded folders were wiped by a refetch, the collapse assertions
    // below would pass with the guard removed. This is what rules that out.
    const utils = renderWithFolders()
    expect(await utils.findByLabelText('Folder Work')).not.toBeNull()
    expect(await utils.findByLabelText('Folder Personal')).not.toBeNull()
  })

  it('collapses a folder whose sessions are all excluded by a tag-only filter', async () => {
    // The folder lane drops a folder with no surviving children, but its guard
    // knew only search and the status filters -- not a tag-only filter.
    localStorage.setItem('mc-session-tag-filter', JSON.stringify(['t1']))
    const utils = renderWithFolders()
    await utils.findByLabelText('Folder Work')
    await waitFor(() => expect(utils.queryByLabelText('Folder Personal')).toBeNull())
    expect(utils.queryByText('New chat in Personal')).toBeNull()
    expect(utils.queryByText('alpha session')).not.toBeNull()
  })

  it('says so instead of going blank when a tag filter matches nothing', async () => {
    localStorage.setItem('mc-session-tag-filter', JSON.stringify(['t2']))
    const utils = renderWithFolders([FOLDER_SLOTS[0]])
    await waitFor(() => expect(utils.queryByText('No sessions match')).not.toBeNull())
  })

  it('clears the tag filter for a reveal that arrives while tags are still loading', async () => {
    // Mid-flight nothing is filtered yet, so a clear reachable only once the row
    // is excluded never runs -- and the query settling re-hides it afterwards.
    const scrollIntoView = vi.fn()
    const original = Element.prototype.scrollIntoView
    Element.prototype.scrollIntoView = scrollIntoView
    try {
      localStorage.setItem('mc-session-tag-filter', JSON.stringify(['t1']))
      // Reveal is present on the FIRST render, before chatTags resolves.
      const utils = renderSidebar(SLOTS, { key: 'k-beta', nonce: 1 })
      await waitFor(() =>
        expect(JSON.parse(localStorage.getItem('mc-session-tag-filter') || '[]')).toEqual([]))
      // And it stays revealed once the vocabulary lands.
      await waitFor(() => expect(utils.queryByText('alpha session')).not.toBeNull())
      expect(utils.queryByText('beta session')).not.toBeNull()
    } finally {
      Element.prototype.scrollIntoView = original
    }
  })

  it('clears the tag filter when a reveal target is hidden by it', async () => {
    // Reveal drops the filters excluding its target; without clearing the tag
    // filter the row never renders and the bounded retry expires silently.
    const scrollIntoView = vi.fn()
    const original = Element.prototype.scrollIntoView
    Element.prototype.scrollIntoView = scrollIntoView
    try {
      localStorage.setItem('mc-session-tag-filter', JSON.stringify(['t1']))
      const utils = renderSidebar()
      // Wait for the tag vocabulary to load and actually exclude the target,
      // which is the precondition the reveal effect's filter-drop branch tests.
      await waitFor(() => expect(utils.queryByText('beta session')).toBeNull())
      utils.store.dispatch(requestSlotReveal('k-beta'))
      await waitFor(() =>
        expect(JSON.parse(localStorage.getItem('mc-session-tag-filter') || '[]')).toEqual([]))
      expect(utils.queryByText('beta session')).not.toBeNull()
    } finally {
      Element.prototype.scrollIntoView = original
    }
  })
})
