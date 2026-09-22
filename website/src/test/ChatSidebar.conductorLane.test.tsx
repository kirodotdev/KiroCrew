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
) {
  mocks.folders = folders
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null, slotStatusDetail: {}, subagents: {}, slotActivity: {}, revealRequest, ...chatExtra } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], folders)
  const utils = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots as never} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return { ...utils, store }
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

  it('is COLLAPSED by default: only roots render', () => {
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    const { getByTestId } = renderSidebar()
    expect(laneRows(getByTestId('conductor-view-lane'))).toEqual(['k-conductor'])
  })

  it('expanding shows the direct children only, not the whole subtree', () => {
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    const { getByTestId } = renderSidebar()
    fireEvent.click(getByTestId('conductor-chevron-k-conductor'))
    // k-deep is behind Worker A's own chevron, still closed.
    expect(laneRows(getByTestId('conductor-view-lane'))).toEqual(['k-conductor', 'k-worker-a', 'k-worker-b'])
  })

  it('expands to three levels, one chevron at a time', () => {
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    const { getByTestId } = renderSidebar()
    fireEvent.click(getByTestId('conductor-chevron-k-conductor'))
    fireEvent.click(getByTestId('conductor-chevron-k-worker-a'))
    const lane = getByTestId('conductor-view-lane')
    expect(laneRows(lane)).toEqual(['k-conductor', 'k-worker-a', 'k-deep', 'k-worker-b'])
    // Depth is on the row wrapper, which is what drives the indentation.
    const deep = lane.querySelector('[data-slot-key="k-deep"]')!.closest('[data-conductor-depth]')
    expect(deep?.getAttribute('data-conductor-depth')).toBe('2')
  })

  it('persists the expanded set', () => {
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    const first = renderSidebar()
    fireEvent.click(first.getByTestId('conductor-chevron-k-conductor'))
    expect(JSON.parse(localStorage.getItem('mc-sidebar-conductor-expanded') ?? '[]')).toContain('k-conductor')
    first.unmount()

    const second = renderSidebar()
    expect(laneRows(second.getByTestId('conductor-view-lane'))).toContain('k-worker-a')
  })

  it('a collapsed conductor shows its child count', () => {
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    const { getByTestId } = renderSidebar()
    // Two DIRECT children; the count is the chevron's subject, not the subtree size.
    expect(getByTestId('conductor-child-count-k-conductor').textContent).toBe('2')
  })

  it('a collapsed conductor bubbles its subtree needs-you and running counts', () => {
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    const { getByTestId } = renderSidebar()
    // k-deep needs input (two levels down) and k-worker-a is running.
    expect(getByTestId('conductor-needs-you-k-conductor').textContent).toBe('1')
    expect(getByTestId('conductor-running-k-conductor').textContent).toBe('1')
  })

  it('stops bubbling once expanded, so no session is counted twice on screen', () => {
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    localStorage.setItem('mc-sidebar-conductor-expanded', JSON.stringify(['k-conductor', 'k-worker-a']))
    const { queryByTestId } = renderSidebar()
    expect(queryByTestId('conductor-needs-you-k-conductor')).toBeNull()
    expect(queryByTestId('conductor-running-k-conductor')).toBeNull()
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
    fireEvent.click(within(lane).getByTestId('conductor-chevron-k-conductor'))
    // Three distinct cards once the conductor is open -- the collision cost none.
    expect(laneRows(getByTestId('conductor-view-lane')).length).toBe(3)
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
    localStorage.setItem('mc-sidebar-conductor-expanded', JSON.stringify(chain.map(r => r.key)))

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
    expect(laneRows(getByTestId('conductor-view-lane'))).toEqual(['k-new', 'k-old'])
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
    expect(laneRows(lane())).toEqual(['k-conductor'])
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
