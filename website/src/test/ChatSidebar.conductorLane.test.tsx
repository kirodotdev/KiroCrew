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
import { screen, waitFor } from '@testing-library/react'
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
const mocks = vi.hoisted(() => ({ folders: [] as unknown[], chatSlots: vi.fn(), navigate: vi.fn(), crewWorkersFail: false, crewWorkersNotServed: false, crewWorkersRows: null as unknown[] | null, roster: [] as unknown[], rosterFails: false, membersCalls: 0, lastMembers: null as Promise<unknown> | null }))

// The router is real (MemoryRouter below); only `useNavigate` is a spy, so a row that
// leaves the chat page can be asked WHERE it went rather than inferred from a route
// that this harness does not mount.
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom')
  return { ...actual, useNavigate: () => mocks.navigate }
})

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, p: string) => {
      if (p === 'chatFolders') return vi.fn().mockImplementation(() => Promise.resolve(mocks.folders))
      if (p === 'chatSlots') return mocks.chatSlots
      // The root row's summary reads the server's groups (the worker list API).
      if (p === 'crewWorkers' && mocks.crewWorkersFail) return vi.fn().mockRejectedValue(new Error('down'))
      if (p === 'crewWorkers' && mocks.crewWorkersNotServed) return vi.fn().mockRejectedValue(Object.assign(new Error('not found'), { status: 404 }))
      if (p === 'crewWorkers' && mocks.crewWorkersRows) return vi.fn().mockResolvedValue({ root: 'k-root', workers: mocks.crewWorkersRows })
      if (p === 'crewWorkers') return vi.fn().mockResolvedValue({ root: 'k-root', workers: [
        { key: 'k-w1', title: 'W1', agent: '', status: 'idle', group: 'done', last_activity: '', ledger_item: '', created_by: 'k-root' },
        { key: 'k-w2', title: 'W2', agent: '', status: 'needs_approval', group: 'approval', last_activity: '', ledger_item: '', created_by: 'k-root' },
      ] })
      // The crewmates roster the member row's face is drawn from. Empty by default,
      // so every other test sees the "Members page" fallback chip.
      if (p === 'members') return vi.fn().mockImplementation(() => {
        mocks.membersCalls++
        const read = mocks.rosterFails ? Promise.reject(new Error('roster down')) : Promise.resolve({ members: mocks.roster })
        // Tests await this (settled either way) as the barrier for the query having answered.
        mocks.lastMembers = read.then(() => undefined, () => undefined)
        return read
      })
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
import { crewRowOwnState } from '../pages/chat-sidebar/SessionRow'
import type { RootState } from '../store'
import { crewModePreviewOn } from './crewModePreview'

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
  /** What the STORE holds, when it is wider than what the sidebar is handed. `ChatPage`
   *  filters `dashboard.slots` by surface before passing it down, so a member's own DM
   *  thread is in the store and absent from the prop -- the split the creator-anchor
   *  tests need. Defaults to `slots`, the everyday case where the two agree. */
  storeSlots: TestSlot[] = slots,
) {
  mocks.folders = folders
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots: storeSlots, approvalMode: 'normal',
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
              slots={rows as never} activeSlot={(chatExtra.activeSlot as string | undefined) ?? null} unreadSlots={unreadSlots as never}
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
  return { ...utils, store, pushFrame, qc }
}

/** Row keys in the conductor lane, in render order. */
function laneRows(lane: HTMLElement): string[] {
  return Array.from(lane.querySelectorAll('[data-slot-key]')).map(el => el.getAttribute('data-slot-key') ?? '')
}

/** Every row a fixture in this file cites as a creator. The lane is COLLAPSED by
 *  default, so a test about nesting, filters or adoption -- not about the default --
 *  opens these first; `openAllBut` names the rows a test wants shut. */
const CREATORS = [
  'k-conductor', 'k-worker-a', 'k-new', 'k-root', 'k-old', 'k-a', 'k-lead',
  'member-pipeline', 'cron-nightly', 'k-released',
  ...Array.from({ length: 10 }, (_, i) => `lvl-${i}`),
]
function openAllBut(...shut: string[]) {
  localStorage.setItem('mc-sidebar-conductor-expanded', JSON.stringify(CREATORS.filter(k => !shut.includes(k))))
}
const openedSet = () => JSON.parse(localStorage.getItem('mc-sidebar-conductor-expanded') ?? '[]') as string[]

beforeEach(() => {
  localStorage.clear()
  localStorage.setItem('mc-session-stale-collapse-ms', '0')
  openAllBut()
  // A default, so a test that sets its own resolved value cannot leak it into the next.
  mocks.chatSlots.mockReset()
  mocks.chatSlots.mockResolvedValue([])
  mocks.navigate.mockReset()
  mocks.crewWorkersFail = false
  mocks.crewWorkersNotServed = false
  mocks.crewWorkersRows = null
})
afterEach(() => {
  vi.clearAllMocks()
  vi.useRealTimers()
})

describe('chat sidebar — conductor lane', () => {
  it('draws a Ultra Mode root as a plain row with an inline ghost, eyebrow, worker ghosts and what needs you', async () => {
    const slots = [
      { key: 'k-root', title: 'Crew root', agent: 'kirocrew-conductor', messages: 1, running: false, modified: 4000, crew_mode_root: true },
      { key: 'k-w1', title: 'W1', messages: 2, running: false, modified: 3000, created_by: 'k-root', crew_root: 'k-root' },
      { key: 'k-w2', title: 'W2', messages: 0, running: false, pending_approval: true, modified: 2000, created_by: 'k-root', crew_root: 'k-root' },
      { key: 'k-plain', title: 'Plain chat', agent: 'kirocrew', messages: 1, running: false, modified: 1000 },
    ]
    const { container, getByTestId, getAllByTestId } = renderSidebar(slots, [], null, {}, [], slots)
    const card = container.querySelector('[data-crew-root="true"]') as HTMLElement
    expect(card).toBeTruthy()
    // A plain session row: no tinted card frame.
    expect(card.className).not.toContain('crew-card')
    const eyebrow = within(card).getByTestId('crew-eyebrow')
    expect(eyebrow).toHaveTextContent('Ultra Mode')
    // A whole-pixel tracking: an em fraction at this size renders "CRE W MODE".
    expect(eyebrow.className).toContain('tracking-[1px]')
    expect(eyebrow.className).not.toMatch(/tracking-\[[\d.]+em\]/)
    // The Ultra Mode mark is a 12px lightning bolt inline on the eyebrow line, just
    // before the eyebrow, not a badge before the title: the title keeps the row's width.
    expect(within(card).queryByTestId('crew-ghost-badge')).toBeNull()
    const bolt = eyebrow.previousElementSibling as SVGElement
    expect(bolt.getAttribute('class')).toContain('lucide-zap')
    expect(bolt.getAttribute('width')).toBe('12')
    // The conductor's name is not shown: Crew Mode chooses it.
    expect(within(card).queryByText('kirocrew-conductor')).toBeNull()
    // The server's groups, not a guess from the rows: two sessions, none working.
    // No ghost per worker: one small ghost with the counts.
    await waitFor(() => {
      const counts = within(container.querySelector('[data-crew-root="true"]') as HTMLElement).getByTestId('crew-root-counts')
      // The words live in the accessible name and tooltip; the row shows the count.
      expect(counts).toHaveAccessibleName('2 sessions')
      expect(counts).toHaveAttribute('title', '2 sessions')
      expect(counts).toHaveTextContent(/^2$/)
      // The session count wears the chat icon, not the ghost.
      expect(counts.querySelector('.lucide-message-square')).not.toBeNull()
      expect(within(counts).queryByTestId('kiro-ghost-mark')).toBeNull()
      // No running dot: the crew's working count says that.
      expect(within(counts).queryByTestId('crew-root-running-dot')).toBeNull()
    })
    expect(within(container.querySelector('[data-crew-root="true"]') as HTMLElement).queryByTestId('crew-worker-stack')).toBeNull()
    // The left side is one state word: an approval makes it "Needs you".
    expect(getByTestId('crew-root-state')).toHaveTextContent(/^Needs you$/)
    expect(getByTestId('crew-root-state')).not.toHaveClass('truncate')
    // Only the root wears the eyebrow.
    expect(getAllByTestId('crew-eyebrow')).toHaveLength(1)
    // An idle crew's tag stays still: motion on the list means work is running.
    expect(eyebrow).toHaveAttribute('data-flowing', 'false')
  })

  it('flows the Ultra Mode tag only while the crew root has live work', () => {
    const slots = [
      { key: 'k-root', title: 'Crew root', agent: 'kirocrew-conductor', messages: 1, running: true, modified: 4000, crew_mode_root: true },
      { key: 'k-plain', title: 'Plain chat', agent: 'kirocrew', messages: 1, running: false, modified: 1000 },
    ]
    const { container } = renderSidebar(slots, [], null, {}, [], slots)
    const card = container.querySelector('[data-crew-root="true"]') as HTMLElement
    expect(within(card).getByTestId('crew-eyebrow')).toHaveAttribute('data-flowing', 'true')
  })

  it('flows the Ultra Mode tag while a crew member runs, even with the root idle', () => {
    const slots = [
      { key: 'k-root', title: 'Crew root', agent: 'kirocrew-conductor', messages: 1, running: false, modified: 4000, crew_mode_root: true },
      { key: 'k-w1', title: 'W1', messages: 2, running: true, modified: 3000, created_by: 'k-root', crew_root: 'k-root' },
      { key: 'k-plain', title: 'Plain chat', agent: 'kirocrew', messages: 1, running: false, modified: 1000 },
    ]
    const { container } = renderSidebar(slots, [], null, {}, [], slots)
    const card = container.querySelector('[data-crew-root="true"]') as HTMLElement
    expect(within(card).getByTestId('crew-eyebrow')).toHaveAttribute('data-flowing', 'true')
  })

  it('keeps an Ultra Mode row compact: its own state is one word, the sentence in the tooltip', async () => {
    const slots = [
      { key: 'k-root', title: 'Crew root', agent: 'kirocrew-conductor', messages: 1, running: false, needs_input: true, modified: 4000, crew_mode_root: true },
      { key: 'k-w1', title: 'W1', messages: 2, running: false, modified: 3000, created_by: 'k-root', crew_root: 'k-root' },
      { key: 'k-w2', title: 'W2', messages: 0, running: false, modified: 2000, created_by: 'k-root', crew_root: 'k-root' },
    ]
    const { getByTestId } = renderSidebar(slots, [], null, {}, [], slots)
    await waitFor(() => expect(getByTestId('crew-root-state')).toHaveTextContent(/^Needs you$/))
    expect(getByTestId('crew-root-state')).toHaveAttribute('title', 'Needs your answer')
    expect(getByTestId('crew-root-summary')).not.toHaveTextContent('Needs your answer')
  })

  it('reads "Running" with the working count, and no second running dot, while a crew member works', async () => {
    // The live rows stand in for a gateway that serves no crew list.
    mocks.crewWorkersNotServed = true
    const slots = [
      { key: 'k-root', title: 'Crew root', agent: 'kirocrew-conductor', messages: 1, running: false, modified: 4000, crew_mode_root: true },
      { key: 'k-w1', title: 'W1', messages: 2, running: true, modified: 3000, created_by: 'k-root', crew_root: 'k-root' },
    ]
    const { getByTestId } = renderSidebar(slots, [], null, {}, [], slots)
    await waitFor(() => expect(getByTestId('crew-root-counts')).toHaveAccessibleName('1 session'))
    expect(getByTestId('crew-root-state')).toHaveTextContent(/^Running$/)
    const counts = getByTestId('crew-root-counts')
    // One number for the busy workers: the working count, not a second running dot.
    expect(within(counts).queryByTestId('crew-root-running-dot')).toBeNull()
    expect(counts).toHaveTextContent(/^1$/)
    expect(getByTestId('crew-root-breakdown')).toHaveAccessibleName('1 working')
  })

  it('counts the crew on the root row: working, waiting and needs you, each its own glyph and a number', async () => {
    mocks.crewWorkersNotServed = true
    const slots = [
      { key: 'k-root', title: 'Crew root', agent: 'kirocrew-conductor', messages: 1, running: false, modified: 4000, crew_mode_root: true },
      { key: 'k-w1', title: 'W1', messages: 2, running: true, modified: 3000, created_by: 'k-root', crew_root: 'k-root' },
      // A question sits with the conductor: it counts as working, never as needs you.
      { key: 'k-w2', title: 'W2', messages: 2, running: false, needs_input: true, modified: 2900, created_by: 'k-root', crew_root: 'k-root' },
      { key: 'k-w3', title: 'W3', messages: 2, running: false, modified: 2800, created_by: 'k-root', crew_root: 'k-root' },
      { key: 'k-w4', title: 'W4', messages: 2, running: false, pending_approval: true, modified: 2700, created_by: 'k-root', crew_root: 'k-root' },
    ]
    const { getByTestId, queryByTestId } = renderSidebar(slots, [], null, {}, [], slots)
    await waitFor(() => expect(getByTestId('crew-root-breakdown')).toHaveAccessibleName('1 needs you, 2 working, and 1 waiting'))
    // A glyph of its own and a number each, no words: the words are each count's tooltip.
    const parts = within(getByTestId('crew-root-breakdown')).getAllByTestId('crew-root-count')
    expect(parts.map(p => [p.getAttribute('data-bucket'), p.textContent, p.getAttribute('title'), p.querySelector('svg')?.getAttribute('class')?.match(/lucide-(loader|hourglass|triangle-alert)\b/)?.[1]])).toEqual([
      ['needs_you', '1', '1 needs you', 'triangle-alert'], ['working', '2', '2 working', 'loader'], ['waiting', '1', '1 waiting', 'hourglass'],
    ])
    // One set with the session count: its 11px glyph and muted number; each
    // glyph wears its group's colour (needs you warm, working the running green).
    expect(parts.every(p => p.className.includes('crew-tone-muted') && p.className.includes('gap-1'))).toBe(true)
    expect(parts.map(p => p.querySelector('svg')?.getAttribute('width'))).toEqual(['11', '11', '11'])
    expect(parts[0].querySelector('svg')?.getAttribute('class')).toContain('crew-tone-warn')
    expect(parts[1].querySelector('svg')?.getAttribute('class')).toContain('text-[var(--ok)]')
    // The loader turns only where motion is allowed.
    expect(parts[1].querySelector('svg')?.getAttribute('class')).toContain('motion-safe:animate-spin')
    expect(parts[1].querySelector('svg')?.getAttribute('class')).not.toMatch(/(^|\s)animate-spin/)
    expect(getByTestId('crew-root-breakdown')).toHaveAttribute('title', '1 needs you, 2 working, and 1 waiting')
    // A count that does not fit is dropped whole, never cut in half; all fit here, so no ellipsis.
    expect(getByTestId('crew-root-breakdown').className).toContain('flex-wrap')
    expect(getByTestId('crew-root-breakdown').className).toContain('overflow-hidden')
    expect(queryByTestId('crew-root-hidden')).toBeNull()
  })

  it('reads "Running", not "Waiting", beside a working count of a worker whose question sits with its conductor', async () => {
    mocks.crewWorkersNotServed = true
    const slots = [
      { key: 'k-root', title: 'Crew root', agent: 'kirocrew-conductor', messages: 1, running: false, modified: 4000, crew_mode_root: true },
      { key: 'k-w1', title: 'W1', messages: 2, running: false, needs_input: true, modified: 3000, created_by: 'k-root', crew_root: 'k-root' },
    ]
    const { getByTestId } = renderSidebar(slots, [], null, {}, [], slots)
    await waitFor(() => expect(getByTestId('crew-root-breakdown')).toHaveAccessibleName('1 working'))
    expect(getByTestId('crew-root-state')).toHaveTextContent(/^Running$/)
  })

  it('shows an ellipsis when a narrow row wraps counts away, so a hidden count never looks like none', async () => {
    mocks.crewWorkersNotServed = true
    // A narrow row: the waiting count wraps onto the hidden second line.
    const top = vi.spyOn(HTMLElement.prototype, 'offsetTop', 'get').mockImplementation(function (this: HTMLElement) {
      return this.getAttribute('data-bucket') === 'waiting' ? 16 : 0
    })
    try {
      const slots = [
        { key: 'k-root', title: 'Crew root', agent: 'kirocrew-conductor', messages: 1, running: false, modified: 4000, crew_mode_root: true },
        { key: 'k-w1', title: 'W1', messages: 2, running: true, modified: 3000, created_by: 'k-root', crew_root: 'k-root' },
        { key: 'k-w2', title: 'W2', messages: 2, running: false, modified: 2900, created_by: 'k-root', crew_root: 'k-root' },
      ]
      const { findByTestId, getByTestId } = renderSidebar(slots, [], null, {}, [], slots)
      expect(await findByTestId('crew-root-hidden')).toHaveTextContent(/^…$/)
      expect(getByTestId('crew-root-hidden')).toHaveAttribute('title', '1 working and 1 waiting')
    } finally {
      top.mockRestore()
    }
  })

  it('shows no counts when every crew member is done', async () => {
    mocks.crewWorkersRows = [
      { key: 'k-w1', title: 'W1', agent: '', status: 'idle', group: 'done', last_activity: '', ledger_item: '', created_by: 'k-root' },
      { key: 'k-w2', title: 'W2', agent: '', status: 'idle', group: 'done', last_activity: '', ledger_item: '', created_by: 'k-root' },
    ]
    const slots = [{ key: 'k-root', title: 'Crew root', agent: 'kirocrew-conductor', messages: 1, running: false, modified: 4000, crew_mode_root: true }]
    const { getByTestId, queryByTestId } = renderSidebar(slots, [], null, {}, [], slots)
    await waitFor(() => expect(getByTestId('crew-root-counts')).toHaveAccessibleName('2 sessions'))
    expect(queryByTestId('crew-root-breakdown')).toBeNull()
  })

  it('marks the root row as the parent while one of its unlisted workers is in the pane', async () => {
    const slots = [
      { key: 'k-root', title: 'Crew root', agent: 'kirocrew-conductor', messages: 1, running: false, modified: 4000, crew_mode_root: true },
      { key: 'k-w1', title: 'W1', messages: 2, running: false, modified: 3000, created_by: 'k-root', crew_root: 'k-root' },
      { key: 'k-plain', title: 'Plain chat', agent: 'kirocrew', messages: 1, running: false, modified: 1000 },
    ]
    const { container } = renderSidebar(slots, [], null, { activeSlot: 'k-w1' }, [], slots)
    const card = container.querySelector('[data-crew-root="true"]') as HTMLElement
    expect(card).toHaveAttribute('data-crew-parent', 'true')
    expect(card).toHaveClass('session-crew-parent')
    expect(within(card).getByTestId('crew-parent-hint')).toHaveTextContent('Showing one of its crew members')
    // The parent is marked, not made the active row.
    expect(card).not.toHaveAttribute('aria-current')
    expect(container.querySelectorAll('[data-crew-parent="true"]')).toHaveLength(1)
  })

  it('marks no crew parent with the Ultra Mode preview off', async () => {
    localStorage.removeItem('mc-preview-crew-mode')
    const slots = [
      { key: 'k-root', title: 'Crew root', agent: 'kirocrew-conductor', messages: 1, running: false, modified: 4000, crew_mode_root: true },
      { key: 'k-w1', title: 'W1', messages: 2, running: false, modified: 3000, created_by: 'k-root', crew_root: 'k-root' },
    ]
    const { container } = renderSidebar(slots, [], null, { activeSlot: 'k-w1' }, [], slots)
    await screen.findByText('Crew root')
    expect(container.querySelectorAll('[data-crew-parent="true"]')).toHaveLength(0)
    expect(container.querySelector('.session-crew-parent')).toBeNull()
  })

  it('marks no parent while a plain chat is in the pane', async () => {
    const slots = [
      { key: 'k-root', title: 'Crew root', agent: 'kirocrew-conductor', messages: 1, running: false, modified: 4000, crew_mode_root: true },
      { key: 'k-plain', title: 'Plain chat', agent: 'kirocrew', messages: 1, running: false, modified: 1000 },
    ]
    const { container } = renderSidebar(slots, [], null, { activeSlot: 'k-plain' }, [], slots)
    expect(container.querySelector('[data-crew-parent]')).toBeNull()
    expect(container.querySelector('[data-testid="crew-parent-hint"]')).toBeNull()
  })

  it("a running conductor's row still says a crew member waits on an approval", () => {
    // A running turn (or an armed patrol) claims the row's status line and hides
    // the crew summary; the crew member's own row is not in the list. The wait
    // must outrank the work, as the row's own approval does.
    const slots = [
      { key: 'k-root', title: 'Crew root', agent: 'kirocrew-conductor', messages: 1, running: true, modified: 4000, crew_mode_root: true },
      { key: 'k-w1', title: 'W1', messages: 2, running: false, pending_approval: true, modified: 3000, created_by: 'k-root', crew_root: 'k-root' },
    ]
    const { container } = renderSidebar(slots, [], null, {}, [], slots)
    // The Ultra Mode row is compact: one word, the sentence in its tooltip.
    const card = container.querySelector('[data-crew-root="true"]') as HTMLElement
    expect(within(card).getByTestId('crew-root-state')).toHaveTextContent(/^Needs you$/)
    expect(within(card).getByTestId('crew-root-state')).toHaveAttribute('title', 'Approval needed · 1')
    // No approval waiting: the running word comes back.
    const { container: idle } = renderSidebar(slots.map(s => ({ ...s, pending_approval: false })), [], null, {}, [], slots.map(s => ({ ...s, pending_approval: false })))
    expect(within(idle.querySelector('[data-crew-root="true"]') as HTMLElement).getByTestId('crew-root-state')).toHaveTextContent(/^Running$/)
    // A question the conductor itself owes the person reads Needs you too.
    const asking = slots.map(s => s.key === 'k-root' ? { ...s, needs_input: true } : s)
    const { container: owed } = renderSidebar(asking, [], null, {}, [], asking)
    expect(within(owed.querySelector('[data-crew-root="true"]') as HTMLElement).getByTestId('crew-root-state')).toHaveAttribute('title', 'Needs your answer')
  })

  it("folds the workers summary into the row's one status line, in place of its last message", async () => {
    const slots = [
      { key: 'k-root', title: 'Crew root', agent: 'kirocrew-conductor', messages: 1, running: false, modified: 4000, crew_mode_root: true, last_message: 'Dispatched two workers' },
      { key: 'k-w1', title: 'W1', messages: 2, running: false, modified: 3000, created_by: 'k-root', crew_root: 'k-root' },
    ]
    const { container } = renderSidebar(slots, [], null, {}, [], slots)
    await screen.findByTestId('crew-root-summary')
    const card = container.querySelector('[data-crew-root="true"]') as HTMLElement
    // One status line: the summary stands where the last message would, not under it.
    expect(within(card).queryByText('Dispatched two workers')).toBeNull()
  })

  it('a gateway that serves no crew list is not a failed read: the row shows the live summary and no notice', async () => {
    // The crew list route lands in a later part. On a gateway without it the row
    // is built from the live rows alone and says nothing is wrong; the route is
    // not asked again.
    mocks.crewWorkersNotServed = true
    const slots = [
      { key: 'k-root', title: 'Crew root', agent: 'kirocrew-conductor', messages: 1, running: false, modified: 4000, crew_mode_root: true, last_message: 'Dispatched two workers' },
      { key: 'k-w1', title: 'W1', messages: 2, running: false, modified: 3000, created_by: 'k-root', crew_root: 'k-root', pending_approval: true },
    ]
    const { qc } = renderSidebar(slots, [], null, {}, [], slots)
    await waitFor(() => expect(qc.getQueryState(['crew-workers', 'k-root'])?.status).toBe('error'))
    const summary = screen.getByTestId('crew-root-summary')
    expect(summary).toHaveTextContent('Needs you')
    expect(summary).not.toHaveTextContent('Could not load the crew.')
    expect(summary.querySelector('[role="alert"]')).toBeNull()
  })

  it('a failed crew read keeps the row\'s own Needs you beside the failure', async () => {
    mocks.crewWorkersFail = true
    const slots = [
      { key: 'k-root', title: 'Crew root', agent: 'kirocrew-conductor', messages: 1, running: false, needs_input: true, modified: 4000, crew_mode_root: true },
      { key: 'k-w1', title: 'W1', messages: 2, running: false, modified: 3000, created_by: 'k-root', crew_root: 'k-root' },
    ]
    renderSidebar(slots, [], null, {}, [], slots)
    await waitFor(() => expect(screen.getByTestId('crew-root-summary')).toHaveTextContent('Could not load the crew.'))
    expect(screen.getByTestId('crew-root-state')).toHaveTextContent(/^Needs you$/)
  })

  it('keeps an Ultra Mode row compact before it has any crew member', async () => {
    mocks.crewWorkersNotServed = true
    const slots = [{ key: 'k-root', title: 'Crew root', agent: 'kirocrew-conductor', messages: 1, running: false, needs_input: true, modified: 4000, crew_mode_root: true }]
    renderSidebar(slots, [], null, {}, [], slots)
    await waitFor(() => expect(screen.getByTestId('crew-root-state')).toHaveTextContent(/^Needs you$/))
    expect(screen.queryByTestId('crew-root-counts')).toBeNull()
    // No crew: no worker counts either.
    expect(screen.queryByTestId('crew-root-breakdown')).toBeNull()
  })

  it('maps queued work to Waiting, never Done, and keeps its line as the tooltip', () => {
    for (const key of ['subagents_waiting', 'structured_monitor_passive']) {
      const own = crewRowOwnState(key, 'Waiting for memory')
      expect(own?.word).toBe('Waiting')
      expect(own?.title).toBe('Waiting for memory')
    }
  })

  it('maps a stalled goal loop to Paused, never Running', () => {
    expect(crewRowOwnState('goal_loop', 'interrupted', true)?.word).toBe('Paused')
    expect(crewRowOwnState('goal_loop', '', false)?.word).toBe('Running')
  })

  it('a failed workers poll keeps the ghosts and says the read failed on the row', async () => {
    const slots = [
      { key: 'k-root', title: 'Crew root', agent: 'kirocrew-conductor', messages: 1, running: false, modified: 4000, crew_mode_root: true, last_message: 'Dispatched two workers' },
      { key: 'k-w1', title: 'W1', messages: 2, running: false, modified: 3000, created_by: 'k-root', crew_root: 'k-root' },
    ]
    // A failed poll after a good read: the ghosts stay, the counts give way to the
    // panel's wording (an ErrorNotice with the agent hand-off), so a stale summary
    // is never read as a fresh one.
    const { qc, unmount } = renderSidebar(slots, [], null, {}, [], slots)
    // The row paints the store's own live row first (W1 idle: "Done 0/1"), then
    // the list route's answer, whose group wins for the key it names.
    await waitFor(() => expect(screen.getByTestId('crew-root-summary')).toHaveTextContent('Needs you'))
    mocks.crewWorkersFail = true
    await qc.refetchQueries({ queryKey: ['crew-workers', 'k-root'] })
    await waitFor(() => expect(qc.getQueryState(['crew-workers', 'k-root'])?.status).toBe('error'))
    const summary = screen.getByTestId('crew-root-summary')
    expect(summary).toHaveTextContent('Could not load the crew.')
    expect(summary).not.toHaveTextContent('Needs you')
    expect(summary.querySelector('[role="alert"]')).not.toBeNull()
    unmount()
    // Never any data: the row still says the read failed, in place of its last message.
    const second = renderSidebar(slots, [], null, {}, [], slots)
    await waitFor(() => expect(second.qc.getQueryState(['crew-workers', 'k-root'])?.status).toBe('error'))
    // The error lands in the query first and on the row a tick later.
    await waitFor(() => expect(screen.getByTestId('crew-root-summary')).toHaveTextContent('Could not load the crew.'))
    expect(screen.queryByText('Dispatched two workers')).toBeNull()
  })

  it("leaves a Ultra Mode root's workers out of the person's list and keeps the root", () => {
    const { queryByText } = renderSidebar([
      { key: 'k-root', title: 'Crew root', messages: 1, running: false, modified: 4000, crew_mode_root: true },
      { key: 'k-w1', title: 'Crew worker', messages: 1, running: false, modified: 3000, created_by: 'k-root', crew_root: 'k-root' },
      { key: 'k-plain', title: 'Plain chat', messages: 1, running: false, modified: 2000 },
    ])
    expect(queryByText('Crew root')).toBeTruthy()
    expect(queryByText('Plain chat')).toBeTruthy()
    expect(queryByText('Crew worker')).toBeNull()
  })

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

  it('is COLLAPSED by default: one row per crew, the workers behind the chevron', () => {
    // The lane exists so a conductor and its workers read as ONE unit of work. Fifteen
    // rows is not one unit; a row with a count and the subtree's badges on it is. The
    // System page's Sessions tab is where every row is shown at once.
    localStorage.removeItem('mc-sidebar-conductor-expanded')
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    const { getByTestId } = renderSidebar()
    expect(laneRows(getByTestId('conductor-view-lane'))).toEqual(['k-conductor'])
    expect(getByTestId('conductor-child-count-k-conductor').textContent).toBe('2')
  })

  it('every level is shut by default, not just the root', () => {
    // Opening the conductor shows its workers and nothing below them: a worker that
    // opened workers of its own is one row with a count too.
    localStorage.removeItem('mc-sidebar-conductor-expanded')
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    const { getByTestId } = renderSidebar()
    fireEvent.click(getByTestId('conductor-chevron-k-conductor'))
    expect(laneRows(getByTestId('conductor-view-lane'))).toEqual(['k-conductor', 'k-worker-a', 'k-worker-b'])
    expect(getByTestId('conductor-child-count-k-worker-a').textContent).toBe('1')
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

  it('persists the opened set', () => {
    localStorage.removeItem('mc-sidebar-conductor-expanded')
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    const first = renderSidebar()
    fireEvent.click(first.getByTestId('conductor-chevron-k-conductor'))
    expect(openedSet()).toContain('k-conductor')
    first.unmount()

    const second = renderSidebar()
    expect(laneRows(second.getByTestId('conductor-view-lane'))).toEqual(['k-conductor', 'k-worker-a', 'k-worker-b'])
  })

  it('a collapsed conductor shows its child count', () => {
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    openAllBut('k-conductor')
    const { getByTestId } = renderSidebar()
    // Two DIRECT children; the count is the chevron's subject, not the subtree size.
    expect(getByTestId('conductor-child-count-k-conductor').textContent).toBe('2')
  })

  it('a collapsed conductor bubbles its subtree needs-you and running counts', () => {
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    openAllBut('k-conductor')
    const { getByTestId } = renderSidebar()
    // k-deep needs input (two levels down) and k-worker-a is running.
    expect(getByTestId('conductor-needs-you-k-conductor').textContent).toBe('1')
    expect(getByTestId('conductor-running-k-conductor').textContent).toBe('1')
  })

  it('a Ultra Mode worker raises no needs-your-answer count on its conductor (S3)', () => {
    // Its question goes to the conductor; its unread reply is the conductor's to read.
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    openAllBut('k-conductor')
    const crew = NESTED.map(row => (row.key === 'k-conductor' ? row : { ...row, crew_root: 'k-conductor' }))
    const { queryByTestId } = renderSidebar(crew, [], null, {}, ['k-worker-b'])
    expect(queryByTestId('conductor-needs-you-k-conductor')).toBeNull()
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
    openAllBut('k-conductor')
    const { getByTestId } = renderSidebar(NESTED, [], null, {}, ['k-worker-a'])
    const lane = getByTestId('conductor-view-lane')

    // The conductor is the only row: its matching child is folded away, not promoted.
    expect(laneRows(lane)).toEqual(['k-conductor'])
    const row = lane.querySelector('[data-slot-key="k-conductor"]')!.closest('[data-conductor-depth]')
    expect(row?.getAttribute('data-conductor-anchor')).toBe('true')
    // The count is over the KEPT subtree, so it says one thing is in there, not three.
    expect(getByTestId('conductor-child-count-k-conductor').textContent).toBe('1')
  })

  it('drops the superseded collapsed-set key rather than leaving it in storage', () => {
    /* The old key held the opposite sense and no reading of it produces the new one, so
       the honest migration is to remove it and take the new default. Left behind it would
       outlive every build that understands it. */
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    localStorage.setItem('mc-sidebar-conductor-collapsed', JSON.stringify(['k-conductor']))
    const { getByTestId } = renderSidebar()

    expect(getByTestId('conductor-view-lane')).toBeTruthy()
    expect(localStorage.getItem('mc-sidebar-conductor-collapsed')).toBeNull()
  })

  it('opens the destination when a row the FILTER excludes is adopted', () => {
    /* The row is on screen as a dimmed anchor, so the no-disappearance promise covers it
       -- but it is absent from the filtered set. Tracking citation changes over the
       filtered set alone missed exactly these rows: the anchor moves under a conductor the
       user had collapsed, nothing opens the destination, and its whole subtree leaves the
       screen with no signal. Tracked over every row instead. */
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    localStorage.setItem('mc-session-unread-only', '1')
    openAllBut('k-new')
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
    expect(openedSet()).toContain('k-new')
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
    // The closed-creator fact rides the tooltip, not a line of visible text: the row
    // is one fixed-height card and a stacked line under it is what the session-row rule
    // forbids. The indicator itself is a Lucide icon in the existing badge cluster, not
    // a hand-authored glyph whose shape depends on the platform's fonts.
    const hint = within(lane).getByTestId('conductor-orphan-k-orphan')
    // The visible label is a GRACEFUL phrase, never the raw slot id: `k-gone` is a
    // closed session with no row in this payload, so an id nobody recognises would
    // explain the nesting no better than silence (#18153). The slot id survives only
    // on the data attribute, for tests and the DOM, never in the tooltip the reader sees.
    expect(hint.getAttribute('title')).toBe('Opened by a closed session')
    expect(hint.getAttribute('title')).not.toContain('k-gone')
    expect(hint.getAttribute('aria-label')).toBe('Opened by a closed session')
    expect(hint.getAttribute('data-orphan-of')).toBe('k-gone')
    expect(hint.querySelector('svg')).toBeTruthy()
    expect(hint.textContent).toBe('')
  })

  it('nests a worker whose conductor closed under the lead, and still says the conductor is gone', () => {
    /* The case the lane kept getting wrong: a lead opens a conductor, the conductor
       opens the workers, the conductor is closed. The backend resolves `key` to the
       nearest ancestor still open and marks the edge `ancestor`, so the workers hang
       off the lead instead of scattering to the top level -- and each one keeps the
       closed-creator glyph, because `k-mid` really did open it and really is gone. */
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    const { getByTestId } = renderSidebar([
      { key: 'k-lead', title: 'Lead', messages: 1, running: false, modified: 3000 },
      { key: 'k-worker', title: 'Worker', messages: 1, running: true, modified: 2000, parent: { slot: 'k-mid', key: 'k-lead', ancestor: true } },
    ] as never)
    const lane = getByTestId('conductor-view-lane')
    expect(laneRows(lane)).toEqual(['k-lead', 'k-worker'])
    const placed = lane.querySelector('[data-slot-key="k-worker"]')!.closest('[data-conductor-depth]')
    expect(placed?.getAttribute('data-conductor-depth')).toBe('1')
    // The citation names the CREATOR, not the ancestor it was placed under.
    const hint = within(lane).getByTestId('conductor-orphan-k-worker')
    expect(hint.getAttribute('data-orphan-of')).toBe('k-mid')
    // Graceful label, not the raw slot id of the closed creator (#18153).
    expect(hint.getAttribute('title')).toBe('Opened by a closed session')
    expect(hint.getAttribute('title')).not.toContain('k-mid')
    // And not the other glyph: that one says the creator is open and merely hidden.
    expect(within(lane).queryByTestId('conductor-cites-parent-k-worker')).toBeNull()
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
    openAllBut('k-new', 'k-conductor')
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
    openAllBut('k-new')
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
    openAllBut('k-new')
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
    openAllBut('k-conductor')
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
    openAllBut('k-conductor')
    const { getByTestId, pushFrame } = renderSidebar()
    expect(laneRows(getByTestId('conductor-view-lane'))).toEqual(['k-conductor'])

    pushFrame(NESTED.map(row => (row.key === 'k-worker-a' ? { ...row, parent: null } : row)) as never)

    // `k-worker-a` is a root now, so it shows; `k-conductor` stays folded, untouched.
    expect(laneRows(getByTestId('conductor-view-lane'))).toEqual(['k-conductor', 'k-worker-a', 'k-deep'])
    expect(openedSet()).not.toContain('k-conductor')
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

  it('nests a peer worker under the peer conductor that opened it, shut by default', () => {
    // The hub forwards `parent` on a peer row (useInstanceSessions). The lane then
    // resolves it within the row's own origin: `peer-1:k-lead` owns `peer-1:k-w1` and
    // `peer-1:k-w2`, and a LOCAL `k-lead` does not gain them. Before the citation
    // crossed the wire every remote worker rendered here as a top-level stray.
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    localStorage.removeItem('mc-sidebar-conductor-expanded')
    const peer = (key: string, title: string, modified: number, parent?: string) => ({
      key, title, messages: 0, running: false, modified,
      peer_id: 'peer-1', row_identity: `peer-1:${key}`,
      ...(parent ? { parent: { slot: parent, key: parent } } : {}),
    })
    const { getByTestId, getByPlaceholderText } = renderSidebar([
      { key: 'k-lead', title: 'Local lead', messages: 1, running: false, modified: 5000 },
      peer('k-lead', 'Peer lead', 4000),
      peer('k-w1', 'Peer worker 1', 3000, 'k-lead'),
      peer('k-w2', 'Peer worker 2', 2000, 'k-lead'),
    ])
    // The lane element is re-queried after the click: the lane remounts on a toggle.
    const lane = () => getByTestId('conductor-view-lane')
    // Collapsed by default: two roots, no children rendered, and no stray glyph.
    expect(laneRows(lane())).toEqual(['k-lead', 'k-lead'])
    expect(within(lane()).getByTestId('conductor-child-count-peer-1:k-lead').textContent).toBe('2')
    expect(within(lane()).queryByTestId('conductor-child-count-k-lead')).toBeNull()
    expect(within(lane()).queryByTestId('conductor-orphan-peer-1:k-w1')).toBeNull()
    expect(within(lane()).queryByTestId('conductor-orphan-peer-1:k-w2')).toBeNull()

    fireEvent.click(within(lane()).getByTestId('conductor-chevron-peer-1:k-lead'))
    expect(laneRows(lane())).toEqual(['k-lead', 'k-lead', 'k-w1', 'k-w2'])
    expect(within(lane()).queryByTestId('conductor-orphan-peer-1:k-w1')).toBeNull()
    const w1 = lane().querySelector('[data-slot-key="k-w1"]')
    expect(w1?.getAttribute('data-conductor-depth')).toBe('1')
    // Search flattens the tree; the peer child then wears the same "opened by"
    // glyph a local child does, read off `parent.slot` -- the half the hub now
    // forwards. Same query on the local twin as a control.
    fireEvent.change(getByPlaceholderText(/search/i), { target: { value: 'worker' } })
    expect(within(lane()).getByTestId('conductor-cites-parent-peer-1:k-w1')).toBeTruthy()
  })

  it('gives a peer worker whose conductor closed the closed-creator glyph, like a local one', () => {
    /* The peer's own backend walked up to its lead and sent `ancestor`, and the hub
       forwards it (`useInstanceSessions`). The lane must then read a peer row exactly
       as it reads a local one: nested under the peer lead, wearing the glyph for the
       conductor that is gone. Without the flag crossing, the row nests on the key and
       the glyph disappears -- the peer's crew reads as sessions the lead opened
       itself, and this lane disagrees with the peer's own sidebar about one fact. */
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    localStorage.setItem('mc-sidebar-conductor-expanded', JSON.stringify(['peer-1:k-lead']))
    const { getByTestId } = renderSidebar([
      { key: 'k-lead', title: 'Peer lead', messages: 1, running: false, modified: 4000, peer_id: 'peer-1', row_identity: 'peer-1:k-lead' },
      { key: 'k-w1', title: 'Peer worker', messages: 1, running: true, modified: 3000, peer_id: 'peer-1', row_identity: 'peer-1:k-w1', parent: { slot: 'k-mid', key: 'k-lead', ancestor: true } },
    ] as never)
    const lane = getByTestId('conductor-view-lane')
    expect(laneRows(lane)).toEqual(['k-lead', 'k-w1'])
    const placed = lane.querySelector('[data-slot-key="k-w1"]')!.closest('[data-conductor-depth]')
    expect(placed?.getAttribute('data-conductor-depth')).toBe('1')
    const hint = within(lane).getByTestId('conductor-orphan-peer-1:k-w1')
    expect(hint.getAttribute('data-orphan-of')).toBe('k-mid')
    expect(within(lane).queryByTestId('conductor-cites-parent-peer-1:k-w1')).toBeNull()
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

  it("carries a Ultra Mode worker's tool approval on its conductor's own summary once the worker leaves the lane", async () => {
    // With the list rule on, a worker is no row in the lane, so the collapsed
    // root's needs-you badge cannot count it; the root's crew summary does
    // ("Approval needed · 1"), from the live rows this gateway stamps, so the ask
    // is still said on the root. Red if the summary drops live rows.
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    openAllBut('k-root')
    const { getByTestId, queryByText } = renderSidebar([
      { key: 'k-root', title: 'Crew root', messages: 1, running: false, modified: 4000, crew_mode_root: true },
      { key: 'k-w1', title: 'W1', messages: 2, running: false, modified: 3000, parent: { slot: 'k-root', key: 'k-root' }, crew_root: 'k-root', pending_approval: true },
      { key: 'k-w2', title: 'W2', messages: 2, running: false, modified: 2000, parent: { slot: 'k-root', key: 'k-root' }, crew_root: 'k-root' },
    ])
    expect(queryByText('W1')).toBeNull()
    await waitFor(() => expect(getByTestId('crew-root-summary')).toHaveTextContent('Needs you'))
  })

  it('pairs each aggregate count with the glyph its children show, not a tint alone', () => {
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    openAllBut('k-conductor')
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
    openAllBut('k-root')
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

  it('does not poll while lineage is provisional; the pushed rows nest it', async () => {
    // A cold start ships `parent: null` with `lineage_pending` while the gateway's
    // projection seeds. The gateway pushes the settled rows when the seed lands, so
    // the sidebar never reads the slot list again on its own.
    vi.useFakeTimers()
    try {
      localStorage.setItem('mc-sidebar-lane', 'conductor')
      const { queryByTestId, getByTestId, pushFrame } = renderSidebar([
        { key: 'k-root', title: 'Conductor', messages: 1, running: false, modified: 2000, parent: null, lineage_pending: true },
        { key: 'k-kid', title: 'Worker', messages: 1, running: false, modified: 1000, parent: null, lineage_pending: true },
      ])
      expect(queryByTestId('conductor-view-lane')).toBeNull()
      await act(async () => { await vi.advanceTimersByTimeAsync(31_000) })
      expect(mocks.chatSlots).not.toHaveBeenCalled()

      act(() => {
        pushFrame([
          { key: 'k-root', title: 'Conductor', messages: 1, running: false, modified: 2000, parent: null },
          { key: 'k-kid', title: 'Worker', messages: 1, running: false, modified: 1000, parent: { slot: 'k-root', key: 'k-root' } },
        ])
      })
      expect(getByTestId('conductor-view-lane')).toBeTruthy()
      expect(getByTestId('conductor-child-count-k-root').textContent).toBe('1')
    } finally {
      vi.useRealTimers()
    }
  })

  it('does not read the cold-start seed as an adoption, so the collapsed default survives a reload', () => {
    /* The provisional frame ships every row `parent: null` with `lineage_pending`, and
       the settling frame then carries the real citations. Bookkeeping that recorded the
       nulls would see every null -> key transition as a MOVE and persist every crew open,
       which inverts the PR's own default on every cold start and writes that inversion
       to localStorage. A provisional row is not a baseline: skip it, so the settling
       frame reads as the first sighting -- a creation -- and nothing expands. */
    localStorage.removeItem('mc-sidebar-conductor-expanded')
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    const { queryByTestId, getByTestId, pushFrame } = renderSidebar([
      { key: 'k-root', title: 'Conductor', messages: 1, running: false, modified: 2000, parent: null, lineage_pending: true },
      { key: 'k-kid', title: 'Worker', messages: 1, running: false, modified: 1000, parent: null, lineage_pending: true },
    ])
    expect(queryByTestId('conductor-view-lane')).toBeNull()
    pushFrame([
      { key: 'k-root', title: 'Conductor', messages: 1, running: false, modified: 2000, parent: null },
      { key: 'k-kid', title: 'Worker', messages: 1, running: false, modified: 1000, parent: { slot: 'k-root', key: 'k-root' } },
    ])
    const lane = getByTestId('conductor-view-lane')
    expect(laneRows(lane)).toEqual(['k-root'])
    expect(getByTestId('conductor-child-count-k-root').textContent).toBe('1')
    expect(openedSet()).toEqual([])
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

  describe('a creator this page does not list (a crew member\u2019s own thread)', () => {
    /* The gateway ran a member-driven crew: the pipeline conductor is a crew MEMBER,
       its session is the member's own DM thread (`surface: 'member'`), and every
       worker it opened cites that slot. The backend resolved each citation to a live
       `parent.key`, and the System page nested all of them. `ChatPage`, though, hands
       this sidebar only chat-surface rows, so the member's row never reached the lane:
       every worker resolved no parent, rendered at the top level, and wore the glyph
       that says its creator CLOSED -- about a session that was open and dispatching. */
    const MEMBER: TestSlot = {
      key: 'member-pipeline', title: 'Pipeline Work Focus On Issues', messages: 40, running: true,
      modified: 5000, mode: 'member', surface: 'member', agent: 'kirocrew-pipeline-conductor',
    }
    const WORKERS: TestSlot[] = [
      { key: 'k-fix-1', title: 'issue-fix: #1', messages: 1, running: true, modified: 4000, parent: { slot: 'member-pipeline', key: 'member-pipeline' } },
      { key: 'k-fix-2', title: 'issue-fix: #2', messages: 1, running: false, modified: 3000, parent: { slot: 'member-pipeline', key: 'member-pipeline' } },
      { key: 'k-solo', title: 'Unrelated chat', messages: 1, running: false, modified: 2000 },
    ]
    /** What ChatPage passes (chat surfaces only) vs. what the store holds (everything). */
    const listed = WORKERS
    const inStore = [MEMBER, ...WORKERS]

    it('draws the member as an anchor and nests its workers under it', () => {
      localStorage.setItem('mc-sidebar-lane', 'conductor')
      const { getByTestId, queryByTestId } = renderSidebar(listed, [], null, {}, [], inStore)
      const lane = getByTestId('conductor-view-lane')
      expect(laneRows(lane)).toEqual(['member-pipeline', 'k-fix-1', 'k-fix-2', 'k-solo'])
      for (const key of ['k-fix-1', 'k-fix-2']) {
        const row = lane.querySelector(`[data-slot-key="${key}"]`)!.closest('[data-conductor-depth]')
        expect(row?.getAttribute('data-conductor-depth')).toBe('1')
        // Nested, so neither citation glyph: the indent already says who opened it, and
        // the orphan copy in particular would be false about a live creator.
        expect(queryByTestId(`conductor-orphan-${key}`)).toBeNull()
        expect(queryByTestId(`conductor-cites-parent-${key}`)).toBeNull()
      }
      // Context, not a match: the filter this page applies never admitted the row.
      const anchor = lane.querySelector('[data-slot-key="member-pipeline"]')!.closest('[data-conductor-depth]')
      expect(anchor?.getAttribute('data-conductor-anchor')).toBe('true')
      expect(anchor?.getAttribute('data-conductor-depth')).toBe('0')
    })

    it('the anchor is drawn only while a worker of it is on screen', () => {
      // No listed row cites the member: nothing to hang, so nothing borrowed from the
      // store. The lane must not become a second Members page. (An unrelated chat
      // conductor supplies the one edge the lane needs to be offered at all.)
      localStorage.setItem('mc-sidebar-lane', 'conductor')
      const { getByTestId } = renderSidebar(NESTED, [], null, {}, [], [MEMBER, ...NESTED])
      expect(laneRows(getByTestId('conductor-view-lane')))
        .toEqual(['k-conductor', 'k-worker-a', 'k-deep', 'k-worker-b'])
    })

    it('follows a chain of unlisted creators, not just one level', () => {
      // member -> lead (a chat session, listed) -> worker (listed). The member is the
      // only unlisted row, but it is reached THROUGH the lead's citation, so the walk
      // must continue from every admitted row rather than stop at the listed set.
      localStorage.setItem('mc-sidebar-lane', 'conductor')
      const lead: TestSlot = { key: 'k-lead', title: 'Lead', messages: 1, running: true, modified: 4500, parent: { slot: 'member-pipeline', key: 'member-pipeline' } }
      const worker: TestSlot = { key: 'k-w', title: 'Worker', messages: 1, running: false, modified: 4400, parent: { slot: 'k-lead', key: 'k-lead' } }
      const { getByTestId } = renderSidebar([lead, worker], [], null, {}, [], [MEMBER, lead, worker])
      const lane = getByTestId('conductor-view-lane')
      expect(laneRows(lane)).toEqual(['member-pipeline', 'k-lead', 'k-w'])
      expect(lane.querySelector('[data-slot-key="k-w"]')!.closest('[data-conductor-depth]')?.getAttribute('data-conductor-depth')).toBe('2')
    })

    it('clicking the anchor goes to the member on the Members page, not to the chat pane', () => {
      // The chat pane cannot show a member's thread (the page's own surface filter
      // says so), so `switchSlot` would strand the user on the previous transcript.
      localStorage.setItem('mc-sidebar-lane', 'conductor')
      const { getByTestId, store } = renderSidebar(listed, [], null, {}, [], inStore)
      const row = getByTestId('conductor-view-lane').querySelector('[data-session-row="member-pipeline"]') as HTMLElement
      fireEvent.click(row)
      expect(mocks.navigate).toHaveBeenCalledWith('/members?member=kirocrew-pipeline-conductor')
      expect(store.getState().chat.activeSlot).toBeNull()
      // Enter does the same as click, per WCAG 2.1.1.
      mocks.navigate.mockReset()
      fireEvent.keyDown(row, { key: 'Enter' })
      expect(mocks.navigate).toHaveBeenCalledWith('/members?member=kirocrew-pipeline-conductor')
    })

    it("shows the mate's name as the anchor title, not the stored generated one", () => {
      // The member's own DM thread is one long conversation with one crewmate; a stored
      // topic title ('Pipeline Work Focus On Issues') must not name its row.
      localStorage.setItem('mc-sidebar-lane', 'conductor')
      const { getByTestId } = renderSidebar(listed, [], null, {}, [], inStore)
      const anchor = getByTestId('conductor-view-lane').querySelector('[data-session-row="member-pipeline"]') as HTMLElement
      const title = anchor.querySelector('[data-session-title]') as HTMLElement
      expect(title.textContent).toBe('kirocrew-pipeline-conductor')
      expect(title.getAttribute('title')).toBe('kirocrew-pipeline-conductor: Pipeline Work Focus On Issues')
      // A listed worker beside it keeps its own title.
      const worker = getByTestId('conductor-view-lane').querySelector('[data-session-row="k-fix-1"] [data-session-title]') as HTMLElement
      expect(worker.textContent).toBe('issue-fix: #1')
    })

    it('withholds the local-only affordances from the anchor, as it does for a peer row', () => {
      // Rename, close, fork, drag: the slot's lifecycle belongs to the Members page. A
      // control that looks live and does nothing (or worse, closes a member's thread
      // from a list that does not otherwise show it) is not offered.
      localStorage.setItem('mc-sidebar-lane', 'conductor')
      const { getByTestId } = renderSidebar(listed, [], null, {}, [], inStore)
      const lane = getByTestId('conductor-view-lane')
      const anchor = lane.querySelector('[data-session-row="member-pipeline"]') as HTMLElement
      expect(anchor.getAttribute('data-draggable')).toBe('false')
      expect(anchor.getAttribute('title') ?? '').toMatch(/Members page/)
      // The destination is also VISIBLE text, not hover-only: a touch reader gets no
      // title, and a page jump with no cue on the row is unexplained.
      expect(anchor.querySelector('[data-testid="members-page-chip"]')?.textContent).toBe('Members page')
      expect(anchor.querySelector('[data-close]')).toBeNull()
      expect(anchor.querySelector('[data-fork]')).toBeNull()
      // A listed worker beside it keeps everything, and wears no destination chip.
      const worker = lane.querySelector('[data-session-row="k-fix-1"]') as HTMLElement
      expect(worker.getAttribute('data-draggable')).toBe('true')
      expect(worker.querySelector('[data-testid="members-page-chip"]')).toBeNull()
    })

    it("wears the crewmate's roster face beside its name instead of the Members page chip", async () => {
      // The crewmate is on the roster, so the row says WHO it is: the same avatar the
      // Crewmates page draws, leading the name. The destination stays in the row's
      // content for a screen reader, outside the title text the scroll peek copies.
      mocks.roster = [{ name: 'kirocrew-pipeline-conductor', slug: 'kirocrew-pipeline-conductor', slot_key: 'member-pipeline', running: false, avatar: { kind: 'ghost' } }]
      try {
        localStorage.setItem('mc-sidebar-lane', 'conductor')
        const { getByTestId, findByTestId } = renderSidebar(listed, [], null, {}, [], inStore)
        await findByTestId('member-row-face')
        const anchor = getByTestId('conductor-view-lane').querySelector('[data-session-row="member-pipeline"]') as HTMLElement
        const title = anchor.querySelector('[data-session-title]') as HTMLElement
        const face = title.querySelector('[data-testid="member-row-face"]') as HTMLElement
        expect(face).not.toBeNull()
        expect(face.querySelector('svg, img')).not.toBeNull()
        expect(title.textContent).toBe('kirocrew-pipeline-conductor')
        expect(anchor.querySelector('[data-testid="members-page-chip"]')).toBeNull()
        expect(anchor.querySelector('[data-testid="members-page-sr"]')?.textContent).toBe('Members page')
        // The click still lands on the Members page.
        fireEvent.click(anchor)
        expect(mocks.navigate).toHaveBeenCalledWith('/members?member=kirocrew-pipeline-conductor')
      } finally {
        mocks.roster = []
      }
    })

    it('draws the face and name on the first line, beside the time, with no second title line', async () => {
      mocks.roster = [{ name: 'kirocrew-pipeline-conductor', slug: 'kirocrew-pipeline-conductor', slot_key: 'member-pipeline', running: false }]
      try {
        localStorage.setItem('mc-sidebar-lane', 'conductor')
        const { getByTestId, findByTestId } = renderSidebar(listed, [], null, {}, [], inStore)
        await findByTestId('member-row-head')
        const anchor = getByTestId('conductor-view-lane').querySelector('[data-session-row="member-pipeline"]') as HTMLElement
        const head = anchor.querySelector('[data-member-head]') as HTMLElement
        expect(head).not.toBeNull()
        // The head sits on the meta line, the same line as the timestamp group.
        expect(head.closest('.session-agent-label')).not.toBeNull()
        // The row's own title line is hidden by the text column while a head is drawn.
        const column = head.closest('.session-agent-label')!.parentElement as HTMLElement
        expect(column.className).toContain('[&:has([data-member-head])>[data-session-title]]:hidden')
        expect(column.querySelector(':scope > [data-session-title]')).not.toBeNull()
      } finally {
        mocks.roster = []
      }
    })

    it('falls back to the Members page chip, with no banner, when the roster read fails', async () => {
      // A recoverable read shows no error banner: the row still works and keeps the chip.
      mocks.rosterFails = true
      mocks.membersCalls = 0
      mocks.lastMembers = null
      try {
        localStorage.setItem('mc-sidebar-lane', 'conductor')
        const { getByTestId, container } = renderSidebar(listed, [], null, {}, [], inStore)
        // Wait on the roster read itself, then let the query commit its error state.
        await waitFor(() => expect(mocks.lastMembers).not.toBeNull())
        await act(async () => { await mocks.lastMembers })
        expect(mocks.membersCalls).toBeGreaterThan(0)
        const anchor = getByTestId('conductor-view-lane').querySelector('[data-session-row="member-pipeline"]') as HTMLElement
        expect(anchor.querySelector('[data-testid="members-page-chip"]')?.textContent).toBe('Members page')
        expect(document.querySelector('[data-testid="sidebar-member-roster-error"]')).toBeNull()
        expect(container.querySelector('[role="alert"]')).toBeNull()
      } finally {
        mocks.rosterFails = false
      }
    })

    it('does not read the crewmate roster in a lane that draws no crewmate row', async () => {
      // The member thread sits in the store, but the tree lane never draws it, so no
      // face is wanted and the roster must not be fetched.
      mocks.membersCalls = 0
      localStorage.setItem('mc-sidebar-lane', 'tree')
      const { container, findAllByText } = renderSidebar(listed, [], null, {}, [], inStore)
      // The lane has drawn its rows: every mount-time read has been issued by now.
      await findAllByText('Unrelated chat')
      expect(container.querySelector('[data-session-row="member-pipeline"]')).toBeNull()
      expect(mocks.membersCalls).toBe(0)
    })

    it('keeps the crewmate row free of a rename box: no hidden editor can trap it', async () => {
      // The head hides the title line, which is where the rename box lives; rename must
      // never start on this row, so a double-click on the head opens nothing.
      mocks.roster = [{ name: 'kirocrew-pipeline-conductor', slug: 'kirocrew-pipeline-conductor', slot_key: 'member-pipeline', running: false }]
      try {
        localStorage.setItem('mc-sidebar-lane', 'conductor')
        const { getByTestId, findByTestId } = renderSidebar(listed, [], null, {}, [], inStore)
        await findByTestId('member-row-head')
        const anchor = getByTestId('conductor-view-lane').querySelector('[data-session-row="member-pipeline"]') as HTMLElement
        fireEvent.doubleClick(anchor.querySelector('[data-member-head]') as HTMLElement)
        expect(anchor.querySelector('textarea')).toBeNull()
        // The ⋯ menu that carries Rename is withheld from this row too.
        expect(anchor.querySelector('[data-close]')).toBeNull()
      } finally {
        mocks.roster = []
      }
    })

    it('keeps the Members page chip for a crewmate the roster no longer lists', async () => {
      // Deleted or renamed: no face nobody can open, and the destination cue survives.
      mocks.lastMembers = null
      mocks.roster = [{ name: 'someone-else', slug: 'someone-else', slot_key: '', running: false }]
      try {
        localStorage.setItem('mc-sidebar-lane', 'conductor')
        const { getByTestId, queryByTestId } = renderSidebar(listed, [], null, {}, [], inStore)
        await waitFor(() => expect(mocks.lastMembers).not.toBeNull())
        await act(async () => { await mocks.lastMembers })
        const anchor = getByTestId('conductor-view-lane').querySelector('[data-session-row="member-pipeline"]') as HTMLElement
        expect(anchor.querySelector('[data-testid="members-page-chip"]')?.textContent).toBe('Members page')
        expect(queryByTestId('member-row-face')).toBeNull()
      } finally {
        mocks.roster = []
      }
    })

    it('names the unit of the child count for a reader who cannot see the chevron', () => {
      // A bare "2" in front of a crew reads as "a count of something": unread, needs-you,
      // children. The accessible name and the tooltip say which, with the number.
      localStorage.setItem('mc-sidebar-lane', 'conductor')
      const { getByTestId } = renderSidebar(listed, [], null, {}, [], inStore)
      const count = getByTestId('conductor-child-count-member-pipeline')
      expect(count.textContent).toBe('2')
      expect(count.getAttribute('aria-label')).toBe('2 sessions this one opened')
      expect(count.getAttribute('title')).toBe('2 sessions this one opened')
    })

    it('a creator that is genuinely gone still reads as an orphan', () => {
      // The store has no such slot, so nothing is borrowed and the glyph's "closed"
      // reading stays true. This is the case the anchor must not swallow.
      localStorage.setItem('mc-sidebar-lane', 'conductor')
      const rows: TestSlot[] = [
        { key: 'k-left', title: 'Left behind', messages: 1, running: false, modified: 1000, parent: { slot: 'member-gone', key: null } },
      ]
      const { getByTestId } = renderSidebar(rows, [], null, {}, [], rows)
      expect(laneRows(getByTestId('conductor-view-lane'))).toEqual(['k-left'])
      expect(getByTestId('conductor-orphan-k-left').getAttribute('data-orphan-of')).toBe('member-gone')
    })
  })
})

// Last, after the file's own hooks that clear localStorage: Crew Mode is a feature preview.
crewModePreviewOn()
