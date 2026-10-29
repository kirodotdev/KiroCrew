/**
 * Sidebar "Group by kind" (issue #25861) -- a flat-lane, opt-in, view-only option
 * in the Sort and filter menu that splits the one flat session list into three
 * headed sections: Crew Mode, Normal, From crewmates.
 *
 * Locks the contract:
 *  (1) OFF by default: the flat list renders exactly as today (date segments on
 *      a date sort, no kind headers).
 *  (2) ON: three headed sections in order Crew Mode / Normal / From crewmates,
 *      each with the right sessions; the pinned band is untouched (pinned rows
 *      lead, with no kind header); an active status filter still narrows the list.
 *  (3) The choice persists to localStorage and is restored on a fresh mount, like
 *      the other Sort/filter menu choices.
 *
 * The flat lane is the surface the issue describes ("one list" that mixes the
 * three kinds); it renders only with folders present and the flat lane chosen,
 * which is why the fixtures seed folders and pre-arm the flat lane.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { safeSetItem } from '../utils/safeStorage'
import type { ChatFolder, ChatSlot } from '../types'

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

const FOLDERS: ChatFolder[] = [
  { id: 'f1', name: 'Alpha', order: 0 },
  { id: 'f2', name: 'Beta', order: 1 },
]

const mocks = vi.hoisted(() => ({ folders: [] as unknown[], config: undefined as unknown }))

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, p: string) => {
      if (p === 'chatFolders') return vi.fn().mockImplementation(() => Promise.resolve(mocks.folders))
      if (p === 'kirocrewConfig') return vi.fn().mockImplementation(() => Promise.resolve(mocks.config ?? []))
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

/** One session of each kind, plus a pinned one. `modified` (epoch seconds)
 *  gives a stable date-desc order when the option is OFF; `crew_mode_root` and
 *  `agent_kind: 'member'` are the kind signals the feature reads. */
const SLOTS: ChatSlot[] = [
  { key: 'k-pin', title: 'Pinned', messages: 1, running: false, folder_id: 'f1', modified: 5000, pinned: true },
  { key: 'k-crew', title: 'A conductor', messages: 1, running: false, folder_id: 'f1', modified: 4000, crew_mode_root: true },
  { key: 'k-normal', title: 'A normal chat', messages: 1, running: false, folder_id: 'f2', modified: 3000 },
  { key: 'k-mate', title: 'A crewmate chat', messages: 1, running: false, folder_id: 'f2', modified: 2000, agent_kind: 'member' },
] as unknown as ChatSlot[]

function renderSidebar(slots: ChatSlot[] = SLOTS, folders: ChatFolder[] = FOLDERS) {
  mocks.folders = folders
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null, slotStatusDetail: {}, subagents: {}, slotActivity: {} } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], folders)
  if (mocks.config !== undefined) qc.setQueryData(['kirocrewConfig'], mocks.config)
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

/** Document-order walk over the flat lane: a kind/date header reads as
 *  `H:<text>`, a session row as its slot key. */
function laneSequence(lane: HTMLElement): string[] {
  return Array.from(lane.querySelectorAll('[data-testid="date-segment-header"], [data-slot-key]'))
    .map(el => el.getAttribute('data-slot-key') ?? `H:${el.textContent}`)
}

/** Open the Sort and filter menu (the toggle lives inside it), the same way the
 *  folder-order test reaches its menu items. */
function openMenu(utils: ReturnType<typeof renderSidebar>) {
  fireEvent.keyDown(utils.getByLabelText('Sort and filter sessions'), { key: 'Enter' })
}

beforeEach(() => {
  localStorage.clear()
  mocks.config = undefined
  // Flat lane chosen, stale-collapse off so every fixture row stays queryable.
  safeSetItem('mc-sidebar-flat-view', '1')
  localStorage.setItem('mc-session-stale-collapse-ms', '0')
})
afterEach(() => { vi.clearAllMocks() })

describe('chat sidebar -- Group by kind (flat lane, opt-in, view only)', () => {
  it('is OFF by default: no kind headers, the list renders as today', async () => {
    const utils = renderSidebar()
    const lane = utils.getByTestId('flat-view-lane')
    // No kind-section headers. Rows are the usual pinned-first, date-desc order.
    const rows = Array.from(lane.querySelectorAll('[data-slot-key]')).map(el => el.getAttribute('data-slot-key'))
    expect(rows).toEqual(['k-pin', 'k-crew', 'k-normal', 'k-mate'])
    for (const el of lane.querySelectorAll('[data-testid="date-segment-header"]')) {
      expect(['Crew Mode', 'Normal', 'From crewmates']).not.toContain(el.textContent)
    }
    // The toggle is present (we are in the flat lane) and reads unchecked.
    openMenu(utils)
    const toggle = await utils.findByTestId('group-by-kind-toggle')
    expect(toggle.getAttribute('aria-checked')).toBe('false')
  })

  it('ON: three headed sections in order, each with the right sessions; pinned band untouched', async () => {
    const utils = renderSidebar()
    openMenu(utils)
    fireEvent.click(await utils.findByTestId('group-by-kind-toggle'))
    await waitFor(() => expect(utils.getByTestId('group-by-kind-toggle').getAttribute('aria-checked')).toBe('true'))
    const lane = utils.getByTestId('flat-view-lane')
    // Pinned row leads with NO kind header above it (the pinned band is untouched),
    // then Crew Mode, Normal, From crewmates sections in that order.
    expect(laneSequence(lane)).toEqual([
      'k-pin',
      'H:Crew Mode', 'k-crew',
      'H:Normal', 'k-normal',
      'H:From crewmates', 'k-mate',
    ])
  })

  it('ON: an empty section renders no header', async () => {
    // No crewmate session, so the "From crewmates" header must not appear.
    const slots = [
      { key: 'k-crew', title: 'A conductor', messages: 1, running: false, folder_id: 'f1', modified: 4000, crew_mode_root: true },
      { key: 'k-normal', title: 'A normal chat', messages: 1, running: false, folder_id: 'f2', modified: 3000 },
    ] as unknown as ChatSlot[]
    const utils = renderSidebar(slots)
    openMenu(utils)
    fireEvent.click(await utils.findByTestId('group-by-kind-toggle'))
    await waitFor(() => expect(utils.getByTestId('group-by-kind-toggle').getAttribute('aria-checked')).toBe('true'))
    const lane = utils.getByTestId('flat-view-lane')
    expect(laneSequence(lane)).toEqual(['H:Crew Mode', 'k-crew', 'H:Normal', 'k-normal'])
  })

  it('ON: an active status filter still narrows the grouped list', async () => {
    // Only "In progress" sessions should remain, still split by kind. Both the
    // filter and the group choice are pre-armed from storage, so no menu needed.
    safeSetItem('mc-session-running-only', '1')
    safeSetItem('mc-sidebar-group-by-kind', '1')
    const slots = [
      { key: 'k-crew-run', title: 'Running conductor', messages: 1, running: true, folder_id: 'f1', modified: 4000, crew_mode_root: true },
      { key: 'k-crew-idle', title: 'Idle conductor', messages: 1, running: false, folder_id: 'f1', modified: 3900, crew_mode_root: true },
      { key: 'k-normal-run', title: 'Running normal', messages: 1, running: true, folder_id: 'f2', modified: 3000 },
      { key: 'k-normal-idle', title: 'Idle normal', messages: 1, running: false, folder_id: 'f2', modified: 2000 },
    ] as unknown as ChatSlot[]
    const { getByTestId } = renderSidebar(slots)
    const lane = getByTestId('flat-view-lane')
    await waitFor(() => expect(laneSequence(lane)).toEqual(['H:Crew Mode', 'k-crew-run', 'H:Normal', 'k-normal-run']))
  })

  it('persists the choice to localStorage and restores it on a fresh mount', async () => {
    const first = renderSidebar()
    openMenu(first)
    fireEvent.click(await first.findByTestId('group-by-kind-toggle'))
    await waitFor(() => expect(localStorage.getItem('mc-sidebar-group-by-kind')).toBe('1'))
    first.unmount()
    // Fresh mount reads the persisted choice straight away: still grouped.
    const second = renderSidebar()
    const lane = second.getByTestId('flat-view-lane')
    await waitFor(() => expect(laneSequence(lane)).toEqual([
      'k-pin',
      'H:Crew Mode', 'k-crew',
      'H:Normal', 'k-normal',
      'H:From crewmates', 'k-mate',
    ]))
  })
})


describe('chat sidebar -- Group by kind in the tree lane (the default view)', () => {
  // Unfiled (no folder_id) sessions of each kind + a pinned one, plus one
  // session inside folder f1 to prove folders are left as they are. The tree
  // lane is the default, so these tests do NOT pre-arm the flat view.
  const TREE_SLOTS: ChatSlot[] = [
    { key: 't-pin', title: 'Pinned root', messages: 1, running: false, modified: 5000, pinned: true },
    { key: 't-crew', title: 'Root conductor', messages: 1, running: false, modified: 4000, crew_mode_root: true },
    { key: 't-normal', title: 'Root normal', messages: 1, running: false, modified: 3000 },
    { key: 't-mate', title: 'Root crewmate', messages: 1, running: false, modified: 2000, agent_kind: 'member' },
    { key: 't-infolder', title: 'Lives in Alpha', messages: 1, running: false, folder_id: 'f1', modified: 3500 },
  ] as unknown as ChatSlot[]

  // The tree lane renders unfiled rows directly under the lane AND foldered rows
  // inside folder blocks (each wrapped in a [data-folder-drop] container). Walk
  // the lane's own headers + the ROOT rows only: a row inside a folder block is
  // excluded by its [data-folder-drop] ancestor.
  function rootSequence(lane: HTMLElement): string[] {
    const nodes = Array.from(lane.querySelectorAll('[data-testid="date-segment-header"], [data-slot-key]'))
    return nodes
      .filter(el => el.getAttribute('data-testid') === 'date-segment-header'
        || !el.closest('[data-folder-drop]'))
      .map(el => el.getAttribute('data-slot-key') ?? `H:${el.textContent}`)
  }

  beforeEach(() => { localStorage.removeItem('mc-sidebar-flat-view') })

  it('OFF by default: the unfiled list renders with no kind headers', async () => {
    const utils = renderSidebar(TREE_SLOTS)
    const lane = utils.getByTestId('tree-view-lane')
    await waitFor(() => expect(lane.querySelector('[data-slot-key="t-crew"]')).not.toBeNull())
    expect(lane.querySelectorAll('[data-testid="date-segment-header"]').length).toBe(0)
  })

  it('ON: the unfiled list splits into Crew Mode / Normal / From crewmates, pinned first', async () => {
    localStorage.setItem('mc-sidebar-group-by-kind', '1')
    const utils = renderSidebar(TREE_SLOTS)
    const lane = utils.getByTestId('tree-view-lane')
    await waitFor(() => {
      expect(rootSequence(lane)).toEqual([
        't-pin',
        'H:Crew Mode', 't-crew',
        'H:Normal', 't-normal',
        'H:From crewmates', 't-mate',
      ])
    })
  })

  it('ON: a foldered session stays inside its folder, not pulled into a root kind section', async () => {
    localStorage.setItem('mc-sidebar-group-by-kind', '1')
    const utils = renderSidebar(TREE_SLOTS)
    const lane = utils.getByTestId('tree-view-lane')
    await waitFor(() => expect(lane.querySelector('[data-slot-key="t-infolder"]')).not.toBeNull())
    // The foldered row is not one of the root rows the kind grouping reorders.
    expect(rootSequence(lane)).not.toContain('t-infolder')
  })
})
