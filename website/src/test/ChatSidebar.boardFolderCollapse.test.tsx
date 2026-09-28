/**
 * Board view (tag-columns) renders the same root folders once per column, but
 * a folder's `collapsed` field is one server-persisted flag. Collapsing a
 * folder from one column must NOT collapse its copies in the other columns:
 * each (column, folder) pair keeps a client-local override layered over the
 * server flag.
 *
 * Load-bearing assertions:
 *   (1) toggling a folder in column A leaves column B's copy untouched;
 *   (2) the toggle never writes the server flag (no updateChatFolder call);
 *   (3) overrides survive a remount via localStorage;
 *   (4) a column with no override follows the server default.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'
import type { ChatTag, TagColumn, ChatFolder } from '../types'

// Render framer-motion elements as plain DOM because jsdom cannot run projection.
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
  loadChatConfig: () => ({ tagColumnsEnabled: true, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

const mocks = vi.hoisted(() => ({
  updateChatFolder: vi.fn(),
}))

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

const BLOCKED = '11111111-1111-1111-1111-111111111111'
const REVIEW = '22222222-2222-2222-2222-222222222222'
const COL_A = 'col-aaaa'
const COL_B = 'col-bbbb'
const FOLDER_ID = 'folder-zzzz'

const tags: ChatTag[] = [
  { id: BLOCKED, name: 'Blocked', color: '#e11', order: 0, status: true },
  { id: REVIEW, name: 'Review', color: '#1a1', order: 1, status: true },
]
const columns: TagColumn[] = [
  { id: COL_A, name: 'Planned/Blocked', tag_ids: [BLOCKED], mode: 'any', order: 0 },
  { id: COL_B, name: 'Review', tag_ids: [REVIEW], mode: 'any', order: 1 },
]

function renderSidebar(folderData: ChatFolder[]) {
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
  qc.setQueryData(['chat-folders'], folderData)
  const view = render(
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
  return { ...view, store, qc }
}

function folderHeader(container: HTMLElement, columnId: string): HTMLElement {
  const block = container.querySelector(`[data-testid="col-${columnId}-folder-${FOLDER_ID}"]`) as HTMLElement
  expect(block).toBeTruthy()
  return block.querySelector('[role="button"][aria-expanded]') as HTMLElement
}

beforeEach(() => {
  localStorage.clear()
  mocks.updateChatFolder.mockResolvedValue({ ok: true })
})
afterEach(() => vi.clearAllMocks())

describe('board view: per-column folder collapse', () => {
  it('collapsing a folder in one column leaves the other column expanded', () => {
    const { container } = renderSidebar([{ id: FOLDER_ID, name: 'CDF', order: 0, collapsed: false }])
    const headerA = folderHeader(container, COL_A)
    const headerB = folderHeader(container, COL_B)
    expect(headerA.getAttribute('aria-expanded')).toBe('true')
    expect(headerB.getAttribute('aria-expanded')).toBe('true')

    fireEvent.click(headerA)

    expect(folderHeader(container, COL_A).getAttribute('aria-expanded')).toBe('false')
    // The load-bearing assertion: column B's copy did not follow.
    expect(folderHeader(container, COL_B).getAttribute('aria-expanded')).toBe('true')
  })

  it('never writes the server collapsed flag from a board toggle', () => {
    const { container } = renderSidebar([{ id: FOLDER_ID, name: 'CDF', order: 0, collapsed: false }])
    fireEvent.click(folderHeader(container, COL_A))
    fireEvent.click(folderHeader(container, COL_B))
    expect(mocks.updateChatFolder).not.toHaveBeenCalled()
  })

  it('persists per-column state across a remount', () => {
    const folderData: ChatFolder[] = [{ id: FOLDER_ID, name: 'CDF', order: 0, collapsed: false }]
    const first = renderSidebar(folderData)
    fireEvent.click(folderHeader(first.container, COL_A))
    first.unmount()

    const second = renderSidebar(folderData)
    expect(folderHeader(second.container, COL_A).getAttribute('aria-expanded')).toBe('false')
    expect(folderHeader(second.container, COL_B).getAttribute('aria-expanded')).toBe('true')
  })

  it('a column without an override follows the server default', () => {
    // Server says collapsed; expanding in A must not expand B, whose state is
    // still the server flag.
    const { container } = renderSidebar([{ id: FOLDER_ID, name: 'CDF', order: 0, collapsed: true }])
    expect(folderHeader(container, COL_A).getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(folderHeader(container, COL_A))
    expect(folderHeader(container, COL_A).getAttribute('aria-expanded')).toBe('true')
    expect(folderHeader(container, COL_B).getAttribute('aria-expanded')).toBe('false')
  })
})
