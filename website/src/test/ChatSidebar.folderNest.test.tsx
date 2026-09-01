/**
 * Folder-into-folder drag-to-NEST wiring, both layouts.
 *
 * The backend has always supported re-parenting (PATCH /api/chat/folders/{id}
 * with parent_id, cycle-guarded server-side). The gap this locks in was
 * frontend-only:
 *  - Board (tag-column) view rendered folders as dnd-kit sortables for REORDER
 *    but exposed no dnd-kit `folder-drop` droppable, so a folder dragged onto
 *    another could only ever reorder, never nest. Its native onDrop handled
 *    SESSION cards only (folders drag via the pointer sensor, not native DnD,
 *    so their id never reaches dataTransfer).
 *  - List view already nested, but only in a hard-to-hit middle band.
 *
 * dnd-kit's pointer-drag lifecycle can't be faithfully simulated in jsdom (it
 * needs real PointerEvents + layout measurement), so — exactly like
 * ChatSidebar.boardFolderReorder.test.tsx — this asserts the load-bearing
 * WIRING: each folder header is a `folder-drop` droppable (data-folder-drop),
 * which is what handleSidebarDragEnd routes to moveFolderTo. It also guards the
 * native session-assign drop target still composes with the new droppable
 * rather than being replaced.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { ChatTag, TagColumn, ChatFolder } from '../types'
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
// Board (tag-column) layout — same static mock the boardFolderReorder test uses.
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: true, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

const mocks = vi.hoisted(() => ({ updateChatFolder: vi.fn() }))

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

const REVIEW = '22222222-2222-2222-2222-222222222222'
const COL_A = 'col-aaaa'
const FOLDER_A = 'folder-aaaa'
const FOLDER_B = 'folder-bbbb'

import ChatSidebar from '../pages/ChatSidebar'

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
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null } as unknown as RootState['chat'],
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
  mocks.updateChatFolder.mockResolvedValue({})
})
afterEach(() => vi.clearAllMocks())

describe('board view: folder-into-folder nest wiring', () => {
  it('exposes a folder-drop droppable on each board folder (the nest target)', () => {
    const { container } = renderSidebar()
    // data-folder-drop is the dnd-kit nest target handleSidebarDragEnd routes to
    // moveFolderTo. Its ABSENCE is exactly why board nesting did not work before.
    expect(container.querySelector(`[data-folder-drop="${FOLDER_A}"]`)).toBeTruthy()
    expect(container.querySelector(`[data-folder-drop="${FOLDER_B}"]`)).toBeTruthy()
  })

  it('keeps the native session-assign drop target composing with the nest droppable', () => {
    const { container } = renderSidebar()
    // The nest droppable wraps — not replaces — the block carrying the native
    // session onDrop (its testid). Both must be present, nested.
    const drop = container.querySelector(`[data-folder-drop="${FOLDER_A}"]`) as HTMLElement
    expect(drop).toBeTruthy()
    // The nest droppable and the native session-assign block are the SAME div:
    // the dnd-kit setNodeRef + folder-drop marker were added ONTO the existing
    // element that already carried the session onDrop testid, so both live on
    // one node (compose, not replace).
    expect(drop.getAttribute('data-testid')).toBe(`col-${COL_A}-folder-${FOLDER_A}`)
  })

  it('still wraps board folders in the reorder sortable (nest does not break reorder)', () => {
    const { container } = renderSidebar()
    expect(container.querySelector(`[data-col-folder-sortable="${FOLDER_A}"]`)).toBeTruthy()
    expect(container.querySelector(`[data-col-folder-sortable="${FOLDER_B}"]`)).toBeTruthy()
  })
})
