/**
 * Nested subfolder rows are registered SORTABLES, not bare draggables.
 *
 * The DOM half of #10428. A nested row was wrapped in a plain draggable, so it
 * had no sortable id and appeared in no sibling ring: dnd-kit had no reorder
 * target to resolve to, whatever the collision layer asked for. The wrapper
 * marker asserted here is what proves the registration happened — jsdom cannot
 * drive a pointer drag, so the routing itself is covered by
 * ChatSidebar.nestedFolderReorder.test.tsx and the renumber by
 * reorderFolders.test.ts.
 *
 * Root rows keep their own marker, and both are asserted together: the point of
 * the change is that the two levels are now the same kind of thing, and a test
 * that watched only the nested one would pass just as well if the root ring had
 * been dismantled to get there.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { ChatFolder } from '../types'
import type { RootState } from '../store'

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
// Legacy list layout: tag columns OFF, which is the lane that draws nested rows.
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

const mocks = vi.hoisted(() => ({ updateChatFolder: vi.fn(), reorderChatFolders: vi.fn() }))

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

const PARENT = 'folder-parent'
const KID_ONE = 'folder-kid-one'
const KID_TWO = 'folder-kid-two'
const folders: ChatFolder[] = [
  { id: PARENT, name: 'Parent', order: 0, collapsed: false },
  // Two children, because a single one cannot show that the RING exists: with one
  // row there is no second member for a reorder to move against.
  { id: KID_ONE, name: 'Kid One', order: 0, collapsed: false, parent_id: PARENT },
  { id: KID_TWO, name: 'Kid Two', order: 1, collapsed: false, parent_id: PARENT },
]

function renderSidebar() {
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
  qc.setQueryData(['chat-tags'], [])
  qc.setQueryData(['tag-columns'], [])
  qc.setQueryData(['chat-folders'], folders)
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
  mocks.reorderChatFolders.mockResolvedValue({})
})
afterEach(() => vi.clearAllMocks())

describe('nested subfolders are sortable rows', () => {
  it('wraps every nested subfolder in a sortable', () => {
    const { container } = renderSidebar()
    expect(container.querySelector(`[data-subfolder-sortable="${KID_ONE}"]`)).toBeTruthy()
    expect(container.querySelector(`[data-subfolder-sortable="${KID_TWO}"]`)).toBeTruthy()
  })

  it('keeps the root row sortable, so both levels reorder the same way', () => {
    const { container } = renderSidebar()
    expect(container.querySelector(`[data-folder-sortable="${PARENT}"]`)).toBeTruthy()
    // And the parent is not ALSO wrapped as a nested row: a root folder belongs
    // to the root ring only, or one drag would carry two sibling sets.
    expect(container.querySelector(`[data-subfolder-sortable="${PARENT}"]`)).toBeNull()
  })

  it('keeps each nested row a re-parent drop target as well', () => {
    // Both gestures, not a swap of one for the other: the `folder-drop` zone is
    // what handleSidebarDragEnd routes to the move path.
    const { container } = renderSidebar()
    expect(container.querySelector(`[data-folder-drop="${KID_ONE}"]`)).toBeTruthy()
    expect(container.querySelector(`[data-folder-drop="${KID_TWO}"]`)).toBeTruthy()
  })
})
