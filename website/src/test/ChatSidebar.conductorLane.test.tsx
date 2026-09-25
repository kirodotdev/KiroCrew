/**
 * Chat sidebar — conductor lane: sessions nested under the session that OPENED them.
 *
 * The lane's whole value is that a conductor and its workers read as one unit of work,
 * so the properties pinned here are the ones that make it that: collapsed by default
 * (fifteen rows must not become the default view of one job), a collapsed row carrying
 * its subtree's badges (otherwise collapsing HIDES the thing you need to act on), and
 * a reveal opening the rows above its target (otherwise revealing a nested session
 * scrolls to nothing).
 *
 * The toggle's cycle and the localStorage migration are here too, because both are
 * promises to a user who already had a lane preference before this existed.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, fireEvent, render, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

// Render framer-motion elements as plain DOM (jsdom can't run projection).
//
// `animate` is stripped like every other framer prop EXCEPT for its opacity, which is
// applied as inline style. Real Motion writes its animation targets to the element's
// style, so an `animate` opacity OUTRANKS an opacity utility class on the same element;
// a mock that drops `animate` shows the class winning and cannot see that the pixels
// disagree. Opacity is the one property this lane decides a row's meaning with -- a
// dimmed row is context rather than a match -- so it is the one the mock reproduces.
vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'layoutScroll', 'initial', 'animate', 'exit',
    'transition', 'variants', 'whileHover', 'whileTap', 'whileInView',
    'drag', 'dragConstraints', 'dragElastic', 'onAnimationComplete',
  ])
  const animatedOpacity = (animate: unknown): number | undefined => {
    if (!animate || typeof animate !== 'object' || Array.isArray(animate)) return undefined
    const value = (animate as Record<string, unknown>).opacity
    return typeof value === 'number' ? value : undefined
  }
  const make = (tag: string) =>
    React.forwardRef((props: Record<string, unknown>, ref: React.Ref<unknown>) => {
      const clean: Record<string, unknown> = {}
      for (const k of Object.keys(props)) {
        if (k === 'children') continue
        if (k === 'layoutId') { clean['data-layout-id'] = props[k]; continue }
        if (FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      const opacity = animatedOpacity(props.animate)
      if (opacity !== undefined) {
        clean.style = { ...(props.style as object | undefined), opacity }
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

// `chatSlots` is a STABLE spy, unlike the proxy's per-access `vi.fn()`: the
// provisional-lineage test asserts on whether the sidebar came back for a second
// read, which a fresh mock per property access cannot record.
const mocks = vi.hoisted(() => ({ folders: [] as unknown[], chatSlots: vi.fn() }))

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, p: string) => {
      if (p === 'chatFolders') return vi.fn().mockImplementation(() => Promise.resolve(mocks.folders))
      if (p === 'chatSlots') return mocks.chatSlots
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

type TestSlot = Record<string, unknown>

/**
 * A conductor with two workers, one of which opened a worker of its own — the
 * three-level case this gateway produces for real.
 *
 *   k-conductor
 *     k-worker-a
 *       k-deep
 *     k-worker-b
 */
const NESTED: TestSlot[] = [
  { key: 'k-conductor', title: 'Conductor', messages: 1, running: false, modified: 4000 },
  { key: 'k-worker-a', title: 'Worker A', messages: 1, running: true, modified: 3000, parent: { slot: 'k-conductor', key: 'k-conductor' } },
  { key: 'k-deep', title: 'Deep worker', messages: 1, running: false, needs_input: true, modified: 2000, parent: { slot: 'k-worker-a', key: 'k-worker-a' } },
  { key: 'k-worker-b', title: 'Worker B', messages: 1, running: false, modified: 1000, parent: { slot: 'k-conductor', key: 'k-conductor' } },
]

function renderSidebar(
  slots: TestSlot[] = NESTED,
  folders: unknown[] = [],
  revealRequest: { kind: string; target: string } | null = null,
  chatExtra: Record<string, unknown> = {},
  unreadSlots: string[] = [],
) {
  mocks.folders = folders
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots, updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null, slotStatusDetail: {}, subagents: {}, slotActivity: {}, revealRequest, ...chatExtra } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], folders)
  const tree = (rows: TestSlot[]) => (
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={rows as never} activeSlot={null} unreadSlots={unreadSlots as never}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>
  )
  const utils = render(tree(slots))
  /** The NEXT slots frame, the way the broadcast delivers one: same mounted lane, new
   *  rows. What a re-render cannot stand in for is exactly what the adoption tests
   *  need, since the lane tells a move from a creation by comparing two frames. */
  const pushFrame = (rows: TestSlot[]) => utils.rerender(tree(rows))
  return { ...utils, store, pushFrame }
}

/** Row keys in the conductor lane, in render order. */
function laneRows(lane: HTMLElement): string[] {
  return Array.from(lane.querySelectorAll('[data-slot-key]')).map(el => el.getAttribute('data-slot-key') ?? '')
}

beforeEach(() => {
  localStorage.clear()
  localStorage.setItem('mc-session-stale-collapse-ms', '0')
  // A default, so a test that sets its own resolved value cannot leak it into the next.
  mocks.chatSlots.mockReset()
  mocks.chatSlots.mockResolvedValue([])
})
afterEach(() => {
  vi.clearAllMocks()
  vi.useRealTimers()
})

describe('chat sidebar — conductor lane', () => {
  it('is not the default lane: the tree renders and the conductor lane does not', () => {
    const { queryByTestId } = renderSidebar()
    expect(queryByTestId('conductor-view-lane')).toBeNull()
  })

  it('the toggle appears with no folders when there is lineage to show', () => {
    // Pre-existing rule was "no folders, nothing to flatten, hide the button". The
    // conductor lane is a different axis, so lineage alone now earns the button.
    const { getByTestId } = renderSidebar()
    expect(getByTestId('flat-view-toggle')).toBeTruthy()
  })

  it('stays hidden when there is neither a folder nor an edge', () => {
    const { queryByTestId } = renderSidebar([
      { key: 'k-a', title: 'Alone', messages: 1, running: false, modified: 1000 },
    ])
    expect(queryByTestId('flat-view-toggle')).toBeNull()
  })

  it('one press of the toggle enters the conductor lane', () => {
    const { getByTestId } = renderSidebar()
    fireEvent.click(getByTestId('flat-view-toggle'))
    expect(getByTestId('conductor-view-lane')).toBeTruthy()
    expect(localStorage.getItem('mc-sidebar-lane')).toBe('conductor')
  })

  it('cycles tree -> conductor -> flat -> tree when both lanes are available', () => {
    const folders = [{ id: 'f1', name: 'Alpha', order: 0 }]
    const { getByTestId, queryByTestId } = renderSidebar(NESTED, folders)
    const toggle = () => getByTestId('flat-view-toggle')
    fireEvent.click(toggle())
    expect(queryByTestId('conductor-view-lane')).toBeTruthy()
    fireEvent.click(toggle())
    expect(queryByTestId('conductor-view-lane')).toBeNull()
    expect(queryByTestId('flat-view-lane')).toBeTruthy()
    fireEvent.click(toggle())
    expect(queryByTestId('flat-view-lane')).toBeNull()
    expect(localStorage.getItem('mc-sidebar-lane')).toBe('tree')
  })

  it('skips the flat lane in the cycle when there are no folders to flatten', () => {
    const { getByTestId, queryByTestId } = renderSidebar()
    const toggle = () => getByTestId('flat-view-toggle')
    fireEvent.click(toggle())
    expect(queryByTestId('conductor-view-lane')).toBeTruthy()
    fireEvent.click(toggle())
    // Straight back to the tree: a flat lane with no folders renders the same list.
    expect(queryByTestId('conductor-view-lane')).toBeNull()
    expect(queryByTestId('flat-view-lane')).toBeNull()
  })

  it('migrates a stored flat-view boolean to the flat lane', () => {
    localStorage.setItem('mc-sidebar-flat-view', '1')
    const folders = [{ id: 'f1', name: 'Alpha', order: 0 }]
    const { getByTestId } = renderSidebar(NESTED, folders)
    expect(getByTestId('flat-view-lane')).toBeTruthy()
  })

  it('opens in the conductor lane when that is the stored preference', () => {
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    const { getByTestId } = renderSidebar()
    expect(getByTestId('conductor-view-lane')).toBeTruthy()
  })

  it('is EXPANDED by default: the whole tree renders, like the System page', () => {
    // The lane exists to show the tree the System page's Sessions tab shows, and that
    // one arrives open. A conductor whose workers sit behind a chevron the user has to
    // find is a different view of the same gateway.
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    const { getByTestId } = renderSidebar()
    expect(laneRows(getByTestId('conductor-view-lane')))
      .toEqual(['k-conductor', 'k-worker-a', 'k-deep', 'k-worker-b'])
  })

  it('collapsing hides that row\u2019s subtree and nothing else', () => {
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    const { getByTestId } = renderSidebar()
    fireEvent.click(getByTestId('conductor-chevron-k-worker-a'))
    // Only Worker A's branch folds; Worker B is a sibling and stays.
    expect(laneRows(getByTestId('conductor-view-lane')))
      .toEqual(['k-conductor', 'k-worker-a', 'k-worker-b'])
  })

  it('renders three levels open, with depth on the row', () => {
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    const { getByTestId } = renderSidebar()
    const lane = getByTestId('conductor-view-lane')
    expect(laneRows(lane)).toEqual(['k-conductor', 'k-worker-a', 'k-deep', 'k-worker-b'])
    // Depth is on the row wrapper, which is what drives the indentation.
    const deep = lane.querySelector('[data-slot-key="k-deep"]')!.closest('[data-conductor-depth]')
    expect(deep?.getAttribute('data-conductor-depth')).toBe('2')
  })

  it('persists the collapsed set', () => {
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    const first = renderSidebar()
    fireEvent.click(first.getByTestId('conductor-chevron-k-conductor'))
    expect(JSON.parse(localStorage.getItem('mc-sidebar-conductor-collapsed') ?? '[]')).toContain('k-conductor')
    first.unmount()

    const second = renderSidebar()
    expect(laneRows(second.getByTestId('conductor-view-lane'))).toEqual(['k-conductor'])
  })

  it('a collapsed conductor shows its child count', () => {
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    localStorage.setItem('mc-sidebar-conductor-collapsed', JSON.stringify(['k-conductor']))
    const { getByTestId } = renderSidebar()
    // Two DIRECT children; the count is the chevron's subject, not the subtree size.
    expect(getByTestId('conductor-child-count-k-conductor').textContent).toBe('2')
  })

  it('a collapsed conductor bubbles its subtree needs-you and running counts', () => {
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    localStorage.setItem('mc-sidebar-conductor-collapsed', JSON.stringify(['k-conductor']))
    const { getByTestId } = renderSidebar()
    // k-deep needs input (two levels down) and k-worker-a is running.
    expect(getByTestId('conductor-needs-you-k-conductor').textContent).toBe('1')
    expect(getByTestId('conductor-running-k-conductor').textContent).toBe('1')
  })

  it('stops bubbling once expanded, so no session is counted twice on screen', () => {
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    const { queryByTestId } = renderSidebar()
    expect(queryByTestId('conductor-needs-you-k-conductor')).toBeNull()
    expect(queryByTestId('conductor-running-k-conductor')).toBeNull()
  })

  it('keeps a filtered-out conductor as the anchor its workers hang from', () => {
    /* The Unread filter admits the two workers and not the conductor that opened them.
       Built from the filtered list the tree had no row with that key, so each worker
       resolved no parent and rendered as a top-level orphan -- a conductor's workers
       scattered across the lane while the System page nested all of them. */
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    localStorage.setItem('mc-session-unread-only', '1')
    const { getByTestId } = renderSidebar(NESTED, [], null, {}, ['k-worker-a', 'k-worker-b'])
    const lane = getByTestId('conductor-view-lane')

    expect(laneRows(lane)).toEqual(['k-conductor', 'k-worker-a', 'k-worker-b'])
    const worker = lane.querySelector('[data-slot-key="k-worker-a"]')!.closest('[data-conductor-depth]')
    expect(worker?.getAttribute('data-conductor-depth')).toBe('1')
    // k-deep is not unread and nothing under it is, so it is not kept at all.
    expect(lane.querySelector('[data-slot-key="k-deep"]')).toBeNull()
  })

  it('dims the anchor, because it is context rather than a match', () => {
    /* The RENDERED opacity, not the marker attribute and not a utility class. The row is
       a motion element whose animation target includes opacity, and Motion writes that
       target to the element's own style -- so an opacity class on the same row loses to
       it and the anchor paints at full strength. The marker attribute would still be
       there, which is what makes an attribute-only assertion agree with a lane whose
       every row looks identical. Dimming is the ONE signal separating a row kept for
       context from a row the filter admitted, so it is asserted where a person sees it. */
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    localStorage.setItem('mc-session-unread-only', '1')
    const { getByTestId } = renderSidebar(NESTED, [], null, {}, ['k-worker-a'])
    const lane = getByTestId('conductor-view-lane')
    const anchor = lane.querySelector('[data-slot-key="k-conductor"]')!.closest('[data-conductor-depth]')
    expect(anchor?.getAttribute('data-conductor-anchor')).toBe('true')
    expect((anchor as HTMLElement).style.opacity).toBe('0.55')
    const match = lane.querySelector('[data-slot-key="k-worker-a"]')!.closest('[data-conductor-depth]')
    expect(match?.getAttribute('data-conductor-anchor')).toBeNull()
    expect((match as HTMLElement).style.opacity).toBe('1')
  })

  it('keeps a collapsed conductor folded when a filter matches only its children', () => {
    /* The two controls compose rather than override each other: the filter decides WHICH
       rows the lane may show, the fold decides how much of the kept tree is on screen.
       So a conductor the user closed stays closed, and the match beneath it stays behind
       the chevron -- with the row carrying that subtree's kept-child count and its
       needs-you aggregate, which is what says something is in there.

       Pinned because nothing else does: without it the intended answer here is only
       implied, and the alternative -- a filter-scoped "effective expanded" that overrides
       the persisted set -- would put a second source of truth on the fold and make the
       chevron a no-op while a filter is live. */
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    localStorage.setItem('mc-session-unread-only', '1')
    localStorage.setItem('mc-sidebar-conductor-collapsed', JSON.stringify(['k-conductor']))
    const { getByTestId } = renderSidebar(NESTED, [], null, {}, ['k-worker-a'])
    const lane = getByTestId('conductor-view-lane')

    // The conductor is the only row: its matching child is folded away, not promoted.
    expect(laneRows(lane)).toEqual(['k-conductor'])
    const row = lane.querySelector('[data-slot-key="k-conductor"]')!.closest('[data-conductor-depth]')
    expect(row?.getAttribute('data-conductor-anchor')).toBe('true')
    // The count is over the KEPT subtree, so it says one thing is in there, not three.
    expect(getByTestId('conductor-child-count-k-conductor').textContent).toBe('1')
  })

  it('drops the superseded expanded-set key rather than leaving it in storage', () => {
    /* The old key held the opposite sense and no reading of it produces the new one, so
       the honest migration is to remove it and take the new default. Left behind it would
       outlive every build that understands it. */
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    localStorage.setItem('mc-sidebar-conductor-expanded', JSON.stringify(['k-conductor']))
    const { getByTestId } = renderSidebar()

    expect(getByTestId('conductor-view-lane')).toBeTruthy()
    expect(localStorage.getItem('mc-sidebar-conductor-expanded')).toBeNull()
  })

  it('opens the destination when a row the FILTER excludes is adopted', () => {
    /* The row is on screen as a dimmed anchor, so the no-disappearance promise covers it
       -- but it is absent from the filtered set. Tracking citation changes over the
       filtered set alone missed exactly these rows: the anchor moves under a conductor the
       user had collapsed, nothing opens the destination, and its whole subtree leaves the
       screen with no signal. Tracked over every row instead. */
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    localStorage.setItem('mc-session-unread-only', '1')
    localStorage.setItem('mc-sidebar-conductor-collapsed', JSON.stringify(['k-new']))
    const before = [
      { key: 'k-new', title: 'New conductor', messages: 1, running: false, modified: 5000 },
      ...NESTED,
    ]
    // Only `k-worker-a` is unread, so `k-conductor` renders as an anchor: on screen, and
    // outside the filtered set the tracker used to read.
    const { getByTestId, pushFrame } = renderSidebar(before as never, [], null, {}, ['k-worker-a'])
    expect(laneRows(getByTestId('conductor-view-lane'))).toContain('k-conductor')

    pushFrame(before.map(row =>
      row.key === 'k-conductor' ? { ...row, parent: { slot: 'k-new', key: 'k-new' } } : row,
    ) as never)

    // The destination was opened, so the moved anchor is still on screen rather than
    // folded away inside a row the person never touched.
    expect(JSON.parse(localStorage.getItem('mc-sidebar-conductor-collapsed') ?? '[]')).not.toContain('k-new')
    expect(laneRows(getByTestId('conductor-view-lane'))).toContain('k-conductor')
  })

  it('keeps an intermediate worker as an anchor so a deep match still nests', () => {
    /* Two levels of anchor: only the deepest row matches, and both rows above it are
       needed for it to render where it belongs. */
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    localStorage.setItem('mc-session-unread-only', '1')
    const { getByTestId } = renderSidebar(NESTED, [], null, {}, ['k-deep'])
    const lane = getByTestId('conductor-view-lane')

    expect(laneRows(lane)).toEqual(['k-conductor', 'k-worker-a', 'k-deep'])
    const deep = lane.querySelector('[data-slot-key="k-deep"]')!.closest('[data-conductor-depth]')
    expect(deep?.getAttribute('data-conductor-depth')).toBe('2')
  })

  it('keeps the lane available when a filter hides every row that carries a creator', () => {
    /* `lineageAvailable` gates the whole lane, so computed from the filtered list a
       filter admitting only parentless rows took the conductor view away entirely --
       the nesting appeared and vanished with no control touched that says so. */
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    localStorage.setItem('mc-session-unread-only', '1')
    const { getByTestId } = renderSidebar(NESTED, [], null, {}, ['k-conductor'])
    expect(getByTestId('conductor-view-lane')).toBeTruthy()
    expect(laneRows(getByTestId('conductor-view-lane'))).toEqual(['k-conductor'])
  })

  it('gives a member DM conductor a row of its own, so its workers nest under it', () => {
    /* A crew member's thread and a cron's session are creators like any other, and the
       System page lists both as parents. A lane that had no row for them left every
       worker they opened at the top level with an orphan mark. */
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    const { getByTestId } = renderSidebar([
      { key: 'member-pipeline', title: 'Pipeline Work', messages: 1, running: false, modified: 3000 },
      { key: 'cron-nightly', title: 'Nightly sweep', messages: 1, running: false, modified: 2500 },
      { key: 'k-worker', title: 'Worker', messages: 1, running: false, modified: 2000, parent: { slot: 'member-pipeline', key: 'member-pipeline' } },
      { key: 'k-cron-kid', title: 'Cron worker', messages: 1, running: false, modified: 1000, parent: { slot: 'cron-nightly', key: 'cron-nightly' } },
    ])
    const lane = getByTestId('conductor-view-lane')
    expect(laneRows(lane)).toEqual(['member-pipeline', 'k-worker', 'cron-nightly', 'k-cron-kid'])
    const worker = lane.querySelector('[data-slot-key="k-worker"]')!.closest('[data-conductor-depth]')
    expect(worker?.getAttribute('data-conductor-depth')).toBe('1')
    const cronKid = lane.querySelector('[data-slot-key="k-cron-kid"]')!.closest('[data-conductor-depth]')
    expect(cronKid?.getAttribute('data-conductor-depth')).toBe('1')
  })

  it('marks an orphan as a root that still names the session that opened it', () => {
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    const { getByTestId } = renderSidebar([
      { key: 'k-conductor', title: 'Conductor', messages: 1, running: false, modified: 2000 },
      // Creator cited but not running: key is null.
      { key: 'k-orphan', title: 'Orphaned worker', messages: 1, running: false, modified: 1000, parent: { slot: 'k-gone', key: null } },
    ])
    const lane = getByTestId('conductor-view-lane')
    expect(laneRows(lane)).toEqual(['k-conductor', 'k-orphan'])
    // The creator's name rides the tooltip, not a line of visible text: the row is one
    // fixed-height card and a stacked line under it is what the session-row rule
    // forbids. The indicator itself is a Lucide icon in the existing badge cluster, not
    // a hand-authored glyph whose shape depends on the platform's fonts.
    const hint = within(lane).getByTestId('conductor-orphan-k-orphan')
    expect(hint.getAttribute('title')).toContain('k-gone')
    expect(hint.getAttribute('data-orphan-of')).toBe('k-gone')
    expect(hint.querySelector('svg')).toBeTruthy()
    expect(hint.textContent).toBe('')
  })

  it('nests an adopted session under its new parent, and its children with it', () => {
    /* The takeover, seen from the renderer. The payload's `parent` is whatever the
       backend fold decided -- an adoption changes that value and nothing else -- so the
       lane needs no new code for it, and this is the test that says so. `k-conductor`
       and its whole branch move under `k-new`, which no row under it has to mention. */
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    const adopted = [
      { key: 'k-new', title: 'New conductor', messages: 1, running: false, modified: 5000 },
      ...NESTED.map(row =>
        row.key === 'k-conductor' ? { ...row, parent: { slot: 'k-new', key: 'k-new' } } : row,
      ),
    ]
    const { getByTestId } = renderSidebar(adopted as never)
    const lane = getByTestId('conductor-view-lane')
    expect(laneRows(lane)).toEqual(['k-new', 'k-conductor', 'k-worker-a', 'k-deep', 'k-worker-b'])
    const moved = lane
      .querySelector('[data-slot-key="k-worker-a"]')!
      .closest('[data-conductor-depth]')
    expect(moved?.getAttribute('data-conductor-depth')).toBe('2')
  })

  it('returns a released session to the top level, keeping what hangs under it', () => {
    /* The release, which is the only thing that takes an edge away. `k-worker-a` becomes
       a root and `k-deep` stays under it: only its own edge upward went. Root order is
       the payload's, which the lane inherits rather than deciding. */
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    const released = NESTED.map(row =>
      row.key === 'k-worker-a' ? { ...row, parent: null } : row,
    )
    const { getByTestId } = renderSidebar(released as never)
    const lane = getByTestId('conductor-view-lane')
    expect(laneRows(lane)).toEqual(['k-conductor', 'k-worker-b', 'k-worker-a', 'k-deep'])
    const root = lane.querySelector('[data-slot-key="k-worker-a"]')!.closest('[data-conductor-depth]')
    expect(root?.getAttribute('data-conductor-depth')).toBe('0')
    expect(within(lane).queryByTestId('conductor-orphan-k-worker-a')).toBeNull()
  })

  it('opens the new parent when a row MOVES there, so an adoption never hides its own result', () => {
    /* A session the person folded away stays folded; one that moves on its own must
       not disappear into it. `k-new` is COLLAPSED here, then an adoption re-parents
       `k-conductor` under it. Without the expand the row and its whole branch unmount
       on an action the person did not take, leaving a child count where their sessions
       were. The assertion is the row still being THERE on the second frame. */
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    localStorage.setItem('mc-sidebar-conductor-collapsed', JSON.stringify(['k-new', 'k-conductor']))
    const before = [
      { key: 'k-new', title: 'New conductor', messages: 1, running: false, modified: 5000 },
      ...NESTED,
    ]
    const { getByTestId, pushFrame } = renderSidebar(before as never)
    expect(laneRows(getByTestId('conductor-view-lane'))).toEqual(['k-new', 'k-conductor'])

    pushFrame(before.map(row =>
      row.key === 'k-conductor' ? { ...row, parent: { slot: 'k-new', key: 'k-new' } } : row,
    ) as never)

    const lane = getByTestId('conductor-view-lane')
    expect(laneRows(lane)).toEqual(['k-new', 'k-conductor'])
    const moved = lane.querySelector('[data-slot-key="k-conductor"]')!.closest('[data-conductor-depth]')
    expect(moved?.getAttribute('data-conductor-depth')).toBe('1')
  })

  it('opens the new parent on the FIRST adoption, even though the lane was suppressed before it', () => {
    /* The saved conductor view renders nothing until some row carries a parent, so on a
       gateway whose tree is not seeded yet the lane is suppressed. Clearing the citation
       map on those frames looked harmless and was not: the FIRST adoption is the frame
       that both activates the lane and carries the move, so with no baseline behind it the
       row reads as newly created -- so the collapse the person set on `k-new` stands and
       the session they were looking at is replaced by a child count on an action they did
       not take. The baseline has to predate the move, so the bookkeeping cannot be gated
       on the lane. */
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    localStorage.setItem('mc-sidebar-conductor-collapsed', JSON.stringify(['k-new']))
    const flat = [
      { key: 'k-new', title: 'New conductor', messages: 1, running: false, modified: 5000 },
      { key: 'k-conductor', title: 'Conductor', messages: 2, running: false, modified: 4000 },
    ]
    const { getByTestId, queryByTestId, pushFrame } = renderSidebar(flat as never)
    // Suppressed: no row cites a creator, so the lane has nothing to nest.
    expect(queryByTestId('conductor-view-lane')).toBeNull()

    pushFrame(flat.map(row =>
      row.key === 'k-conductor' ? { ...row, parent: { slot: 'k-new', key: 'k-new' } } : row,
    ) as never)

    const lane = getByTestId('conductor-view-lane')
    expect(laneRows(lane)).toEqual(['k-new', 'k-conductor'])
    const moved = lane.querySelector('[data-slot-key="k-conductor"]')!.closest('[data-conductor-depth]')
    expect(moved?.getAttribute('data-conductor-depth')).toBe('1')
  })

  it('keeps a row\u2019s baseline while a filter hides it, so a later adoption still opens', () => {
    /* `flatSlots` is search- and folder-filtered, so a row the current filter excludes is
       absent from a frame without having gone anywhere. Rebuilding the citation map from
       that frame alone evicts its baseline, and the adoption that lands while the filter is
       active is then read as a creation once the filter clears -- so the collapse on
       `k-new` stands and the row that moved is hidden. Same failure as the suppressed-lane
       case above, by a different route, so the map carries forward instead of being
       replaced. */
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    localStorage.setItem('mc-sidebar-conductor-collapsed', JSON.stringify(['k-new']))
    const flat = [
      { key: 'k-new', title: 'New conductor', messages: 1, running: false, modified: 5000 },
      { key: 'k-conductor', title: 'Conductor', messages: 2, running: false, modified: 4000 },
    ]
    const { getByTestId, pushFrame } = renderSidebar(flat as never)

    // A filter that excludes the row about to move: it is absent, not gone.
    pushFrame([flat[0]] as never)
    // The filter clears on the same frame that carries the adoption.
    pushFrame(flat.map(row =>
      row.key === 'k-conductor' ? { ...row, parent: { slot: 'k-new', key: 'k-new' } } : row,
    ) as never)

    const lane = getByTestId('conductor-view-lane')
    expect(laneRows(lane)).toEqual(['k-new', 'k-conductor'])
    const moved = lane.querySelector('[data-slot-key="k-conductor"]')!.closest('[data-conductor-depth]')
    expect(moved?.getAttribute('data-conductor-depth')).toBe('1')
  })

  it('respects a collapse when a NEW session is created under it', () => {
    /* The mirror, and the reason the lane compares citations rather than counting rows.
       A row absent from the last frame was just opened -- `session_create` -- so a
       conductor the person folded away keeps its fourteen new workers folded with it.
       Only a row that was ALREADY listed under one creator and now names another was
       moved by something other than the person looking at it. */
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    localStorage.setItem('mc-sidebar-conductor-collapsed', JSON.stringify(['k-conductor']))
    const { getByTestId, pushFrame } = renderSidebar()
    expect(laneRows(getByTestId('conductor-view-lane'))).toEqual(['k-conductor'])

    pushFrame([
      ...NESTED,
      { key: 'k-fresh', title: 'Fresh worker', messages: 1, running: false, modified: 500, parent: { slot: 'k-conductor', key: 'k-conductor' } },
    ] as never)

    const lane = getByTestId('conductor-view-lane')
    expect(laneRows(lane)).toEqual(['k-conductor'])
    expect(lane.querySelector('[data-slot-key="k-fresh"]')).toBeNull()
  })

  it('opens nothing when a row is RELEASED, because it lands at the top level', () => {
    /* A release clears the citation, so the row moves to where nothing is collapsed in
       front of it. Opening the creator it just left would re-show the branch the person
       detached it from, which is the opposite of what they asked for. */
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    localStorage.setItem('mc-sidebar-conductor-collapsed', JSON.stringify(['k-conductor']))
    const { getByTestId, pushFrame } = renderSidebar()
    expect(laneRows(getByTestId('conductor-view-lane'))).toEqual(['k-conductor'])

    pushFrame(NESTED.map(row => (row.key === 'k-worker-a' ? { ...row, parent: null } : row)) as never)

    // `k-worker-a` is a root now, so it shows; `k-conductor` stays folded, untouched.
    expect(laneRows(getByTestId('conductor-view-lane'))).toEqual(['k-conductor', 'k-worker-a', 'k-deep'])
    expect(JSON.parse(localStorage.getItem('mc-sidebar-conductor-collapsed') ?? '[]')).toEqual(['k-conductor'])
  })

  it('does not mark a released session as an orphan: the two mean different things', () => {
    /* The orphan marker means "the session that opened this one is gone" -- the row
       still CITES a creator it cannot nest under. A release clears the citation itself,
       so there is nothing to mark, and conflating them would tell the person a session
       they deliberately detached had lost its opener. */
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    const mixed = [
      { key: 'k-root', title: 'Root', messages: 1, running: false, modified: 4000 },
      { key: 'k-released', title: 'Released worker', messages: 1, running: false, modified: 3000, parent: null },
      { key: 'k-orphan', title: 'Orphaned worker', messages: 1, running: false, modified: 2000, parent: { slot: 'k-gone', key: null } },
    ]
    const lane = renderSidebar(mixed as never).getByTestId('conductor-view-lane')
    expect(laneRows(lane)).toEqual(['k-root', 'k-released', 'k-orphan'])
    expect(within(lane).queryByTestId('conductor-orphan-k-released')).toBeNull()
    const hint = within(lane).getByTestId('conductor-orphan-k-orphan')
    expect(hint.getAttribute('data-orphan-of')).toBe('k-gone')
  })

  it('names the lane the next press opens, never the lane in view', () => {
    // The button is the feature's only entry point and every user meets it on every
    // press, so copy naming the CURRENT lane misdirects all of them -- and a screen
    // reader user has nothing else to go on.
    const { getByTestId } = renderSidebar()
    const fromTree = getByTestId('flat-view-toggle')
    expect(fromTree.getAttribute('data-lane')).toBe('tree')
    expect(fromTree.getAttribute('data-next-lane')).toBe('conductor')
    expect(fromTree.getAttribute('aria-label')).toBe('Switch to conductor view (sessions nested under the session that opened them)')
    expect(fromTree.getAttribute('title')).toBe(fromTree.getAttribute('aria-label'))

    // One press later the lane IS conductor, and with no folders the cycle returns to
    // the tree -- so the copy must now offer the tree, not the lane being left.
    fireEvent.click(fromTree)
    const fromConductor = getByTestId('flat-view-toggle')
    expect(fromConductor.getAttribute('data-lane')).toBe('conductor')
    expect(fromConductor.getAttribute('data-next-lane')).toBe('tree')
    expect(fromConductor.getAttribute('aria-label')).toBe('Switch to folder view')
  })

  it('keeps a peer row and a local row that share a slot key as two rows', () => {
    // Local and federated gateways do not share a slot-key namespace: deterministic
    // keys collide. Keyed on the raw key, the peer row replaces the local one -- the
    // local session disappears from the lane and the peer renders twice.
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    const { getByTestId } = renderSidebar([
      { key: 'k-conductor', title: 'Local conductor', messages: 1, running: false, modified: 3000 },
      { key: 'k-worker', title: 'Local worker', messages: 1, running: false, modified: 2000, parent: { slot: 'k-conductor', key: 'k-conductor' } },
      { key: 'k-worker', title: 'Peer worker', messages: 1, running: false, modified: 1000, peer_id: 'peer-1', row_identity: 'peer-1:k-worker', parent: { slot: 'k-conductor', key: 'k-conductor' } },
    ])
    const lane = getByTestId('conductor-view-lane')
    // The conductor owns its LOCAL child and only that one.
    expect(within(lane).getByTestId('conductor-child-count-k-conductor').textContent).toBe('1')
    // The peer row is top-level: its citation names a slot on the PEER's gateway, so
    // it must not resolve to a local session whose key merely matches.
    expect(within(lane).queryByTestId('conductor-orphan-peer-1:k-worker')).toBeTruthy()
    // Three distinct cards -- the collision cost none.
    expect(laneRows(lane).length).toBe(3)
  })

  it('caps the indent past six levels and names the level in the tooltip', () => {
    // Depth has no ceiling and each level costs 14px, so a deep chain would walk the
    // card off a 320px sidebar. Past the cap the rows stop stepping and the level is
    // carried as a number -- whose tooltip must read the DEPTH, not a placeholder: the
    // string interpolates `{{depth}}`, so passing any other name renders it literally.
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    const chain: TestSlot[] = Array.from({ length: 9 }, (_, i) => ({
      key: `lvl-${i}`,
      title: `Level ${i}`,
      messages: 1,
      running: false,
      modified: 9000 - i,
      ...(i === 0 ? {} : { parent: { slot: `lvl-${i - 1}`, key: `lvl-${i - 1}` } }),
    }))

    const { getByTestId, queryByTestId } = renderSidebar(chain)
    const lane = getByTestId('conductor-view-lane')

    // Within the cap there is no level badge: the indentation itself says the depth.
    expect(queryByTestId('conductor-depth-lvl-3')).toBeNull()

    const deep = within(lane).getByTestId('conductor-depth-lvl-8')
    // Carries a middot, not a bare digit: this badge sits in the same cluster as the
    // child and aggregate counts, and a lone "8" there reads as one more count.
    expect(deep.textContent).toBe('\u00b78')
    expect(deep.getAttribute('title')).toContain('8')
    expect(deep.getAttribute('title')).not.toContain('{{')

    // The indent stops rather than continuing to step right. It is carried by a
    // spacer INSIDE the row -- so the row's own divider still spans the full width at
    // every depth -- and the spacer's width is what the cap bounds.
    const indentAt = (d: number) => {
      const row = lane.querySelector(`[data-conductor-depth="${d}"]`)
      const spacer = row?.querySelector('[data-conductor-indent]') as HTMLElement | null
      return spacer?.style.width ?? null
    }
    expect(indentAt(3)).toBe('42px')
    expect(indentAt(6)).toBe('84px')
    expect(indentAt(8)).toBe('84px')
  })

  it('says so, without erroring, when nothing has opened anything', () => {
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    const { getByTestId } = renderSidebar([
      { key: 'k-a', title: 'One', messages: 1, running: false, modified: 2000 },
      { key: 'k-b', title: 'Two', messages: 1, running: false, modified: 1000, parent: { slot: 'k-gone', key: null } },
    ])
    const lane = getByTestId('conductor-view-lane')
    // Every session still renders; only the nesting is absent.
    expect(laneRows(lane)).toEqual(['k-a', 'k-b'])
    expect(getByTestId('conductor-lane-empty-note')).toBeTruthy()
  })

  it('falls back to the tree when NO row carries a creator, rather than stranding', () => {
    // The sibling case above has rows citing a creator that has closed: lineage IS
    // available, the lane stays, and the note explains the flat result. With no creator
    // anywhere the lane is not available at all -- and since the cycle then holds `tree`
    // alone the toggle is not drawn, so rendering the lane would leave the user in a
    // layout with no control to leave it. It falls back instead, and returns by itself
    // the moment any row carries a creator again.
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    const { queryByTestId } = renderSidebar([
      { key: 'k-a', title: 'One', messages: 1, running: false, modified: 2000 },
      { key: 'k-b', title: 'Two', messages: 1, running: false, modified: 1000 },
    ])
    expect(queryByTestId('conductor-view-lane')).toBeNull()
    expect(queryByTestId('conductor-lane-empty-note')).toBeNull()
    // Nothing is lost: the sessions render, in the tree, and no lane control is offered
    // because there is only one lane to be in.
    expect(queryByTestId('flat-view-toggle')).toBeNull()
  })

  it('pairs each aggregate count with the glyph its children show, not a tint alone', () => {
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    localStorage.setItem('mc-sidebar-conductor-collapsed', JSON.stringify(['k-conductor']))
    const { getByTestId } = renderSidebar()
    // Same collapsed root as the bubbling test above: one child needs input, one runs.
    const needsYou = getByTestId('conductor-needs-you-k-conductor')
    const running = getByTestId('conductor-running-k-conductor')
    // Two counts that differ only by background colour are indistinguishable to a
    // colour-blind reader and identical in a high-contrast theme, so each carries the
    // icon its child rows already show for that state.
    expect(needsYou.querySelector('svg')).toBeTruthy()
    expect(running.querySelector('svg')).toBeTruthy()
    // The number itself is unchanged: the glyph is added beside it, not instead of it.
    expect(needsYou.textContent).toBe('1')
    expect(running.textContent).toBe('1')
    // The child count stays plain: it has no per-child glyph to echo.
    expect(getByTestId('conductor-child-count-k-conductor').querySelector('svg')).toBeNull()
  })

  it('counts a child by the same running signal its own row is drawn from', () => {
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    // This child is NOT running its own turn; a live workflow is what makes it active.
    // Its own row shows the running state, so the collapsed parent must agree: an
    // aggregate that disagrees with the glyphs it stands for is worse than no aggregate.
    localStorage.setItem('mc-sidebar-conductor-collapsed', JSON.stringify(['k-root']))
    const { getByTestId, queryByTestId } = renderSidebar(
      [
        { key: 'k-root', title: 'Conductor', messages: 1, running: false, modified: 3000 },
        { key: 'k-wf', title: 'Workflow child', messages: 1, running: false, modified: 2000, parent: { slot: 'k-root', key: 'k-root' } },
      ],
      [],
      null,
      { workflowRuns: { 'r-1': { run_id: 'r-1', name: 'build', status: 'running', sessionKey: 'k-wf', phase: '' } } },
    )
    const running = queryByTestId('conductor-running-k-root')
    expect(running).not.toBeNull()
    expect(running!.textContent).toBe('1')
    expect(getByTestId('conductor-child-count-k-root').textContent).toBe('1')
  })

  it('advances from the rendered lane when the stored lane is unavailable', () => {
    // Stored flat, but there are no folders, so the flat lane cannot render and is not
    // in the cycle. Indexing the stored lane directly gives -1, whose successor is
    // position 0 -- tree, which is exactly what is already on screen, so the press would
    // change the button's icon and nothing else. It must offer the conductor lane, the
    // one thing a press can visibly change here.
    localStorage.setItem('mc-sidebar-lane', 'flat')
    const { getByTestId } = renderSidebar()
    const toggle = getByTestId('flat-view-toggle')
    const says = `${toggle.getAttribute('title') ?? ''} ${toggle.getAttribute('aria-label') ?? ''}`
    expect(says.toLowerCase()).toContain('conductor')
  })

  it('refetches while lineage is provisional, then nests once the seed lands', async () => {
    // A cold start ships `parent: null` with `lineage_pending`, because the gateway's
    // projection is still seeding and it deliberately does not broadcast when it lands.
    // On an IDLE gateway no further frame is coming, so the sidebar has to come back for
    // the real answer or it stays unnested until the user acts.
    vi.useFakeTimers()
    try {
      localStorage.setItem('mc-sidebar-lane', 'conductor')
      const { queryByTestId } = renderSidebar([
        { key: 'k-root', title: 'Conductor', messages: 1, running: false, modified: 2000, parent: null, lineage_pending: true },
        { key: 'k-kid', title: 'Worker', messages: 1, running: false, modified: 1000, parent: null, lineage_pending: true },
      ])
      // Provisional and flat: no edges in this frame, so the lane is not offered yet.
      expect(queryByTestId('conductor-view-lane')).toBeNull()
      expect(mocks.chatSlots).not.toHaveBeenCalled()

      // The seed lands; the next read carries the edge.
      mocks.chatSlots.mockResolvedValue([
        { key: 'k-root', title: 'Conductor', messages: 1, running: false, modified: 2000, parent: null },
        { key: 'k-kid', title: 'Worker', messages: 1, running: false, modified: 1000, parent: { slot: 'k-root', key: 'k-root' } },
      ])
      await act(async () => { await vi.advanceTimersByTimeAsync(2100) })
      expect(mocks.chatSlots).toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
    }
  })

  it('keeps root order the same as the flat lane', () => {
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    const { getByTestId } = renderSidebar([
      { key: 'k-old', title: 'Older root', messages: 1, running: false, modified: 1000 },
      { key: 'k-new', title: 'Newer root', messages: 1, running: false, modified: 3000 },
      { key: 'k-kid', title: 'A child', messages: 1, running: false, modified: 2000, parent: { slot: 'k-old', key: 'k-old' } },
    ])
    // Default sort is date-desc, so the newer root leads — exactly as the flat lane
    // would order the same two rows.
    expect(laneRows(getByTestId('conductor-view-lane'))).toEqual(['k-new', 'k-old', 'k-kid'])
  })

  it('a childless row gets no chevron', () => {
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    const { queryByTestId } = renderSidebar([
      { key: 'k-a', title: 'One', messages: 1, running: false, modified: 2000 },
      { key: 'k-b', title: 'Two', messages: 1, running: false, modified: 1000, parent: { slot: 'k-a', key: 'k-a' } },
    ])
    expect(queryByTestId('conductor-chevron-k-b')).toBeNull()
  })

  it('a reveal of a nested session opens every row above it', () => {
    // The reveal effect runs on mount against a pending request, which is how the
    // "jump to this session" affordance arrives. Without ancestor expansion the
    // target is behind two collapsed chevrons and the scroll lands on nothing.
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    const { getByTestId } = renderSidebar(NESTED, [], { kind: 'session', target: 'k-deep' })
    expect(laneRows(getByTestId('conductor-view-lane'))).toContain('k-deep')
  })

  it('a search flattens the lane so a nested match is never hidden behind a chevron', () => {
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    const { getByTestId, getByPlaceholderText } = renderSidebar()
    const lane = () => getByTestId('conductor-view-lane')
    expect(laneRows(lane())).toEqual(['k-conductor', 'k-worker-a', 'k-deep', 'k-worker-b'])
    const search = getByPlaceholderText(/search/i)
    fireEvent.change(search, { target: { value: 'Deep' } })
    // The match is two levels down and renders without any expanding.
    expect(laneRows(lane())).toEqual(['k-deep'])
  })

  it('marks a flattened match that has a creator, so it does not read as a root', () => {
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    const { getByTestId, queryByTestId, getByPlaceholderText } = renderSidebar([
      { key: 'k-root', title: 'Pipeline conductor', messages: 1, running: false, modified: 3000 },
      { key: 'k-kid', title: 'Pipeline worker', messages: 1, running: false, modified: 2000, parent: { slot: 'k-root', key: 'k-root' } },
    ])
    // Nested, the indent says who opened what, so the row needs no marker.
    expect(queryByTestId('conductor-cites-parent-k-kid')).toBeNull()

    fireEvent.change(getByPlaceholderText(/search/i), { target: { value: 'Pipeline' } })
    const marker = getByTestId('conductor-cites-parent-k-kid')
    expect(marker.getAttribute('data-cites-parent')).toBe('k-root')
    // NOT the closed-creator copy: that creator is open, it is only not above this row
    // while the lane is flattened, and one tooltip for both facts would be a lie.
    expect(marker.getAttribute('title') ?? '').not.toMatch(/closed/i)
    expect(queryByTestId('conductor-orphan-k-kid')).toBeNull()
  })
})
