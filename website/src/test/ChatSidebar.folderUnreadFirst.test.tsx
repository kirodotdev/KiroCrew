/**
 * "Move folders with unread chats to the top" (#13410): an opt-in view over the
 * folder order. Off by default; on, folders holding an unread chat are drawn
 * first, newest unread first, and no folder position is ever written.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

// Captures the sidebar's drag-end handler so a case can script a folder drop
// without driving dnd-kit's sensors through jsdom (the technique of
// ChatSidebar.dragFreezeOrder.test.tsx). Every DndContext in the sidebar is
// wired to the same handler, so the last one rendered is as good as any. The
// per-row `useSortable` arguments are captured too: whether a folder row is a
// reorder TARGET is decided there (`disabled.droppable`), which no DOM query
// can see.
const dnd = vi.hoisted(() => ({
  handlers: {} as Record<string, ((e: unknown) => void) | undefined>,
  sortables: new Map<string, { disabled?: boolean | { draggable?: boolean; droppable?: boolean } }>(),
}))
vi.mock('@dnd-kit/core', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@dnd-kit/core')>()
  return {
    ...actual,
    DndContext: (props: { children?: unknown; onDragStart?: (e: unknown) => void; onDragEnd?: (e: unknown) => void }) => {
      dnd.handlers.onDragStart = props.onDragStart
      dnd.handlers.onDragEnd = props.onDragEnd
      return props.children as never
    },
  }
})
vi.mock('@dnd-kit/sortable', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@dnd-kit/sortable')>()
  return {
    ...actual,
    useSortable: (args: Parameters<typeof actual.useSortable>[0]) => {
      const data = args.data as { type?: string } | undefined
      if (data?.type === 'folder') dnd.sortables.set(String(args.id), { disabled: args.disabled })
      return actual.useSortable(args)
    },
  }
})

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

/** The gateway's config as the test's fake server holds it. `patchConfig` writes
 *  into it and `kirocrewConfig` reads it back, so the settle-time refetch the
 *  optimistic overlay triggers after a save sees the value the save landed --
 *  exactly the round trip the real endpoint pair performs. */
const serverConfig: { dashboard: Record<string, unknown> } = { dashboard: {} }
const patchConfig = vi.fn(async (path: string, value: unknown) => {
  if (path === 'dashboard.folder_sort') serverConfig.dashboard.folder_sort = value
  return { ok: true }
})
const kirocrewConfig = vi.fn(async () => ({ dashboard: { ...serverConfig.dashboard } }))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, name: string) => {
      if (name === 'patchConfig') return patchConfig
      if (name === 'kirocrewConfig') return kirocrewConfig
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
import type { ChatFolder, ChatSlot } from '../types'
import { bySidebarOrder } from '../utils/folderTree'

import { unreadFolderLatest, unreadFirstComparator } from '../pages/chat-sidebar/unreadFolderFloat'

/** Stored order A, B, C, D. B and D each hold an unread chat; D's is newer. */
const FOLDERS: ChatFolder[] = [
  { id: 'fa', name: 'A', order: 0, collapsed: true },
  { id: 'fb', name: 'B', order: 1, collapsed: true },
  { id: 'fc', name: 'C', order: 2, collapsed: true },
  { id: 'fd', name: 'D', order: 3, collapsed: true },
]
const SLOTS = [
  { key: 's-a', folder_id: 'fa', last_turn_ts: '2026-10-29T09:00:00Z' },
  { key: 's-b', folder_id: 'fb', last_turn_ts: '2026-10-29T10:00:00Z' },
  { key: 's-d', folder_id: 'fd', last_turn_ts: '2026-10-29T11:00:00Z' },
] as unknown as ChatSlot[]
const UNREAD = ['s-b', 's-d']

function renderSidebar(folders: ChatFolder[]) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots: SLOTS, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: UNREAD, updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null, slotStatusDetail: {}, subagents: {}, slotActivity: {}, workflowRuns: {} } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity, refetchOnMount: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], folders)
  qc.setQueryData(['kirocrewConfig'], { dashboard: { folder_sort: 'custom' } })
  const utils = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={SLOTS} activeSlot={null} unreadSlots={UNREAD}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return { ...utils, qc }
}

function drawnFolderIds(container: HTMLElement): string[] {
  return [...container.querySelectorAll('[data-folder-row]')].map(el => el.getAttribute('data-folder-row') ?? '')
}

beforeEach(() => { localStorage.clear(); patchConfig.mockClear(); kirocrewConfig.mockClear(); dnd.sortables.clear() })
afterEach(() => vi.clearAllMocks())

describe('chat sidebar — move folders with unread chats to the top', () => {
  it('is off by default: the stored order is drawn, unread or not', () => {
    const { container } = renderSidebar(FOLDERS)
    expect(drawnFolderIds(container)).toEqual(['fa', 'fb', 'fc', 'fd'])
  })

  it('on, draws unread folders first, newest unread first, the rest in stored order', () => {
    localStorage.setItem('mc-sidebar-folders-unread-first', '1')
    const { container } = renderSidebar(FOLDERS)
    expect(drawnFolderIds(container)).toEqual(['fd', 'fb', 'fa', 'fc'])
  })

  it('keeps the new-chat folder picker in stored order while the tree floats unread folders', async () => {
    localStorage.setItem('mc-sidebar-folders-unread-first', '1')
    const view = renderSidebar(FOLDERS)
    expect(drawnFolderIds(view.container)).toEqual(['fd', 'fb', 'fa', 'fc'])
    fireEvent.keyDown(view.getByLabelText('More create options'), { key: 'Enter' })
    fireEvent.keyDown(await view.findByRole('menuitem', { name: 'New chat in folder' }), { key: 'ArrowRight' })
    const items = await view.findAllByRole('menuitem', { name: /^[ABCD]$/ })
    expect(items.map(item => item.textContent?.trim())).toEqual(['A', 'B', 'C', 'D'])
    expect(drawnFolderIds(view.container)).toEqual(['fd', 'fb', 'fa', 'fc'])
  })

  it('the menu toggle switches the view and never writes a folder or the folder mode', async () => {
    const view = renderSidebar(FOLDERS)
    fireEvent.keyDown(view.getByLabelText('Sort and filter sessions'), { key: 'Enter' })
    const item = await view.findByTestId('folder-order-unread-first')
    expect(item).toHaveAttribute('role', 'menuitemcheckbox')
    expect(item).toHaveAttribute('aria-checked', 'false')
    expect(item.textContent).toContain('Move folders with unread chats to the top')
    fireEvent.click(item)
    await waitFor(() => expect(drawnFolderIds(view.container)).toEqual(['fd', 'fb', 'fa', 'fc']))
    expect(localStorage.getItem('mc-sidebar-folders-unread-first')).toBe('1')
    expect(view.getByTestId('folder-order-unread-first')).toHaveAttribute('aria-checked', 'true')
    expect(view.getByTestId('folder-order-unread-first-note').textContent).toBe('Folders cannot be dragged into place while this is on')
    fireEvent.click(view.getByTestId('folder-order-unread-first'))
    await waitFor(() => expect(drawnFolderIds(view.container)).toEqual(['fa', 'fb', 'fc', 'fd']))
    // Display only: the stored positions and the server-side mode are untouched.
    expect(view.qc.getQueryData<ChatFolder[]>(['chat-folders'])).toEqual(FOLDERS)
    expect(patchConfig).not.toHaveBeenCalled()
  })

  it('on, a sibling drop is no reorder: nothing is written and no "Switch to Custom" hint shows', () => {
    localStorage.setItem('mc-sidebar-folders-unread-first', '1')
    const view = renderSidebar(FOLDERS)
    expect(dnd.sortables.get('fa')?.disabled).toEqual({ droppable: true })
    act(() => {
      dnd.handlers.onDragEnd!({
        active: { id: 'fa', data: { current: { type: 'folder' } } },
        over: { id: 'fd', data: { current: { type: 'folder' } } },
      })
    })
    expect(view.qc.getQueryData<ChatFolder[]>(['chat-folders'])).toEqual(FOLDERS)
    expect(view.queryByTestId('folder-reorder-hint')).toBeNull()
  })

  it('nested: an unread chat in a subfolder floats its parent and the subfolder among its siblings', () => {
    localStorage.setItem('mc-sidebar-folders-unread-first', '1')
    const nested: ChatFolder[] = [
      { id: 'p1', name: 'P1', order: 0, collapsed: false },
      { id: 'p2', name: 'P2', order: 1, collapsed: false },
      { id: 'c1', name: 'C1', order: 0, parent_id: 'p2', collapsed: true },
      { id: 'c2', name: 'C2', order: 1, parent_id: 'p2', collapsed: true },
    ]
    const slots = [{ key: 's-b', folder_id: 'c2', last_turn_ts: '2026-10-29T10:00:00Z' }] as unknown as ChatSlot[]
    const latest = unreadFolderLatest(nested, slots, { 's-b': 'c2' }, new Set(['s-b']))
    expect([...latest.keys()].sort()).toEqual(['c2', 'p2'])
    const cmp = unreadFirstComparator(bySidebarOrder, latest)
    expect(nested.filter(f => !f.parent_id).sort(cmp).map(f => f.id)).toEqual(['p2', 'p1'])
    expect(nested.filter(f => f.parent_id === 'p2').sort(cmp).map(f => f.id)).toEqual(['c2', 'c1'])
  })

  it('an unread chat with no timestamp still floats, after the stamped ones; a parent_id loop terminates', () => {
    const loop: ChatFolder[] = [
      { id: 'x', name: 'X', order: 0, parent_id: 'y', collapsed: true },
      { id: 'y', name: 'Y', order: 1, parent_id: 'x', collapsed: true },
      { id: 'z', name: 'Z', order: 2, collapsed: true },
    ]
    const slots = [{ key: 's1', folder_id: 'x' }, { key: 's2', folder_id: 'z', last_turn_ts: '2026-10-29T10:00:00Z' }] as unknown as ChatSlot[]
    const latest = unreadFolderLatest(loop, slots, { s1: 'x', s2: 'z' }, new Set(['s1', 's2']))
    expect(latest.get('x')).toBe(0)
    expect(latest.get('y')).toBe(0)
    expect([...loop].sort(unreadFirstComparator(bySidebarOrder, latest)).map(f => f.id)).toEqual(['z', 'x', 'y'])
  })
})
