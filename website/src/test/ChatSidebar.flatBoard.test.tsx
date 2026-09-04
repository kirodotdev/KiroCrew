/**
 * Flat view inside the board: with tag columns configured, the flat-view
 * toggle no longer replaces the board with a single lane — the board keeps
 * rendering, and the toggle applies INSIDE each column (folder blocks stop
 * rendering; every matching session sits directly in the lane).
 *
 * Locks the contract:
 *  (1) Columns win the layout: flat view on + columns configured renders the
 *      column strip, never the single flat lane. (Before this contract, the
 *      flat lane silently suppressed the board: "Switch to board view" seeded
 *      lanes, auto-widened the sidebar, and rendered... the same flat list.)
 *  (2) In flat view a column holds foldered AND unfoldered matching sessions
 *      as direct rows, in the tree's sort order (pinned first, then the
 *      active sort) — no folder headers.
 *  (3) In flat view a column with no matching sessions says "No sessions"
 *      even though folders exist — folder structure alone is not content.
 *  (4) Toggling flat off restores the folder blocks inside columns.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent, within, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { safeSetItem } from '../utils/safeStorage'
import type { ChatFolder } from '../types'
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
    React.forwardRef<HTMLElement, Record<string, unknown> & { children?: React.ReactNode }>(
      (props, ref) => {
        const clean: Record<string, unknown> = {}
        for (const k of Object.keys(props)) {
          if (k === 'children') continue
          if (FRAMER_PROPS.has(k)) continue
          clean[k] = props[k]
        }
        return React.createElement(tag, { ...clean, ref }, props.children)
      })
  const motion = new Proxy({}, { get: (_t, tag: string) => make(tag) })
  return {
    motion,
    AnimatePresence: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
    LayoutGroup: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
  }
})

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))
// Stub the folder modal: clicking its submit button drives the real onSubmit
// with a minimal create draft, letting tests exercise the create-success path.
vi.mock('../components/FolderConfigModal', () => ({
  default: ({ open, onSubmit }: { open: boolean; onSubmit: (d: unknown) => Promise<void> }) =>
    open ? (
      <button
        data-testid="stub-folder-modal-submit"
        onClick={() => { void onSubmit({ name: 'New folder', touched: [] }) }}
      >
        submit
      </button>
    ) : null,
}))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: true, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

// Two columns: a bare one (empty tag_ids ⇒ matches every session) and a
// tag-filtered one no fixture session matches (⇒ always empty).
const { columns, folders } = vi.hoisted(() => ({
  columns: [
    { id: 'col-all', name: 'Everything', tag_ids: [] as string[], mode: 'any' as const, order: 0 },
    { id: 'col-empty', name: 'Tagged only', tag_ids: ['t-missing'], mode: 'any' as const, order: 1 },
  ],
  folders: [
    { id: 'f1', name: 'Alpha', order: 0 },
    { id: 'f2', name: 'Beta', order: 1 },
  ] as ChatFolder[],
}))

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, p: string) => {
      if (p === 'chatFolders') return vi.fn().mockImplementation(() => Promise.resolve(folders))
      if (p === 'tagColumns') return vi.fn().mockImplementation(() => Promise.resolve(columns))
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

// Sessions spread across folders + one pinned unfoldered, distinct recency.
const SLOTS = [
  { key: 'k-old-alpha', title: 'Old in Alpha', messages: 1, running: false, folder_id: 'f1', modified: 1000 },
  { key: 'k-new-beta', title: 'Newest in Beta', messages: 1, running: false, folder_id: 'f2', modified: 3000 },
  { key: 'k-mid-root', title: 'Middle unfoldered', messages: 1, running: false, modified: 2000, pinned: true },
]

function renderSidebar(slots: Record<string, unknown>[] = SLOTS) {
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
  qc.setQueryData(['tag-columns'], columns)
  qc.setQueryData(['chat-tags'], [])
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

const columnEl = (container: HTMLElement, id: string) => {
  const col = container.querySelector(`[data-testid="column-${id}"]`)
  expect(col, `column ${id} missing`).toBeTruthy()
  return col as HTMLElement
}
const slotKeysIn = (container: HTMLElement, id: string) =>
  Array.from(columnEl(container, id).querySelectorAll('[data-slot-key]'))
    .map(el => el.getAttribute('data-slot-key'))

// Fixtures carry fixed old timestamps; keep the stale-session collapse off so
// every row stays queryable (its own behavior is pinned elsewhere).
beforeEach(() => {
  localStorage.clear()
  localStorage.setItem('mc-session-stale-collapse-ms', '0')
})
afterEach(() => vi.clearAllMocks())

describe('flat view inside the board', () => {
  it('keeps the board rendered when flat view is on — columns win the layout', () => {
    safeSetItem('mc-sidebar-flat-view', '1')
    const { getByTestId, queryByTestId } = renderSidebar()
    expect(getByTestId('column-strip')).toBeTruthy()
    expect(queryByTestId('flat-view-lane')).toBeNull()
  })

  it('renders foldered + unfoldered sessions as direct rows in the lane, tree sort order, no folder headers', () => {
    safeSetItem('mc-sidebar-flat-view', '1')
    const { container } = renderSidebar()
    // Pinned unfoldered first, then date-desc — same order the flat lane uses.
    expect(slotKeysIn(container, 'col-all')).toEqual(['k-mid-root', 'k-new-beta', 'k-old-alpha'])
    // No folder blocks inside the column.
    const col = columnEl(container, 'col-all')
    expect(within(col).queryByText('Alpha')).toBeNull()
    expect(within(col).queryByText('Beta')).toBeNull()
  })

  it('says "No sessions" in an empty lane even though folders exist', () => {
    safeSetItem('mc-sidebar-flat-view', '1')
    const { container } = renderSidebar()
    expect(slotKeysIn(container, 'col-empty')).toEqual([])
    expect(within(columnEl(container, 'col-empty')).getByText('No sessions')).toBeTruthy()
  })

  it('toggling flat off restores the folder blocks inside columns', () => {
    safeSetItem('mc-sidebar-flat-view', '1')
    const { container, getByTestId } = renderSidebar()
    fireEvent.click(getByTestId('flat-view-toggle'))
    const col = columnEl(container, 'col-all')
    // Folder headers are back...
    expect(within(col).getByText('Alpha')).toBeTruthy()
    expect(within(col).getByText('Beta')).toBeTruthy()
    // ...and folder structure suppresses the empty notice again.
    expect(within(columnEl(container, 'col-empty')).queryByText('No sessions')).toBeNull()
    // The board itself never unmounted.
    expect(getByTestId('column-strip')).toBeTruthy()
  })

  it('keeps the flat toggle reachable in board view, with board-specific copy', () => {
    const { getByTestId } = renderSidebar()
    const toggle = getByTestId('flat-view-toggle')
    // With a board configured the toggle flattens INSIDE each column, so its
    // copy must not promise "all chats without folders" (one combined list).
    expect(toggle.getAttribute('title')).toBe('Flat view — hide folders in the board columns')
    fireEvent.click(toggle)
    const on = getByTestId('flat-view-toggle')
    expect(on.getAttribute('title')).toBe('Show folders in the board columns')
    // The aria-label mirrors the tooltip rather than paraphrasing it.
    expect(on.getAttribute('aria-label')).toBe('Show folders in the board columns')
  })

  it('keeps the per-column New-folder button available in flat view', () => {
    // Folder creation is safe in flat view (creating exits flat mode below),
    // so the affordance must not disappear with the mode.
    const { container, getByTestId } = renderSidebar()
    expect(within(columnEl(container, 'col-all')).queryByTestId('column-new-folder-col-all')).toBeTruthy()
    fireEvent.click(getByTestId('flat-view-toggle'))
    expect(within(columnEl(container, 'col-all')).queryByTestId('column-new-folder-col-all')).toBeTruthy()
  })

  it('creating a folder while flat view is on exits flat view so the folder is visible', async () => {
    safeSetItem('mc-sidebar-flat-view', '1')
    const { container, getByTestId } = renderSidebar()
    // Flat view active: no folder blocks inside the lane.
    expect(within(columnEl(container, 'col-all')).queryByText('Alpha')).toBeNull()
    fireEvent.click(within(columnEl(container, 'col-all')).getByTestId('column-new-folder-col-all'))
    fireEvent.click(getByTestId('stub-folder-modal-submit'))
    // Create succeeded -> flat view exits: folder blocks render again and the
    // persisted preference is cleared.
    await waitFor(() => {
      expect(within(columnEl(container, 'col-all')).getByText('Alpha')).toBeTruthy()
    })
    expect(localStorage.getItem('mc-sidebar-flat-view')).toBe('0')
  })
})
