/**
 * Folder re-parenting by drag arms an undo offer, like session drags already do.
 *
 * A folder dragged into (or out of) another folder used to complete silently —
 * the one move in the sidebar that still had no confirmation and no inverse.
 * The two `moveFolderTo` drag call sites in handleSidebarDragEnd now arm a
 * second useMoveUndo instance whose deps are folder-shaped (locate = the
 * folder's parent_id, apply = moveFolderTo), and the existing MoveUndoBar
 * renders the offer. Session moves and folder moves share ONE visual slot: the
 * most recently armed offer wins, so at most one bar (and one ⌘Z listener)
 * exists at a time.
 *
 * The dnd-kit pointer-drag lifecycle can't be simulated in jsdom, so this stubs
 * the DndContext, captures the sidebar's real `onDragEnd`, and invokes it with
 * the payloads the draggables declare — the established pattern from
 * ChatSidebar.dragFreezeOrder.test.tsx.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent, waitFor, act, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { useAppSelector } from '../store'
import { ThemeProvider } from '../hooks/useTheme'
import type { ChatFolder, Slot } from '../types'
import type { RootState } from '../store'

const ARCHIVE = 'folder-archive'
const OTHER = 'folder-later'
const CHILD = 'folder-child'
const CHILD_PREVIOUS = 'folder-child-previous'
const CHILD_NEXT = 'folder-child-next'
const SLOT_KEY = 'chat-undo-1'

// STATEFUL folder mock: `updateChatFolder` persists the move, so the
// post-settle refetch returns the moved state. A mock frozen on the pre-move
// list would retire a live offer (live state stops matching = offer dropped)
// and hide exactly the lifecycle these tests exist to pin.
const mocks = vi.hoisted(() => {
  const state = {
    folders: [] as Array<{ id: string; name: string; order: number; rank?: string; parent_id?: string }>,
    // The person's Folder order (`dashboard.folder_sort`) as the fake gateway
    // holds it; the sidebar reads it through the shared config query.
    folderSort: 'custom' as string,
  }
  return {
    state,
    setSlotFolder: vi.fn(),
    kirocrewConfig: vi.fn(async () => ({ dashboard: { folder_sort: state.folderSort } })),
    chatFolders: vi.fn(async () => state.folders.map(f => ({ ...f }))),
    updateChatFolder: vi.fn(async (id: string, body: { parent_id?: string; before?: string; after?: string }) => {
      const f = state.folders.find(x => x.id === id)
      if (f && body.parent_id !== undefined) f.parent_id = body.parent_id
      return {}
    }),
  }
})

// Captured lifecycle props from the sidebar's DndContext. Stubbing the context
// (children pass through) is what lets the real handlers run without a gesture.
const dnd = vi.hoisted(() => ({ handlers: {} as Record<string, ((e: unknown) => void) | undefined> }))

vi.mock('@dnd-kit/core', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@dnd-kit/core')>()
  return {
    ...actual,
    DndContext: (props: { children?: unknown; onDragEnd?: (e: unknown) => void }) => {
      dnd.handlers.onDragEnd = props.onDragEnd
      return props.children as never
    },
  }
})

vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'layoutScroll', 'initial', 'animate', 'exit',
    'transition', 'variants', 'whileHover', 'whileTap', 'whileInView',
    'drag', 'dragConstraints', 'dragElastic', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef<HTMLElement, Record<string, unknown> & { children?: React.ReactNode }>((props, ref) => {
      const clean: Record<string, unknown> = {}
      for (const k of Object.keys(props)) {
        if (k === 'children' || FRAMER_PROPS.has(k)) continue
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
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as unknown as Record<string, unknown>, {
    get: (t, p: string) => (p in t ? t[p] : vi.fn().mockResolvedValue([])),
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
import { ApiError } from '../api/apiError'
import { MOVE_UNDO_MS } from '../components/MoveUndoBar'

const FOLDERS: ChatFolder[] = [
  { id: ARCHIVE, name: 'Archive', order: 0 },
  { id: OTHER, name: 'Later', order: 1 },
  { id: CHILD_PREVIOUS, name: 'Child previous', order: 0, rank: '7', parent_id: ARCHIVE },
  { id: CHILD, name: 'Child', order: 1, rank: 'F', parent_id: ARCHIVE },
  { id: CHILD_NEXT, name: 'Child next', order: 2, rank: 'V', parent_id: ARCHIVE },
]

function renderSidebar() {
  const slot = {
    key: SLOT_KEY, title: 'Session drag lands in the wrong folder', messages: 0,
    running: false, tags: [], created: '', last_ts: '', folder_id: '',
  } as Slot
  const store = createTestStore({
    dashboard: {
      status: {}, connected: false, slots: [slot], slotsLoaded: true, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as RootState['dashboard'],
    chat: { activeSlot: null } as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], FOLDERS.map(f => ({ ...f })))
  // `slots` is read from the store, not pinned to a literal: the session move
  // is OPTIMISTIC (it dispatches the new folder_id into the store), and a
  // frozen prop would never show the sidebar the move it just made — the
  // offer would be retired on the spot (same harness as ChatSidebar.moveUndo).
  const Harness = () => {
    const slots = useAppSelector(st => st.dashboard.slots)
    return (
      <ChatSidebar
        slots={slots} activeSlot={null} unreadSlots={[]}
        history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
      />
    )
  }
  const utils = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <Harness />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return { ...utils, store, qc }
}

const barIn = (c: HTMLElement) => c.querySelector('[data-testid="session-move-undo"]') as HTMLElement | null
const noticeIn = (c: HTMLElement) => c.querySelector('[data-testid="folder-undo-anchor-gone"]') as HTMLElement | null

/** jsdom has no scrollIntoView; the reveal calls it on the row it lands on. */
function stubScrollIntoView() {
  const proto = HTMLElement.prototype as HTMLElement & { scrollIntoView?: (o?: unknown) => void }
  const had = proto.scrollIntoView
  const spy = vi.fn()
  proto.scrollIntoView = spy
  return {
    spy,
    restore: () => { if (had) proto.scrollIntoView = had; else delete proto.scrollIntoView },
  }
}

/** Drag CHILD into OTHER, then undo into a 409 so the parent-only fallback
 *  runs and the status line names where the folder landed. */
async function landParentOnlyFallback(container: HTMLElement) {
  await waitFor(() => expect(dnd.handlers.onDragEnd).toBeTruthy())
  dropNestedFolder(CHILD, OTHER)
  await waitFor(() => expect(barIn(container)).toBeTruthy())
  mocks.updateChatFolder.mockClear()
  mocks.updateChatFolder.mockRejectedValueOnce(new ApiError(
    409,
    'anchor is not a sibling',
    '{"code":"folder_anchor_not_sibling"}',
  ))
  fireEvent.click(undoButtonIn(container))
  await waitFor(() => expect(mocks.updateChatFolder).toHaveBeenNthCalledWith(2, CHILD, { parent_id: ARCHIVE }))
  return waitFor(() => {
    const el = noticeIn(container)
    expect(el).toBeTruthy()
    return el as HTMLElement
  })
}
const undoButtonIn = (c: HTMLElement) => c.querySelector('[data-testid="session-move-undo-button"]') as HTMLElement

/** Invoke the sidebar's real onDragEnd with a nested-subfolder drop payload. */
function dropNestedFolder(id: string, toFolderId: string | null) {
  act(() => {
    dnd.handlers.onDragEnd?.({
      active: { id, data: { current: { type: 'folder', nested: true } } },
      over: { id: `folder-drop:${toFolderId ?? 'root'}`, data: { current: { type: 'folder-drop', folderId: toFolderId } } },
    })
  })
}

/** Root-folder drag resolved to a header-band folder-drop hit (= nest INTO). */
function dropRootFolder(id: string, toFolderId: string) {
  act(() => {
    dnd.handlers.onDragEnd?.({
      active: { id, data: { current: { type: 'folder' } } },
      over: { id: `folder-drop:${toFolderId}`, data: { current: { type: 'folder-drop', folderId: toFolderId } } },
    })
  })
}

function dropSession(key: string, toFolderId: string) {
  act(() => {
    dnd.handlers.onDragEnd?.({
      active: { id: key, data: { current: { type: 'session', key } } },
      over: { id: `folder-drop:${toFolderId}`, data: { current: { type: 'folder-drop', folderId: toFolderId } } },
    })
  })
}

beforeEach(() => {
  localStorage.clear()
  dnd.handlers = {}
  mocks.state.folders = FOLDERS.map(f => ({ ...f }))
  mocks.state.folderSort = 'custom'
  mocks.setSlotFolder.mockResolvedValue({})
})
afterEach(() => { vi.clearAllMocks(); vi.useRealTimers() })

describe('folder re-parent undo', () => {
  it('performs the nested-folder move and offers it back, naming the destination', async () => {
    const { container } = renderSidebar()
    await waitFor(() => expect(dnd.handlers.onDragEnd).toBeTruthy())
    expect(barIn(container)).toBeNull()
    dropNestedFolder(CHILD, OTHER)
    await waitFor(() => expect(mocks.updateChatFolder).toHaveBeenCalledWith(CHILD, { parent_id: OTHER }))
    await waitFor(() => expect(barIn(container)).toBeTruthy())
    expect(barIn(container)!.textContent).toContain('Later')
  })

  it('arms the root-folder header-band drop too (the second call site)', async () => {
    const { container } = renderSidebar()
    await waitFor(() => expect(dnd.handlers.onDragEnd).toBeTruthy())
    dropRootFolder(OTHER, ARCHIVE)
    await waitFor(() => expect(mocks.updateChatFolder).toHaveBeenCalledWith(OTHER, { parent_id: ARCHIVE }))
    await waitFor(() => expect(barIn(container)).toBeTruthy())
    expect(barIn(container)!.textContent).toContain('Archive')
  })

  it('a drag re-parent drops the old section\'s rank from the cached row BEFORE the PATCH resolves, and sends only parent_id', async () => {
    const { qc } = renderSidebar()
    await waitFor(() => expect(dnd.handlers.onDragEnd).toBeTruthy())
    await waitFor(() => expect(qc.getQueryData<ChatFolder[]>(['chat-folders'])!.find(f => f.id === CHILD)!.rank).toBe('F'))
    // Hold the PATCH open: the stale rank must be gone from the optimistic row,
    // not only once the refetch lands.
    let release!: () => void
    mocks.updateChatFolder.mockImplementationOnce(() => new Promise<Record<string, never>>(resolve => { release = () => resolve({}) }))
    dropNestedFolder(CHILD, OTHER)
    await waitFor(() => expect(mocks.updateChatFolder).toHaveBeenCalledTimes(1))
    // The wire body carries `parent_id` alone -- the rank is the gateway's to pick.
    const [, body] = mocks.updateChatFolder.mock.calls[0]
    expect(Object.keys(body)).toEqual(['parent_id'])
    expect(body).toEqual({ parent_id: OTHER })
    const cached = qc.getQueryData<ChatFolder[]>(['chat-folders'])!.find(f => f.id === CHILD)!
    expect(cached.parent_id).toBe(OTHER)
    // 'F' was a key in ARCHIVE's sequence; kept, the rank-first comparator
    // would read it as a position among OTHER's ranked rows.
    expect(cached.rank).toBeUndefined()
    // No other row is touched.
    expect(qc.getQueryData<ChatFolder[]>(['chat-folders'])!.find(f => f.id === CHILD_NEXT)!.rank).toBe('V')
    await act(async () => { release() })
    await waitFor(() => expect(qc.getQueryData<ChatFolder[]>(['chat-folders'])!.find(f => f.id === CHILD)!.rank).toBe('F'))
  })

  it('a failed drag re-parent restores the rank it dropped', async () => {
    const { qc } = renderSidebar()
    await waitFor(() => expect(dnd.handlers.onDragEnd).toBeTruthy())
    await waitFor(() => expect(qc.getQueryData<ChatFolder[]>(['chat-folders'])!.find(f => f.id === CHILD)!.rank).toBe('F'))
    let reject!: (e: Error) => void
    mocks.updateChatFolder.mockImplementationOnce(() => new Promise<Record<string, never>>((_r, rej) => { reject = rej }))
    dropNestedFolder(CHILD, OTHER)
    await waitFor(() => expect(qc.getQueryData<ChatFolder[]>(['chat-folders'])!.find(f => f.id === CHILD)!.parent_id).toBe(OTHER))
    expect(qc.getQueryData<ChatFolder[]>(['chat-folders'])!.find(f => f.id === CHILD)!.rank).toBeUndefined()
    await act(async () => { reject(new ApiError(500, 'boom', '{}')) })
    await waitFor(() => {
      const c = qc.getQueryData<ChatFolder[]>(['chat-folders'])!.find(f => f.id === CHILD)!
      expect(c.parent_id).toBe(ARCHIVE)
      expect(c.rank).toBe('F')
    })
  })

  it('an anchored undo into a section it would re-spread moves the row UNRANKED, not with its stale rank', async () => {
    // CHILD_NEXT has no rank, so placing CHILD before it re-spreads ARCHIVE:
    // the client computes no provisional rank and must not leave the one the
    // row carried in OTHER on the cached row.
    mocks.state.folders.find(f => f.id === CHILD_NEXT)!.rank = undefined
    const { container, qc } = renderSidebar()
    await waitFor(() => expect(dnd.handlers.onDragEnd).toBeTruthy())
    dropNestedFolder(CHILD, OTHER)
    await waitFor(() => expect(barIn(container)).toBeTruthy())
    await waitFor(() => expect(qc.getQueryData<ChatFolder[]>(['chat-folders'])!.find(f => f.id === CHILD)!.parent_id).toBe(OTHER))
    // The refetch handed the row its stored rank back (the mock store never
    // re-ranks); that stored 'F' is the stale key the undo must drop.
    await waitFor(() => expect(qc.getQueryData<ChatFolder[]>(['chat-folders'])!.find(f => f.id === CHILD)!.rank).toBe('F'))
    mocks.updateChatFolder.mockClear()
    let release!: () => void
    mocks.updateChatFolder.mockImplementationOnce(() => new Promise<Record<string, never>>(resolve => { release = () => resolve({}) }))
    fireEvent.click(undoButtonIn(container))
    await waitFor(() => expect(mocks.updateChatFolder).toHaveBeenCalledWith(CHILD, { parent_id: ARCHIVE, after: CHILD_PREVIOUS, before: CHILD_NEXT }))
    const cached = qc.getQueryData<ChatFolder[]>(['chat-folders'])!.find(f => f.id === CHILD)!
    expect(cached.parent_id).toBe(ARCHIVE)
    expect(cached.rank).toBeUndefined()
    mocks.state.folders.find(f => f.id === CHILD)!.parent_id = ARCHIVE
    await act(async () => { release() })
    await waitFor(() => expect(qc.getQueryData<ChatFolder[]>(['chat-folders'])!.find(f => f.id === CHILD)!.rank).toBe('F'))
  })

  it('undo restores the previous parent, then retires the offer', async () => {
    const { container } = renderSidebar()
    await waitFor(() => expect(dnd.handlers.onDragEnd).toBeTruthy())
    dropNestedFolder(CHILD, OTHER)
    await waitFor(() => expect(barIn(container)).toBeTruthy())
    mocks.updateChatFolder.mockClear()
    fireEvent.click(undoButtonIn(container))
    await waitFor(() => expect(mocks.updateChatFolder).toHaveBeenCalledWith(CHILD, { parent_id: ARCHIVE, after: CHILD_PREVIOUS, before: CHILD_NEXT }))
    expect(barIn(container)).toBeNull()
  })

  it('undo moves the folder back in the cache BEFORE the anchored PATCH resolves', async () => {
    const { container, qc } = renderSidebar()
    await waitFor(() => expect(dnd.handlers.onDragEnd).toBeTruthy())
    dropNestedFolder(CHILD, OTHER)
    await waitFor(() => expect(barIn(container)).toBeTruthy())
    await waitFor(() => expect(qc.getQueryData<ChatFolder[]>(['chat-folders'])!.find(f => f.id === CHILD)!.parent_id).toBe(OTHER))
    mocks.updateChatFolder.mockClear()
    // Hold the undo PATCH open: the cache must not wait for it.
    let release!: () => void
    mocks.updateChatFolder.mockImplementationOnce(() => new Promise<Record<string, never>>(resolve => { release = () => resolve({}) }))
    fireEvent.click(undoButtonIn(container))
    await waitFor(() => expect(mocks.updateChatFolder).toHaveBeenCalledWith(CHILD, { parent_id: ARCHIVE, after: CHILD_PREVIOUS, before: CHILD_NEXT }))
    const cached = qc.getQueryData<ChatFolder[]>(['chat-folders'])!.find(f => f.id === CHILD)!
    expect(cached.parent_id).toBe(ARCHIVE)
    // The provisional rank sorts it back before its captured sibling, and the
    // request-only anchor keys never land on the cached row.
    expect(cached.rank! < 'V').toBe(true)
    expect(cached).not.toHaveProperty('before')
    expect(cached).not.toHaveProperty('after')
    // A second folder is untouched by the write.
    expect(qc.getQueryData<ChatFolder[]>(['chat-folders'])!.find(f => f.id === OTHER)!.parent_id).toBeUndefined()
    mocks.state.folders.find(f => f.id === CHILD)!.parent_id = ARCHIVE
    await act(async () => { release() })
    await waitFor(() => expect(qc.getQueryData<ChatFolder[]>(['chat-folders'])!.find(f => f.id === CHILD)!.parent_id).toBe(ARCHIVE))
  })

  it('a failed anchored undo rolls back only the fields it set', async () => {
    const { container, qc } = renderSidebar()
    await waitFor(() => expect(dnd.handlers.onDragEnd).toBeTruthy())
    dropNestedFolder(CHILD, OTHER)
    await waitFor(() => expect(barIn(container)).toBeTruthy())
    await waitFor(() => expect(qc.getQueryData<ChatFolder[]>(['chat-folders'])!.find(f => f.id === CHILD)!.parent_id).toBe(OTHER))
    mocks.updateChatFolder.mockClear()
    let reject!: (e: Error) => void
    mocks.updateChatFolder.mockImplementationOnce(() => new Promise<Record<string, never>>((_r, rej) => { reject = rej }))
    fireEvent.click(undoButtonIn(container))
    await waitFor(() => expect(mocks.updateChatFolder).toHaveBeenCalledWith(CHILD, { parent_id: ARCHIVE, after: CHILD_PREVIOUS, before: CHILD_NEXT }))
    expect(qc.getQueryData<ChatFolder[]>(['chat-folders'])!.find(f => f.id === CHILD)!.parent_id).toBe(ARCHIVE)
    // The mock store still says OTHER (the server never moved it), so the
    // rollback and the re-sync agree: the folder is back in the drag destination.
    await act(async () => { reject(new ApiError(500, 'boom', '{}')) })
    await waitFor(() => {
      const c = qc.getQueryData<ChatFolder[]>(['chat-folders'])!.find(f => f.id === CHILD)!
      expect(c.parent_id).toBe(OTHER)
    })
    expect(mocks.updateChatFolder).toHaveBeenCalledTimes(1)
  })

  it('falls back to a parent-only undo when the captured sibling is no longer there, and says where the folder landed', async () => {
    const { container } = renderSidebar()
    await waitFor(() => expect(dnd.handlers.onDragEnd).toBeTruthy())
    dropNestedFolder(CHILD, OTHER)
    await waitFor(() => expect(barIn(container)).toBeTruthy())
    mocks.updateChatFolder.mockClear()
    mocks.updateChatFolder.mockRejectedValueOnce(new ApiError(
      409,
      'anchor is not a sibling',
      '{"code":"folder_anchor_not_sibling"}',
    ))
    fireEvent.click(undoButtonIn(container))
    await waitFor(() => expect(mocks.updateChatFolder).toHaveBeenNthCalledWith(1, CHILD, { parent_id: ARCHIVE, after: CHILD_PREVIOUS, before: CHILD_NEXT }))
    await waitFor(() => expect(mocks.updateChatFolder).toHaveBeenNthCalledWith(2, CHILD, { parent_id: ARCHIVE }))
    // The retry SUCCEEDED, but it seated the folder last in its old section
    // rather than where it was. A silent landing reads as a wrong move, so
    // the sidebar says what happened -- as STATUS naming the folder, not under
    // the "Folder update failed" title: nothing failed.
    const notice = await waitFor(() => {
      const el = container.querySelector('[data-testid="folder-undo-anchor-gone"]')
      expect(el).toBeTruthy()
      return el as HTMLElement
    })
    expect(notice.getAttribute('role')).toBe('status')
    // Names the folder AND the parent it went back under; no rank-model word.
    expect(notice.textContent).toContain('Child')
    expect(notice.textContent).toContain('went back under Archive, at the end.')
    expect(notice.textContent).toContain('The folder it sat next to has moved or was deleted.')
    expect(notice.textContent).not.toContain('section')
    expect(container.querySelector('[data-testid="folder-action-error"]')).toBeNull()
    expect(container.textContent).not.toContain('Folder update failed')
    // Dismissable, like the error line.
    fireEvent.click(within(notice).getByRole('button', { name: 'Dismiss' }))
    expect(container.querySelector('[data-testid="folder-undo-anchor-gone"]')).toBeNull()
  })

  it('says "the top level" when the parent-only fallback returns a root folder', async () => {
    const { container } = renderSidebar()
    await waitFor(() => expect(dnd.handlers.onDragEnd).toBeTruthy())
    dropRootFolder(OTHER, ARCHIVE)
    await waitFor(() => expect(barIn(container)).toBeTruthy())
    mocks.updateChatFolder.mockClear()
    mocks.updateChatFolder.mockRejectedValueOnce(new ApiError(
      409,
      'anchor is not a sibling',
      '{"code":"folder_anchor_not_sibling"}',
    ))
    fireEvent.click(undoButtonIn(container))
    // OTHER sat after ARCHIVE at the root, so the undo anchors there with an
    // empty parent_id; the fallback repeats the empty parent_id alone.
    await waitFor(() => expect(mocks.updateChatFolder).toHaveBeenNthCalledWith(1, OTHER, { parent_id: '', after: ARCHIVE }))
    await waitFor(() => expect(mocks.updateChatFolder).toHaveBeenNthCalledWith(2, OTHER, { parent_id: '' }))
    const notice = await waitFor(() => {
      const el = container.querySelector('[data-testid="folder-undo-anchor-gone"]')
      expect(el).toBeTruthy()
      return el as HTMLElement
    })
    expect(notice.textContent).toContain('Later')
    expect(notice.textContent).toContain('went back to the top level, at the end.')
    expect(notice.textContent).toContain('The folder it sat next to has moved or was deleted.')
    expect(notice.textContent).not.toContain('section')
  })

  it('a parent-only fallback that lands reveals the folder: ancestors open, row scrolled to and flashed', async () => {
    const scroll = stubScrollIntoView()
    try {
      const { container } = renderSidebar()
      await landParentOnlyFallback(container)
      // The line says "at the end"; the reveal shows which row that is. Same
      // request the Command Bar raises, consumed by the sidebar's reveal
      // effect, so the folder row is the element scrolled to and lit.
      await waitFor(() => expect(scroll.spy).toHaveBeenCalled())
      const row = container.querySelector(`[data-folder-row="${CHILD}"]`)
      expect(row).toBeTruthy()
      // The node scrolled is the folder's row (the refetch that settles the
      // rank re-mounts it, so compare the row attribute, not the instance); the
      // flash is state-keyed, so the mounted row carries it.
      const scrolled = scroll.spy.mock.instances as HTMLElement[]
      expect(scrolled.length).toBe(1)
      expect(scrolled[0].getAttribute('data-folder-row')).toBe(CHILD)
      expect(row!.className).toContain('session-reveal-flash')
      expect(row!.closest('[inert]')).toBeNull()
    } finally {
      scroll.restore()
    }
  })

  it('under Folder order By name, the parent-only fallback reveals the folder but shows no "at the end" status line', async () => {
    // "At the end" describes the stored Custom order. By name draws the folder
    // at its alphabetical position whatever its rank, so the fallback changes
    // nothing on screen: the line stays away, the reveal still runs.
    mocks.state.folderSort = 'name'
    const scroll = stubScrollIntoView()
    try {
      const { container } = renderSidebar()
      await waitFor(() => expect(dnd.handlers.onDragEnd).toBeTruthy())
      await waitFor(() => expect(mocks.kirocrewConfig).toHaveBeenCalled())
      dropNestedFolder(CHILD, OTHER)
      await waitFor(() => expect(barIn(container)).toBeTruthy())
      mocks.updateChatFolder.mockClear()
      mocks.updateChatFolder.mockRejectedValueOnce(new ApiError(
        409,
        'anchor is not a sibling',
        '{"code":"folder_anchor_not_sibling"}',
      ))
      fireEvent.click(undoButtonIn(container))
      await waitFor(() => expect(mocks.updateChatFolder).toHaveBeenNthCalledWith(2, CHILD, { parent_id: ARCHIVE }))
      await waitFor(() => expect(scroll.spy).toHaveBeenCalled())
      const scrolled = scroll.spy.mock.instances as HTMLElement[]
      expect(scrolled[0].getAttribute('data-folder-row')).toBe(CHILD)
      const row = container.querySelector(`[data-folder-row="${CHILD}"]`)
      expect(row!.className).toContain('session-reveal-flash')
      // Settled: the write landed and the reveal ran, and no notice appeared.
      await act(async () => { await Promise.resolve(); await Promise.resolve() })
      expect(noticeIn(container)).toBeNull()
      expect(container.textContent).not.toContain('at the end')
    } finally {
      scroll.restore()
    }
  })

  it('a failed drag re-parent reveals nothing', async () => {
    const scroll = stubScrollIntoView()
    try {
      const { container } = renderSidebar()
      await waitFor(() => expect(dnd.handlers.onDragEnd).toBeTruthy())
      mocks.updateChatFolder.mockRejectedValueOnce(new ApiError(500, 'boom', '{}'))
      dropNestedFolder(CHILD, OTHER)
      await waitFor(() => expect(container.querySelector('[data-testid="folder-action-error"]')).toBeTruthy())
      expect(scroll.spy).not.toHaveBeenCalled()
      expect(noticeIn(container)).toBeNull()
    } finally {
      scroll.restore()
    }
  })

  it('starting the next folder move clears the landing notice', async () => {
    const { container, qc } = renderSidebar()
    await landParentOnlyFallback(container)
    await waitFor(() => expect(qc.getQueryData<ChatFolder[]>(['chat-folders'])!.find(f => f.id === CHILD)!.parent_id).toBe(ARCHIVE))
    // The notice describes the last move. A new drag of any folder that gets
    // past the guards (here CHILD back into Later) starts with it gone, before
    // the write resolves.
    let release!: () => void
    mocks.updateChatFolder.mockImplementationOnce(() => new Promise<Record<string, never>>(resolve => { release = () => resolve({}) }))
    dropNestedFolder(CHILD, OTHER)
    await waitFor(() => expect(mocks.updateChatFolder).toHaveBeenCalledWith(CHILD, { parent_id: OTHER }))
    expect(noticeIn(container)).toBeNull()
    await act(async () => { release() })
    expect(noticeIn(container)).toBeNull()
  })

  it('a drop the guards refuse leaves the landing notice up', async () => {
    const { container } = renderSidebar()
    await landParentOnlyFallback(container)
    mocks.updateChatFolder.mockClear()
    // CHILD sits under Archive after the fallback; dropping it there moves nothing.
    dropNestedFolder(CHILD, ARCHIVE)
    await Promise.resolve()
    expect(mocks.updateChatFolder).not.toHaveBeenCalled()
    expect(noticeIn(container)).toBeTruthy()
  })

  it('restores the cached parent and rank when the parent-only fallback fails', async () => {
    const { container, qc } = renderSidebar()
    await waitFor(() => expect(dnd.handlers.onDragEnd).toBeTruthy())
    dropNestedFolder(CHILD, OTHER)
    await waitFor(() => expect(barIn(container)).toBeTruthy())
    await waitFor(() => {
      const cached = qc.getQueryData<ChatFolder[]>(['chat-folders'])!.find(f => f.id === CHILD)!
      expect(cached.parent_id).toBe(OTHER)
      expect(cached.rank).toBe('F')
    })
    mocks.updateChatFolder.mockClear()
    mocks.chatFolders.mockImplementation(() => new Promise<ChatFolder[]>(() => {}))
    mocks.updateChatFolder
      .mockRejectedValueOnce(new ApiError(409, 'anchor is not a sibling', '{"code":"folder_anchor_not_sibling"}'))
      .mockRejectedValueOnce(new ApiError(500, 'boom', '{}'))
    fireEvent.click(undoButtonIn(container))
    await waitFor(() => expect(mocks.updateChatFolder).toHaveBeenNthCalledWith(1, CHILD, { parent_id: ARCHIVE, after: CHILD_PREVIOUS, before: CHILD_NEXT }))
    await waitFor(() => expect(mocks.updateChatFolder).toHaveBeenNthCalledWith(2, CHILD, { parent_id: ARCHIVE }))
    await waitFor(() => {
      const cached = qc.getQueryData<ChatFolder[]>(['chat-folders'])!.find(f => f.id === CHILD)!
      expect(cached.parent_id).toBe(OTHER)
      expect(cached.rank).toBe('F')
    })
  })

  it('arms nothing when the folder is dropped on its current parent', async () => {
    const { container } = renderSidebar()
    await waitFor(() => expect(dnd.handlers.onDragEnd).toBeTruthy())
    dropNestedFolder(CHILD, ARCHIVE)
    await Promise.resolve()
    expect(mocks.updateChatFolder).not.toHaveBeenCalled()
    expect(barIn(container)).toBeNull()
  })

  it('expires the offer on its own clock', async () => {
    const { container } = renderSidebar()
    await waitFor(() => expect(dnd.handlers.onDragEnd).toBeTruthy())
    vi.useFakeTimers()
    dropNestedFolder(CHILD, OTHER)
    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    expect(barIn(container)).toBeTruthy()
    act(() => { vi.advanceTimersByTime(MOVE_UNDO_MS + 50) })
    expect(barIn(container)).toBeNull()
  })

  it('a second move supersedes the first — undo replays only the newest inverse', async () => {
    const { container } = renderSidebar()
    await waitFor(() => expect(dnd.handlers.onDragEnd).toBeTruthy())
    dropNestedFolder(CHILD, OTHER)
    await waitFor(() => expect(barIn(container)?.textContent).toContain('Later'))
    // Second drag: out to the top level (root lane drop, folderId null).
    dropNestedFolder(CHILD, null)
    await waitFor(() => expect(barIn(container)?.textContent).toContain('Removed from folder'))
    mocks.updateChatFolder.mockClear()
    fireEvent.click(undoButtonIn(container))
    // Back to Later (the parent before the SECOND move), not to Archive.
    await waitFor(() => expect(mocks.updateChatFolder).toHaveBeenCalledWith(CHILD, { parent_id: OTHER }))
  })

  it('arming a folder move DISMISSES a live session offer — one bar, and no resurrection', async () => {
    const { container } = renderSidebar()
    await waitFor(() => expect(dnd.handlers.onDragEnd).toBeTruthy())
    dropSession(SLOT_KEY, ARCHIVE)
    await waitFor(() => expect(mocks.setSlotFolder).toHaveBeenCalledWith(SLOT_KEY, ARCHIVE))
    await waitFor(() => expect(barIn(container)).toBeTruthy())
    dropNestedFolder(CHILD, OTHER)
    await waitFor(() => expect(mocks.updateChatFolder).toHaveBeenCalled())
    // One bar (one ⌘Z listener), and it is the folder move's: the moved item's
    // tooltip carries its title, which is the discriminator.
    await waitFor(() => {
      const bars = container.querySelectorAll('[data-testid="session-move-undo"]')
      expect(bars.length).toBe(1)
      expect(bars[0].querySelector('[title*="Child"]')).toBeTruthy()
    })
    // The displaced session offer was RETIRED, not hidden: undoing the winner
    // must not let the older bar re-mount under the cursor.
    fireEvent.click(undoButtonIn(container))
    await waitFor(() => expect(barIn(container)).toBeNull())
    await act(async () => { await Promise.resolve() })
    expect(barIn(container)).toBeNull()
  })
})
