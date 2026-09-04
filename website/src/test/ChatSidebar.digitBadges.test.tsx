/**
 * Sidebar digit-jump integration:
 *  (1) The sidebar publishes its DISPLAYED session order to
 *      `dashboard.sidebarOrder` (recency-desc by default), which is what the
 *      Ctrl/Alt+digit chat-jump shortcuts index — so Ctrl+1 hits the top row
 *      even under "recent" sort where store order (backend insertion order)
 *      disagrees.
 *  (2) While the jump modifier is held (Alt on non-Mac), the first nine rows
 *      show a digit badge revealing which key picks them; released → gone.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { jumpLetters } from '../hooks/useKeyboardShortcuts'
import { ThemeProvider } from '../hooks/useTheme'
import type { ChatFolder, ChatSlot } from '../types'
import type { RootState } from '../store'

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
// Legacy single-lane list (no tag columns) keeps the rows flat + easy to query.
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

// Store order (backend insertion order) is OLDEST-first on purpose: the
// display order under the default date-desc sort is the exact reverse, which
// is what makes these assertions meaningful.
const SLOTS = [
  { key: 'k-oldest', title: 'Oldest', messages: 1, running: false, modified: 1000 },
  { key: 'k-middle', title: 'Middle', messages: 1, running: false, modified: 2000 },
  { key: 'k-newest', title: 'Newest', messages: 1, running: false, modified: 3000 },
] as unknown as ChatSlot[]

function renderSidebar(slots: ChatSlot[] = SLOTS, folders: ChatFolder[] = []) {
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
  const tree = (s: ChatSlot[]) => (
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={s} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>
  )
  const utils = render(tree(slots))
  return { store, rerenderWith: (s: ChatSlot[]) => utils.rerender(tree(s)), ...utils }
}

// Fixtures here carry fixed old timestamps; keep the stale-session collapse
// off so every row stays queryable (its own behavior is pinned in
// ChatSidebar.staleCollapse.test.tsx).
beforeEach(() => {
  localStorage.clear()
  localStorage.setItem('mc-session-stale-collapse-ms', '0')
})
afterEach(() => vi.clearAllMocks())

describe('chat sidebar — published shortcut order', () => {
  it('publishes the DISPLAYED order (recency-desc), not store order', () => {
    const { store } = renderSidebar()
    expect(store.getState().dashboard.sidebarOrder).toEqual(['k-newest', 'k-middle', 'k-oldest'])
  })

  it('republishes when the sort flips to oldest-first', () => {
    localStorage.setItem('mc-session-sort', 'date-asc')
    const { store } = renderSidebar()
    expect(store.getState().dashboard.sidebarOrder).toEqual(['k-oldest', 'k-middle', 'k-newest'])
  })

  it('tree view: publishes the RENDERED order (folder children first, ungrouped after), not the pinned+sort list', () => {
    // Under date-desc the pinned+sort list is [k-b, k-c, k-a], but the tree
    // renders folder Alpha's children first (k-c, k-a in sort order within
    // the folder) and the ungrouped k-b below the folders. Digit 1 must hit
    // the row the user sees at the top: k-c.
    const { store } = renderSidebar(
      [
        { key: 'k-a', title: 'A', messages: 1, running: false, modified: 1000, folder_id: 'f1' },
        { key: 'k-b', title: 'B', messages: 1, running: false, modified: 3000 },
        { key: 'k-c', title: 'C', messages: 1, running: false, modified: 2000, folder_id: 'f1' },
      ] as unknown as ChatSlot[],
      [{ id: 'f1', name: 'Alpha', order: 0 }] as ChatFolder[],
    )
    expect(store.getState().dashboard.sidebarOrder).toEqual(['k-c', 'k-a', 'k-b'])
  })

  it('tree view: a collapsed folder\'s sessions drop out of the published order', () => {
    // Collapsed children are not rendered, so they cannot be digit targets —
    // the jump handler appends them after the published list for cycling,
    // but digits 1..9 belong to visible rows only.
    const { store } = renderSidebar(
      [
        { key: 'k-a', title: 'A', messages: 1, running: false, modified: 1000, folder_id: 'f1' },
        { key: 'k-b', title: 'B', messages: 1, running: false, modified: 3000 },
        { key: 'k-c', title: 'C', messages: 1, running: false, modified: 2000, folder_id: 'f1' },
      ] as unknown as ChatSlot[],
      [{ id: 'f1', name: 'Alpha', order: 0, collapsed: true }] as ChatFolder[],
    )
    expect(store.getState().dashboard.sidebarOrder).toEqual(['k-b'])
  })
})

describe('chat sidebar — held-modifier digit badges', () => {
  it('shows digits 1..N on rows in display order while Alt is held, hides on release', () => {
    const { queryAllByTestId, getAllByTestId } = renderSidebar()
    expect(queryAllByTestId('digit-jump-badge')).toHaveLength(0)

    act(() => { fireEvent.keyDown(window, { altKey: true, location: 1 }) })
    const badges = getAllByTestId('digit-jump-badge')
    expect(badges).toHaveLength(3)
    // Badge digit ↔ row mapping: 1 = top displayed row (newest).
    const byRow = badges.map(b => [b.closest('[data-session-row]')?.getAttribute('data-session-row'), b.textContent])
    expect(byRow).toContainEqual(['k-newest', '1'])
    expect(byRow).toContainEqual(['k-middle', '2'])
    expect(byRow).toContainEqual(['k-oldest', '3'])

    act(() => { fireEvent.keyUp(window, { altKey: false }) })
    expect(queryAllByTestId('digit-jump-badge')).toHaveLength(0)
  })

  it('rows 10+ get letter badges from the shared jump sequence', () => {
    const many = Array.from({ length: 12 }, (_, i) => ({
      key: `k-${i}`, title: `S${i}`, messages: 1, running: false, modified: 10_000 - i,
    }))
    const { getAllByTestId } = renderSidebar(many)
    act(() => { fireEvent.keyDown(window, { altKey: true, location: 1 }) })
    const byRow = getAllByTestId('digit-jump-badge').map(b => [b.closest('[data-session-row]')?.getAttribute('data-session-row'), b.textContent])
    expect(byRow).toHaveLength(12)
    expect(byRow).toContainEqual(['k-8', '9'])
    // 10th/11th/12th rows: letters b, e, h (a/c/d/f are excluded — select-all,
    // panel nav, split pane, and find own them).
    expect(byRow).toContainEqual(['k-9', 'b'])
    expect(byRow).toContainEqual(['k-10', 'e'])
    expect(byRow).toContainEqual(['k-11', 'h'])
  })

  it('keeps LETTER badges visible while focus is in a text field (clicking a row autofocuses the composer — the overlay must not lose letters)', () => {
    const many = Array.from({ length: 12 }, (_, i) => ({
      key: `k-${i}`, title: `S${i}`, messages: 1, running: false, modified: 10_000 - i,
    }))
    const { getAllByTestId } = renderSidebar(many)
    const textarea = document.createElement('textarea')
    document.body.appendChild(textarea)
    try {
      // Focus the composer BEFORE holding the modifier — the common flow
      // (selecting a session autofocuses the composer, so this is the state
      // the sidebar is in almost always).
      act(() => { textarea.focus(); fireEvent.focusIn(textarea) })
      act(() => { fireEvent.keyDown(window, { altKey: true, location: 1 }) })
      const byRow = getAllByTestId('digit-jump-badge').map(b => b.textContent)
      // Full addressable range badges: digits AND letters.
      expect(byRow).toHaveLength(12)
      expect(byRow).toContain('9')
      expect(byRow).toContain('b')
      expect(byRow).toContain('e')
    } finally {
      textarea.remove()
    }
  })

  it('badges stop at the end of the addressable range', () => {
    const cap = 9 + jumpLetters().length
    const many = Array.from({ length: cap + 3 }, (_, i) => ({
      key: `k-${i}`, title: `S${i}`, messages: 1, running: false, modified: 100_000 - i,
    }))
    const { getAllByTestId } = renderSidebar(many)
    act(() => { fireEvent.keyDown(window, { altKey: true, location: 1 }) })
    expect(getAllByTestId('digit-jump-badge')).toHaveLength(cap)
  })

  it('freezes digits and the published order while held: a background recency bump neither renumbers badges nor moves the jump targets until release', () => {
    const { store, rerenderWith, getAllByTestId } = renderSidebar()
    act(() => { fireEvent.keyDown(window, { altKey: true, location: 1 }) })
    expect(store.getState().dashboard.sidebarOrder).toEqual(['k-newest', 'k-middle', 'k-oldest'])

    // Mid-hold, agent activity bumps the oldest session to the top of the
    // date-desc sort (touchSlotActivity semantics: last-activity changes).
    rerenderWith([
      { key: 'k-oldest', title: 'Oldest', messages: 1, running: false, modified: 4000 },
      { key: 'k-middle', title: 'Middle', messages: 1, running: false, modified: 2000 },
      { key: 'k-newest', title: 'Newest', messages: 1, running: false, modified: 3000 },
    ] as unknown as ChatSlot[])

    // Frozen: each digit stays glued to the session the user aimed at (badges
    // travel with their rows), and the store order the jump handler reads is
    // unchanged — pressing 1 still picks k-newest.
    const byRow = getAllByTestId('digit-jump-badge').map(b => [b.closest('[data-session-row]')?.getAttribute('data-session-row'), b.textContent])
    expect(byRow).toContainEqual(['k-newest', '1'])
    expect(byRow).toContainEqual(['k-middle', '2'])
    expect(byRow).toContainEqual(['k-oldest', '3'])
    expect(store.getState().dashboard.sidebarOrder).toEqual(['k-newest', 'k-middle', 'k-oldest'])

    // Release: the deferred reorder publishes.
    act(() => { fireEvent.keyUp(window, { altKey: false }) })
    expect(store.getState().dashboard.sidebarOrder).toEqual(['k-oldest', 'k-newest', 'k-middle'])
  })

  it('renumbers badges compactly when a session closes mid-hold, matching the jump handler compaction', () => {
    const { store, rerenderWith, getAllByTestId } = renderSidebar()
    act(() => { fireEvent.keyDown(window, { altKey: true, location: 1 }) })

    // Mid-hold, the middle session closes. The jump handler compacts the
    // frozen order (digit 2 now targets k-oldest), so the badges must
    // renumber the same way — a stale "3" on k-oldest would be picked by
    // digit 2, the exact badge/target drift the freeze exists to prevent.
    rerenderWith([
      { key: 'k-oldest', title: 'Oldest', messages: 1, running: false, modified: 1000 },
      { key: 'k-newest', title: 'Newest', messages: 1, running: false, modified: 3000 },
    ] as unknown as ChatSlot[])

    const byRow = getAllByTestId('digit-jump-badge').map(b => [b.closest('[data-session-row]')?.getAttribute('data-session-row'), b.textContent])
    expect(byRow).toHaveLength(2)
    expect(byRow).toContainEqual(['k-newest', '1'])
    expect(byRow).toContainEqual(['k-oldest', '2'])
    // The published frozen order is untouched — the handler compacts at
    // keypress time from the same live-slot basis the badge map now uses.
    expect(store.getState().dashboard.sidebarOrder).toEqual(['k-newest', 'k-middle', 'k-oldest'])
  })
})
