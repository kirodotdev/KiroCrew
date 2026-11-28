/**
 * Chat sidebar — a session row dropped on a Split View pane (#36883).
 *
 * With `onShowSessionInPane` set, the drag's targets in `chatDropTarget` are the
 * panes SessionGridLayout marks `data-split-leaf`: one droppable per pane, portaled into
 * it. A drop shows the session in that pane; a session already in a pane is
 * refused on the hovered pane (the self string on its own pane, "already open"
 * on another), and the refused drop is swallowed, so it never files the session
 * into a folder.
 *
 * A pointer drag cannot be simulated in jsdom, so, as in
 * ChatSidebar.hapticTaps.test.tsx, the DndContext is stubbed to capture the real
 * lifecycle props, and `useDroppable` is stubbed to record each droppable's data
 * and report the one the test hovers.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, waitFor } from '@testing-library/react'
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
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

const hapticMock = vi.hoisted(() => vi.fn())
vi.mock('../lib/haptic', () => ({ haptic: hapticMock }))

const dnd = vi.hoisted(() => ({
  onDragStart: undefined as ((e: unknown) => void) | undefined,
  onDragEnd: undefined as ((e: unknown) => void) | undefined,
  active: null as { id: string } | null,
  overId: null as string | null,
  droppables: new Map<string, Record<string, unknown>>(),
}))
vi.mock('@dnd-kit/core', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@dnd-kit/core')>()
  return {
    ...actual,
    DndContext: (props: { children?: unknown; onDragStart?: (e: unknown) => void; onDragEnd?: (e: unknown) => void }) => {
      dnd.onDragStart = props.onDragStart
      dnd.onDragEnd = props.onDragEnd
      return props.children as never
    },
    useDndContext: () => ({ ...actual.useDndContext(), active: dnd.active }),
    useDroppable: ({ id, data }: { id: string; data?: Record<string, unknown> }) => {
      if (data) dnd.droppables.set(id, data)
      return { setNodeRef: () => {}, isOver: dnd.overId === id, active: null, over: null, node: { current: null }, rect: { current: null } }
    },
  }
})

const mocks = vi.hoisted(() => ({
  chatFolders: vi.fn(),
  kirocrewConfig: vi.fn(),
  setSlotFolder: vi.fn(),
  sessions: vi.fn(),
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

import ChatSidebar from '../pages/ChatSidebar'

const FOLDERS: ChatFolder[] = [{ id: 'f1', name: 'Alpha', order: 0 }]
const SHOWN_LEFT = 'chat-left'
const SHOWN_RIGHT = 'chat-right'
const LOOSE = 'chat-loose'
const SLOTS = [
  { key: SHOWN_LEFT, title: 'Left work', running: false, messages: 1, last_ts: '2026-03-03T00:00:00Z' },
  { key: SHOWN_RIGHT, title: 'Right work', running: false, messages: 1, last_ts: '2026-03-02T00:00:00Z' },
  { key: LOOSE, title: 'Loose work', running: false, messages: 2, last_ts: '2026-03-01T00:00:00Z' },
]

let host: HTMLElement | null = null

/** The sidebar with a Split View host of two panes: L1 shows SHOWN_LEFT, L2 SHOWN_RIGHT. */
async function renderSplit() {
  host = document.createElement('div')
  for (const [leaf, slot] of [['L1', SHOWN_LEFT], ['L2', SHOWN_RIGHT]]) {
    const el = document.createElement('div')
    el.dataset.splitLeaf = leaf
    el.dataset.splitSlot = slot
    host.appendChild(el)
  }
  document.body.appendChild(host)
  const onShow = vi.fn()
  const defaults = createTestStore().getState()
  const store = createTestStore({
    dashboard: {
      ...defaults.dashboard,
      status: { platform: 'darwin' }, connected: true, slots: SLOTS, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null, slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: {
      ...defaults.chat,
      activeSlot: null,
      slotStatusDetail: {}, subagents: {}, slotActivity: {},
      automations: {}, workflowRuns: {}, subagentQueued: {}, slotHistory: [],
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], FOLDERS)
  qc.setQueryData(['kirocrewConfig'], { dashboard: {} })
  render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={SLOTS as never} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false}
              defaultAgent="" installedAgents={[{ name: 'builder', source: 'builtin' }]}
              chatDropTarget={host}
              onDropSessionRef={vi.fn()}
              onShowSessionInPane={onShow}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  await waitFor(() => expect(dnd.onDragEnd).toBeTruthy())
  hapticMock.mockClear()
  return onShow
}

/** Pick `key` up while hovering pane `leaf`, then release over it. Returns what
 *  the hovered pane showed mid-drag. */
async function dropOnPane(key: string, leaf: string) {
  dnd.overId = `split-pane:${leaf}`
  const active = { id: `session:${key}`, data: { current: { type: 'session', key } } }
  await act(async () => {
    dnd.active = { id: active.id }
    dnd.onDragStart?.({ active })
  })
  const pane = host!.querySelector(`[data-split-leaf="${leaf}"]`)!
  const shown = {
    accepted: !!pane.querySelector('[data-testid="split-pane-drop-accept"]'),
    text: pane.textContent,
  }
  hapticMock.mockClear()
  const data = dnd.droppables.get(dnd.overId)
  expect(data).toBeTruthy()
  await act(async () => {
    dnd.active = null
    dnd.onDragEnd?.({ active, over: { id: dnd.overId, data: { current: data } } })
  })
  return shown
}

beforeEach(() => {
  localStorage.clear()
  localStorage.setItem('mc-session-stale-collapse-ms', '0')
  dnd.onDragStart = undefined
  dnd.onDragEnd = undefined
  dnd.active = null
  dnd.overId = null
  dnd.droppables.clear()
  hapticMock.mockReset()
  for (const m of Object.values(mocks)) m.mockReset()
  mocks.chatFolders.mockResolvedValue(FOLDERS)
  mocks.kirocrewConfig.mockResolvedValue({ dashboard: {} })
  mocks.setSlotFolder.mockResolvedValue({ ok: true })
  mocks.sessions.mockResolvedValue({ sessions: [], has_more: false })
})
afterEach(() => {
  vi.clearAllMocks()
  host?.remove()
  host = null
})

describe('a session dropped on a Split View pane', () => {
  it('highlights the hovered pane and shows the session there', async () => {
    const onShow = await renderSplit()
    const shown = await dropOnPane(LOOSE, 'L2')
    expect(shown.accepted).toBe(true)
    expect(onShow).toHaveBeenCalledWith('L2', LOOSE)
    expect(hapticMock.mock.calls).toEqual([['light']])
    expect(mocks.setSlotFolder).not.toHaveBeenCalled()
  })

  it('refuses a session already in a pane, on its own pane and on another, and files it nowhere', async () => {
    const onShow = await renderSplit()
    const own = await dropOnPane(SHOWN_LEFT, 'L1')
    expect(own.accepted).toBe(false)
    expect(own.text).toContain("Can't drop a session into itself")
    const other = await dropOnPane(SHOWN_LEFT, 'L2')
    expect(other.accepted).toBe(false)
    expect(other.text).toContain('Already open in another pane')
    expect(onShow).not.toHaveBeenCalled()
    expect(hapticMock).not.toHaveBeenCalled()
    expect(mocks.setSlotFolder).not.toHaveBeenCalled()
  })
})
