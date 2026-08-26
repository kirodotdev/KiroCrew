/**
 * Board view (tag-columns) folder drag-to-reorder: renderColumnFolder wraps
 * each root folder in a per-column DndContext + SortableContext and makes the
 * folder header the drag handle, routing reorders through the same global
 * reorderFolders() path as list view.
 *
 * dnd-kit's pointer-drag lifecycle can't be faithfully simulated in jsdom (it
 * needs real PointerEvents + layout measurement), so this test asserts the
 * load-bearing WIRING: every root folder is wrapped in the sortable
 * (data-col-folder-sortable) and the header carries the grab-cursor
 * drag-handle affordance.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { ChatTag, TagColumn, ChatFolder } from '../types'

// Render framer-motion elements as plain DOM (jsdom can't run projection).
vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'layoutScroll', 'initial', 'animate', 'exit',
    'transition', 'variants', 'whileHover', 'whileTap', 'whileInView',
    'drag', 'dragConstraints', 'dragElastic', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef((props: any, ref: any) => {
      const clean: any = {}
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
    AnimatePresence: ({ children }: any) => React.createElement(React.Fragment, null, children),
    LayoutGroup: ({ children }: any) => React.createElement(React.Fragment, null, children),
  }
})

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: true, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

const mocks = vi.hoisted(() => ({ updateChatFolder: vi.fn(), chatFolders: vi.fn(), tagColumns: vi.fn(), chatTags: vi.fn() }))

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (target, prop: string) => (prop in target ? target[prop] : vi.fn().mockResolvedValue([])),
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

const REVIEW = '22222222-2222-2222-2222-222222222222'
const COL_A = 'col-aaaa'
const FOLDER_A = 'folder-aaaa'
const FOLDER_B = 'folder-bbbb'

const tags: ChatTag[] = [{ id: REVIEW, name: 'Review', color: '#1a1', order: 0, status: true }]
const columns: TagColumn[] = [{ id: COL_A, name: 'Review', tag_ids: [REVIEW], mode: 'any', order: 0 }]
const folders: ChatFolder[] = [
  { id: FOLDER_A, name: 'Alpha', order: 0 },
  { id: FOLDER_B, name: 'Bravo', order: 1 },
]

function renderSidebar(foldersOverride: ChatFolder[] = folders) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: false, slots: [], approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as any,
    chat: { activeSlot: null } as any,
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-tags'], tags)
  qc.setQueryData(['tag-columns'], columns)
  qc.setQueryData(['chat-folders'], foldersOverride)
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={[]} activeSlot={null} unreadSlots={[]}
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
  // Back the chat-folders refetch with real data: the API proxy resolves
  // unmocked fetches to [], and an awaited act() flush gives that empty
  // refetch time to unmount every board folder mid-test.
  mocks.chatFolders.mockResolvedValue(folders)
  mocks.tagColumns.mockResolvedValue(columns)
  mocks.chatTags.mockResolvedValue(tags)
  mocks.updateChatFolder.mockResolvedValue({})
})
afterEach(() => vi.clearAllMocks())

describe('board view: folder reorder wiring', () => {
  it('wraps every root folder in a sortable so board-view folders are draggable', () => {
    const { container } = renderSidebar()
    expect(container.querySelector(`[data-col-folder-sortable="${FOLDER_A}"]`)).toBeTruthy()
    expect(container.querySelector(`[data-col-folder-sortable="${FOLDER_B}"]`)).toBeTruthy()
  })

  it('marks the folder header as a drag handle (grab cursor)', () => {
    const { container } = renderSidebar()
    // Fork divergence: the board folder header is role="button" (collapse
    // toggle), not upstream's role="group" header rework (from un-ported
    // declutter work) — same element, different role.
    const header = container.querySelector(
      `[data-testid="col-${COL_A}-folder-${FOLDER_A}"] [role="button"]`,
    ) as HTMLElement | null
    expect(header).toBeTruthy()
    expect(header!.className).toContain('cursor-grab')
  })

  it('renders the folder within a sortable inside the column drop target', () => {
    const { container } = renderSidebar()
    const sortable = container.querySelector(`[data-col-folder-sortable="${FOLDER_A}"]`) as HTMLElement
    // The sortable wrapper contains the folder block (with its native
    // session-drop target testid) — proving reorder wiring composes with the
    // existing assign-to-folder drop target rather than replacing it.
    expect(within(sortable).getByTestId(`col-${COL_A}-folder-${FOLDER_A}`)).toBeTruthy()
  })

  it('collapse toggle still works through the drag-handle header', async () => {
    // The whole header carries pointer drag listeners; the 5px activation
    // distance must let plain clicks reach the collapse <button>. Regression
    // guard: a click on the toggle flips this column's collapse state.
    // Board collapse is per-column and client-local, so the flip never
    // writes the server flag.
    const { container } = renderSidebar()
    const header = () => container.querySelector(
      `[data-testid="col-${COL_A}-folder-${FOLDER_A}"] [role="button"][aria-expanded]`,
    ) as HTMLElement
    expect(header().getAttribute('aria-expanded')).toBe('true')
    const { fireEvent, waitFor } = await import('@testing-library/react')
    fireEvent.click(header())
    await waitFor(() => expect(header().getAttribute('aria-expanded')).toBe('false'))
    expect(mocks.updateChatFolder).not.toHaveBeenCalled()
  })

  it('renders board folders sorted by their order field, not cache array position', () => {
    // A drop only rewrites `order` values — the cache array positions never
    // change. If the board renders raw array order, every reorder appears to
    // revert on release. Seed the cache with array order OPPOSITE to the
    // order fields and assert the render follows the order fields.
    const { container } = renderSidebar([
      { id: FOLDER_B, name: 'Bravo', order: 1 },
      { id: FOLDER_A, name: 'Alpha', order: 0 },
    ])
    const rendered = [...container.querySelectorAll('[data-col-folder-sortable]')]
      .map(el => el.getAttribute('data-col-folder-sortable'))
    expect(rendered).toEqual([FOLDER_A, FOLDER_B])
  })
})
