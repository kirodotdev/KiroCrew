/**
 * Every lane that renders session rows honours the person's folder hide.
 *
 * The filter menu's folder checkboxes are the person saying "do not show me this
 * folder". That is a stronger statement than the status, tag and search chips: those
 * decide what a lane is ABOUT, so a row they exclude can still appear as a dimmed
 * context anchor, while a folder the person unchecked must put no session row on
 * screen at all. The one sanctioned way back in is the reveal row at the bottom of a
 * container, which is the person asking to look.
 *
 * The lanes are ENUMERATED OUT OF THE SOURCE rather than listed here, because the way
 * this defect comes back is a lane nobody has written yet. A new member of the
 * `SidebarLane` union with no entry in `LANE_COVERAGE` fails this file before it can
 * ship, which a hand-written list of today's lanes would not do.
 *
 * Both directions are asserted for every lane. "Hidden stays hidden" alone would pass
 * for an implementation that filtered every folder away, so each lane must also prove
 * that a folder the person did NOT hide still renders its sessions.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { render, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

const { boardColumns } = vi.hoisted(() => ({
  boardColumns: [
    {
      id: 'col-idle', name: '', tag_ids: [] as string[], mode: 'any' as const,
      order: 0, source: 'state' as const, state_key: 'idle' as const,
    },
  ],
}))

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
  loadChatConfig: () => ({ tagColumnsEnabled: true, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, prop: string) => {
      if (prop === 'tagColumns') return () => Promise.resolve(boardColumns)
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
import type { ChatFolder, ChatSlot } from '../types'

// ── the lane universe, read out of the component's own source ────────────────

const SIDEBAR_SRC = readFileSync(join(__dirname, '..', 'pages', 'ChatSidebar.tsx'), 'utf8')

/** The `SidebarLane` union's members, in declaration order. */
function declaredLanes(src: string): string[] {
  const m = /^type SidebarLane =([^\n]+)$/m.exec(src)
  if (!m) return []
  return m[1].split('|').map(s => s.trim().replace(/^'(.*)'$/, '$1')).filter(Boolean)
}

const DECLARED_LANES = declaredLanes(SIDEBAR_SRC)

/**
 * How to put each lane on screen.
 *
 * `board` and `board-flat` are deliberately here without being `SidebarLane`s: columns
 * are a separate axis that PREEMPTS the union's choice whenever any are configured, so
 * the two renderers that axis produces still have to answer for themselves. They are
 * genuinely different renderers, not one with a flag: the board draws folder blocks per
 * column, and its flat mode draws the column's rows whole with no folder block at all.
 */
const LANE_COVERAGE: Record<string, { lanePref: string; columns: boolean }> = {
  tree: { lanePref: 'tree', columns: false },
  flat: { lanePref: 'flat', columns: false },
  conductor: { lanePref: 'conductor', columns: false },
  board: { lanePref: 'tree', columns: true },
  'board-flat': { lanePref: 'flat', columns: true },
}

// ── fixture ─────────────────────────────────────────────────────────────────

const HIDDEN_FOLDER = 'folder-hidden'
const SHOWN_FOLDER = 'folder-shown'
/** A folder NESTED under a visible one, so hiding it exercises the recursive paths a
 *  root-only filter never reaches. */
const NESTED_FOLDER = 'folder-nested'

const FOLDERS: ChatFolder[] = [
  { id: HIDDEN_FOLDER, name: 'hidden folder', collapsed: false, order: 0 },
  { id: SHOWN_FOLDER, name: 'shown folder', collapsed: false, order: 1 },
  { id: NESTED_FOLDER, name: 'nested folder', collapsed: false, order: 2, parent_id: SHOWN_FOLDER },
] as unknown as ChatFolder[]

/**
 * A conductor inside the hidden folder with a child outside it.
 *
 * This shape is what makes the conductor lane's anchor rule reachable at all: the
 * child matches the filter, so the lane wants the ancestor it hangs from, and that
 * ancestor is exactly the row the person hid.
 */
const SLOTS: ChatSlot[] = [
  { key: 'k-hidden-conductor', title: 'Hidden Conductor', running: false, messages: 2, modified: 4000, folder_id: HIDDEN_FOLDER },
  { key: 'k-shown-child', title: 'Shown Child', running: false, messages: 2, modified: 3000, folder_id: SHOWN_FOLDER, parent: { slot: 'k-hidden-conductor', key: 'k-hidden-conductor' } },
  { key: 'k-shown-plain', title: 'Shown Plain', running: false, messages: 2, modified: 2000, folder_id: SHOWN_FOLDER },
  { key: 'k-nested', title: 'Nested Session', running: false, messages: 2, modified: 1000, folder_id: NESTED_FOLDER },
] as unknown as ChatSlot[]

/** The same population with the one citation pointed at a session that is not there.
 *  The lane is still OFFERED (a row carries a creator) and its tree still has no edge,
 *  which is the note's own case and owes nothing to any hide. */
const NO_NESTING_SLOTS: ChatSlot[] = SLOTS.map(s =>
  (s as unknown as { key: string }).key === 'k-shown-child'
    ? { ...s, parent: { slot: 'k-absent', key: 'k-absent' } }
    : s,
) as unknown as ChatSlot[]

function renderSidebar(withColumns: boolean, slots: ChatSlot[] = SLOTS) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null, slotStatusDetail: {}, subagents: {}, slotActivity: {}, workflowRuns: {} } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity, refetchOnMount: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], FOLDERS)
  qc.setQueryData(['tag-columns'], withColumns ? boardColumns : [])
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

beforeEach(() => {
  localStorage.clear()
  // The stale collapse would fold settled rows behind an expander, which would make a
  // reverse assertion fail for a reason that has nothing to do with folder hiding.
  localStorage.setItem('mc-session-stale-collapse-ms', '0')
})
afterEach(() => vi.clearAllMocks())

/** Seed the persisted hidden-folder set the filter checkboxes write to. */
function hideFolders(...ids: string[]) {
  localStorage.setItem('mc-flat-hidden-folders', JSON.stringify(ids))
}

// ── the enumeration itself ──────────────────────────────────────────────────

describe('sidebar lanes honour the folder hide — enumerated from source', () => {
  it('parses the lane union out of the source', () => {
    // Control for the two assertions below: a regex that silently matched nothing
    // would make "every declared lane is covered" vacuously true, so the parse has to
    // prove it found the real union first.
    expect(DECLARED_LANES.length).toBeGreaterThanOrEqual(3)
    expect(DECLARED_LANES).toContain('conductor')
  })

  it('covers every lane the source declares', () => {
    for (const lane of DECLARED_LANES) {
      expect(
        Object.prototype.hasOwnProperty.call(LANE_COVERAGE, lane),
        `SidebarLane declares '${lane}' but this file never renders it. A lane that renders `
        + 'session rows has to prove it honours the folder hide; add it to LANE_COVERAGE.',
      ).toBe(true)
    }
  })

  it('still preempts the union with the board lane', () => {
    // Why `board` is in LANE_COVERAGE without being a SidebarLane. If this gate moves,
    // the board entry is either misnamed or no longer reachable, and the reader of this
    // file needs to know which.
    expect(SIDEBAR_SRC).toMatch(/conductorLaneActive\s*=\s*!boardLaneActive/)
  })

  it('still reaches the board lane flat mode through the flat pref', () => {
    // What makes the `board-flat` entry above a real renderer rather than a guess: the
    // board reads the same pref the flat lane does, so columns plus 'flat' is the mode
    // that renders a column's rows with no folder block. If this derivation changes,
    // that entry is photographing the wrong renderer.
    expect(SIDEBAR_SRC).toMatch(/const flatView\s*=\s*lane === 'flat'/)
  })
})

describe('what a lane derives from the concealed population', () => {
  it('counts the board column badge over the rows the column shows', () => {
    // The badge is the population's most silent reader: a count taken before the hide
    // says "3" over two rows and no lane draws anything explaining the gap, which reads
    // as a session that went missing rather than one the person put away.
    hideFolders(HIDDEN_FOLDER)
    localStorage.setItem('mc-sidebar-lane', 'tree')
    const { getByTestId } = renderSidebar(true)
    const column = getByTestId('column-col-idle')
    const rendered = column.querySelectorAll('[data-slot-key]').length
    expect(rendered).toBeGreaterThan(0)
    expect(getByTestId('column-count-col-idle').textContent).toBe(String(rendered))
  })

  it('withholds the conductor no-nesting note while the lane conceals a folder', () => {
    // With the hide taking away the only nesting, the note lands under live session rows
    // and contradicts them. The reveal row below it already says a folder is hidden,
    // which is the real reason there is nothing nested to draw.
    hideFolders(HIDDEN_FOLDER)
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    const { queryByTestId } = renderSidebar(false)
    expect(queryByTestId('conductor-lane-empty-note')).toBeNull()
  })

  it('still shows that note when nothing is concealed and nothing is nested', () => {
    // The control: the note is not simply gone. Its own case is a lane with sessions and
    // no lineage, which is what this fixture is once the nesting is removed.
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    const { queryByTestId } = renderSidebar(false, NO_NESTING_SLOTS)
    expect(queryByTestId('conductor-lane-empty-note')).toBeTruthy()
  })

  it('keeps the un-hide controls reachable while a board renders', async () => {
    // A hide the person cannot reverse from the view they are standing in is a trap, and
    // the board is the one view with no reveal row of its own. So the filter menu's
    // folder section has to be there while a board renders -- both the per-folder
    // checkbox and the control that clears the whole set.
    hideFolders(HIDDEN_FOLDER)
    localStorage.setItem('mc-sidebar-lane', 'tree')
    const { getByLabelText, findByTestId } = renderSidebar(true)
    fireEvent.keyDown(getByLabelText('Sort and filter sessions'), { key: 'Enter' })
    expect(await findByTestId(`folder-filter-${HIDDEN_FOLDER}`)).toBeTruthy()
    expect(await findByTestId('folder-filter-show-all')).toBeTruthy()
  })

  it('calls a concealed creator open rather than closed', () => {
    // The citation glyph has two readings and the concealed case must take the right one:
    // the creator here is running, so the closed-creator label would state something
    // false about a live session.
    hideFolders(HIDDEN_FOLDER)
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    const { container } = renderSidebar(false)
    const cites = container.querySelector('[data-cites-parent]')
    expect(cites?.getAttribute('data-cites-parent')).toBe('k-hidden-conductor')
    expect(container.querySelector('[data-orphan-of]')).toBeNull()
  })

  it('still calls a genuinely absent creator closed', () => {
    // The other direction, which is what stops the fix above from simply deleting orphan
    // detection: this fixture cites a session that is not in the population at all.
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    const { container } = renderSidebar(false, NO_NESTING_SLOTS)
    expect(container.querySelector('[data-orphan-of]')?.getAttribute('data-orphan-of')).toBe('k-absent')
    expect(container.querySelector('[data-cites-parent]')).toBeNull()
  })

  it('marks the filter button while the hide is withholding rows', () => {
    // In a board the funnel is the hide's only trace: no folder header means no reveal
    // row, so without a mark the rows are just absent and a reload keeps them absent.
    hideFolders(HIDDEN_FOLDER)
    localStorage.setItem('mc-sidebar-lane', 'tree')
    const { container } = renderSidebar(true)
    const funnel = container.querySelector('[data-folder-hide-active]')
    expect(funnel?.getAttribute('data-folder-hide-active')).toBe('1')
    expect(funnel?.getAttribute('title')).toMatch(/1 hidden/)
  })

  it('leaves the filter button unmarked when nothing is hidden', () => {
    // The other direction: a permanent mark would say a filter is active when none is,
    // which costs the mark its meaning.
    localStorage.setItem('mc-sidebar-lane', 'tree')
    const { container } = renderSidebar(true)
    expect(container.querySelector('[data-folder-hide-active]')).toBeNull()
  })
})

describe.each(Object.entries(LANE_COVERAGE))(
  'the %s lane',
  (laneName, setup) => {
    it('does not render a session whose folder the person hid', () => {
      hideFolders(HIDDEN_FOLDER)
      localStorage.setItem('mc-sidebar-lane', setup.lanePref)
      const { queryByText } = renderSidebar(setup.columns)
      expect(
        queryByText('Hidden Conductor'),
        `the ${laneName} lane rendered a session from a folder the person unchecked`,
      ).toBeNull()
    })

    it('renders sessions whose folder the person left alone', () => {
      // The other direction. Without it, filtering every folder away would pass.
      hideFolders(HIDDEN_FOLDER)
      localStorage.setItem('mc-sidebar-lane', setup.lanePref)
      const { getByText } = renderSidebar(setup.columns)
      expect(getByText('Shown Plain')).toBeTruthy()
      // The hidden conductor's child is filed OUTSIDE the hide, so concealing its
      // parent must not take it down too -- it stands on its own instead.
      expect(getByText('Shown Child')).toBeTruthy()
    })

    it('does not render a session whose NESTED folder the person hid', () => {
      // A root-only version of this filter passes every case above and still leaves a
      // hidden nested folder on screen, because the recursive paths build their own
      // population. So the hide is asserted one level down too, where the parent folder
      // stays visible and has to keep rendering its own sessions.
      hideFolders(NESTED_FOLDER)
      localStorage.setItem('mc-sidebar-lane', setup.lanePref)
      const { queryByText, getByText } = renderSidebar(setup.columns)
      expect(
        queryByText('Nested Session'),
        `the ${laneName} lane rendered a session from a nested folder the person unchecked`,
      ).toBeNull()
      // The HEADER as well, not only the sessions. Stripping the rows alone leaves the
      // bare name of a folder the person unchecked on screen, which is the same claim
      // failing more quietly -- and in a lane that draws folder headers it is the whole
      // visible difference once the body is empty.
      expect(
        queryByText('nested folder'),
        `the ${laneName} lane rendered the header of a nested folder the person unchecked`,
      ).toBeNull()
      expect(getByText('Shown Plain')).toBeTruthy()
    })

    it('renders every session when the person hid nothing', () => {
      // The control for the forward assertions: it proves both concealed rows are
      // renderable in this lane at all, so their absence above is the hide doing work
      // and not a fixture this lane was never going to show.
      localStorage.setItem('mc-sidebar-lane', setup.lanePref)
      const { getByText } = renderSidebar(setup.columns)
      expect(getByText('Hidden Conductor')).toBeTruthy()
      expect(getByText('Shown Child')).toBeTruthy()
      expect(getByText('Shown Plain')).toBeTruthy()
      expect(getByText('Nested Session')).toBeTruthy()
    })
  },
)
