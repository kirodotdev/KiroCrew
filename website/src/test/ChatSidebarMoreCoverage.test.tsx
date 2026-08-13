/**
 * Coverage for the ChatSidebar's drag-and-drop brain — the parts no sibling
 * suite reaches because they are not driven by DOM events at all:
 *
 *  1. `sidebarCollision`, the custom dnd-kit collision detector. Four distinct
 *     gestures share one DndContext (nested-folder re-parent, root-folder
 *     header-band re-parent, root-folder reorder, session assign), and the
 *     detector is the only thing that tells them apart.
 *  2. The `onDragStart` / `onDragOver` / `onDragEnd` / `onDragCancel` lifecycle,
 *     including the drop routing table and the 500ms hover-to-expand timer.
 *  3. The surfaces that exist ONLY while a drag is live: the chat-pane drop zone
 *     portaled into ChatPage's pane, the root un-nest hint, and the DragOverlay
 *     ghost.
 *
 * A pointer drag cannot be faithfully simulated in jsdom (it needs real
 * PointerEvents plus layout measurement), so — following the precedent in
 * `ChatSidebar.dragFreezeOrder.test.tsx` — DndContext is stubbed to capture the
 * real lifecycle props, which are then invoked directly. The collision detector
 * runs against fabricated rects, so dnd-kit's own `pointerWithin` /
 * `closestCenter` (kept real, not mocked) do the geometry.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, act, waitFor, within, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'
import type { ChatFolder, ChatTag, TagColumn } from '../types'

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
/** Mutable so one test can flip the sidebar into board (tag-column) view. */
const cfg = vi.hoisted(() => ({
  value: { tagColumnsEnabled: false, confirmCloseSession: false, defaultAutopilot: false } as Record<string, unknown>,
}))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => cfg.value,
  saveChatConfig: vi.fn(),
}))

/** Lifecycle props + the collision detector, captured off the stubbed context. */
const dnd = vi.hoisted(() => ({
  onDragStart: undefined as ((e: unknown) => void) | undefined,
  onDragOver: undefined as ((e: unknown) => void) | undefined,
  onDragEnd: undefined as ((e: unknown) => void) | undefined,
  onDragCancel: undefined as (() => void) | undefined,
  collision: undefined as unknown,
  /** Every `useDraggable` registration, so a row's pickup state is assertable
   *  (a pointer drag itself cannot be simulated — see the file header). */
  draggables: [] as Array<{ id: string; disabled: boolean }>,
}))

vi.mock('@dnd-kit/core', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@dnd-kit/core')>()
  return {
    ...actual,
    DndContext: (props: {
      children?: unknown
      collisionDetection?: unknown
      onDragStart?: (e: unknown) => void
      onDragOver?: (e: unknown) => void
      onDragEnd?: (e: unknown) => void
      onDragCancel?: () => void
    }) => {
      // Board-column contexts pass dnd-kit's own closestCenter; only the
      // list-view context passes the sidebar's own detector, which is the
      // one under test here.
      if (props.collisionDetection && props.collisionDetection !== actual.closestCenter) {
        dnd.collision = props.collisionDetection
      }
      dnd.onDragStart = props.onDragStart
      dnd.onDragOver = props.onDragOver
      dnd.onDragEnd = props.onDragEnd
      dnd.onDragCancel = props.onDragCancel
      return props.children as never
    },
    useDraggable: (args: Parameters<typeof actual.useDraggable>[0]) => {
      dnd.draggables.push({ id: String(args.id), disabled: !!args.disabled })
      return actual.useDraggable(args)
    },
    // The real overlay reads the active item off DndContext's internal store,
    // which the stub above does not provide, so it would render nothing. It is
    // a presentational portal — passing children through is what lets the ghost
    // itself be asserted.
    DragOverlay: (props: { children?: unknown }) => props.children as never,
  }
})

const mocks = vi.hoisted(() => ({
  chatFolders: vi.fn(),
  updateChatFolder: vi.fn(),
  setSlotFolder: vi.fn(),
  sessions: vi.fn(),
  sessionsSearch: vi.fn(),
  chatTags: vi.fn(),
  tagColumns: vi.fn(),
  reorderTagColumns: vi.fn(),
  dropSlotToColumn: vi.fn(),
}))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as unknown as Record<string, unknown>, {
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
globalThis.fetch = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }) as unknown as typeof fetch

import ChatSidebar, { sidebarCollision } from '../pages/ChatSidebar'

// ── Fixtures ────────────────────────────────────────────────────────────────

/** `f4` is hidden by the folder filter, so it lives in `folders` while its row
 *  (and therefore its `[data-folder-drop]` node) is NOT in the DOM — the input
 *  the hover-to-expand fallback branch needs. */
const HIDDEN_FOLDER_ID = 'f4'
const HIDDEN_FOLDERS_LS_KEY = 'mc-flat-hidden-folders'
const FLAT_VIEW_LS_KEY = 'mc-sidebar-flat-view'

const FOLDERS: ChatFolder[] = [
  { id: 'f1', name: 'Alpha', order: 0 },
  { id: 'f2', name: 'Beta', order: 1, collapsed: true },
  { id: 'f3', name: 'Gamma', order: 2, parent_id: 'f2' },
  { id: HIDDEN_FOLDER_ID, name: 'Delta', order: 3, collapsed: true },
]

interface TestSlot {
  key: string
  title?: string
  running: boolean
  messages?: number
  folder_id?: string
  memory_mode?: 'persistent' | 'incognito' | 'temporary'
  last_ts?: string
}

const SLOT_IN_FOLDER = 'chat-foldered'
const SLOT_LOOSE = 'chat-loose'
const SLOT_PRIVATE = 'chat-private'

const SLOTS: TestSlot[] = [
  { key: SLOT_IN_FOLDER, title: 'Foldered work', running: false, messages: 7, folder_id: 'f1', last_ts: '2026-03-01T00:00:00Z' },
  { key: SLOT_LOOSE, title: 'Loose work', running: false, messages: 2, last_ts: '2026-02-01T00:00:00Z' },
  { key: SLOT_PRIVATE, title: 'Private work', running: false, messages: 1, memory_mode: 'incognito', last_ts: '2026-01-01T00:00:00Z' },
]

/** Board (tag-column) view fixtures. `t-doing` is a STATUS tag, which is what
 *  makes its column accept a session-card drop; `t-note` is a plain label, so
 *  its column only accepts column reorders. */
const TAGS: ChatTag[] = [
  { id: 't-doing', name: 'Doing', color: '#4488ff', order: 0, status: true },
  { id: 't-note', name: 'Note', color: '#dd8844', order: 1 },
]
const COLUMNS: TagColumn[] = [
  { id: 'col-status', name: 'Doing lane', tag_ids: ['t-doing'], mode: 'any', order: 0 },
  { id: 'col-plain', name: 'Notes lane', tag_ids: ['t-note'], mode: 'any', order: 1 },
]

interface RenderOpts {
  slots?: TestSlot[]
  folders?: ChatFolder[]
  /** Attach a chat-pane element (with a composer inside unless suppressed). */
  chatPane?: boolean
  /** Omit the `[data-testid="input-wrapper"]` child from the pane. */
  paneWithoutComposer?: boolean
  onDropSessionRef?: (ref: { key: string; title: string; messages?: number }) => void
  activeSlot?: string | null
  /** Flip into board (tag-column) view and seed the column/tag caches. */
  board?: boolean
  /** Flip into flat view (every chat in one lane, no folder tree). */
  flat?: boolean
}

let panes: HTMLElement[] = []

function renderSidebar(opts: RenderOpts = {}) {
  const slots = opts.slots ?? SLOTS
  const folders = opts.folders ?? FOLDERS
  mocks.chatFolders.mockResolvedValue(folders)
  if (opts.flat) localStorage.setItem(FLAT_VIEW_LS_KEY, '1')
  if (opts.board) {
    cfg.value = { tagColumnsEnabled: true, confirmCloseSession: false, defaultAutopilot: false }
    mocks.chatTags.mockResolvedValue(TAGS)
    mocks.tagColumns.mockResolvedValue(COLUMNS)
  }

  let pane: HTMLElement | null = null
  if (opts.chatPane) {
    pane = document.createElement('div')
    if (!opts.paneWithoutComposer) {
      const composer = document.createElement('div')
      composer.setAttribute('data-testid', 'input-wrapper')
      pane.appendChild(composer)
    }
    document.body.appendChild(pane)
    panes.push(pane)
  }

  // Redux Toolkit REPLACES a slice's state with `preloadedState` rather than
  // merging it into the slice's initialState, so a hand-rolled partial silently
  // drops every key it forgets and reducers that assume the real shape then
  // throw as UNHANDLED rejections (which fail the run even while every test
  // passes). Spread the genuine defaults first.
  const defaults = createTestStore().getState()
  const store = createTestStore({
    dashboard: {
      ...defaults.dashboard,
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: {
      ...defaults.chat,
      activeSlot: opts.activeSlot ?? null,
      slotStatusDetail: {}, subagents: {}, slotActivity: {},
      goalLoops: {}, workflowRuns: {}, subagentQueued: {}, slotHistory: [],
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], folders)
  qc.setQueryData(['tag-columns'], opts.board ? COLUMNS : [])
  qc.setQueryData(['chat-tags'], opts.board ? TAGS : [])
  const view = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots} activeSlot={opts.activeSlot ?? null} unreadSlots={[]}
              history={[]} historyHasMore={false}
              defaultAgent="" installedAgents={[{ name: 'builder', source: 'builtin' }]}
              chatDropTarget={pane}
              onDropSessionRef={opts.onDropSessionRef}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return { ...view, store, pane, qc }
}

// ── Collision-detector harness ──────────────────────────────────────────────

interface Rect {
  top: number
  left: number
  bottom: number
  right: number
  width: number
  height: number
}
const rect = (top: number, left: number, width = 100, height = 34): Rect =>
  ({ top, left, width, height, bottom: top + height, right: left + width })

interface Container {
  id: string
  data: { current: Record<string, unknown> }
  rect: { current: Rect }
}
const container = (id: string, data: Record<string, unknown>, r: Rect): Container =>
  ({ id, data: { current: data }, rect: { current: r } })

/** Invoke the captured detector against fabricated geometry. */
function collide(opts: {
  active: Record<string, unknown>
  containers: Container[]
  pointer?: { x: number; y: number }
  collisionRect?: Rect
}): string[] {
  const detect = dnd.collision as ((args: unknown) => Array<{ id: string }>) | undefined
  if (!detect) throw new Error('collisionDetection was never captured')
  const hits = detect({
    active: { id: 'active-item', data: { current: opts.active }, rect: { current: { initial: null, translated: null } } },
    collisionRect: opts.collisionRect ?? rect(0, 0),
    droppableRects: new Map(opts.containers.map(c => [c.id, c.rect.current])),
    droppableContainers: opts.containers,
    pointerCoordinates: opts.pointer ?? null,
  })
  return (hits ?? []).map(h => h.id)
}

/** The band geometry `sidebarCollision` uses for the root-folder header gesture
 *  (FOLDER_HEADER_DROP_BAND = 34, re-parent between 25% and 75%). */
const HEADER = rect(0, 0, 120, 34)
const BAND_MIDDLE = { x: 20, y: 17 }
const BAND_TOP_EDGE = { x: 20, y: 2 }

beforeEach(() => {
  localStorage.clear()
  localStorage.setItem(HIDDEN_FOLDERS_LS_KEY, JSON.stringify([HIDDEN_FOLDER_ID]))
  cfg.value = { tagColumnsEnabled: false, confirmCloseSession: false, defaultAutopilot: false }
  mocks.chatFolders.mockResolvedValue(FOLDERS)
  mocks.updateChatFolder.mockResolvedValue({ ok: true })
  mocks.setSlotFolder.mockResolvedValue({ ok: true })
  mocks.sessions.mockResolvedValue({ sessions: [], has_more: false })
  mocks.sessionsSearch.mockResolvedValue({ sessions: [] })
  mocks.chatTags.mockResolvedValue([])
  mocks.tagColumns.mockResolvedValue([])
  mocks.reorderTagColumns.mockResolvedValue({ ok: true })
  mocks.dropSlotToColumn.mockResolvedValue({ ok: true })
  dnd.draggables = []
  // Reset so an assertion on the detector reads THIS test's render, not a
  // leftover capture from the previous one (every test renders before it looks).
  dnd.collision = undefined
  // The sidebar (and the rows it renders) schedule setTimeout / setInterval
  // work; without fake timers those callbacks fire after teardown and surface
  // as unhandled 'window is not defined' errors, which redden the run even
  // when every test passes.
  vi.useFakeTimers({ shouldAdvanceTime: true })
})

afterEach(() => {
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.clearAllMocks()
  for (const p of panes) p.remove()
  panes = []
})

describe('ChatSidebar — sidebarCollision routing', () => {
  it('re-parents a NESTED folder drag to the innermost folder drop zone under the pointer', () => {
    renderSidebar()
    const ids = collide({
      active: { type: 'folder', nested: true, subtree: ['f3'] },
      containers: [
        container('folder-drop:f1', { type: 'folder-drop', folderId: 'f1' }, HEADER),
        container('f1', { type: 'folder' }, HEADER),
      ],
      pointer: BAND_MIDDLE,
    })
    // Only the folder-drop container survives the filter, and the header-band
    // arithmetic is skipped entirely on the nested path.
    expect(ids).toEqual(['folder-drop:f1'])
  })

  it('never offers a nested folder its own subtree as a drop target', () => {
    renderSidebar()
    const ids = collide({
      active: { type: 'folder', nested: true, subtree: ['f2', 'f3'] },
      containers: [
        container('folder-drop:f2', { type: 'folder-drop', folderId: 'f2' }, HEADER),
        container('folder-drop:f3', { type: 'folder-drop', folderId: 'f3' }, HEADER),
      ],
      pointer: BAND_MIDDLE,
    })
    expect(ids).toEqual([])
  })

  it('keeps the root lane (folderId null) reachable for a nested folder drag', () => {
    renderSidebar()
    const ids = collide({
      active: { type: 'folder', nested: true, subtree: ['f3'] },
      containers: [container('root-lane', { type: 'folder-drop', folderId: null }, HEADER)],
      pointer: BAND_MIDDLE,
    })
    expect(ids).toEqual(['root-lane'])
  })

  it('a ROOT folder drag over the middle of another header re-parents (single folder-drop hit)', () => {
    renderSidebar()
    const ids = collide({
      active: { type: 'folder', subtree: ['f1'] },
      containers: [
        container('folder-drop:f2', { type: 'folder-drop', folderId: 'f2' }, HEADER),
        container('f2', { type: 'folder' }, HEADER),
        container('f4', { type: 'folder' }, rect(400, 0)),
      ],
      pointer: BAND_MIDDLE,
    })
    expect(ids).toEqual(['folder-drop:f2'])
  })

  it('a ROOT folder drag on a header EDGE falls through to the sortable reorder', () => {
    renderSidebar()
    const ids = collide({
      active: { type: 'folder', subtree: ['f1'] },
      containers: [
        container('folder-drop:f2', { type: 'folder-drop', folderId: 'f2' }, HEADER),
        container('f2', { type: 'folder' }, HEADER),
        container('f4', { type: 'folder' }, rect(400, 0)),
      ],
      pointer: BAND_TOP_EDGE,
    })
    // closestCenter over the `folder` containers only — the folder-drop zone is
    // filtered out, and the nearest sortable to the collision rect leads.
    expect(ids).toEqual(['f2', 'f4'])
  })

  it('a ROOT folder drag with no pointer coordinates reorders rather than re-parenting', () => {
    renderSidebar()
    const ids = collide({
      active: { type: 'folder', subtree: ['f1'] },
      containers: [
        container('folder-drop:f2', { type: 'folder-drop', folderId: 'f2' }, HEADER),
        container('f4', { type: 'folder' }, rect(400, 0)),
        container('f2', { type: 'folder' }, HEADER),
      ],
    })
    expect(ids).toEqual(['f2', 'f4'])
  })

  it('a SESSION drag resolves to whatever droppable the pointer is inside', () => {
    renderSidebar()
    const ids = collide({
      active: { type: 'session', key: SLOT_LOOSE },
      containers: [
        container('folder-drop:f1', { type: 'folder-drop', folderId: 'f1' }, HEADER),
        container('chat-pane-ref', { type: 'chat-pane-ref' }, rect(500, 500, 600, 400)),
      ],
      pointer: BAND_MIDDLE,
    })
    expect(ids).toEqual(['folder-drop:f1'])
  })

  it('a near-miss SESSION drag falls back to the nearest target but NEVER the chat pane', () => {
    renderSidebar()
    const ids = collide({
      active: { type: 'session', key: SLOT_LOOSE },
      containers: [
        // Dead centre on the collision rect — it would win closestCenter
        // outright if it were not excluded by type.
        container('chat-pane-ref', { type: 'chat-pane-ref' }, rect(-17, -50, 100, 34)),
        container('folder-drop:f1', { type: 'folder-drop', folderId: 'f1' }, rect(200, 0)),
      ],
      pointer: { x: 5_000, y: 5_000 },
    })
    expect(ids).toEqual(['folder-drop:f1'])
  })
})

describe('ChatSidebar — drop routing (onDragEnd)', () => {
  /** dnd-kit shapes: `active`/`over` each carry an id plus `data.current`. */
  const dragEnd = (
    active: { id: string; data?: Record<string, unknown> },
    over: { id: string; data?: Record<string, unknown> } | null,
  ) => act(() => {
    dnd.onDragEnd?.({
      active: { id: active.id, data: { current: active.data ?? {} } },
      over: over ? { id: over.id, data: { current: over.data ?? {} } } : null,
    })
  })

  it('a drop outside every target is a no-op', async () => {
    renderSidebar()
    await waitFor(() => expect(dnd.onDragEnd).toBeTruthy())
    dragEnd({ id: 'f1', data: { type: 'folder' } }, null)
    expect(mocks.updateChatFolder).not.toHaveBeenCalled()
    expect(mocks.setSlotFolder).not.toHaveBeenCalled()
  })

  it('re-parents a nested folder dropped on a folder zone', async () => {
    renderSidebar()
    await waitFor(() => expect(dnd.onDragEnd).toBeTruthy())
    dragEnd(
      { id: 'f3', data: { type: 'folder', nested: true } },
      { id: 'folder-drop:f1', data: { type: 'folder-drop', folderId: 'f1' } },
    )
    await waitFor(() => expect(mocks.updateChatFolder).toHaveBeenCalledWith('f3', { parent_id: 'f1' }))
  })

  it('moves a nested folder to the top level when dropped on the root lane', async () => {
    renderSidebar()
    await waitFor(() => expect(dnd.onDragEnd).toBeTruthy())
    dragEnd(
      { id: 'f3', data: { type: 'folder', nested: true } },
      { id: 'root-lane', data: { type: 'folder-drop', folderId: null } },
    )
    await waitFor(() => expect(mocks.updateChatFolder).toHaveBeenCalledWith('f3', { parent_id: '' }))
  })

  it('ignores a nested folder dropped on a sortable container rather than a folder zone', async () => {
    renderSidebar()
    await waitFor(() => expect(dnd.onDragEnd).toBeTruthy())
    dragEnd({ id: 'f3', data: { type: 'folder', nested: true } }, { id: 'f1', data: { type: 'folder' } })
    expect(mocks.updateChatFolder).not.toHaveBeenCalled()
  })

  it('re-parents a root folder dropped on a folder zone', async () => {
    renderSidebar()
    await waitFor(() => expect(dnd.onDragEnd).toBeTruthy())
    dragEnd(
      { id: 'f1', data: { type: 'folder' } },
      { id: 'folder-drop:f2', data: { type: 'folder-drop', folderId: 'f2' } },
    )
    await waitFor(() => expect(mocks.updateChatFolder).toHaveBeenCalledWith('f1', { parent_id: 'f2' }))
  })

  it('ignores a root folder dropped on the root lane (it is already top level)', async () => {
    renderSidebar()
    await waitFor(() => expect(dnd.onDragEnd).toBeTruthy())
    dragEnd(
      { id: 'f1', data: { type: 'folder' } },
      { id: 'root-lane', data: { type: 'folder-drop', folderId: null } },
    )
    expect(mocks.updateChatFolder).not.toHaveBeenCalled()
  })

  it('reorders sibling root folders when the drop lands on a sortable container', async () => {
    renderSidebar()
    await waitFor(() => expect(dnd.onDragEnd).toBeTruthy())
    dragEnd({ id: 'f1', data: { type: 'folder' } }, { id: HIDDEN_FOLDER_ID, data: { type: 'folder' } })
    await waitFor(() => expect(mocks.updateChatFolder).toHaveBeenCalled())
    // Every persisted change is an `order` write — never a re-parent.
    for (const call of mocks.updateChatFolder.mock.calls) {
      expect(Object.keys(call[1] as object)).toEqual(['order'])
    }
  })

  it('does nothing when a folder is dropped onto itself', async () => {
    renderSidebar()
    await waitFor(() => expect(dnd.onDragEnd).toBeTruthy())
    dragEnd({ id: 'f1', data: { type: 'folder' } }, { id: 'f1', data: { type: 'folder' } })
    expect(mocks.updateChatFolder).not.toHaveBeenCalled()
  })

  it('assigns a session to the folder whose drop zone received it', async () => {
    renderSidebar()
    await waitFor(() => expect(dnd.onDragEnd).toBeTruthy())
    dragEnd(
      { id: SLOT_LOOSE, data: { type: 'session', key: SLOT_LOOSE } },
      { id: 'folder-drop:f1', data: { type: 'folder-drop', folderId: 'f1' } },
    )
    await waitFor(() => expect(mocks.setSlotFolder).toHaveBeenCalledWith(SLOT_LOOSE, 'f1'))
  })

  it('ungroups a session dropped on the root lane', async () => {
    renderSidebar()
    await waitFor(() => expect(dnd.onDragEnd).toBeTruthy())
    dragEnd(
      { id: SLOT_IN_FOLDER, data: { type: 'session', key: SLOT_IN_FOLDER } },
      { id: 'root-lane', data: { type: 'folder-drop', folderId: null } },
    )
    await waitFor(() => expect(mocks.setSlotFolder).toHaveBeenCalledWith(SLOT_IN_FOLDER, null))
  })

  it('assigns a session dropped on a folder sortable block to that folder', async () => {
    renderSidebar()
    await waitFor(() => expect(dnd.onDragEnd).toBeTruthy())
    dragEnd(
      { id: SLOT_LOOSE, data: { type: 'session', key: SLOT_LOOSE } },
      { id: 'f2', data: { type: 'folder' } },
    )
    await waitFor(() => expect(mocks.setSlotFolder).toHaveBeenCalledWith(SLOT_LOOSE, 'f2'))
  })

  it('stages a session reference when the drop lands on the chat pane', async () => {
    const onDropSessionRef = vi.fn()
    renderSidebar({ chatPane: true, onDropSessionRef })
    await waitFor(() => expect(dnd.onDragEnd).toBeTruthy())
    dragEnd(
      { id: SLOT_IN_FOLDER, data: { type: 'session', key: SLOT_IN_FOLDER } },
      { id: 'chat-pane-ref', data: { type: 'chat-pane-ref' } },
    )
    expect(onDropSessionRef).toHaveBeenCalledWith({ key: SLOT_IN_FOLDER, title: 'Foldered work', messages: 7 })
    expect(mocks.setSlotFolder).not.toHaveBeenCalled()
  })

  it('refuses to stage a reference to a private session, independently of the affordance', async () => {
    const onDropSessionRef = vi.fn()
    renderSidebar({ chatPane: true, onDropSessionRef })
    await waitFor(() => expect(dnd.onDragEnd).toBeTruthy())
    dragEnd(
      { id: SLOT_PRIVATE, data: { type: 'session', key: SLOT_PRIVATE } },
      { id: 'chat-pane-ref', data: { type: 'chat-pane-ref' } },
    )
    expect(onDropSessionRef).not.toHaveBeenCalled()
  })

  it('refuses to stage a reference to the session already on screen', async () => {
    const onDropSessionRef = vi.fn()
    renderSidebar({ chatPane: true, onDropSessionRef, activeSlot: SLOT_LOOSE })
    await waitFor(() => expect(dnd.onDragEnd).toBeTruthy())
    dragEnd(
      { id: SLOT_LOOSE, data: { type: 'session', key: SLOT_LOOSE } },
      { id: 'chat-pane-ref', data: { type: 'chat-pane-ref' } },
    )
    expect(onDropSessionRef).not.toHaveBeenCalled()
  })
})

describe('ChatSidebar — hover-to-expand (onDragOver)', () => {
  const dragOver = (over: { id: string; data?: Record<string, unknown> } | null) => act(() => {
    dnd.onDragOver?.({ over: over ? { id: over.id, data: { current: over.data ?? {} } } : null })
  })

  it('expands a collapsed folder after a sustained hover, animating its ring first', async () => {
    renderSidebar()
    await waitFor(() => expect(dnd.onDragOver).toBeTruthy())
    const zone = document.querySelector('[data-folder-drop="f2"]') as HTMLElement | null
    expect(zone).toBeTruthy()

    dragOver({ id: 'folder-drop:f2', data: { type: 'folder-drop', folderId: 'f2' } })
    // Nothing yet — the dwell has to be sustained.
    act(() => { vi.advanceTimersByTime(400) })
    expect(mocks.updateChatFolder).not.toHaveBeenCalled()

    // 500ms dwell fires the blink, which paints the ring inline.
    act(() => { vi.advanceTimersByTime(150) })
    expect(zone?.style.boxShadow).toContain('var(--accent)')
    expect(mocks.updateChatFolder).not.toHaveBeenCalled()

    // The blink runs for 450ms before the expand commits and the inline
    // overrides are handed back to the stylesheet.
    act(() => { vi.advanceTimersByTime(500) })
    await waitFor(() => expect(mocks.updateChatFolder).toHaveBeenCalledWith('f2', { collapsed: false }))
    expect(zone?.style.boxShadow).toBe('')
  })

  it('expands straight away when the folder has no row on screen to animate', async () => {
    renderSidebar()
    await waitFor(() => expect(dnd.onDragOver).toBeTruthy())
    // Hidden by the folder filter: present in `folders`, absent from the DOM.
    expect(document.querySelector(`[data-folder-drop="${HIDDEN_FOLDER_ID}"]`)).toBeNull()

    dragOver({ id: `folder-drop:${HIDDEN_FOLDER_ID}`, data: { type: 'folder-drop', folderId: HIDDEN_FOLDER_ID } })
    act(() => { vi.advanceTimersByTime(600) })
    await waitFor(() => expect(mocks.updateChatFolder).toHaveBeenCalledWith(HIDDEN_FOLDER_ID, { collapsed: false }))
  })

  it('cancels the pending expand when the pointer moves off the folder', async () => {
    renderSidebar()
    await waitFor(() => expect(dnd.onDragOver).toBeTruthy())
    dragOver({ id: 'folder-drop:f2', data: { type: 'folder-drop', folderId: 'f2' } })
    act(() => { vi.advanceTimersByTime(200) })
    // Moving onto an EXPANDED folder clears the timer instead of re-arming it.
    dragOver({ id: 'folder-drop:f1', data: { type: 'folder-drop', folderId: 'f1' } })
    act(() => { vi.advanceTimersByTime(1_000) })
    expect(mocks.updateChatFolder).not.toHaveBeenCalled()
  })

  it('re-arms the timer when the hover moves to a DIFFERENT collapsed folder', async () => {
    renderSidebar()
    await waitFor(() => expect(dnd.onDragOver).toBeTruthy())
    dragOver({ id: `folder-drop:${HIDDEN_FOLDER_ID}`, data: { type: 'folder-drop', folderId: HIDDEN_FOLDER_ID } })
    act(() => { vi.advanceTimersByTime(200) })
    dragOver({ id: 'folder-drop:f2', data: { type: 'folder-drop', folderId: 'f2' } })
    act(() => { vi.advanceTimersByTime(1_200) })
    // Only the second folder ever expands — the first one's timer was dropped.
    await waitFor(() => expect(mocks.updateChatFolder).toHaveBeenCalledWith('f2', { collapsed: false }))
    const targets = mocks.updateChatFolder.mock.calls.map(c => c[0])
    expect(targets).not.toContain(HIDDEN_FOLDER_ID)
  })

  it('clears a pending expand when the drag leaves every droppable', async () => {
    renderSidebar()
    await waitFor(() => expect(dnd.onDragOver).toBeTruthy())
    dragOver({ id: 'folder-drop:f2', data: { type: 'folder-drop', folderId: 'f2' } })
    act(() => { vi.advanceTimersByTime(200) })
    dragOver(null)
    act(() => { vi.advanceTimersByTime(1_000) })
    expect(mocks.updateChatFolder).not.toHaveBeenCalled()
  })

  it('a pending expand does not survive the drop', async () => {
    renderSidebar()
    await waitFor(() => expect(dnd.onDragOver).toBeTruthy())
    dragOver({ id: 'folder-drop:f2', data: { type: 'folder-drop', folderId: 'f2' } })
    act(() => { vi.advanceTimersByTime(200) })
    act(() => {
      dnd.onDragEnd?.({
        active: { id: SLOT_LOOSE, data: { current: { type: 'session', key: SLOT_LOOSE } } },
        over: null,
      })
    })
    act(() => { vi.advanceTimersByTime(1_000) })
    expect(mocks.updateChatFolder).not.toHaveBeenCalled()
  })
})

describe('ChatSidebar — surfaces that exist only during a drag', () => {
  const dragStart = (data: Record<string, unknown>, id: string) => act(() => {
    dnd.onDragStart?.({ active: { id, data: { current: data } } })
  })

  it('portals the chat-pane drop zone into the pane and outlines the composer', async () => {
    renderSidebar({ chatPane: true, onDropSessionRef: vi.fn() })
    await waitFor(() => expect(dnd.onDragStart).toBeTruthy())
    expect(screen.queryByTestId('chat-pane-drop-zone')).toBeNull()

    dragStart({ type: 'session', key: SLOT_LOOSE }, SLOT_LOOSE)
    const zone = await screen.findByTestId('chat-pane-drop-zone', undefined, { timeout: 5_000 })
    expect(zone.parentElement).toBe(panes[0])
    expect(within(zone).getByText('Drop to reference this session')).toBeTruthy()
    // Composer was measurable, so the destination itself is outlined.
    expect(within(zone).getByTestId('chat-pane-drop-target')).toBeTruthy()
    expect(zone.hasAttribute('data-refused')).toBe(false)
  })

  it('shows the refusal state (and no destination outline) for a private session', async () => {
    renderSidebar({ chatPane: true, onDropSessionRef: vi.fn() })
    await waitFor(() => expect(dnd.onDragStart).toBeTruthy())
    dragStart({ type: 'session', key: SLOT_PRIVATE }, SLOT_PRIVATE)
    const zone = await screen.findByTestId('chat-pane-drop-zone', undefined, { timeout: 5_000 })
    expect(zone.hasAttribute('data-refused')).toBe(true)
    expect(zone.getAttribute('data-refused')).toBe('private')
    expect(within(zone).getByText("Private sessions can't be referenced")).toBeTruthy()
    expect(within(zone).queryByTestId('chat-pane-drop-target')).toBeNull()
  })

  /**
   * Dragging the OPEN session onto its own pane is a no-op, but it is the user's
   * near-miss rather than a guard firing — so it must not borrow the privacy
   * refusal's copy (which would state something untrue about the session) nor its
   * warn tone (which would report a harmless gesture as a problem). It gets its
   * own recursive line, and the two refusals stay tellable apart in the DOM.
   */
  it('answers a session dropped onto its own pane with the recursive line, not the privacy refusal', async () => {
    renderSidebar({ chatPane: true, onDropSessionRef: vi.fn(), activeSlot: SLOT_LOOSE })
    await waitFor(() => expect(dnd.onDragStart).toBeTruthy())
    dragStart({ type: 'session', key: SLOT_LOOSE }, SLOT_LOOSE)
    const zone = await screen.findByTestId('chat-pane-drop-zone', undefined, { timeout: 5_000 })
    expect(zone.getAttribute('data-refused')).toBe('self')
    expect(within(zone).getByText("Can't drop a session into itself… itself… itself…")).toBeTruthy()
    expect(within(zone).queryByText("Private sessions can't be referenced")).toBeNull()
    // Still a refusal: no destination is outlined, and the pill is not the invite.
    expect(within(zone).queryByTestId('chat-pane-drop-target')).toBeNull()
    expect(within(zone).queryByText('Drop to reference this session')).toBeNull()
  })

  it('stages nothing when the open session is dropped onto its own pane', async () => {
    const onDropSessionRef = vi.fn()
    renderSidebar({ chatPane: true, onDropSessionRef, activeSlot: SLOT_LOOSE })
    await waitFor(() => expect(dnd.onDragEnd).toBeTruthy())
    act(() => {
      dnd.onDragEnd?.({
        active: { id: SLOT_LOOSE, data: { current: { type: 'session', key: SLOT_LOOSE } } },
        over: { id: 'chat-pane-ref', data: { current: { type: 'chat-pane-ref' } } },
      })
    })
    expect(onDropSessionRef).not.toHaveBeenCalled()
  })

  it('falls back to a centred pill when the pane has no composer to measure', async () => {
    renderSidebar({ chatPane: true, paneWithoutComposer: true, onDropSessionRef: vi.fn() })
    await waitFor(() => expect(dnd.onDragStart).toBeTruthy())
    dragStart({ type: 'session', key: SLOT_LOOSE }, SLOT_LOOSE)
    const zone = await screen.findByTestId('chat-pane-drop-zone', undefined, { timeout: 5_000 })
    expect(within(zone).queryByTestId('chat-pane-drop-target')).toBeNull()
    expect(within(zone).getByText('Drop to reference this session')).toBeTruthy()
  })

  it('never mounts the zone when the host gave no drop handler', async () => {
    renderSidebar({ chatPane: true })
    await waitFor(() => expect(dnd.onDragStart).toBeTruthy())
    dragStart({ type: 'session', key: SLOT_LOOSE }, SLOT_LOOSE)
    expect(screen.queryByTestId('chat-pane-drop-zone')).toBeNull()
  })

  it('offers an un-nest target while a subfolder is being dragged', async () => {
    renderSidebar()
    await waitFor(() => expect(dnd.onDragStart).toBeTruthy())
    expect(screen.queryByText('Drop here to remove from folder')).toBeNull()
    dragStart({ type: 'folder' }, 'f3')  // f3 is parented under f2
    expect(await screen.findByText('Drop here to remove from folder', undefined, { timeout: 5_000 })).toBeTruthy()
  })

  it('offers an un-nest target while a foldered session is dragged over an empty root lane', async () => {
    // Only slot is inside a folder, so the ungrouped lane is empty — the case
    // the placeholder exists for.
    renderSidebar({ slots: [SLOTS[0]] })
    await waitFor(() => expect(dnd.onDragStart).toBeTruthy())
    dragStart({ type: 'session', key: SLOT_IN_FOLDER }, SLOT_IN_FOLDER)
    expect(await screen.findByText('Drop here to remove from folder', undefined, { timeout: 5_000 })).toBeTruthy()
  })

  it('previews the dragged folder in the overlay ghost', async () => {
    renderSidebar()
    await waitFor(() => expect(dnd.onDragStart).toBeTruthy())
    dragStart({ type: 'folder' }, 'f1')
    // "Alpha" is also the folder header, so count instances rather than
    // asserting a single match.
    await waitFor(() => expect(screen.getAllByText('Alpha').length).toBeGreaterThan(1))
  })

  it('previews the dragged session in the overlay ghost and clears it on cancel', async () => {
    renderSidebar({ slots: [SLOTS[1]] })
    await waitFor(() => expect(dnd.onDragStart).toBeTruthy())
    dragStart({ type: 'session', key: SLOT_LOOSE }, SLOT_LOOSE)
    await waitFor(() => expect(screen.getAllByText('Loose work').length).toBeGreaterThan(1))
    act(() => { dnd.onDragCancel?.() })
    await waitFor(() => expect(screen.getAllByText('Loose work')).toHaveLength(1))
  })

  it('falls back to the session key when the dragged slot has no distinct title', async () => {
    renderSidebar({ slots: [{ key: SLOT_LOOSE, title: SLOT_LOOSE, running: false, messages: 1 }] })
    await waitFor(() => expect(dnd.onDragStart).toBeTruthy())
    dragStart({ type: 'session', key: SLOT_LOOSE }, SLOT_LOOSE)
    await waitFor(() => expect(screen.getAllByText(SLOT_LOOSE).length).toBeGreaterThan(1))
  })

  it('renders the preview OUTSIDE the sidebar, where the drawer clip cannot hide it', async () => {
    // The sidebar rides inside OverlayDrawer's morph clip-path, which clips
    // fixed-position descendants too — an in-place overlay disappeared the
    // moment the cursor crossed into the chat pane, i.e. for the whole second
    // half of the one gesture that aims there.
    renderSidebar({ slots: [SLOTS[1]] })
    await waitFor(() => expect(dnd.onDragStart).toBeTruthy())
    dragStart({ type: 'session', key: SLOT_LOOSE }, SLOT_LOOSE)
    await waitFor(() => expect(screen.getAllByText('Loose work').length).toBeGreaterThan(1))
    const ghosts = screen.getAllByText('Loose work').filter(el => !el.closest('.sidebar-inner'))
    expect(ghosts).toHaveLength(1)
    expect(ghosts[0].parentElement).toBe(document.body)
  })
})

describe('ChatSidebar — flat view', () => {
  const dragStart = (data: Record<string, unknown>, id: string) => act(() => {
    dnd.onDragStart?.({ active: { id, data: { current: data } } })
  })
  const dragEnd = (
    active: { id: string; data: Record<string, unknown> },
    over: { id: string; data: Record<string, unknown> } | null,
  ) => act(() => {
    dnd.onDragEnd?.({
      active: { id: active.id, data: { current: active.data } },
      over: over ? { id: over.id, data: { current: over.data } } : null,
    })
  })
  /** Most recent registration for a row, since every render re-registers. */
  const pickup = (slotKey: string) =>
    dnd.draggables.filter(d => d.id === `session:${slotKey}`).at(-1)

  it('picks a session up with dnd-kit, the same as the folder tree', async () => {
    renderSidebar({ flat: true })
    await waitFor(() => expect(screen.getByTestId('flat-view-lane')).toBeTruthy())
    await waitFor(() => expect(pickup(SLOT_LOOSE)).toBeTruthy())
    expect(pickup(SLOT_LOOSE)?.disabled).toBe(false)
    // The board layout's separate native-HTML5 drag stays off here — one
    // gesture, not two competing ones.
    const row = document.querySelector(`[data-session-row="${SLOT_LOOSE}"]`) as HTMLElement
    expect(row.getAttribute('draggable')).not.toBe('true')
  })

  it('stages a session reference when a flat-lane drag lands on the chat pane', async () => {
    const onDropSessionRef = vi.fn()
    renderSidebar({ flat: true, chatPane: true, onDropSessionRef })
    await waitFor(() => expect(dnd.onDragEnd).toBeTruthy())
    dragEnd(
      { id: SLOT_IN_FOLDER, data: { type: 'session', key: SLOT_IN_FOLDER } },
      { id: 'chat-pane-ref', data: { type: 'chat-pane-ref' } },
    )
    expect(onDropSessionRef).toHaveBeenCalledWith({ key: SLOT_IN_FOLDER, title: 'Foldered work', messages: 7 })
  })

  it('mounts the chat-pane drop zone for a flat-lane drag', async () => {
    renderSidebar({ flat: true, chatPane: true, onDropSessionRef: vi.fn() })
    await waitFor(() => expect(dnd.onDragStart).toBeTruthy())
    expect(screen.queryByTestId('chat-pane-drop-zone')).toBeNull()
    dragStart({ type: 'session', key: SLOT_LOOSE }, SLOT_LOOSE)
    const zone = await screen.findByTestId('chat-pane-drop-zone', undefined, { timeout: 5_000 })
    expect(zone.parentElement).toBe(panes[0])
  })

  it('registers no in-lane drop target, so the order stays unchangeable by hand', async () => {
    renderSidebar({ flat: true })
    await waitFor(() => expect(screen.getByTestId('flat-view-lane')).toBeTruthy())
    await waitFor(() => expect(pickup(SLOT_LOOSE)).toBeTruthy())
    // No folder-drop zones and no sortable folder blocks exist in this lane, so
    // a release inside the sidebar has nothing to resolve to: neither re-filing
    // nor reordering is reachable. (`sidebarCollision` keeps the chat pane out
    // of its closestCenter fallback, so it cannot capture the miss either.)
    expect(document.querySelectorAll('[data-folder-drop]')).toHaveLength(0)
    dragEnd({ id: SLOT_LOOSE, data: { type: 'session', key: SLOT_LOOSE } }, null)
    expect(mocks.setSlotFolder).not.toHaveBeenCalled()
    expect(mocks.updateChatFolder).not.toHaveBeenCalled()
  })

  it('detects collisions with the sidebar detector, so the pane cannot steal a near miss', async () => {
    // Pins the DETECTOR, not just the absence of targets. dnd-kit's default
    // closestCenter would hand a release anywhere in the sidebar to the only
    // registered droppable — the pane-sized chat zone — turning every near miss
    // into a staged reference. `sidebarCollision` excludes that zone from its
    // fallback, and nothing else in the flat lane's assertions can see the swap.
    renderSidebar({ flat: true })
    await waitFor(() => expect(screen.getByTestId('flat-view-lane')).toBeTruthy())
    await waitFor(() => expect(dnd.collision).toBeTruthy())
    expect(dnd.collision).toBe(sidebarCollision)
  })
})

describe('ChatSidebar — Older Sessions pane resize', () => {
  const HISTORY_HEIGHT_LS_KEY = 'mc-history-height'

  /** The separator only exists while the pane is expanded. */
  function openAndGrabHandle() {
    renderSidebar({ slots: [SLOTS[1]] })
    act(() => { screen.getByLabelText('Older sessions').click() })
    return screen.getByRole('separator', { name: 'Resize history pane' })
  }

  const drag = (handle: HTMLElement, fromY: number, toY: number) => {
    fireEvent.pointerDown(handle, { clientY: fromY, pointerId: 1 })
    fireEvent.pointerMove(handle, { clientY: toY, pointerId: 1 })
  }

  it('grows the pane when dragged upward and persists the new height', () => {
    const handle = openAndGrabHandle()
    // Default height is 240; the handle sits ABOVE the pane, so dragging up
    // (negative dy) grows it.
    drag(handle, 400, 300)
    expect(document.body.style.cursor).toBe('ns-resize')
    fireEvent.pointerUp(handle, { clientY: 300, pointerId: 1 })
    expect(localStorage.getItem(HISTORY_HEIGHT_LS_KEY)).toBe('340')
    // Global cursor / selection locks are released on drag end.
    expect(document.body.style.cursor).toBe('')
    expect(document.body.style.userSelect).toBe('')
  })

  it('clamps to the minimum height when dragged far downward', () => {
    const handle = openAndGrabHandle()
    drag(handle, 400, 4_000)
    fireEvent.pointerUp(handle, { clientY: 4_000, pointerId: 1 })
    expect(localStorage.getItem(HISTORY_HEIGHT_LS_KEY)).toBe('120')
  })

  it('clamps to the maximum height when dragged far upward', () => {
    const handle = openAndGrabHandle()
    drag(handle, 4_000, 0)
    fireEvent.pointerUp(handle, { clientY: 0, pointerId: 1 })
    expect(localStorage.getItem(HISTORY_HEIGHT_LS_KEY)).toBe('800')
  })

  it('restores the global cursor lock when the sidebar unmounts mid-drag', () => {
    const view = renderSidebar({ slots: [SLOTS[1]] })
    act(() => { screen.getByLabelText('Older sessions').click() })
    const handle = screen.getByRole('separator', { name: 'Resize history pane' })
    drag(handle, 400, 350)
    expect(document.body.style.cursor).toBe('ns-resize')
    // onEnd can never fire once the captured element is gone, so the unmount
    // guard is the only thing that releases the lock.
    view.unmount()
    expect(document.body.style.cursor).toBe('')
    expect(document.body.style.userSelect).toBe('')
  })

  it('restores a persisted height on mount and ignores an out-of-range one', () => {
    localStorage.setItem(HISTORY_HEIGHT_LS_KEY, '999999')
    const first = openAndGrabHandle()
    expect(first).toBeTruthy()
    // Rejected as out of range, so the default is written back instead.
    expect(localStorage.getItem(HISTORY_HEIGHT_LS_KEY)).toBe('240')
  })
})

describe('ChatSidebar — board column drag targets', () => {
  /** A minimal DataTransfer stand-in: only `types` / `getData` / `setData` are
   *  read by the handlers under test. */
  const transfer = (types: string[], data: Record<string, string> = {}) => ({
    types,
    getData: (t: string) => data[t] ?? '',
    setData: vi.fn(),
    effectAllowed: 'none',
  })

  async function renderBoard() {
    const view = renderSidebar({ board: true })
    await waitFor(() => expect(screen.getByTestId('column-strip')).toBeTruthy())
    return view
  }

  const column = (id: string) => screen.getByTestId(`column-${id}`)

  it('accepts a column reorder anywhere on a column surface', async () => {
    await renderBoard()
    const notPrevented = fireEvent.dragOver(column('col-plain'), {
      dataTransfer: transfer(['application/mc-column']),
    })
    // fireEvent returns false once the handler called preventDefault.
    expect(notPrevented).toBe(false)
    // A column reorder is not a card drop, so no ring highlight.
    expect(column('col-plain').className).not.toContain('ring-accent')
  })

  it('highlights only a STATUS lane while a session card is dragged over it', async () => {
    await renderBoard()
    const status = column('col-status')
    expect(fireEvent.dragOver(status, { dataTransfer: transfer(['text/plain']) })).toBe(false)
    expect(status.className).toContain('ring-accent')

    const plain = column('col-plain')
    // A plain label column cannot receive a card, so the drag is not accepted.
    expect(fireEvent.dragOver(plain, { dataTransfer: transfer(['text/plain']) })).toBe(true)
    expect(plain.className).not.toContain('ring-accent')
  })

  it('drops the highlight again when the card leaves', async () => {
    await renderBoard()
    const status = column('col-status')
    fireEvent.dragOver(status, { dataTransfer: transfer(['text/plain']) })
    expect(status.className).toContain('ring-accent')
    fireEvent.dragLeave(status)
    expect(status.className).not.toContain('ring-accent')
  })

  it('persists a new column order when a column is dropped on a sibling', async () => {
    await renderBoard()
    fireEvent.drop(column('col-status'), {
      dataTransfer: transfer(['application/mc-column'], { 'application/mc-column': 'col-plain' }),
    })
    await waitFor(() => expect(mocks.reorderTagColumns).toHaveBeenCalledWith(['col-plain', 'col-status']))
  })

  it('ignores a column dropped back onto itself', async () => {
    await renderBoard()
    fireEvent.drop(column('col-status'), {
      dataTransfer: transfer(['application/mc-column'], { 'application/mc-column': 'col-status' }),
    })
    expect(mocks.reorderTagColumns).not.toHaveBeenCalled()
    // Falls through to the card path, which finds no session key to move.
    expect(mocks.dropSlotToColumn).not.toHaveBeenCalled()
  })

  it('assigns the dropped session to a status lane', async () => {
    await renderBoard()
    fireEvent.drop(column('col-status'), {
      dataTransfer: transfer(['text/plain'], { 'text/plain': SLOT_LOOSE }),
    })
    await waitFor(() => expect(mocks.dropSlotToColumn).toHaveBeenCalledWith(SLOT_LOOSE, 'col-status'))
  })

  it('refuses a session drop on a column that is not a status lane', async () => {
    await renderBoard()
    fireEvent.drop(column('col-plain'), {
      dataTransfer: transfer(['text/plain'], { 'text/plain': SLOT_LOOSE }),
    })
    expect(mocks.dropSlotToColumn).not.toHaveBeenCalled()
  })

  it('publishes the column id from the reorder grip', async () => {
    await renderBoard()
    const grip = within(column('col-status')).getAllByTitle('Drag to reorder')[0]
    const dt = transfer([])
    fireEvent.dragStart(grip, { dataTransfer: dt })
    expect(dt.setData).toHaveBeenCalledWith('application/mc-column', 'col-status')
  })
})
