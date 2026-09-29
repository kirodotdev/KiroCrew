/**
 * Chat sidebar — the browser-storage keys it owns, as a contract.
 *
 * Every key below is read at mount and written on interaction. What a reload
 * restores, what a hand-edited or foreign value falls back to, what a storage
 * that throws on read falls back to, and which legacy keys are migrated are all
 * promises to a returning user, so each is pinned through what renders (or what
 * lands back in storage), never through the module's internals.
 *
 * Already pinned elsewhere and deliberately not repeated here:
 *  - `?history=1` opening the Older Sessions pane and fetching it:
 *    ChatSidebar.historyDeepLink.test.tsx (used below only as the way to open the
 *    pane on mount)
 *  - legacy flat-view '1' migrating to the flat lane, a stored 'conductor' lane,
 *    the superseded conductor key removed in the conductor lane:
 *    ChatSidebar.conductorLane.test.tsx
 *  - corrupt hidden-folder JSON: ChatSidebar.flatFolderFilter.test.tsx
 *  - stale collapse absent / '0' / custom threshold: ChatSidebar.staleCollapse.test.tsx
 *  - pointer-drag width persistence: ChatSidebar.resizeTouch.test.tsx
 *  - restoring the pre-board width when a persisted one is already present:
 *    ChatSidebarCoverage.test.tsx
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'
import type { ChatFolder } from '../types'

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

// Mutable so the board case can flip into board view. The save mirrors the real
// one: it persists the value and announces it, which is how the open sidebar
// learns that board view was turned on or off.
const cfg = vi.hoisted(() => ({
  value: { tagColumnsEnabled: false, confirmCloseSession: false, defaultAutopilot: false } as Record<string, unknown>,
}))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => cfg.value,
  saveChatConfig: (next: Record<string, unknown>) => {
    cfg.value = next
    window.dispatchEvent(new Event('mc-config-changed'))
  },
}))

const mocks = vi.hoisted(() => ({
  chatFolders: vi.fn(),
  chatTags: vi.fn(),
  tagColumns: vi.fn(),
  createTagColumn: vi.fn(),
  kirocrewConfig: vi.fn(),
  sessions: vi.fn(),
  sessionsSearch: vi.fn(),
}))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as unknown as Record<string, unknown>, {
    get: (target, prop: string) => (prop in target ? target[prop] : vi.fn().mockResolvedValue([])),
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
globalThis.fetch = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }) as unknown as typeof fetch

import ChatSidebar from '../pages/ChatSidebar'

type TestSlot = Record<string, unknown>

const DAY_MS = 24 * 60 * 60 * 1000
const hoursAgo = (h: number) => new Date(Date.now() - h * 3600_000).toISOString()

const ALPHA: ChatFolder = { id: 'fA', name: 'Alpha', order: 0 }
const BETA: ChatFolder = { id: 'fB', name: 'Beta', order: 1 }

/** A conductor filed in a folder plus the worker it opened: with a folder AND a
 *  lineage edge, all three lanes are available, so every stored lane can render. */
const LANE_SLOTS: TestSlot[] = [
  { key: 'k-conductor', title: 'Conductor', messages: 1, running: false, last_turn_ts: hoursAgo(1), folder_id: 'fA' },
  { key: 'k-worker', title: 'Worker', messages: 1, running: false, last_turn_ts: hoursAgo(2), parent: { slot: 'k-conductor', key: 'k-conductor' } },
]

function renderSidebar(opts: {
  slots?: TestSlot[]
  folders?: ChatFolder[]
  unreadSlots?: string[]
} = {}) {
  const slots = opts.slots ?? LANE_SLOTS
  const folders = opts.folders ?? [ALPHA]
  const unreadSlots = opts.unreadSlots ?? []
  mocks.chatFolders.mockResolvedValue(folders)
  // Spread the real slice defaults: RTK REPLACES a slice with preloadedState
  // rather than merging, so a partial drops keys the reducers assume exist.
  const defaults = createTestStore().getState()
  const store = createTestStore({
    dashboard: {
      ...defaults.dashboard,
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots, updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: {
      ...defaults.chat,
      activeSlot: null, slotStatusDetail: {}, subagents: {}, slotActivity: {},
      revealRequest: null, revealNonce: 0,
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], folders)
  const view = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots as never} activeSlot={null} unreadSlots={unreadSlots}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return { ...view, store, qc }
}

/** Make `getItem` throw for ONE key, the way disabled or private-mode storage
 *  refuses a read, while every other key keeps working. */
function throwOnGetItem(key: string) {
  const real = Storage.prototype.getItem
  return vi.spyOn(Storage.prototype, 'getItem').mockImplementation(function (this: Storage, k: string) {
    if (k === key) throw new Error('storage refused the read')
    return real.call(this, k)
  })
}

/** Which lane is on screen, read off the lane containers the sidebar renders. */
function laneInView(): 'tree' | 'flat' | 'conductor' {
  if (screen.queryByTestId('conductor-view-lane')) return 'conductor'
  if (screen.queryByTestId('flat-view-lane')) return 'flat'
  return 'tree'
}

const laneRows = (lane: HTMLElement) =>
  Array.from(lane.querySelectorAll('[data-slot-key]')).map(el => el.getAttribute('data-slot-key'))

function openFilterMenu() {
  fireEvent.keyDown(screen.getByRole('button', { name: 'Sort and filter sessions' }), { key: 'Enter' })
}

beforeEach(() => {
  localStorage.clear()
  window.history.replaceState({}, '', '/chat')
  cfg.value = { tagColumnsEnabled: false, confirmCloseSession: false, defaultAutopilot: false }
  for (const m of Object.values(mocks)) m.mockReset()
  mocks.chatFolders.mockResolvedValue([])
  mocks.chatTags.mockResolvedValue([])
  mocks.tagColumns.mockResolvedValue([])
  mocks.createTagColumn.mockResolvedValue({ id: 'col-new' })
  mocks.kirocrewConfig.mockResolvedValue({ dashboard: {} })
  mocks.sessions.mockResolvedValue({ sessions: [], has_more: false })
  mocks.sessionsSearch.mockResolvedValue({ sessions: [] })
})
afterEach(() => {
  vi.restoreAllMocks()
  vi.clearAllMocks()
  window.history.replaceState({}, '', '/')
})

describe('mc-sidebar-lane, with the legacy mc-sidebar-flat-view it replaces', () => {
  // Lane fixtures are the subject here, not dormancy.
  beforeEach(() => localStorage.setItem('mc-session-stale-collapse-ms', '0'))

  it('opens in each stored lane', () => {
    for (const lane of ['tree', 'flat', 'conductor'] as const) {
      localStorage.setItem('mc-sidebar-lane', lane)
      const view = renderSidebar()
      expect(laneInView()).toBe(lane)
      view.unmount()
    }
  })

  it('reads an unrecognised lane as absent and falls back to the legacy boolean', () => {
    const cases: Array<[string | null, 'tree' | 'flat']> = [['1', 'flat'], ['0', 'tree'], [null, 'tree']]
    for (const [legacy, expected] of cases) {
      localStorage.clear()
      localStorage.setItem('mc-session-stale-collapse-ms', '0')
      localStorage.setItem('mc-sidebar-lane', 'board')
      if (legacy !== null) localStorage.setItem('mc-sidebar-flat-view', legacy)
      const view = renderSidebar()
      expect(laneInView()).toBe(expected)
      view.unmount()
    }
  })

  it('lets a recognised new key win over the legacy boolean', () => {
    localStorage.setItem('mc-sidebar-flat-view', '1')
    localStorage.setItem('mc-sidebar-lane', 'tree')
    const tree = renderSidebar()
    expect(laneInView()).toBe('tree')
    tree.unmount()

    localStorage.setItem('mc-sidebar-lane', 'conductor')
    renderSidebar()
    expect(laneInView()).toBe('conductor')
  })

  it('writes the next lane AND mirrors the legacy boolean on every press of the toggle', () => {
    renderSidebar()
    const toggle = () => screen.getByTestId('flat-view-toggle')
    const steps: Array<['tree' | 'flat' | 'conductor', string]> = [['conductor', '0'], ['flat', '1'], ['tree', '0']]
    for (const [lane, legacy] of steps) {
      fireEvent.click(toggle())
      // Both keys land in the SAME interaction: nothing else runs between the
      // press and these reads.
      expect(localStorage.getItem('mc-sidebar-lane')).toBe(lane)
      expect(localStorage.getItem('mc-sidebar-flat-view')).toBe(legacy)
      expect(laneInView()).toBe(lane)
    }
  })
})

describe('mc-sidebar-conductor-expanded, and the superseded mc-sidebar-conductor-collapsed', () => {
  beforeEach(() => localStorage.setItem('mc-session-stale-collapse-ms', '0'))

  it('removes the superseded key on mount, before any interaction, whichever lane opens', () => {
    localStorage.setItem('mc-sidebar-conductor-collapsed', JSON.stringify(['k-conductor']))
    // The tree lane: the conductor lane is not in view at all.
    renderSidebar()
    expect(laneInView()).toBe('tree')
    expect(localStorage.getItem('mc-sidebar-conductor-collapsed')).toBeNull()
  })

  it('opens the rows the stored set names', () => {
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    localStorage.setItem('mc-sidebar-conductor-expanded', JSON.stringify(['k-conductor']))
    renderSidebar()
    expect(laneRows(screen.getByTestId('conductor-view-lane'))).toEqual(['k-conductor', 'k-worker'])
  })

  it('reads an unusable stored set as "everything collapsed"', () => {
    for (const raw of ['{not json', JSON.stringify({ 'k-conductor': true }), '', JSON.stringify([5, null, ''])]) {
      localStorage.clear()
      localStorage.setItem('mc-session-stale-collapse-ms', '0')
      localStorage.setItem('mc-sidebar-lane', 'conductor')
      localStorage.setItem('mc-sidebar-conductor-expanded', raw)
      const view = renderSidebar()
      expect(laneRows(screen.getByTestId('conductor-view-lane'))).toEqual(['k-conductor'])
      expect(screen.getByTestId('conductor-child-count-k-conductor').textContent).toBe('1')
      view.unmount()
    }
  })

  it('keeps the string entries of a mixed set and drops the rest', () => {
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    localStorage.setItem('mc-sidebar-conductor-expanded', JSON.stringify(['k-conductor', 7, null]))
    renderSidebar()
    expect(laneRows(screen.getByTestId('conductor-view-lane'))).toEqual(['k-conductor', 'k-worker'])
  })

  it('still reads the opened set when removing the superseded key throws', () => {
    const real = Storage.prototype.removeItem
    const remove = vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(function (this: Storage, k: string) {
      if (k === 'mc-sidebar-conductor-collapsed') throw new Error('storage refused the write')
      return real.call(this, k)
    })
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    localStorage.setItem('mc-sidebar-conductor-collapsed', JSON.stringify(['k-conductor']))
    localStorage.setItem('mc-sidebar-conductor-expanded', JSON.stringify(['k-conductor']))
    renderSidebar()
    expect(remove).toHaveBeenCalledWith('mc-sidebar-conductor-collapsed')
    expect(laneRows(screen.getByTestId('conductor-view-lane'))).toEqual(['k-conductor', 'k-worker'])
  })
})

describe('mc-session-recent-window-ms', () => {
  /** The active Recent chip names the window it filters by. */
  const recentChip = () => screen.getByRole('button', { name: 'Clear Recent filter' })

  it('shows a valid stored window', () => {
    localStorage.setItem('mc-session-recent-only', '1')
    localStorage.setItem('mc-session-recent-window-ms', String(2 * DAY_MS))
    renderSidebar()
    expect(recentChip().textContent).toContain('Recent · 2d')
  })

  it('falls back to the 1 hour default for an invalid, zero, negative or NaN value', () => {
    for (const raw of ['abc', '0', '-3600000', 'NaN', '']) {
      localStorage.clear()
      localStorage.setItem('mc-session-recent-only', '1')
      localStorage.setItem('mc-session-recent-window-ms', raw)
      const view = renderSidebar()
      expect(recentChip().textContent).toContain('Recent · 1h')
      view.unmount()
    }
  })

  it('falls back to the default when reading the window throws', () => {
    localStorage.setItem('mc-session-recent-only', '1')
    localStorage.setItem('mc-session-recent-window-ms', String(2 * DAY_MS))
    throwOnGetItem('mc-session-recent-window-ms')
    renderSidebar()
    expect(recentChip().textContent).toContain('Recent · 1h')
  })
})

describe('mc-session-stale-collapse-ms', () => {
  // Six and eight days idle straddle the seven-day default, so the expander
  // holding exactly the older row pins the default rather than "some threshold".
  const DORMANCY_SLOTS: TestSlot[] = [
    { key: 'k-six', title: 'six days idle', messages: 2, running: false, created: hoursAgo(6 * 24 + 1), last_turn_ts: hoursAgo(6 * 24) },
    { key: 'k-eight', title: 'eight days idle', messages: 2, running: false, created: hoursAgo(8 * 24 + 1), last_turn_ts: hoursAgo(8 * 24) },
  ]

  function expectSevenDayDefault() {
    expect(screen.getByText('six days idle')).toBeInTheDocument()
    expect(screen.queryByText('eight days idle')).toBeNull()
    const expander = screen.getByTestId('stale-expander-root')
    expect(expander).toHaveTextContent('1')
  }

  it('uses the seven-day default when the value is absent, invalid or negative', () => {
    for (const raw of [null, 'abc', '-1', 'NaN']) {
      localStorage.clear()
      if (raw !== null) localStorage.setItem('mc-session-stale-collapse-ms', raw)
      const view = renderSidebar({ slots: DORMANCY_SLOTS, folders: [] })
      expectSevenDayDefault()
      view.unmount()
    }
  })

  it('uses the default when reading the threshold throws, even over a stored "off"', () => {
    localStorage.setItem('mc-session-stale-collapse-ms', '0')
    throwOnGetItem('mc-session-stale-collapse-ms')
    renderSidebar({ slots: DORMANCY_SLOTS, folders: [] })
    expectSevenDayDefault()
  })
})

describe('mc-flat-hidden-folders and mc-filter-folders-shelved', () => {
  beforeEach(() => localStorage.setItem('mc-session-stale-collapse-ms', '0'))

  const FOLDER_SLOTS: TestSlot[] = [
    { key: 'k-a', title: 'alpha work', messages: 1, running: false, folder_id: 'fA' },
    { key: 'k-b', title: 'beta work', messages: 1, running: false, folder_id: 'fB' },
  ]
  const folderRow = (id: string) => document.querySelector(`[data-folder-row="${id}"]`)

  function renderFolders() {
    return renderSidebar({ slots: FOLDER_SLOTS, folders: [ALPHA, BETA] })
  }

  it('hides exactly the string ids of a stored array', () => {
    localStorage.setItem('mc-flat-hidden-folders', JSON.stringify(['fA', 42, null]))
    renderFolders()
    expect(folderRow('fA')).toBeNull()
    expect(folderRow('fB')).not.toBeNull()
  })

  it('hides nothing for a non-array value or an array with no string ids', () => {
    for (const raw of [JSON.stringify({ fA: true }), JSON.stringify([42, null, {}])]) {
      localStorage.clear()
      localStorage.setItem('mc-session-stale-collapse-ms', '0')
      localStorage.setItem('mc-flat-hidden-folders', raw)
      const view = renderFolders()
      expect(folderRow('fA')).not.toBeNull()
      expect(folderRow('fB')).not.toBeNull()
      view.unmount()
    }
  })

  it('hides nothing when reading the hidden set throws', () => {
    localStorage.setItem('mc-flat-hidden-folders', JSON.stringify(['fA']))
    throwOnGetItem('mc-flat-hidden-folders')
    renderFolders()
    expect(folderRow('fA')).not.toBeNull()
    expect(folderRow('fB')).not.toBeNull()
  })

  it('restores a shelved folder list, and reads a throwing store as not shelved', async () => {
    localStorage.setItem('mc-filter-folders-shelved', '1')
    const shelved = renderFolders()
    openFilterMenu()
    expect(await screen.findByTestId('folder-filter-shelve')).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByTestId('folder-filter-fA')).toBeNull()
    shelved.unmount()

    throwOnGetItem('mc-filter-folders-shelved')
    renderFolders()
    openFilterMenu()
    expect(await screen.findByTestId('folder-filter-shelve')).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByTestId('folder-filter-fA')).toBeInTheDocument()
  })
})

describe('mc-session-tag-filter', () => {
  const TAGS = [
    { id: 't1', name: 'Blocked', color: '#ff0000', order: 0 },
    { id: 't2', name: 'Waiting', color: '#00ff00', order: 1 },
  ]
  const TAGGED_SLOTS: TestSlot[] = [
    { key: 'k-t1', title: 'blocked session', messages: 2, running: false, tags: ['t1'] },
    { key: 'k-t2', title: 'waiting session', messages: 2, running: false, tags: ['t2'] },
  ]
  beforeEach(() => {
    localStorage.setItem('mc-session-stale-collapse-ms', '0')
    mocks.chatTags.mockResolvedValue(TAGS)
  })

  it('narrows the list to a stored selection once the vocabulary loads', async () => {
    localStorage.setItem('mc-session-tag-filter', JSON.stringify(['t1']))
    renderSidebar({ slots: TAGGED_SLOTS, folders: [] })
    expect(await screen.findByTestId('tag-filter-chip')).toHaveTextContent('Blocked')
    await waitFor(() => expect(screen.queryByText('waiting session')).toBeNull())
    expect(screen.getByText('blocked session')).toBeInTheDocument()
  })

  it('applies no tag filter for corrupt JSON, a non-array, non-string ids, or a throwing read', async () => {
    const cases: Array<{ raw: string; throws?: boolean }> = [
      { raw: '{not json' },
      { raw: JSON.stringify({ t1: true }) },
      { raw: JSON.stringify([1, null]) },
      { raw: JSON.stringify(['t1']), throws: true },
    ]
    for (const { raw, throws } of cases) {
      localStorage.clear()
      localStorage.setItem('mc-session-stale-collapse-ms', '0')
      localStorage.setItem('mc-session-tag-filter', raw)
      const spy = throws ? throwOnGetItem('mc-session-tag-filter') : null
      const view = renderSidebar({ slots: TAGGED_SLOTS, folders: [] })
      openFilterMenu()
      // Settled: the vocabulary has loaded and the row reports itself unselected.
      expect(await screen.findByTestId('tag-filter-t1')).toHaveAttribute('aria-checked', 'false')
      expect(screen.queryByTestId('tag-filter-chip')).toBeNull()
      expect(screen.getByText('blocked session')).toBeInTheDocument()
      expect(screen.getByText('waiting session')).toBeInTheDocument()
      view.unmount()
      spy?.mockRestore()
    }
  })
})

describe('mc-session-{unread,running,pinned,recent}-only', () => {
  const FILTERS = [
    { storageKey: 'mc-session-unread-only', label: 'Unread' },
    { storageKey: 'mc-session-running-only', label: 'In progress' },
    { storageKey: 'mc-session-pinned-only', label: 'Pinned' },
    { storageKey: 'mc-session-recent-only', label: 'Recent' },
  ]
  const ROWS: TestSlot[] = [{ key: 'k-1', title: 'one session', messages: 1, running: false, last_turn_ts: hoursAgo(1) }]

  it('activates the chip for a stored "1"', () => {
    for (const { storageKey, label } of FILTERS) {
      localStorage.clear()
      localStorage.setItem(storageKey, '1')
      // A non-empty inbox, so the unread auto-drain has nothing to drain.
      const view = renderSidebar({ slots: ROWS, folders: [], unreadSlots: ['k-1'] })
      expect(screen.getByRole('button', { name: `Clear ${label} filter` })).toBeInTheDocument()
      view.unmount()
    }
  })

  it('writes "1" then "0" as the menu row toggles the filter', async () => {
    renderSidebar({ slots: ROWS, folders: [], unreadSlots: ['k-1'] })
    openFilterMenu()
    for (const { storageKey, label } of FILTERS) {
      const row = () => screen.getAllByRole('menuitem').find(el => el.textContent?.startsWith(label))
      await waitFor(() => expect(row()).toBeTruthy())
      fireEvent.click(row()!)
      expect(localStorage.getItem(storageKey)).toBe('1')
      fireEvent.click(row()!)
      expect(localStorage.getItem(storageKey)).toBe('0')
    }
  })
})

describe('mc-history-height', () => {
  const scroller = () => document.querySelector('#history-pane .scroll-shadow') as HTMLElement | null

  it('writes the 240 default on mount, with no interaction', () => {
    renderSidebar({ slots: [], folders: [] })
    expect(localStorage.getItem('mc-history-height')).toBe('240')
  })

  it('honours a stored height inside 120..800 and sizes the open pane with it', () => {
    for (const stored of ['120', '400', '800']) {
      localStorage.clear()
      localStorage.setItem('mc-history-height', stored)
      window.history.replaceState({}, '', '/chat?history=1')
      const view = renderSidebar({ slots: [], folders: [] })
      expect(localStorage.getItem('mc-history-height')).toBe(stored)
      expect(scroller()?.style.height).toBe(`${stored}px`)
      view.unmount()
    }
  })

  it('ignores an out-of-range or unreadable stored height and writes the default back', () => {
    for (const stored of ['119', '801', 'tall']) {
      localStorage.clear()
      localStorage.setItem('mc-history-height', stored)
      window.history.replaceState({}, '', '/chat?history=1')
      const view = renderSidebar({ slots: [], folders: [] })
      expect(localStorage.getItem('mc-history-height')).toBe('240')
      expect(scroller()?.style.height).toBe('240px')
      view.unmount()
    }
  })
})

describe('mc-sidebar-width', () => {
  const separator = () => screen.getByRole('separator', { name: 'Resize sidebar' })

  it('defaults to 260 and honours a stored width inside the sidebar range', () => {
    const cases: Array<[string | null, string]> = [
      [null, '260'], ['180', '180'], ['1400', '1400'], ['400', '400'],
      ['179', '260'], ['1401', '260'], ['wide', '260'],
    ]
    for (const [stored, expected] of cases) {
      localStorage.clear()
      if (stored !== null) localStorage.setItem('mc-sidebar-width', stored)
      const view = renderSidebar({ slots: [], folders: [] })
      expect(separator()).toHaveAttribute('aria-valuenow', expected)
      view.unmount()
    }
  })

  it('persists a keyboard nudge at once, clamped to the range', () => {
    const view = renderSidebar({ slots: [], folders: [] })
    fireEvent.keyDown(separator(), { key: 'ArrowRight' })
    expect(localStorage.getItem('mc-sidebar-width')).toBe('276')
    fireEvent.keyDown(separator(), { key: 'ArrowRight', shiftKey: true })
    expect(localStorage.getItem('mc-sidebar-width')).toBe('340')
    fireEvent.keyDown(separator(), { key: 'ArrowLeft' })
    expect(localStorage.getItem('mc-sidebar-width')).toBe('324')
    expect(separator()).toHaveAttribute('aria-valuenow', '324')
    view.unmount()

    localStorage.setItem('mc-sidebar-width', '180')
    const narrow = renderSidebar({ slots: [], folders: [] })
    fireEvent.keyDown(separator(), { key: 'ArrowLeft' })
    expect(localStorage.getItem('mc-sidebar-width')).toBe('180')
    narrow.unmount()

    localStorage.setItem('mc-sidebar-width', '1400')
    renderSidebar({ slots: [], folders: [] })
    fireEvent.keyDown(separator(), { key: 'ArrowRight', shiftKey: true })
    expect(localStorage.getItem('mc-sidebar-width')).toBe('1400')
  })

  describe('the board auto-widen', () => {
    let innerWidth: PropertyDescriptor | undefined
    beforeEach(() => {
      innerWidth = Object.getOwnPropertyDescriptor(window, 'innerWidth')
      // Wide enough that four 220px lanes fit beside the chat pane: the widen
      // lands on exactly the lanes' width, 4 * 220 + 3 * 8 + 16.
      Object.defineProperty(window, 'innerWidth', { configurable: true, writable: true, value: 2000 })
    })
    afterEach(() => {
      if (innerWidth) Object.defineProperty(window, 'innerWidth', innerWidth)
    })

    it('remembers the prior width before widening for seeded lanes, and gives it back on leaving board view', async () => {
      // The board's column list is live: each created lane is what the next read returns.
      const live: Array<Record<string, unknown>> = []
      mocks.tagColumns.mockImplementation(async () => live.map(c => ({ ...c })))
      mocks.createTagColumn.mockImplementation(async (body: Record<string, unknown>) => {
        const col = { id: `lane-${live.length}`, name: '', order: live.length, ...body }
        live.push(col)
        return col
      })
      const setItem = vi.spyOn(Storage.prototype, 'setItem')
      renderSidebar({ slots: [{ key: 'k-a', title: 'A', messages: 1, running: false }], folders: [] })
      const openHeaderMenu = () => fireEvent.keyDown(screen.getAllByLabelText('More options')[0], { key: 'Enter' })

      openHeaderMenu()
      fireEvent.click(await screen.findByText('Switch to board view'))
      await waitFor(() => expect(localStorage.getItem('mc-sidebar-width')).toBe('920'))
      expect(localStorage.getItem('mc-sidebar-width-pre-board')).toBe('260')
      // The prior width is recorded BEFORE the widened one is persisted.
      const writes = setItem.mock.calls.map(([k, v]) => `${k}=${v}`)
      expect(writes.indexOf('mc-sidebar-width-pre-board=260')).toBeGreaterThanOrEqual(0)
      expect(writes.indexOf('mc-sidebar-width-pre-board=260')).toBeLessThan(writes.indexOf('mc-sidebar-width=920'))
      expect(separator()).toHaveAttribute('aria-valuenow', '920')

      openHeaderMenu()
      const menu = await screen.findByRole('menu')
      fireEvent.click(await within(menu).findByText('Switch to list view'))
      expect(localStorage.getItem('mc-sidebar-width')).toBe('260')
      expect(localStorage.getItem('mc-sidebar-width-pre-board')).toBe('')
      expect(separator()).toHaveAttribute('aria-valuenow', '260')
    })
  })
})
