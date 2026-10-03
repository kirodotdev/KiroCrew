/**
 * Board view: a session filed in a folder can be dragged back to the top level.
 *
 * A board card drags with native HTML5 DnD (the column's dnd-kit context carries
 * only folder reorders), so the root-lane unfile targets the list view renders
 * inside its DndContext never exist here. The way out is a dashed strip at the
 * foot of every column, rendered only while a FILED card is in flight, that
 * calls the same unfile offer as the list view -- so the wording and the undo
 * bar are the ones list view already shows.
 *
 * Six things are pinned:
 *  1. The strip appears when a filed card starts dragging, and not before.
 *  2. Dropping on it unfiles the card (folder -> root) and arms the undo bar.
 *  3. A card that is not filed shows no strip -- there is nothing to leave.
 *  4. One release does one thing: a drop on the strip never also retags the
 *     card, and the column body's own retag drop keeps working untouched.
 *  5. The window-level reset that ends the drag waits for a drop's dispatch to
 *     finish (so the strip's own onDrop can run) and is immediate on dragend.
 *  6. The dragged card leaving the DOM mid-drag ends the drag too, so a strip
 *     never lingers with nothing in flight.
 *
 * Native drop events are dispatched the way the moveUndo test does it: a
 * `dataTransfer` stub carrying the slot key as text/plain.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, renderHook, fireEvent, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { useAppSelector } from '../store'
import { sseSlots } from '../store/dashboardSlice'
import { ThemeProvider } from '../hooks/useTheme'
import type { ChatTag, TagColumn, ChatFolder, Slot } from '../types'
import type { RootState } from '../store'

const mocks = vi.hoisted(() => ({
  setSlotFolder: vi.fn(),
  dropSlotToColumn: vi.fn(),
  tagColumns: vi.fn(),
  chatTags: vi.fn(),
  chatFolders: vi.fn(),
}))

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
  const made = new Map<string, ReturnType<typeof make>>()
  const motion = new Proxy({}, {
    get: (_t, tag: string) => {
      if (!made.has(tag)) made.set(tag, make(tag))
      return made.get(tag)
    },
  })
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
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as Record<string, unknown>, {
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
import { useNativeSessionDrag } from '../pages/chat-sidebar/dnd/useSidebarDrag'

const TAG = '22222222-2222-2222-2222-222222222222'
const COL = 'col-blocked'
const SLOT_KEY = 'chat-filed-1'
const ARCHIVE = 'folder-archive'
const UNFILE_TEXT = 'Drop here to remove from folder'

const tags: ChatTag[] = [{ id: TAG, name: 'Blocked', color: '#e11', order: 0, status: true }]
const columns: TagColumn[] = [{ id: COL, name: 'Blocked', tag_ids: [TAG], mode: 'any', order: 0 }]
const folders: ChatFolder[] = [{ id: ARCHIVE, name: 'Archive', order: 0 }]

function renderSidebar(folderId = ARCHIVE) {
  const slot = {
    key: SLOT_KEY, title: 'A session that lives in Archive', messages: 0,
    running: false, tags: [TAG], created: '', last_ts: '', folder_id: folderId,
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
  qc.setQueryData(['chat-tags'], tags)
  qc.setQueryData(['tag-columns'], columns)
  qc.setQueryData(['chat-folders'], folders)
  // Slots come from the store, as in ChatPage: the unfile is optimistic and
  // dispatches the new folder_id there, so a frozen prop would hide the move.
  const Harness = () => {
    const slots = useAppSelector(s => s.dashboard.slots)
    return (
      <ChatSidebar
        slots={slots} activeSlot={null} unreadSlots={[]}
        history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
      />
    )
  }
  return {
    ...render(
      <QueryClientProvider client={qc}>
        <Provider store={store}>
          <ThemeProvider>
            <MemoryRouter>
              <Harness />
            </MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    ),
    store,
    qc,
  }
}

/** A native-DnD stub for one session card: the key rides as text/plain. */
function sessionTransfer(slotKey = SLOT_KEY) {
  return {
    types: ['text/plain'],
    effectAllowed: 'uninitialized',
    dropEffect: 'none',
    setData: vi.fn(),
    getData: (t: string) => (t === 'text/plain' ? slotKey : ''),
  }
}

const cardIn = (c: HTMLElement, slotKey = SLOT_KEY) =>
  c.querySelector(`[data-session-row="${slotKey}"]`) as HTMLElement
const stripIn = (c: HTMLElement) =>
  c.querySelector(`[data-testid="column-unfile-drop-${COL}"]`) as HTMLElement | null
const columnIn = (c: HTMLElement) =>
  c.querySelector(`[data-testid="column-${COL}"]`) as HTMLElement
const undoBarIn = (c: HTMLElement) =>
  c.querySelector('[data-testid="session-move-undo"]') as HTMLElement | null

beforeEach(() => {
  localStorage.clear()
  mocks.setSlotFolder.mockResolvedValue({})
  mocks.dropSlotToColumn.mockResolvedValue({})
  // The board's queries refetch in the background once the seeded cache paints;
  // answered with the same rows, the board stays on screen for the whole test
  // instead of falling back to the list view mid-way.
  mocks.tagColumns.mockResolvedValue(columns)
  mocks.chatTags.mockResolvedValue(tags)
  mocks.chatFolders.mockResolvedValue(folders)
})
afterEach(() => { vi.clearAllMocks() })

describe('board view: drag a filed session back to the top level', () => {
  it('shows the unfile strip only once a filed card starts dragging', () => {
    const { container } = renderSidebar()
    // Before any drag: no strip, no wording.
    expect(stripIn(container)).toBeNull()
    expect(container.textContent).not.toContain(UNFILE_TEXT)

    const card = cardIn(container)
    expect(card, 'the filed card renders inside its folder block').toBeTruthy()
    fireEvent.dragStart(card, { dataTransfer: sessionTransfer() })

    const strip = stripIn(container)
    expect(strip, 'the column renders its unfile strip while the filed card drags').toBeTruthy()
    expect(strip!.textContent).toContain(UNFILE_TEXT)
    // Inside the column, so it is the column's own target.
    expect(columnIn(container).contains(strip)).toBe(true)
  })

  it('dropping on the strip unfiles the card and arms the undo bar', async () => {
    const { container } = renderSidebar()
    fireEvent.dragStart(cardIn(container), { dataTransfer: sessionTransfer() })
    const strip = stripIn(container)
    expect(strip).toBeTruthy()

    const dataTransfer = sessionTransfer()
    fireEvent.dragOver(strip!, { dataTransfer })
    fireEvent.drop(strip!, { dataTransfer })

    // The same unfile write the list view's root target makes: folder -> null.
    await waitFor(() => expect(mocks.setSlotFolder).toHaveBeenCalledWith(SLOT_KEY, null))
    // The list view's undo bar, not a new one.
    await waitFor(() => expect(undoBarIn(container)).toBeTruthy())
    // The release ends the drag: the strip is gone again.
    await waitFor(() => expect(stripIn(container)).toBeNull())
  })

  it('does not retag on the strip, and the column body still retags', async () => {
    const { container } = renderSidebar()
    fireEvent.dragStart(cardIn(container), { dataTransfer: sessionTransfer() })
    const strip = stripIn(container)
    expect(strip).toBeTruthy()

    // A release on the strip is an unfile and nothing else: the column's own
    // onDrop (a retag on a status lane) must not also see it.
    const onStrip = sessionTransfer()
    fireEvent.dragOver(strip!, { dataTransfer: onStrip })
    fireEvent.drop(strip!, { dataTransfer: onStrip })
    await waitFor(() => expect(mocks.setSlotFolder).toHaveBeenCalledWith(SLOT_KEY, null))
    expect(mocks.dropSlotToColumn).not.toHaveBeenCalled()

    // The column body's retag drop is untouched: a release there retags and
    // leaves the folder alone.
    mocks.setSlotFolder.mockClear()
    const onBody = sessionTransfer()
    fireEvent.dragOver(columnIn(container), { dataTransfer: onBody })
    fireEvent.drop(columnIn(container), { dataTransfer: onBody })
    await waitFor(() => expect(mocks.dropSlotToColumn).toHaveBeenCalledWith(SLOT_KEY, COL))
    expect(mocks.setSlotFolder).not.toHaveBeenCalled()
  })

  it('renders no strip for a card that is not in a folder', () => {
    const { container } = renderSidebar('')
    const card = cardIn(container)
    expect(card).toBeTruthy()
    fireEvent.dragStart(card, { dataTransfer: sessionTransfer() })
    expect(stripIn(container)).toBeNull()
    expect(container.textContent).not.toContain(UNFILE_TEXT)
  })

  it('takes the strip down when the drag ends without a drop', async () => {
    const { container } = renderSidebar()
    const card = cardIn(container)
    fireEvent.dragStart(card, { dataTransfer: sessionTransfer() })
    expect(stripIn(container)).toBeTruthy()
    await act(async () => { fireEvent.dragEnd(card) })
    expect(stripIn(container)).toBeNull()
  })

  it('ends the mirror at once on dragend, but only after the dispatch on drop', async () => {
    // The window hears a `drop` before React's root listener does, and a browser
    // runs a microtask checkpoint between the two, in which React commits a sync
    // state update. A reset that lands there unmounts the strip under the
    // release, the event reaches a detached target, and the strip's own onDrop
    // never runs. So the drop-side reset waits one macrotask, while dragend
    // (dispatched to the source row, which no target depends on) resets at once.
    const { result } = renderHook(() => useNativeSessionDrag())
    act(() => { result.current.startNativeSessionDrag(SLOT_KEY) })
    expect(result.current.nativeSessionDrag).toBe(SLOT_KEY)

    act(() => { window.dispatchEvent(new Event('drop', { bubbles: true })) })
    expect(result.current.nativeSessionDrag, 'a drop must not reset the mirror synchronously').toBe(SLOT_KEY)
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
    expect(result.current.nativeSessionDrag).toBeNull()

    act(() => { result.current.startNativeSessionDrag(SLOT_KEY) })
    act(() => { window.dispatchEvent(new Event('dragend', { bubbles: true })) })
    expect(result.current.nativeSessionDrag).toBeNull()
  })

  it('ends the drag when the dragged card itself leaves the DOM mid-drag', async () => {
    // A card whose lane changes while it is in flight remounts under another
    // column, and a cancel after that fires `dragend` at the detached node,
    // which reaches neither the window nor React. The row's own unmount is the
    // end the window cannot hear. Observable from outside: once the card is
    // back, filed, with no drag in flight, no strip may come back with it.
    const { container, store } = renderSidebar()
    const filed = store.getState().dashboard.slots[0]
    fireEvent.dragStart(cardIn(container), { dataTransfer: sessionTransfer() })
    expect(stripIn(container)).toBeTruthy()
    act(() => { store.dispatch(sseSlots([])) })
    expect(cardIn(container)).toBeNull()
    act(() => { store.dispatch(sseSlots([filed])) })
    await waitFor(() => expect(cardIn(container)).toBeTruthy())
    expect(stripIn(container), 'a mirror that outlived the card would bring the strip back').toBeNull()
  })
})
