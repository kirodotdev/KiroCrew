/**
 * Regression: a session that matches more than one tag-column
 * filter renders once per matching column. Every row is a framer-motion
 * node with a layoutId; if that id is keyed only on the slot key, the two
 * copies share a layoutId inside the one LayoutGroup and Framer paints only
 * one (the other's box stays but its content vanishes, flipping on click).
 *
 * Fix: layoutId is namespaced by render scope (column id), so each copy has a
 * distinct id. jsdom can't run Framer projection, so the load-bearing
 * assertions are: (1) both copies are present, one under each column, and
 * (2) both carry the active class when the slot is the active slot, proving
 * selection highlight applies to every copy, not just the lead.
 */
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest'
import { render } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { ChatTag, TagColumn, ChatFolder } from '../types'
import type { RootState } from '../store'

// Render motion elements as plain DOM, surfacing layoutId as data-layout-id so
// the test can assert id uniqueness. Strips framer-only props to avoid React
// unknown-attribute warnings.
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
          if (k === 'layoutId') { clean['data-layout-id'] = props[k]; continue }
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
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: true, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({}, { get: () => vi.fn().mockResolvedValue([]) }),
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

const BLOCKED = '11111111-1111-1111-1111-111111111111'
const REVIEW = '22222222-2222-2222-2222-222222222222'
const COL_PLANNED_BLOCKED = 'col-aaaa'
const COL_REVIEW = 'col-bbbb'
const SLOT_KEY = 'chat-cdf-1'

const tags: ChatTag[] = [
  { id: BLOCKED, name: 'Blocked', color: '#e11', order: 0, status: true },
  { id: REVIEW, name: 'Review', color: '#1a1', order: 1, status: true },
]

const columns: TagColumn[] = [
  { id: COL_PLANNED_BLOCKED, name: 'Planned/Blocked', tag_ids: [BLOCKED], mode: 'any', order: 0 },
  { id: COL_REVIEW, name: 'Review', tag_ids: [REVIEW], mode: 'any', order: 1 },
]

const FOLDER_ID = 'folder-zzzz'

const dualTagSlot = {
  key: SLOT_KEY, title: 'CDF Process Availability Onboarding',
  running: false, tags: [BLOCKED, REVIEW], created: '', last_ts: '',
}

function renderSidebar(activeSlot: string | null = null, opts: { foldered?: boolean } = {}) {
  // Foldered: put the slot in a folder that exists in both columns, so each
  // column renders the slot via the renderColumnFolder path (scope
  // `${columnId}:${folder.id}`) instead of the ungrouped path (scope col.id).
  const slot = opts.foldered ? { ...dualTagSlot, folder_id: FOLDER_ID } : dualTagSlot
  const folders: ChatFolder[] = opts.foldered
    ? [{ id: FOLDER_ID, name: 'CDF', order: 0 }]
    : []
  const store = createTestStore({
    dashboard: {
      status: {}, connected: false, slots: [slot], approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as RootState['dashboard'],
    chat: { activeSlot } as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  // Seed board config directly so no api round-trip is needed.
  qc.setQueryData(['chat-tags'], tags)
  qc.setQueryData(['tag-columns'], columns)
  qc.setQueryData(['chat-folders'], folders)
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={[slot]} activeSlot={activeSlot} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

beforeEach(() => localStorage.clear())
afterEach(() => vi.clearAllMocks())

function rowsFor(container: HTMLElement, columnId: string): HTMLElement[] {
  const col = container.querySelector(`[data-testid="column-${columnId}"]`)
  expect(col).toBeTruthy()
  return Array.from((col as HTMLElement).querySelectorAll(`[data-slot-key="${SLOT_KEY}"]`))
}

function layoutIdIn(container: HTMLElement, columnId: string): string | null {
  const rows = rowsFor(container, columnId)
  expect(rows).toHaveLength(1)
  return rows[0].getAttribute('data-layout-id')
}

describe('multi-tag session in two columns', () => {
  it('renders the same slot once in each matching column', () => {
    const { container } = renderSidebar()
    expect(rowsFor(container, COL_PLANNED_BLOCKED)).toHaveLength(1)
    expect(rowsFor(container, COL_REVIEW)).toHaveLength(1)
    // Two copies total across the board.
    expect(container.querySelectorAll(`[data-slot-key="${SLOT_KEY}"]`)).toHaveLength(2)
  })

  it('namespaces each ungrouped copy layoutId by its column id', () => {
    const { container } = renderSidebar()
    // Assert the exact namespaced shape, not just distinctness: a regression
    // that made ids distinct-but-wrong (e.g. random suffix) would still pass a
    // bare not.toBe check, so pin the format to slot-<columnId>-<slotKey>.
    expect(layoutIdIn(container, COL_PLANNED_BLOCKED)).toBe(`slot-${COL_PLANNED_BLOCKED}-${SLOT_KEY}`)
    expect(layoutIdIn(container, COL_REVIEW)).toBe(`slot-${COL_REVIEW}-${SLOT_KEY}`)
  })

  it('namespaces each foldered copy layoutId by column id + folder id', () => {
    const { container } = renderSidebar(null, { foldered: true })
    // Foldered rows render via renderColumnFolder, scope `${columnId}:${folderId}`.
    expect(layoutIdIn(container, COL_PLANNED_BLOCKED)).toBe(`slot-${COL_PLANNED_BLOCKED}:${FOLDER_ID}-${SLOT_KEY}`)
    expect(layoutIdIn(container, COL_REVIEW)).toBe(`slot-${COL_REVIEW}:${FOLDER_ID}-${SLOT_KEY}`)
  })

  it('applies the active highlight to every copy, not just one', () => {
    const { container } = renderSidebar(SLOT_KEY)
    const copies = Array.from(container.querySelectorAll(`[data-slot-key="${SLOT_KEY}"]`))
    expect(copies).toHaveLength(2)
    for (const copy of copies) {
      const row = copy.querySelector('.session-row')
      expect(row?.className).toContain('session-active')
    }
  })
})
