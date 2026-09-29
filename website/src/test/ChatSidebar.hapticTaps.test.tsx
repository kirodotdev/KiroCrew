/**
 * Chat sidebar — where the hand is told something happened.
 *
 * `haptic` is the tap a touch device plays. The sidebar plays it at six places,
 * and at each one only once the action has passed every refusal, so a release
 * that changes nothing is felt as nothing:
 *
 *  - drag start ............................ 'medium' (the row is picked up);
 *                                            a cancelled drag is silent
 *  - pinned reorder (drag or Alt+Arrow) .... 'light'
 *  - sibling folder reorder ................ 'light', only in the known Custom
 *                                            order and only when rows renumber
 *  - session dropped on another folder ..... 'light', silent for its own folder
 *  - subfolder re-parented by drag ......... 'light', silent for its current
 *                                            parent or its own subtree
 *  - session dropped on the chat pane ...... 'light', silent when the reference
 *                                            is refused (private, or the open one)
 *
 * The file also pins the drag lifecycle the drop math depends on: drag start
 * releases the hover hold, the order it freezes holds until the drop, and the
 * drop thaws it.
 *
 * A pointer drag cannot be simulated in jsdom, so, as in
 * ChatSidebar.dragFreezeOrder.test.tsx and ChatSidebarMoreCoverage.test.tsx, the
 * DndContext is stubbed to capture the real lifecycle props (last writer wins;
 * the tree lane renders exactly one list context, and the guard below checks it
 * is the one carrying the sidebar's own collision detector).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'
import type { ChatFolder } from '../types'

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
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false, defaultAutopilot: false }),
  saveChatConfig: vi.fn(),
}))

const hapticMock = vi.hoisted(() => vi.fn())
vi.mock('../lib/haptic', () => ({ haptic: hapticMock }))

/** Lifecycle props captured off the stubbed context, plus a stand-in for dnd-kit's
 *  own store: the sidebar reconciles its drag mirror against
 *  `useDndContext().active`, so a scripted start sets it and a scripted end clears it. */
const dnd = vi.hoisted(() => ({
  onDragStart: undefined as ((e: unknown) => void) | undefined,
  onDragEnd: undefined as ((e: unknown) => void) | undefined,
  onDragCancel: undefined as ((e: unknown) => void) | undefined,
  collision: undefined as unknown,
  active: null as { id: string } | null,
}))
vi.mock('@dnd-kit/core', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@dnd-kit/core')>()
  return {
    ...actual,
    DndContext: (props: {
      children?: unknown
      collisionDetection?: unknown
      onDragStart?: (e: unknown) => void
      onDragEnd?: (e: unknown) => void
      onDragCancel?: (e: unknown) => void
    }) => {
      dnd.collision = props.collisionDetection
      dnd.onDragStart = props.onDragStart
      dnd.onDragEnd = props.onDragEnd
      dnd.onDragCancel = props.onDragCancel
      return props.children as never
    },
    useDndContext: () => ({ ...actual.useDndContext(), active: dnd.active }),
  }
})

const mocks = vi.hoisted(() => ({
  chatFolders: vi.fn(),
  kirocrewConfig: vi.fn(),
  updateChatFolder: vi.fn(),
  reorderChatFolders: vi.fn(),
  setSlotFolder: vi.fn(),
  sessions: vi.fn(),
  sessionsSearch: vi.fn(),
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

type TestSlot = Record<string, unknown>

/** f2 holds the subfolder f3; f1 is a plain root folder. */
const FOLDERS: ChatFolder[] = [
  { id: 'f1', name: 'Alpha', order: 0 },
  { id: 'f2', name: 'Beta', order: 1 },
  { id: 'f3', name: 'Gamma', order: 0, parent_id: 'f2' },
]

const IN_F1 = 'chat-in-f1'
const LOOSE = 'chat-loose'
const PRIVATE = 'chat-private'
const PIN_A = 'chat-pin-a'
const PIN_B = 'chat-pin-b'

const SLOTS: TestSlot[] = [
  { key: PIN_A, title: 'Pinned first', running: false, messages: 1, pinned: true, last_ts: '2026-03-05T00:00:00Z' },
  { key: PIN_B, title: 'Pinned second', running: false, messages: 1, pinned: true, last_ts: '2026-03-04T00:00:00Z' },
  { key: IN_F1, title: 'Foldered work', running: false, messages: 7, folder_id: 'f1', last_ts: '2026-03-01T00:00:00Z' },
  { key: LOOSE, title: 'Loose work', running: false, messages: 2, last_ts: '2026-02-01T00:00:00Z' },
  { key: PRIVATE, title: 'Private work', running: false, messages: 1, memory_mode: 'incognito', last_ts: '2026-01-01T00:00:00Z' },
]

let panes: HTMLElement[] = []

function renderSidebar(opts: {
  slots?: TestSlot[]
  folders?: ChatFolder[]
  activeSlot?: string | null
  onDropSessionRef?: (ref: { key: string; title: string; messages?: number }) => void
  /** The folder order the config read returns; `null` leaves the read pending. */
  folderSort?: string | null
} = {}) {
  const slots = opts.slots ?? SLOTS
  const folders = opts.folders ?? FOLDERS
  mocks.chatFolders.mockResolvedValue(folders)
  const config = opts.folderSort === undefined ? { dashboard: {} }
    : opts.folderSort === null ? null
      : { dashboard: { folder_sort: opts.folderSort } }
  if (config) mocks.kirocrewConfig.mockResolvedValue(config)
  else mocks.kirocrewConfig.mockImplementation(() => new Promise(() => {}))

  let pane: HTMLElement | null = null
  if (opts.onDropSessionRef) {
    pane = document.createElement('div')
    const composer = document.createElement('div')
    composer.setAttribute('data-testid', 'input-wrapper')
    pane.appendChild(composer)
    document.body.appendChild(pane)
    panes.push(pane)
  }

  const defaults = createTestStore().getState()
  const store = createTestStore({
    dashboard: {
      ...defaults.dashboard,
      status: { platform: 'darwin' }, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: {
      ...defaults.chat,
      activeSlot: opts.activeSlot ?? null,
      slotStatusDetail: {}, subagents: {}, slotActivity: {},
      automations: {}, workflowRuns: {}, subagentQueued: {}, slotHistory: [],
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], folders)
  if (config) qc.setQueryData(['kirocrewConfig'], config)
  const tree = (rows: TestSlot[]) => (
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={rows as never} activeSlot={opts.activeSlot ?? null} unreadSlots={[]}
              history={[]} historyHasMore={false}
              defaultAgent="" installedAgents={[{ name: 'builder', source: 'builtin' }]}
              chatDropTarget={pane}
              onDropSessionRef={opts.onDropSessionRef}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>
  )
  const view = render(tree(slots))
  return { ...view, store, qc, rerenderSlots: (rows: TestSlot[]) => view.rerender(tree(rows)) }
}

/** Wait until the captured context is the sidebar's list context, then forget
 *  any tap the mount itself produced. */
async function ready() {
  await waitFor(() => expect(dnd.onDragEnd).toBeTruthy())
  expect(dnd.collision).toBe(sidebarCollision)
  hapticMock.mockClear()
}

const dragStart = (id: string, data: Record<string, unknown>) => act(() => {
  dnd.active = { id }
  dnd.onDragStart?.({ active: { id, data: { current: data } } })
})

const dragEnd = (
  active: { id: string; data: Record<string, unknown> },
  over: { id: string; data: Record<string, unknown> } | null,
) => act(() => {
  dnd.active = null
  dnd.onDragEnd?.({
    active: { id: active.id, data: { current: active.data } },
    over: over ? { id: over.id, data: { current: over.data } } : null,
  })
})

const taps = () => hapticMock.mock.calls.map(c => c[0])

beforeEach(() => {
  localStorage.clear()
  localStorage.setItem('mc-session-stale-collapse-ms', '0')
  dnd.onDragStart = undefined
  dnd.onDragEnd = undefined
  dnd.onDragCancel = undefined
  dnd.collision = undefined
  dnd.active = null
  hapticMock.mockReset()
  for (const m of Object.values(mocks)) m.mockReset()
  mocks.chatFolders.mockResolvedValue(FOLDERS)
  mocks.kirocrewConfig.mockResolvedValue({ dashboard: {} })
  mocks.updateChatFolder.mockResolvedValue({ ok: true })
  mocks.reorderChatFolders.mockResolvedValue({ ok: true })
  mocks.setSlotFolder.mockResolvedValue({ ok: true })
  mocks.sessions.mockResolvedValue({ sessions: [], has_more: false })
  mocks.sessionsSearch.mockResolvedValue({ sessions: [] })
})
afterEach(() => {
  vi.useRealTimers()
  vi.clearAllMocks()
  for (const p of panes) p.remove()
  panes = []
})

describe('haptic — drag start', () => {
  it('plays one medium tap when a row is picked up', async () => {
    renderSidebar()
    await ready()
    dragStart(`session:${LOOSE}`, { type: 'session', key: LOOSE })
    expect(taps()).toEqual(['medium'])
  })

  it('stays silent when a picked-up row is cancelled', async () => {
    renderSidebar()
    await ready()
    dragStart(`session:${LOOSE}`, { type: 'session', key: LOOSE })
    hapticMock.mockClear()
    expect(dnd.onDragCancel).toBeTruthy()
    act(() => {
      dnd.active = null
      dnd.onDragCancel?.({ active: { id: `session:${LOOSE}`, data: { current: { type: 'session', key: LOOSE } } }, over: null })
    })
    expect(taps()).toEqual([])
  })
})

describe('haptic — pinned reorder', () => {
  const pinnedRow = (key: string) =>
    document.querySelector<HTMLElement>(`[data-session-row="${key}"][data-session-scope="list"]`)!

  it('plays a light tap when Alt+Arrow moves a pinned row, and nothing at the end of the ring', async () => {
    renderSidebar()
    await ready()
    fireEvent.keyDown(pinnedRow(PIN_A), { key: 'ArrowDown', altKey: true })
    expect(taps()).toEqual(['light'])
    expect(JSON.parse(localStorage.getItem('mc-pinned-session-order')!)).toEqual([PIN_B, PIN_A])

    hapticMock.mockClear()
    // PIN_A is now last: there is no pinned row below it to trade places with.
    fireEvent.keyDown(pinnedRow(PIN_A), { key: 'ArrowDown', altKey: true })
    expect(taps()).toEqual([])
  })

  it('plays a light tap when a pinned row is dropped on another pinned row', async () => {
    renderSidebar()
    await ready()
    const active = { id: `session:${PIN_A}`, data: { type: 'session', key: PIN_A, pinned: true, container: 'root' } }
    dragEnd(active, { id: `pinned-session:list:${PIN_B}`, data: { type: 'pinned-session', key: PIN_B, container: 'root' } })
    expect(taps()).toEqual(['light'])
  })
})

describe('haptic — sibling folder reorder', () => {
  it('plays a light tap when the known Custom order renumbers two root folders', async () => {
    renderSidebar()
    await ready()
    dragEnd({ id: 'f1', data: { type: 'folder' } }, { id: 'f2', data: { type: 'folder' } })
    expect(taps()).toEqual(['light'])
    await waitFor(() => expect(mocks.reorderChatFolders).toHaveBeenCalledTimes(1))
  })

  it('stays silent when nothing renumbers: a folder onto itself, or a reorder across containers', async () => {
    renderSidebar()
    await ready()
    dragEnd({ id: 'f1', data: { type: 'folder' } }, { id: 'f1', data: { type: 'folder' } })
    // f3 lives under f2, so a sortable hit on root f1 is outside its ring.
    dragEnd({ id: 'f3', data: { type: 'folder', nested: true } }, { id: 'f1', data: { type: 'folder' } })
    expect(taps()).toEqual([])
    expect(mocks.reorderChatFolders).not.toHaveBeenCalled()
  })

  it('stays silent outside the Custom order, known or still being read', async () => {
    const byName = renderSidebar({ folderSort: 'name' })
    await ready()
    dragEnd({ id: 'f1', data: { type: 'folder' } }, { id: 'f2', data: { type: 'folder' } })
    expect(taps()).toEqual([])
    byName.unmount()

    renderSidebar({ folderSort: null })
    await ready()
    dragEnd({ id: 'f1', data: { type: 'folder' } }, { id: 'f2', data: { type: 'folder' } })
    expect(taps()).toEqual([])
    expect(mocks.reorderChatFolders).not.toHaveBeenCalled()
  })
})

describe('haptic — session dropped on a folder', () => {
  it('plays a light tap when the session lands somewhere new', async () => {
    renderSidebar()
    await ready()
    dragEnd({ id: `session:${LOOSE}`, data: { type: 'session', key: LOOSE } },
      { id: 'folder-drop:f1', data: { type: 'folder-drop', folderId: 'f1' } })
    expect(taps()).toEqual(['light'])

    hapticMock.mockClear()
    dragEnd({ id: `session:${IN_F1}`, data: { type: 'session', key: IN_F1 } },
      { id: 'root-lane', data: { type: 'folder-drop', folderId: null } })
    expect(taps()).toEqual(['light'])
  })

  it('stays silent when the session is dropped back on its own folder', async () => {
    renderSidebar()
    await ready()
    dragEnd({ id: `session:${IN_F1}`, data: { type: 'session', key: IN_F1 } },
      { id: 'folder-drop:f1', data: { type: 'folder-drop', folderId: 'f1' } })
    dragEnd({ id: `session:${IN_F1}`, data: { type: 'session', key: IN_F1 } },
      { id: 'f1', data: { type: 'folder' } })
    expect(taps()).toEqual([])
    // Let any mutation the drop might have queued run before asserting it never fired.
    await act(async () => {})
    expect(mocks.setSlotFolder).not.toHaveBeenCalled()
  })
})

describe('haptic — subfolder re-parented by drag', () => {
  it('plays a light tap when the subfolder moves under a new parent', async () => {
    renderSidebar()
    await ready()
    dragEnd({ id: 'f3', data: { type: 'folder', nested: true } },
      { id: 'folder-drop:f1', data: { type: 'folder-drop', folderId: 'f1' } })
    expect(taps()).toEqual(['light'])
    await waitFor(() => expect(mocks.updateChatFolder).toHaveBeenCalledWith('f3', { parent_id: 'f1' }))
  })

  it('stays silent for its current parent and for its own subtree', async () => {
    renderSidebar()
    await ready()
    dragEnd({ id: 'f3', data: { type: 'folder', nested: true } },
      { id: 'folder-drop:f2', data: { type: 'folder-drop', folderId: 'f2' } })
    // f2 dropped into its own child f3.
    dragEnd({ id: 'f2', data: { type: 'folder' } },
      { id: 'folder-drop:f3', data: { type: 'folder-drop', folderId: 'f3' } })
    expect(taps()).toEqual([])
    await act(async () => {})
    expect(mocks.updateChatFolder).not.toHaveBeenCalled()
  })
})

describe('haptic — session dropped on the chat pane', () => {
  const PANE = { id: 'chat-pane-ref', data: { type: 'chat-pane-ref' } }

  it('plays a light tap when the reference is staged', async () => {
    const onDropSessionRef = vi.fn()
    renderSidebar({ onDropSessionRef })
    await ready()
    dragEnd({ id: `session:${IN_F1}`, data: { type: 'session', key: IN_F1 } }, PANE)
    expect(taps()).toEqual(['light'])
    expect(onDropSessionRef).toHaveBeenCalledTimes(1)
  })

  it('stays silent when the reference is refused: a private session, or the one already open', async () => {
    const onDropSessionRef = vi.fn()
    const privateDrop = renderSidebar({ onDropSessionRef })
    await ready()
    dragEnd({ id: `session:${PRIVATE}`, data: { type: 'session', key: PRIVATE } }, PANE)
    expect(taps()).toEqual([])
    privateDrop.unmount()

    renderSidebar({ onDropSessionRef, activeSlot: LOOSE })
    await ready()
    dragEnd({ id: `session:${LOOSE}`, data: { type: 'session', key: LOOSE } }, PANE)
    expect(taps()).toEqual([])
    expect(onDropSessionRef).not.toHaveBeenCalled()
  })
})

describe('drag lifecycle order', () => {
  // Local midday, far from both midnight edges, so the date buckets the rows fall
  // in do not depend on when or where the suite runs.
  const PIN = new Date(2026, 0, 15, 12, 0, 0)
  const ago = (ms: number) => new Date(PIN.getTime() - ms).toISOString()
  const MIN = 60_000
  const slot = (key: string, title: string, lastTs: string): TestSlot =>
    ({ key, title, messages: 1, running: false, mode: '', created: '', last_ts: lastTs, pinned: false })
  const A = slot('chat-a', 'Alpha session', ago(60 * MIN))
  const B = slot('chat-b', 'Bravo session', ago(3 * 24 * 60 * MIN))
  const C = slot('chat-c', 'Charlie session', ago(20 * 24 * 60 * MIN))
  const C_BUMPED = { ...C, last_ts: ago(MIN) }
  const A_BUMPED = { ...A, last_ts: ago(MIN / 2) }

  const renderedKeys = () =>
    Array.from(document.querySelectorAll('[data-session-row]')).map(el => el.getAttribute('data-session-row'))

  it('releases the hover hold on drag start, holds the frozen order to the drop, and thaws on the drop', async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(PIN)
    const { rerenderSlots } = renderSidebar({ slots: [A, B, C], folders: [] })
    await ready()
    expect(renderedKeys()).toEqual(['chat-a', 'chat-b', 'chat-c'])

    // Hover B, then C becomes the newest: B holds its place under the pointer.
    fireEvent.pointerOver(document.querySelector('[data-session-row="chat-b"]')!, { pointerType: 'mouse', bubbles: true })
    rerenderSlots([A, B, C_BUMPED])
    expect(renderedKeys()).toEqual(['chat-c', 'chat-b', 'chat-a'])

    // Picking A up drops the hold first, so the order frozen for the drop math is
    // the TRUE order, not the one displaced by the hover.
    dragStart('session:chat-a', { type: 'session', key: 'chat-a' })
    expect(renderedKeys()).toEqual(['chat-c', 'chat-a', 'chat-b'])

    // Frozen: A becoming the newest mid-drag does not move anything.
    rerenderSlots([A_BUMPED, B, C_BUMPED])
    expect(renderedKeys()).toEqual(['chat-c', 'chat-a', 'chat-b'])

    dragEnd({ id: 'session:chat-a', data: { type: 'session', key: 'chat-a' } }, null)
    expect(renderedKeys()).toEqual(['chat-a', 'chat-c', 'chat-b'])
  })
})
