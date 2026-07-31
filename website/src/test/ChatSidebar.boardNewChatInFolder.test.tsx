/**
 * Board view (tag-columns) previously had no way to start a session inside a
 * folder. The list-view folder header has a "New chat in folder" + button,
 * but the compact column folder header only exposed the menu. This adds the
 * same + button to board view.
 *
 * Three load-bearing assertions:
 *   (1) the + button renders inside every column's copy of the folder header;
 *   (2) clicking it publishes the slot with folder membership already set,
 *       so the session never flashes at root before moving into the folder;
 *   (3) the destination expands while creation is still pending and the new
 *       slot is dropped into the clicked column.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/react'
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

const NEW_KEY = 'chat-new-1'
const mocks = vi.hoisted(() => ({
  createChatSlot: vi.fn(),
  setSlotFolder: vi.fn(),
  dropSlotToColumn: vi.fn(),
  updateChatFolder: vi.fn(),
  chatSlotProject: vi.fn(),
  setSlotColor: vi.fn(),
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
const folders: ChatFolder[] = [{ id: FOLDER_ID, name: 'CDF', order: 0, collapsed: true }]

function renderSidebar(folderData: ChatFolder[] = folders) {
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

beforeEach(() => {
  localStorage.clear()
  // Mirror the real endpoint: it files the slot and returns it already carrying
  // folder_id, which is what lets the row render inside the folder on its first
  // paint instead of being corrected afterwards.
  mocks.createChatSlot.mockImplementation((...args: unknown[]) =>
    Promise.resolve({ key: NEW_KEY, folder_id: (args[8] as string) || '' }),
  )
  mocks.setSlotFolder.mockResolvedValue({})
  mocks.dropSlotToColumn.mockResolvedValue({ ok: true })
  mocks.updateChatFolder.mockResolvedValue({ ok: true })
  mocks.chatSlotProject.mockResolvedValue({})
  mocks.setSlotColor.mockResolvedValue({})
})
afterEach(() => vi.clearAllMocks())

describe('board view: new chat in folder', () => {
  it('renders a "new chat in folder" button in every column copy of the folder', () => {
    const { container } = renderSidebar()
    expect(container.querySelector(`[data-testid="col-${COL_A}-folder-${FOLDER_ID}-new-chat"]`)).toBeTruthy()
    expect(container.querySelector(`[data-testid="col-${COL_B}-folder-${FOLDER_ID}-new-chat"]`)).toBeTruthy()
  })

  it('publishes the session in its folder and drops it into the clicked column', async () => {
    const { container, store } = renderSidebar()
    const observedFolderIds: Array<string | null | undefined> = []
    const unsubscribe = store.subscribe(() => {
      const slot = store.getState().dashboard.slots.find(candidate => candidate.key === NEW_KEY)
      if (slot) observedFolderIds.push(slot.folder_id)
    })

    const btn = container.querySelector(`[data-testid="col-${COL_A}-folder-${FOLDER_ID}-new-chat"]`) as HTMLElement
    expect(btn).toBeTruthy()
    fireEvent.click(btn)

    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    // Folder membership must ride the CREATE call. The server broadcasts the
    // new slot before responding, so a follow-up PATCH lands too late and the
    // session visibly flashes at the top level first.
    expect(mocks.createChatSlot.mock.calls[0][8]).toBe(FOLDER_ID)
    await waitFor(() => expect(mocks.dropSlotToColumn).toHaveBeenCalledWith(NEW_KEY, COL_A))
    expect(mocks.setSlotFolder).not.toHaveBeenCalled()
    unsubscribe()

    expect(observedFolderIds.length).toBeGreaterThan(0)
    expect(observedFolderIds.every(folderId => folderId === FOLDER_ID)).toBe(true)
  })

  it('expands the destination before the pending create resolves', async () => {
    let resolveCreate: ((slot: { key: string; folder_id: string }) => void) | undefined
    let resolveFolderUpdate: ((result: { ok: boolean }) => void) | undefined
    mocks.createChatSlot.mockReturnValue(new Promise(resolve => { resolveCreate = resolve }))
    mocks.updateChatFolder.mockReturnValue(new Promise(resolve => { resolveFolderUpdate = resolve }))
    const { container, qc, store } = renderSidebar()

    const btn = container.querySelector(`[data-testid="col-${COL_A}-folder-${FOLDER_ID}-new-chat"]`) as HTMLElement
    fireEvent.click(btn)

    // The expand is optimistic, so it lands while the create is still pending.
    await waitFor(() => {
      const folderData = qc.getQueryData<ChatFolder[]>(['chat-folders'])
      expect(folderData?.find(folder => folder.id === FOLDER_ID)?.collapsed).toBe(false)
    })
    expect(mocks.updateChatFolder).toHaveBeenCalledWith(FOLDER_ID, { collapsed: false })
    expect(store.getState().dashboard.slots.find(slot => slot.key === NEW_KEY)).toBeUndefined()

    resolveFolderUpdate?.({ ok: true })
    resolveCreate?.({ key: NEW_KEY, folder_id: FOLDER_ID })
    // The slot enters the store already filed — never at root.
    await waitFor(() => {
      const slot = store.getState().dashboard.slots.find(s => s.key === NEW_KEY)
      expect(slot?.folder_id).toBe(FOLDER_ID)
    })
    expect(mocks.setSlotFolder).not.toHaveBeenCalled()
  })
})
