/**
 * "Hide Empty Folders" (`ChatConfig.hideEmptyFolders`, issue #16046).
 *
 * With the setting on, a folder whose subtree holds no session is not drawn. It is
 * drawn again once a session lands in it, and every folder is drawn while a session
 * is dragged, so an empty one is still somewhere to drop it (one row each, so the
 * list moves less under the pointer). A folder just created is empty too, and stays
 * on screen, the earlier ones of the visit included. Off, an empty folder is drawn
 * as before, and revealing a folder the user hid replaces the last one revealed.
 * The board is left as it is: its columns draw every folder either way.
 *
 * The drag is scripted the way `ChatSidebar.dragFreezeOrder.test.tsx` does it: the
 * DndContext is stubbed so the real lifecycle handlers run without a gesture.
 */
import { describe, it, expect, vi, beforeEach, afterEach, onTestFinished } from 'vitest'
import { render, act, fireEvent, screen, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { ChatFolder, ChatSlot, ChatTag, TagColumn } from '../types'
import type { RootState } from '../store'
import { requestFolderReveal } from '../store/chatSlice'

const dnd = vi.hoisted(() => ({
  handlers: {} as Record<string, ((e: unknown) => void) | undefined>,
  active: null as { id: string } | null,
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
    useDndContext: () => ({ ...actual.useDndContext(), active: dnd.active }),
  }
})

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))

const mocks = vi.hoisted(() => ({ chatFolders: vi.fn(), createChatFolder: vi.fn(), createChatSlot: vi.fn(), chatTags: vi.fn(), tagColumns: vi.fn() }))

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

const EMPTY_FOLDER = 'folder-empty'
const FULL_FOLDER = 'folder-full'
const SLOT = 'chat-1'

const folders: ChatFolder[] = [
  { id: EMPTY_FOLDER, name: 'Empty', order: 0 },
  { id: FULL_FOLDER, name: 'Full', order: 1 },
]
const sessionIn = (folderId: string): ChatSlot[] => [
  { key: SLOT, title: 'Worker', messages: 3, running: false, folder_id: folderId } as ChatSlot,
]
const dragged = { active: { id: SLOT, data: { current: { type: 'session', key: SLOT } } } }
const BOARD_TAG = { id: 'tag-doing', name: 'Doing', color: '#1a1', order: 0, status: true } as ChatTag
const BOARD_COLUMN = { id: 'col-doing', name: 'Doing', tag_ids: [BOARD_TAG.id], mode: 'any', order: 0 } as TagColumn
/** The folder list `chatFolders` serves, which a create appends to. */
let server: ChatFolder[] = []

function renderSidebar(slots: ChatSlot[], folderList: ChatFolder[] = folders, columns: TagColumn[] = []) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-tags'], columns.length ? [BOARD_TAG] : [])
  qc.setQueryData(['tag-columns'], columns)
  qc.setQueryData(['chat-folders'], folderList)
  server = [...folderList]
  mocks.chatFolders.mockImplementation(async () => [...server])
  // The refetch must serve the same columns, or the board is gone before the assertion.
  mocks.chatTags.mockImplementation(async () => (columns.length ? [BOARD_TAG] : []))
  mocks.tagColumns.mockImplementation(async () => columns)
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
  const { container, rerender } = render(tree(slots))
  return { container, store, rerender: (s: ChatSlot[]) => rerender(tree(s)) }
}

const drawn = (container: HTMLElement, folderId: string) =>
  container.querySelector(`[data-folder-drop="${folderId}"]`) !== null
/** Whether the folder block draws a body (its only direct `[aria-hidden]` child). */
const hasBody = (container: HTMLElement, folderId: string) =>
  container.querySelector(`[data-folder-drop="${folderId}"] > [aria-hidden]`) !== null

async function createFolder(name: string) {
  fireEvent.keyDown(screen.getByLabelText('More create options'), { key: 'Enter' })
  fireEvent.click(screen.getByTestId('new-folder'))
  await screen.findByTestId('folder-config-name')
  await waitFor(() => {
    const field = screen.getByTestId('folder-config-name') as HTMLInputElement
    if (field.value !== name) fireEvent.change(field, { target: { value: name } })
    expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(false)
  })
  fireEvent.click(screen.getByTestId('folder-config-submit'))
  await waitFor(() => expect(screen.queryByTestId('folder-config-name')).toBeNull())
}

describe('ChatSidebar – Hide Empty Folders', () => {
  beforeEach(() => {
    localStorage.clear()
    localStorage.setItem('mc-session-stale-collapse-ms', '0')
    localStorage.setItem('mc-chat-config', JSON.stringify({ hideEmptyFolders: true }))
    dnd.handlers = {}
    dnd.active = null
  })
  afterEach(() => { vi.clearAllMocks() })

  it('leaves an empty folder out until a session lands in it', () => {
    const { container, rerender } = renderSidebar(sessionIn(FULL_FOLDER))
    expect(drawn(container, FULL_FOLDER)).toBe(true)
    expect(drawn(container, EMPTY_FOLDER)).toBe(false)

    rerender(sessionIn(EMPTY_FOLDER))
    expect(drawn(container, EMPTY_FOLDER)).toBe(true)
    expect(drawn(container, FULL_FOLDER)).toBe(false)
  })

  it('draws every folder while a session is dragged', () => {
    const { container } = renderSidebar(sessionIn(FULL_FOLDER))
    expect(typeof dnd.handlers.onDragStart).toBe('function')

    dnd.active = { id: SLOT }
    act(() => { dnd.handlers.onDragStart!(dragged) })
    expect(drawn(container, EMPTY_FOLDER)).toBe(true)
    expect(hasBody(container, EMPTY_FOLDER)).toBe(false)

    dnd.active = null
    act(() => { dnd.handlers.onDragEnd!({ ...dragged, over: null }) })
    expect(drawn(container, EMPTY_FOLDER)).toBe(false)
  })

  it('keeps every folder just created on screen', async () => {
    // happy-dom has no scrollIntoView, which the reveal of the new folder calls.
    const proto = HTMLElement.prototype as HTMLElement & { scrollIntoView?: () => void }
    const had = proto.scrollIntoView
    proto.scrollIntoView = () => {}
    onTestFinished(() => { if (had) proto.scrollIntoView = had; else delete proto.scrollIntoView })
    mocks.createChatFolder.mockImplementation(async (name: string) => {
      const row = { id: `f-${name}`, name, order: server.length, created: 1 } as ChatFolder
      server = [...server, row]
      return row
    })
    const { container } = renderSidebar(sessionIn(FULL_FOLDER))
    await createFolder('Fresh')
    await waitFor(() => expect(drawn(container, 'f-Fresh')).toBe(true))
    await createFolder('Second')
    await waitFor(() => expect(drawn(container, 'f-Second')).toBe(true))
    expect(drawn(container, 'f-Fresh')).toBe(true)
    expect(drawn(container, EMPTY_FOLDER)).toBe(false)
  })

  it('shows a failed create in an empty folder once on the board', async () => {
    localStorage.setItem('mc-chat-config', JSON.stringify({ hideEmptyFolders: true, tagColumnsEnabled: true }))
    mocks.createChatSlot.mockRejectedValue(new Error('no capacity'))
    const { container } = renderSidebar([], folders, [BOARD_COLUMN])
    fireEvent.click(await screen.findByTestId(`col-${BOARD_COLUMN.id}-folder-${EMPTY_FOLDER}-new-chat`))
    const notices = () => container.querySelectorAll(`[data-testid="col-${BOARD_COLUMN.id}-folder-create-error-${EMPTY_FOLDER}"]`)
    await waitFor(() => expect(notices().length).toBeGreaterThan(0))
    expect(notices().length).toBe(1)
  })

  it('draws an empty folder when the setting is off', () => {
    localStorage.setItem('mc-chat-config', JSON.stringify({}))
    const { container } = renderSidebar(sessionIn(FULL_FOLDER))
    expect(drawn(container, EMPTY_FOLDER)).toBe(true)
    expect(hasBody(container, EMPTY_FOLDER)).toBe(true)
  })

  it('replaces the last folder revealed when the setting is off', async () => {
    localStorage.setItem('mc-chat-config', JSON.stringify({}))
    // happy-dom has no scrollIntoView, which the reveal calls.
    const proto = HTMLElement.prototype as HTMLElement & { scrollIntoView?: () => void }
    const had = proto.scrollIntoView
    proto.scrollIntoView = () => {}
    onTestFinished(() => { if (had) proto.scrollIntoView = had; else delete proto.scrollIntoView })
    // "Hide when empty" on both and no session in either, so neither is drawn.
    const hiddenFolders: ChatFolder[] = [
      { id: 'hidden-a', name: 'A', order: 0, hidden: true },
      { id: 'hidden-b', name: 'B', order: 1, hidden: true },
    ]
    const { container, store } = renderSidebar([], hiddenFolders)
    expect(drawn(container, 'hidden-a')).toBe(false)

    act(() => { store.dispatch(requestFolderReveal('hidden-a')) })
    await waitFor(() => expect(drawn(container, 'hidden-a')).toBe(true))
    act(() => { store.dispatch(requestFolderReveal('hidden-b')) })
    await waitFor(() => expect(drawn(container, 'hidden-b')).toBe(true))
    expect(drawn(container, 'hidden-a')).toBe(false)
  })
})
