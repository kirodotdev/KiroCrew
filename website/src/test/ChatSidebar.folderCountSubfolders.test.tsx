/**
 * "Include Subfolder Contents in Folder Counts" (Settings → Chat → Sessions).
 *
 * Off (the default), a folder's count is what sits directly in it: its own
 * sessions plus one per subfolder. On, each subfolder's total is added in,
 * so a collapsed parent says how much sits under it. The fixture is the issue's
 * tree: a parent showing 4 (one session and three subfolders) over subfolders
 * holding 1, 22 and 13 sessions reads 40 with the setting on. Both lanes that
 * draw a folder count are covered, the tree and a board column copy.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { ChatFolder, ChatSlot, ChatTag, TagColumn } from '../types'
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
        if (FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children as React.ReactNode)
    })
  // One component per tag: a fresh type on every read would remount the subtree.
  const byTag = new Map<string, ReturnType<typeof make>>()
  const motion = new Proxy({}, {
    get: (_t, tag: string) => {
      if (!byTag.has(tag)) byTag.set(tag, make(tag))
      return byTag.get(tag)
    },
  })
  return {
    motion,
    AnimatePresence: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
    LayoutGroup: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
  }
})

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))

const chatConfig = vi.hoisted(() => ({ tagColumnsEnabled: false, hideEmptyFolderBody: false, countSubfolderContents: false }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ ...chatConfig, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

const mocks = vi.hoisted(() => ({ chatFolders: vi.fn() }))

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

const TAG = 'cccccccc-cccc-cccc-cccc-cccccccccccc'
const COL = 'col-working'

const folders: ChatFolder[] = [
  { id: 'parent', name: 'Parent', order: 0, collapsed: true },
  { id: 'a', name: 'A', order: 0, parent_id: 'parent' },
  { id: 'b', name: 'B', order: 1, parent_id: 'parent' },
  { id: 'c', name: 'C', order: 2, parent_id: 'parent' },
]
const sessionsIn = (folderId: string, n: number, tags = [TAG]): ChatSlot[] =>
  Array.from({ length: n }, (_, i) => ({ key: `${folderId}-${tags.length}-${i}`, title: `${folderId} ${i}`, messages: 1, running: false, folder_id: folderId, tags }))
const slots: ChatSlot[] = [...sessionsIn('parent', 1), ...sessionsIn('a', 1), ...sessionsIn('b', 22), ...sessionsIn('c', 13)]
// The board counts only its column's sessions, and a subfolder whose only match
// sits deeper. So the board fixture adds untagged sessions (outside the column),
// in the parent and in a subfolder `f` holding nothing else, and a subfolder `d`
// that holds nothing itself but whose subfolder `e` holds one.
const boardFolders: ChatFolder[] = [
  ...folders,
  { id: 'd', name: 'D', order: 3, parent_id: 'parent' },
  { id: 'e', name: 'E', order: 0, parent_id: 'd' },
  { id: 'f', name: 'F', order: 4, parent_id: 'parent' },
]
const boardSlots: ChatSlot[] = [...slots, ...sessionsIn('parent', 1, []), ...sessionsIn('e', 1), ...sessionsIn('f', 2, [])]
const tags: ChatTag[] = [{ id: TAG, name: 'Working', color: '#1a1', order: 0, status: true }]
const columns: TagColumn[] = [{ id: COL, name: 'Working', tag_ids: [TAG], mode: 'any', order: 0 }]

function renderSidebar(folderData: ChatFolder[] = folders, slotData: ChatSlot[] = slots) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: false, slots: slotData, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-tags'], chatConfig.tagColumnsEnabled ? tags : [])
  qc.setQueryData(['tag-columns'], chatConfig.tagColumnsEnabled ? columns : [])
  qc.setQueryData(['chat-folders'], folderData)
  qc.setQueryData(['kirocrewConfig'], { dashboard: {} })
  mocks.chatFolders.mockImplementation(() => Promise.resolve(folderData))
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slotData} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

/** The number a tree folder row prints. */
const treeCount = (container: HTMLElement, id: string) =>
  container.querySelector(`[data-folder-row="${id}"] .tabular-nums`)?.textContent
/** The number a board column's copy of a folder prints. */
const boardCount = (container: HTMLElement, id: string) =>
  container.querySelector(`[data-board-folder-row="${COL}:${id}"] > span.text-\\[10px\\]`)?.textContent

beforeEach(() => {
  localStorage.clear()
  chatConfig.tagColumnsEnabled = false
  chatConfig.countSubfolderContents = false
})
afterEach(() => vi.clearAllMocks())

describe('sidebar: folder counts with and without subfolders', () => {
  it.each([
    [false, '4'],
    [true, '40'],
  ])('tree: setting %s, the collapsed parent reads %s and a subfolder keeps its own count', (on, parent) => {
    chatConfig.countSubfolderContents = on
    const { container } = renderSidebar()
    expect(treeCount(container, 'parent')).toBe(parent)
    expect(treeCount(container, 'b')).toBe('22')
  })

  // Off: 1 column session and 4 subfolders (d counts through e). On: plus a's 1,
  // b's 22, c's 13, and d's 2 (e, and e's session). The untagged sessions, and f
  // with them, count in neither.
  it.each([
    [false, '5'],
    [true, '43'],
  ])('board column: setting %s, the parent reads %s', (on, parent) => {
    chatConfig.tagColumnsEnabled = true
    chatConfig.countSubfolderContents = on
    const { container } = renderSidebar(boardFolders, boardSlots)
    expect(boardCount(container, 'parent')).toBe(parent)
  })
})
