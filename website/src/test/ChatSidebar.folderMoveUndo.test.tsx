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
import { render, fireEvent, waitFor, act } from '@testing-library/react'
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
const SLOT_KEY = 'chat-undo-1'

// STATEFUL folder mock: `updateChatFolder` persists the move, so the
// post-settle refetch returns the moved state. A mock frozen on the pre-move
// list would retire a live offer (live state stops matching = offer dropped)
// and hide exactly the lifecycle these tests exist to pin.
const mocks = vi.hoisted(() => {
  const state = { folders: [] as Array<{ id: string; name: string; order: number; parent_id?: string }> }
  return {
    state,
    setSlotFolder: vi.fn(),
    chatFolders: vi.fn(async () => state.folders.map(f => ({ ...f }))),
    updateChatFolder: vi.fn(async (id: string, body: { parent_id?: string }) => {
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
import { MOVE_UNDO_MS } from '../components/MoveUndoBar'

const FOLDERS: ChatFolder[] = [
  { id: ARCHIVE, name: 'Archive', order: 0 },
  { id: OTHER, name: 'Later', order: 1 },
  { id: CHILD, name: 'Child', order: 0, parent_id: ARCHIVE },
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

  it('undo restores the previous parent, then retires the offer', async () => {
    const { container } = renderSidebar()
    await waitFor(() => expect(dnd.handlers.onDragEnd).toBeTruthy())
    dropNestedFolder(CHILD, OTHER)
    await waitFor(() => expect(barIn(container)).toBeTruthy())
    mocks.updateChatFolder.mockClear()
    fireEvent.click(undoButtonIn(container))
    await waitFor(() => expect(mocks.updateChatFolder).toHaveBeenCalledWith(CHILD, { parent_id: ARCHIVE }))
    expect(barIn(container)).toBeNull()
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
