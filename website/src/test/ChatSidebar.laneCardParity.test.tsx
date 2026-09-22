/**
 * Chat sidebar — the conductor lane renders the SAME card as the flat lane.
 *
 * This is the test that would have caught the defect the first round shipped. The lane
 * used to wrap the card in a flex column with the chevron and the counts beside it, and
 * every visible consequence followed from that one wrapper: the card lost width, so its
 * title truncated early and its hover controls crowded the text; its own divider (inset
 * to the content x) stopped short of the row; and the count cluster sat in the column the
 * card's top line keeps for the timestamp, so the time looked missing.
 *
 * None of that is reachable from an assertion about the lane's own markup, because the
 * lane's markup was right -- it was what it did to the CARD that was wrong. So this test
 * asserts the thing that actually matters: one slot, rendered in both lanes, yields the
 * same DOM apart from three named additions.
 *
 * The additions, and nothing else:
 *   1. an indent spacer (`data-conductor-indent`) on a nested row,
 *   2. a chevron button (`conductor-chevron-*`) on a row that opened sessions,
 *   3. a count cluster inside the card's existing meta group, immediately left of the
 *      time (`conductor-child-count-*`, `conductor-needs-you-*`, `conductor-running-*`,
 *      `conductor-depth-*`, `conductor-orphan-*`).
 *
 * Plus the row-level markers the lane needs to address its rows (`data-conductor-depth`,
 * and the `conductor-nested-row` test id). Anything else differing is a regression.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { fireEvent, render, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

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

const mocks = vi.hoisted(() => ({ folders: [] as unknown[] }))

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, p: string) => {
      if (p === 'chatFolders') return vi.fn().mockImplementation(() => Promise.resolve(mocks.folders))
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

/** The row under comparison, plus a creator so the conductor lane is offered at all.
 *
 *  `last_turn_ts` is an ISO string, NOT `modified`: the card's timestamp comes from
 *  `slotActivityTs`, which reads `last_turn_ts || last_ts || created`. A fixture with
 *  only `modified` renders no time at all, which is how the first round's screenshots
 *  came out timeless and why this file pins the time explicitly. */
const NOW = Date.now()
const iso = (msAgo: number) => new Date(NOW - msAgo).toISOString()
const SUBJECT: TestSlot[] = [
  { key: 'k-parent', title: 'Refactor the ingest pipeline', messages: 12, running: false, modified: NOW - 60_000, last_turn_ts: iso(60_000), agent: 'kirocrew' },
  { key: 'k-subject', title: 'Migrate the schema module', messages: 8, running: true, modified: NOW - 120_000, last_turn_ts: iso(120_000), agent: 'kirocrew', parent: { slot: 'k-parent', key: 'k-parent' } },
]

function renderLane(lane: 'flat' | 'conductor', slots: TestSlot[] = SUBJECT) {
  localStorage.setItem('mc-sidebar-lane', lane)
  // Expanded, so the nested subject row is actually rendered in the conductor lane.
  localStorage.setItem('mc-sidebar-conductor-expanded', JSON.stringify(['k-parent']))
  // The flat lane needs a folder to exist before it is offered.
  mocks.folders = [{ id: 'f', name: 'Infra', parent_id: null }]
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint',
      sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null, slotStatusDetail: {}, subagents: {}, slotActivity: {}, revealRequest: null } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], mocks.folders)
  const utils = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots as never} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="kirocrew" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  const row = utils.container.querySelector('[data-slot-key="k-subject"]') as HTMLElement | null
  if (!row) throw new Error(`no k-subject row in the ${lane} lane`)
  return { ...utils, row }
}

/** The additions the conductor lane is allowed to make, by test id prefix. */
const ALLOWED_TESTIDS = [
  'conductor-chevron-',
  'conductor-child-count-',
  'conductor-needs-you-',
  'conductor-running-',
  'conductor-depth-',
  'conductor-orphan-',
]

/** The row's DOM with the lane's permitted additions stripped out, so what remains is
 *  comparable across lanes. Attribute-level markers the lane needs for addressing
 *  (`data-conductor-depth`, the nested-row test id) are dropped too.
 *
 *  Three further things are normalized, none of them card fidelity:
 *
 *  - `data-session-scope` / `data-session-container` carry the lane's NAME. They are
 *    what the sidebar's nav and hover-hold use to bound themselves to one lane, so of
 *    course they differ -- that is their job.
 *  - `id` on Radix triggers is React's generated identity, different per render tree.
 *  - `draggable` is the one REAL lane-level difference. The flat lane registers a
 *    dnd-kit context, so its rows leave native HTML5 drag off; the conductor lane
 *    registers none (row order there is who-opened-whom, so a drop inside the lane
 *    has nothing to land on), which leaves native drag on. That is a property of the
 *    lane, not of the card, and it is disclosed in the PR body rather than papered
 *    over here. */
function strippedRow(row: HTMLElement): string {
  const copy = row.cloneNode(true) as HTMLElement
  copy.removeAttribute('data-conductor-depth')
  if (copy.getAttribute('data-testid') === 'conductor-nested-row') copy.removeAttribute('data-testid')
  copy.querySelectorAll('[data-conductor-indent]').forEach(n => n.remove())
  for (const el of Array.from(copy.querySelectorAll('[data-testid]'))) {
    const id = el.getAttribute('data-testid') ?? ''
    if (ALLOWED_TESTIDS.some(p => id.startsWith(p))) el.remove()
  }
  // The chevron's placeholder twin on a childless row is an addition too.
  copy.querySelectorAll('span[aria-hidden="true"].mt-2\\.5').forEach(n => n.remove())
  // A row with counts but no time and no pin renders the trailing-meta group only
  // because of the counts. Once those are stripped the wrapper is an addition too.
  copy.querySelectorAll('span.ml-auto').forEach(n => { if (n.children.length === 0) n.remove() })
  for (const el of [copy, ...Array.from(copy.querySelectorAll('*'))]) {
    el.removeAttribute('data-session-scope')
    el.removeAttribute('data-session-container')
    if ((el.getAttribute('id') ?? '').startsWith('radix-')) el.removeAttribute('id')
    if (el.hasAttribute('aria-controls')) el.removeAttribute('aria-controls')
    if (el.hasAttribute('draggable')) el.setAttribute('draggable', 'lane-dependent')
  }
  return copy.innerHTML
}

describe('conductor lane card parity', () => {
  beforeEach(() => { localStorage.clear() })
  afterEach(() => { localStorage.clear(); vi.clearAllMocks() })

  it('renders the same card as the flat lane, apart from the named additions', () => {
    const flat = renderLane('flat')
    const flatHtml = strippedRow(flat.row)
    flat.unmount()

    const conductor = renderLane('conductor')
    const conductorHtml = strippedRow(conductor.row)

    expect(conductorHtml).toBe(flatHtml)
  })

  it('keeps the timestamp in the card, with the counts to its LEFT', () => {
    // The first round's wrapper hung the counts outside the card, in the column the
    // top line keeps for the time -- so the time read as missing. Both must be in the
    // card's own trailing-meta group, counts first.
    const { row } = renderLane('conductor', [
      ...SUBJECT,
      { key: 'k-grandchild', title: 'Profile the hot loop', messages: 2, running: false, modified: NOW - 180_000, last_turn_ts: iso(180_000), agent: 'kirocrew', parent: { slot: 'k-subject', key: 'k-subject' } },
    ])
    const count = within(row).getByTestId('conductor-child-count-k-subject')
    const meta = count.parentElement!
    expect(meta.className).toContain('ml-auto')

    const texts = Array.from(meta.children).map(c => c.textContent ?? '')
    expect(texts[0]).toBe('1')
    // Something after the count renders a relative time, so the time survived and sits
    // to the right of the cluster.
    expect(texts.slice(1).join(' ')).toMatch(/\d/)
  })

  it('leaves the row divider spanning the row at every depth', () => {
    // The divider is a SIBLING of the card, so indenting the card must not shorten it.
    // Indent lives on a spacer inside the row; the divider's own insets are untouched.
    const { container } = renderLane('conductor', [
      ...SUBJECT,
      { key: 'k-grandchild', title: 'Profile the hot loop', messages: 2, running: false, modified: NOW - 180_000, last_turn_ts: iso(180_000), agent: 'kirocrew', parent: { slot: 'k-subject', key: 'k-subject' } },
    ])
    const dividers = Array.from(container.querySelectorAll('.border-b.border-border'))
      .filter(d => (d as HTMLElement).className.includes('-mt-px'))
    expect(dividers.length).toBeGreaterThan(0)
    const classes = new Set(dividers.map(d => (d as HTMLElement).className))
    // One spelling for every depth: no per-depth inset crept in.
    expect(classes.size).toBe(1)
  })

  it('does not give the flat lane any conductor addition', () => {
    const { row } = renderLane('flat')
    expect(row.getAttribute('data-conductor-depth')).toBeNull()
    expect(row.querySelector('[data-conductor-indent]')).toBeNull()
    for (const prefix of ALLOWED_TESTIDS) {
      expect(row.querySelector(`[data-testid^="${prefix}"]`)).toBeNull()
    }
  })

  it('keeps the chevron press off the row, so it does not switch session', () => {
    const { row, container } = renderLane('conductor')
    const parentRow = container.querySelector('[data-slot-key="k-parent"]') as HTMLElement
    const chevron = within(parentRow).getByTestId('conductor-chevron-k-parent')
    fireEvent.click(chevron)
    // The subject is k-parent's child, so a collapse removes it from the lane. If the
    // press had also reached the row, the sidebar would have switched session instead.
    expect(container.querySelector('[data-slot-key="k-subject"]')).toBeNull()
    expect(row).toBeTruthy()
  })
})
