/**
 * Chat sidebar -- the filter menu's "Group by kind" option. Off by default, and
 * then the list is exactly as before. On, each list splits into Crew Mode / Normal
 * / From Mates sections; pinned rows, folders and the filters keep working.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent, screen, waitFor } from '@testing-library/react'
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
import { makeKindOf } from '../pages/chat-sidebar/kindSections'
import { sectionSeat } from '../pages/chat-sidebar/hoverHold'
import type { Slot } from '../pages/chat-sidebar/types'
import type { RootState } from '../store'

const GROUP_BY_KIND = 'mc-session-group-by-kind'
const T = (h: number) => `2026-10-29T${String(h).padStart(2, '0')}:00:00Z`

/** One of each kind, interleaved by recency so the default list mixes them. */
const MIXED = [
  { key: 'k-n1', title: 'normal one', running: false, messages: 3, last_turn_ts: T(10), last_ts: T(10) },
  { key: 'k-root', title: 'crew chat', running: false, messages: 3, crew_mode_root: true, last_turn_ts: T(9), last_ts: T(9) },
  { key: 'k-m1', title: 'mate chat', running: false, messages: 3, agent_kind: 'member', last_turn_ts: T(8), last_ts: T(8) },
  { key: 'k-n2', title: 'normal two', running: false, messages: 3, last_turn_ts: T(7), last_ts: T(7) },
  // Opened by the crewmate's chat: from crewmates too.
  { key: 'k-m2', title: 'mate child', running: false, messages: 3, parent: { slot: 'k-m1', key: 'k-m1' }, last_turn_ts: T(6), last_ts: T(6) },
  // A Crew Mode worker whose root is listed: never a row, sections or not.
  { key: 'k-worker', title: 'crew worker', running: false, messages: 3, crew_root: 'k-root', last_turn_ts: T(11), last_ts: T(11) },
]

function renderSidebar(slots: object[], folders: object[] = [], extra: Record<string, unknown> = {}) {
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
  qc.setQueryData(['chat-folders'], folders)
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots as never} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
              {...extra}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

async function openFilterMenu() {
  fireEvent.keyDown(screen.getByRole('button', { name: 'Sort and filter sessions' }), { key: 'Enter' })
  await screen.findByRole('menu')
}

/** The list as drawn, in order: section headers as `# label`, rows by title. */
function drawn(container: HTMLElement, within?: Element | null): string[] {
  const root = within ?? container
  const out: string[] = []
  root.querySelectorAll('[data-kind-section], [data-session-row]').forEach(el => {
    if (el.hasAttribute('data-kind-section')) { out.push(`# ${el.textContent}`); return }
    const title = ['normal one', 'normal two', 'crew chat', 'mate chat', 'mate child', 'crew worker', 'pinned mate', 'filed crew', 'filed normal']
      .find(t => el.textContent?.includes(t))
    if (title && out[out.length - 1] !== title) out.push(title)
  })
  return out
}

beforeEach(() => {
  localStorage.clear()
  // The fixtures carry fixed dates; with the dormant collapse on they would fold
  // away once the clock passes them. This suite is about sections, not age.
  localStorage.setItem('mc-session-stale-collapse-ms', '0')
})
afterEach(() => vi.clearAllMocks())

describe('Group by kind', () => {
  it('off (the default): the list is unchanged, mixed in recency order, with no headers', async () => {
    const { container } = renderSidebar(MIXED)
    await waitFor(() => expect(screen.queryByText('normal one')).not.toBeNull())
    expect(drawn(container)).toEqual(['normal one', 'crew chat', 'mate chat', 'normal two', 'mate child'])
    expect(screen.queryAllByTestId('kind-section-header')).toHaveLength(0)
  })

  it('on: three sections with the right members, each in list order; workers stay out', async () => {
    localStorage.setItem(GROUP_BY_KIND, '1')
    const { container } = renderSidebar(MIXED)
    await waitFor(() => expect(screen.queryByText('normal one')).not.toBeNull())
    expect(drawn(container)).toEqual([
      '# Crew Mode', 'crew chat',
      '# Normal', 'normal one', 'normal two',
      '# From Mates', 'mate chat', 'mate child',
    ])
    expect(screen.queryByText('crew worker')).toBeNull()
    // Headers are headings, so assistive tech can jump between sections.
    expect(screen.getAllByRole('heading', { level: 3 }).map(h => h.textContent)).toEqual(['Crew Mode', 'Normal', 'From Mates'])
  })

  it('the menu row toggles it, reports its state, and the choice persists across a remount', async () => {
    const first = renderSidebar(MIXED)
    await waitFor(() => expect(screen.queryByText('normal one')).not.toBeNull())
    await openFilterMenu()
    const row = screen.getByTestId('filter-group-by-kind')
    expect(row).toHaveAttribute('role', 'menuitemcheckbox')
    expect(row).toHaveAttribute('aria-checked', 'false')
    fireEvent.click(row)
    await waitFor(() => expect(screen.getByTestId('filter-group-by-kind')).toHaveAttribute('aria-checked', 'true'))
    expect(localStorage.getItem(GROUP_BY_KIND)).toBe('1')
    await waitFor(() => expect(screen.queryAllByTestId('kind-section-header').length).toBeGreaterThan(0))
    first.unmount()
    renderSidebar(MIXED)
    await waitFor(() => expect(screen.queryAllByTestId('kind-section-header')).toHaveLength(3))
    await openFilterMenu()
    fireEvent.click(screen.getByTestId('filter-group-by-kind'))
    await waitFor(() => expect(screen.queryAllByTestId('kind-section-header')).toHaveLength(0))
    expect(localStorage.getItem(GROUP_BY_KIND)).toBe('0')
  })

  it('keeps pinned rows in the pinned band above the sections', async () => {
    localStorage.setItem(GROUP_BY_KIND, '1')
    const slots = [
      ...MIXED,
      { key: 'k-pin', title: 'pinned mate', running: false, messages: 3, agent_kind: 'member', pinned: true, last_turn_ts: T(1), last_ts: T(1) },
    ]
    const { container } = renderSidebar(slots)
    await waitFor(() => expect(screen.queryByText('pinned mate')).not.toBeNull())
    const order = drawn(container)
    expect(order[0]).toBe('pinned mate')
    expect(order.slice(1)).toEqual([
      '# Crew Mode', 'crew chat',
      '# Normal', 'normal one', 'normal two',
      '# From Mates', 'mate chat', 'mate child',
    ])
  })

  it('works with the existing filters: the Crew Mode filter leaves one kind, so no headers', async () => {
    localStorage.setItem(GROUP_BY_KIND, '1')
    localStorage.setItem('mc-session-crew-only', '1')
    const { container } = renderSidebar(MIXED)
    await waitFor(() => expect(screen.queryByText('crew chat')).not.toBeNull())
    expect(drawn(container)).toEqual(['crew chat'])
    expect(screen.queryAllByTestId('kind-section-header')).toHaveLength(0)
  })

  it('leaves folders as they are and splits a mixed folder\'s rows inside it', async () => {
    localStorage.setItem(GROUP_BY_KIND, '1')
    const folders = [{ id: 'f-1', name: 'Work', collapsed: false, order: 0 }]
    const slots = [
      { key: 'k-fn', title: 'filed normal', running: false, messages: 3, folder_id: 'f-1', last_turn_ts: T(5), last_ts: T(5) },
      { key: 'k-fc', title: 'filed crew', running: false, messages: 3, folder_id: 'f-1', crew_mode_root: true, last_turn_ts: T(4), last_ts: T(4) },
      { key: 'k-n1', title: 'normal one', running: false, messages: 3, last_turn_ts: T(3), last_ts: T(3) },
    ]
    const { container } = renderSidebar(slots, folders)
    await waitFor(() => expect(screen.queryByText('filed crew')).not.toBeNull())
    // The folder keeps both its rows, now in sections; the unfiled list holds one
    // kind, so it is drawn without a header.
    expect(drawn(container)).toEqual(['# Crew Mode', 'filed crew', '# Normal', 'filed normal', 'normal one'])
  })

  it('splits the flat lane too, folders exploded as usual', async () => {
    localStorage.setItem(GROUP_BY_KIND, '1')
    localStorage.setItem('mc-sidebar-lane', 'flat')
    const folders = [{ id: 'f-1', name: 'Work', collapsed: false, order: 0 }]
    const slots = [
      { key: 'k-fn', title: 'filed normal', running: false, messages: 3, folder_id: 'f-1', last_turn_ts: T(5), last_ts: T(5) },
      { key: 'k-fc', title: 'filed crew', running: false, messages: 3, folder_id: 'f-1', crew_mode_root: true, last_turn_ts: T(4), last_ts: T(4) },
      { key: 'k-m1', title: 'mate chat', running: false, messages: 3, agent_kind: 'member', last_turn_ts: T(3), last_ts: T(3) },
    ]
    const { container } = renderSidebar(slots, folders)
    const lane = await screen.findByTestId('flat-view-lane')
    await waitFor(() => expect(screen.queryByText('filed crew')).not.toBeNull())
    expect(drawn(container, lane)).toEqual(['# Crew Mode', 'filed crew', '# Normal', 'filed normal', '# From Mates', 'mate chat'])
  })
})

describe('Group by kind and sleeping sessions', () => {
  it('splits the expanded sleeping rows too, with their own headings', async () => {
    localStorage.setItem(GROUP_BY_KIND, '1')
    // A one-day dormant threshold, and rows dated long before any clock this runs on.
    localStorage.setItem('mc-session-stale-collapse-ms', String(24 * 3600 * 1000))
    const OLD = (d: number) => `2020-01-0${d}T00:00:00Z`
    const slots = [
      { key: 'k-n1', title: 'normal one', running: false, messages: 3, last_turn_ts: OLD(5), last_ts: OLD(5) },
      { key: 'k-root', title: 'crew chat', running: false, messages: 3, crew_mode_root: true, last_turn_ts: OLD(4), last_ts: OLD(4) },
      { key: 'k-m1', title: 'mate chat', running: false, messages: 3, agent_kind: 'member', last_turn_ts: OLD(3), last_ts: OLD(3) },
      { key: 'k-n2', title: 'normal two', running: false, messages: 3, last_turn_ts: OLD(2), last_ts: OLD(2) },
    ]
    const { container } = renderSidebar(slots)
    // The expander re-renders as the sidebar's reads settle, so each try clicks the
    // node on screen now; a click on a replaced node would be lost.
    await waitFor(() => {
      const exp = screen.getByTestId('stale-expander-root')
      if (exp.getAttribute('aria-expanded') !== 'true') fireEvent.click(exp)
      expect(screen.getByTestId('stale-expander-root')).toHaveAttribute('aria-expanded', 'true')
    })
    await waitFor(() => expect(screen.queryByText('crew chat')).not.toBeNull())
    const region = container.querySelector('[data-stale-region="root"]')
    expect(drawn(container, region)).toEqual([
      '# Crew Mode', 'crew chat',
      '# Normal', 'normal one', 'normal two',
      '# From Mates', 'mate chat',
    ])
  })
})

describe('Group by kind and the roster', () => {
  it('reads a kind-less crewmate chat from the roster kept across a session switch', async () => {
    localStorage.setItem(GROUP_BY_KIND, '1')
    const slots = [
      { key: 'k-n1', title: 'normal one', running: false, messages: 3, last_turn_ts: T(10), last_ts: T(10) },
      // No agent_kind: a crewmate chat only the roster can name.
      { key: 'k-m1', title: 'mate chat', running: false, messages: 3, agent: 'ava', last_turn_ts: T(9), last_ts: T(9) },
    ]
    // A session switch empties `installedAgents`; `rowAgents` keeps the last roster.
    const { container } = renderSidebar(slots, [], { rowAgents: [{ name: 'ava', source: 'builtin', selection_kind: 'member' }] })
    await waitFor(() => expect(screen.queryByText('mate chat')).not.toBeNull())
    expect(drawn(container)).toEqual(['# Normal', 'normal one', '# From Mates', 'mate chat'])
  })
})

describe('Group by kind in the conductor lane', () => {
  it('splits the top-level rows and keeps a pinned root first, unsplit', async () => {
    localStorage.setItem(GROUP_BY_KIND, '1')
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    const slots = [
      { key: 'k-pin', title: 'pinned mate', running: false, messages: 3, agent_kind: 'member', pinned: true, last_turn_ts: T(1), last_ts: T(1) },
      { key: 'k-n1', title: 'normal one', running: false, messages: 3, last_turn_ts: T(10), last_ts: T(10) },
      { key: 'k-root', title: 'crew chat', running: false, messages: 3, crew_mode_root: true, last_turn_ts: T(9), last_ts: T(9) },
      { key: 'k-m1', title: 'mate chat', running: false, messages: 3, agent_kind: 'member', last_turn_ts: T(8), last_ts: T(8) },
      // A child gives the lane its lineage; collapsed under its creator by default.
      { key: 'k-n2', title: 'normal two', running: false, messages: 3, parent: { slot: 'k-n1', key: 'k-n1' }, last_turn_ts: T(7), last_ts: T(7) },
    ]
    const { container } = renderSidebar(slots)
    const lane = await screen.findByTestId('conductor-view-lane')
    await waitFor(() => expect(screen.queryByText('pinned mate')).not.toBeNull())
    expect(drawn(container, lane)).toEqual([
      'pinned mate',
      '# Crew Mode', 'crew chat',
      '# Normal', 'normal one',
      '# From Mates', 'mate chat',
    ])
  })
})

describe('makeKindOf', () => {
  const s = (o: object) => ({ running: false, ...o }) as Slot
  it('reads a worker whose root closed as Crew Mode, a peer row by its own stamps only', () => {
    const live = [s({ key: 'mate', agent_kind: 'member' }), s({ key: 'kid', parent: { key: 'mate' } })]
    const kindOf = makeKindOf(live)
    expect(kindOf(s({ key: 'w', crew_root: 'gone' }))).toBe('crew')
    expect(kindOf(live[1])).toBe('mate')
    expect(kindOf(s({ key: 'grandkid', parent: { key: 'kid' } }))).toBe('mate')
    expect(kindOf(s({ key: 'p', peer_id: 'peer', parent: { key: 'mate' } }))).toBe('normal')
    // A kind-less chat is a crewmate's when the roster lists its agent as one
    // (the sidebar's shared `isCrewmateChat` rule); a peer row is not.
    const roster = [{ name: 'ava', selection_kind: 'member' }]
    expect(makeKindOf([], roster)(s({ key: 'old', agent: 'ava' }))).toBe('mate')
    expect(makeKindOf([], roster)(s({ key: 'peer', agent: 'ava', peer_id: 'x' }))).toBe('normal')
    // A cycle in the creator chain ends the walk.
    const loop = makeKindOf([s({ key: 'a', parent: { key: 'b' } }), s({ key: 'b', parent: { key: 'a' } })])
    expect(loop(s({ key: 'a', parent: { key: 'b' } }))).toBe('normal')
  })
})

describe('sectionSeat (the hover hold under Group by kind)', () => {
  const row = (key: string) => ({ key, running: false }) as Slot
  const id = (r: Slot) => r.key
  const rankOf = (r: Slot) => ({ c: 1, n: 2, m: 3 } as Record<string, number>)[r.key[0]]
  // Pointer on n1, captured under drawn headers: Crew [c1] Normal [n1, n2].
  // Row heights 36px; the section headers are not in the anchor at all.
  const pin = {
    key: 'n1', scope: 'list', container: 'tree:root', seenOrder: ['c1', 'n1', 'n2'],
    heights: { c1: 36, n1: 36, n2: 36 }, headerPxAbove: 0, sectionHeaderPxAbove: 0, headerH: 24, staleSide: false,
  }

  it('does not move the hovered row when the list is unchanged', () => {
    const list = ['c1', 'n1', 'n2'].map(row)
    // The held row's index in the list without it: c1 first, then n1.
    expect(sectionSeat(pin, list, id, rankOf)).toBe(1)
  })

  it('keeps the row at the top of its own section when a row of another kind arrives above', () => {
    const list = ['c2', 'c1', 'n1', 'n2'].map(row)
    expect(sectionSeat(pin, list, id, rankOf)).toBe(2)
  })

  it('does not count a captured row of another section that has left the list', () => {
    // c1 closed and c2 arrived: the Normal section is unchanged, so n1 stays put.
    const list = ['c2', 'n1', 'n2'].map(row)
    const drawnRank = (k: string) => (k === 'c1' ? 1 : undefined)
    expect(sectionSeat(pin, list, id, rankOf, undefined, drawnRank)).toBe(1)
  })

  it('holds the row in place inside its section when a same-kind row sorts in above it', () => {
    const list = ['c1', 'n0', 'n1', 'n2'].map(row)
    // n0 is new; n1 keeps its captured pixel offset, so it takes n0's seat.
    expect(sectionSeat(pin, list, id, rankOf)).toBe(1)
  })
})
