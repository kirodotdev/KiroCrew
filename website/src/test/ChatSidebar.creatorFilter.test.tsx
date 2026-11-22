/**
 * Filter-menu "Started by" section on a Shared Crew: You, each person and
 * Unattributed with session counts, ticked rows OR together, the list narrows
 * on the client, and the chip under the search box says how many sessions the
 * filter shows and hides. A personal Crew shows neither.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent, waitFor, within } from '@testing-library/react'
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
        if (FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children as React.ReactNode)
    })
  const byTag = new Map<string, ReturnType<typeof make>>()
  const motion = new Proxy({}, {
    get: (_t, tag: string) => {
      if (!byTag.has(tag)) byTag.set(tag, make(tag))
      return byTag.get(tag)
    },
  })
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

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})

const LONG = 'someone.with.a.really.long.name@example.test'
const human = (id: string, name: string) => ({ id, kind: 'human' as const, display: { name, login: '', captured_at: null } })
const UNKNOWN = { id: 'p_unknown', kind: 'unknown' as const, display: { name: '', login: '', captured_at: null } }
const by = (creator: ReturnType<typeof human> | typeof UNKNOWN, trigger?: object) =>
  ({ creator, availability: 'recorded' as const, created_at: 1, ...(trigger ? { trigger } : {}) })

// The list view is the Shared Crew read layer; here it is a stand-in with real
// selection state, so the test drives the menu and the list only.
const crew = vi.hoisted(() => ({ shown: true }))
vi.mock('../pages/chat-sidebar/CreatorFilter', async importOriginal => {
  const React = await import('react')
  return {
    ...(await importOriginal<typeof import('../pages/chat-sidebar/CreatorFilter')>()),
    useSharedCrewListView: () => {
      const [keys, setKeys] = React.useState<ReadonlySet<string>>(new Set())
      const toggleCreator = React.useCallback((key: string) => setKeys(prev => {
        const next = new Set(prev)
        if (next.has(key)) next.delete(key)
        else next.add(key)
        return next
      }), [])
      const clearCreators = React.useCallback(() => setKeys(new Set()), [])
      const bylines = React.useMemo(() => ({ hidden: !crew.shown, viewerId: 'p_me' }), [])
      return {
        noticesAvailable: false,
        creatorFilterShown: crew.shown,
        creatorKeys: crew.shown ? keys : new Set<string>(),
        toggleCreator,
        clearCreators,
        people: [
          { principal: human('p_me', 'Me'), sources: ['observed'], is_viewer: true, is_owner: true },
          { principal: human('p_bob', 'Bob'), sources: ['observed'] },
          { principal: human('p_long', LONG), sources: ['observed'] },
          { principal: human('p_tam', 'Tam'), sources: ['observed'] },
        ],
        // The owner writes under a second local id (the desktop app's): both are You.
        viewerIds: new Set(['p_me', 'p_me_desktop']),
        bylines,
      }
    },
  }
})

import ChatSidebar from '../pages/ChatSidebar'
import type { RootState } from '../store'
import type { ChatSlot } from '../types'

const SLOTS = [
  { key: 'k-mine', title: 'my session', running: false, messages: 2, attribution: by(human('p_me', 'Me')) },
  { key: 'k-mine-desktop', title: 'my desktop session', running: false, messages: 2, attribution: by(human('p_me_desktop', 'Me')) },
  { key: 'k-bob-a', title: 'bob first', running: false, messages: 2, attribution: by(human('p_bob', 'Bob')) },
  { key: 'k-bob-b', title: 'bob second', running: false, messages: 2, attribution: by(human('p_bob', 'Bob')) },
  { key: 'k-long', title: 'long one', running: false, messages: 2, attribution: by(human('p_long', LONG)) },
  { key: 'k-nobody', title: 'nobody session', running: false, messages: 2, attribution: by(UNKNOWN) },
  { key: 'k-cron', title: 'nightly run', running: false, messages: 2, attribution: by(UNKNOWN, { kind: 'scheduled', ref: 'abc', name: 'Nightly', configured_by: null }) },
]
const ALL_TITLES = SLOTS.map(s => s.title)

function renderSidebar() {
  const defaults = createTestStore().getState()
  const store = createTestStore({
    dashboard: {
      ...defaults.dashboard,
      status: {}, connected: true, slots: SLOTS, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { ...defaults.chat, activeSlot: null, slotStatusDetail: {} } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], [])
  qc.setQueryData(['chat-tags'], [])
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={SLOTS as unknown as ChatSlot[]} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

type Utils = ReturnType<typeof renderSidebar>
const openFilterMenu = (utils: Utils) => fireEvent.keyDown(utils.getByLabelText('Sort and filter sessions'), { key: 'Enter' })
const visible = (utils: Utils) => ALL_TITLES.filter(title => utils.queryByText(title) !== null)

beforeEach(() => { localStorage.clear(); crew.shown = true })
afterEach(() => vi.clearAllMocks())

describe('chat sidebar — filter menu "Started by" section', () => {
  it('lists You, each person and Unattributed, each with a session count', async () => {
    const utils = renderSidebar()
    await waitFor(() => expect(utils.queryByText('bob first')).not.toBeNull())
    // No separate creator bar above the list any more.
    expect(utils.queryByTestId('creator-filter')).toBeNull()
    openFilterMenu(utils)
    await utils.findByText('Started by')
    const rows = utils.getAllByTestId(/^creator-filter-row-/)
    expect(rows.map(row => row.getAttribute('data-testid'))).toEqual([
      'creator-filter-row-mine', 'creator-filter-row-p_bob', 'creator-filter-row-p_long', 'creator-filter-row-p_tam', 'creator-filter-row-unattributed',
    ])
    expect(rows.map(row => row.textContent)).toEqual(['You2', 'Bob2', `${LONG}1`, 'Tam0', 'Unattributed1'])
    for (const row of rows) {
      expect(row).toHaveAttribute('role', 'menuitemcheckbox')
      expect(row).toHaveAttribute('aria-checked', 'false')
    }
  })

  it('cuts a long name with an ellipsis and shows it in full on hover', async () => {
    const utils = renderSidebar()
    openFilterMenu(utils)
    const name = await utils.findByTestId('creator-filter-name-p_long')
    expect(name).toHaveClass('truncate')
    expect(name).toHaveAttribute('title', LONG)
  })

  it('narrows the list on the client, ORs ticked rows, and the chip counts and clears', async () => {
    const utils = renderSidebar()
    await waitFor(() => expect(visible(utils)).toEqual(ALL_TITLES))
    openFilterMenu(utils)
    fireEvent.click(await utils.findByTestId('creator-filter-row-p_bob'))
    await waitFor(() => expect(visible(utils)).toEqual(['bob first', 'bob second']))
    expect(utils.getByTestId('creator-filter-row-p_bob')).toHaveAttribute('aria-checked', 'true')
    expect(utils.getByTestId('creator-filter-chip')).toHaveTextContent('Started by Bob')
    expect(utils.getByTestId('creator-filter-counts')).toHaveTextContent('2 matching · 5 hidden by this filter')

    fireEvent.click(utils.getByTestId('creator-filter-row-unattributed'))
    // A scheduled session names its trigger, so it is not Unattributed.
    await waitFor(() => expect(visible(utils)).toEqual(['bob first', 'bob second', 'nobody session']))
    expect(utils.getByTestId('creator-filter-counts')).toHaveTextContent('3 matching · 4 hidden by this filter')

    fireEvent.click(utils.getByTestId('creator-filter-row-mine'))
    await waitFor(() => expect(visible(utils)).toEqual(['my session', 'my desktop session', 'bob first', 'bob second', 'nobody session']))

    fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' })
    const chip = utils.getByTestId('creator-filter-chip')
    expect(within(chip).getByText(/Started by/)).toBeInTheDocument()
    fireEvent.click(chip)
    await waitFor(() => expect(visible(utils)).toEqual(ALL_TITLES))
    expect(utils.queryByTestId('creator-filter-chip')).toBeNull()
    expect(utils.queryByTestId('creator-filter-counts')).toBeNull()
  })

  it('shows no section and no chip on a personal Crew', async () => {
    crew.shown = false
    const utils = renderSidebar()
    await waitFor(() => expect(visible(utils)).toEqual(ALL_TITLES))
    openFilterMenu(utils)
    // The menu is open (its Models section renders) but has no "Started by".
    await utils.findByTestId('model-filter-row-auto')
    expect(utils.queryByText('Started by')).toBeNull()
    expect(utils.queryByTestId(/^creator-filter-row-/)).toBeNull()
    expect(utils.queryByTestId('creator-filter-chip')).toBeNull()
  })
})
