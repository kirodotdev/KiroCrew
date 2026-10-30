/**
 * A fork of a pinned session must leave ONE pinned-section divider per lane.
 *
 * A fork of a pinned session is itself pinned, and it lands at the end of the
 * pinned run, which is where the pointer usually rests: on the first unpinned
 * row, just under the source. The hover hold kept that unpinned row at the
 * pixel offset it was found at, which seated it ABOVE the newly pinned fork.
 * The lane then read pinned, unpinned, pinned, unpinned, and the sidebar drew
 * the divider twice.
 *
 * Locks the contract:
 *  (1) While the pointer rests on the first unpinned row, the fork joins the
 *      pinned run and the lane draws one divider (root lane and folder lane).
 *  (2) After the pointer leaves, the folder's rows take their sorted places and
 *      the folder draws one divider after the pinned run. jsdom does not keep
 *      the orphaned divider the browser kept, so this case passes on main too;
 *      (4) is the one that covers the orphan.
 *  (3) The hold still works inside a section: an unpinned held row keeps its
 *      seat among unpinned rows that re-sort around it.
 *  (4) A folder body that draws two dividers in one frame draws one in the
 *      next. Each divider is keyed by the row it precedes, so React can match
 *      and remove the one that goes away.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { ChatFolder, ChatSlot } from '../types'

// Render framer-motion elements as plain DOM (jsdom can't run projection).
vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'layoutScroll', 'initial', 'animate', 'exit',
    'transition', 'variants', 'whileHover', 'whileTap', 'whileInView',
    'drag', 'dragConstraints', 'dragElastic', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef<HTMLElement, Record<string, unknown>>((props, ref) => {
      const clean: Record<string, unknown> = {}
      for (const k of Object.keys(props)) {
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

// The sidebar's DndContext, stubbed so a test can run the real drag lifecycle
// handlers without a gesture, as ChatSidebar.dragFreezeOrder.test.tsx does.
// `active` stands in for dnd-kit's own store, which the sidebar reconciles
// its drag state against.
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
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

const mocks = vi.hoisted(() => ({ folders: [] as unknown[] }))

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, p: string) => {
      if (p === 'chatFolders') return vi.fn().mockImplementation(() => Promise.resolve(mocks.folders))
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

const PIN = new Date(2026, 0, 15, 12, 0, 0)
const MIN = 60_000
const ago = (ms: number) => new Date(PIN.getTime() - ms).toISOString()

/** A row in folder `fid` ('' for the root lane). Pinned rows carry a stored
 *  rank, as on a gateway where the person has reordered pins; the fork has none
 *  and so follows them, at the end of the pinned run. */
const row = (key: string, fid: string, lastMs: number, pin?: { rank?: number }): ChatSlot => ({
  key, title: key, messages: 1, running: false, mode: '', created: '', last_ts: ago(lastMs),
  pinned: !!pin, ...(pin?.rank != null ? { pin_rank: pin.rank } : {}),
  ...(fid ? { folder_id: fid } : {}),
} as unknown as ChatSlot)

async function renderSidebar(slots: ChatSlot[], folders: ChatFolder[] = []) {
  mocks.folders = folders
  const store = createTestStore({
    dashboard: {
      status: { platform: 'darwin' }, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null, slotStatusDetail: {}, subagents: {}, slotActivity: {} } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], folders)
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
  const { rerender } = render(tree(slots))
  // ThemeProvider and the folder query settle through promise callbacks after
  // mount; drain them before the first assertion, and after each rerender.
  await settle()
  return {
    rerender: async (s: ChatSlot[]) => {
      rerender(tree(s))
      await settle()
    },
  }
}

// Drains pending promise callbacks and the React updates they schedule, with
// no wall-clock wait. Every mock here settles through microtasks.
async function settle(): Promise<void> {
  await act(async () => {
    for (let i = 0; i < 10; i += 1) await Promise.resolve()
  })
}

/** Session rows and pinned dividers in document order: '|' is a divider. */
const shape = () =>
  Array.from(document.querySelectorAll('[data-session-row], [data-pinned-divider]'))
    .map(el => (el.hasAttribute('data-pinned-divider') ? '|' : el.getAttribute('data-session-row')))

const hover = (key: string) => {
  const el = document.querySelector(`[data-session-row="${key}"]`)
  if (!el) throw new Error(`no rendered row for ${key}`)
  fireEvent.pointerOver(el, { pointerType: 'mouse', bubbles: true })
}

const leaveSidebar = () => {
  const root = document.querySelector('.sidebar-inner')
  if (!root) throw new Error('no sidebar root')
  fireEvent.pointerLeave(root, { pointerType: 'mouse' })
}

beforeEach(() => {
  localStorage.clear()
  localStorage.setItem('mc-session-stale-collapse-ms', '0')
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(PIN)
  dnd.handlers = {}
  dnd.active = null
})
afterEach(() => {
  vi.clearAllMocks()
  vi.useRealTimers()
})

describe('ChatSidebar – a fork of a pinned session keeps one pinned divider', () => {
  const FOLDERS: ChatFolder[] = [{ id: 'f1', name: 'Alpha', order: 0 } as ChatFolder]

  const world = (fid: string) => [
    row('p0', fid, 10 * MIN, { rank: 0 }),
    row('s0', fid, 20 * MIN, { rank: 1 }),
    row('u1', fid, 30 * MIN),
    row('u0', fid, 40 * MIN),
  ]
  const fork = (fid: string) => row('fork', fid, MIN, {})

  it('root lane: the fork joins the pinned run while the pointer rests below it', async () => {
    const { rerender } = await renderSidebar(world(''), FOLDERS)
    expect(shape()).toEqual(['p0', 's0', '|', 'u1', 'u0'])

    hover('u1')
    await rerender([...world(''), fork('')])

    expect(shape()).toEqual(['p0', 's0', 'fork', '|', 'u1', 'u0'])
  })

  it('folder lane: the fork joins the pinned run while the pointer rests below it', async () => {
    const { rerender } = await renderSidebar(world('f1'), FOLDERS)
    expect(shape()).toEqual(['p0', 's0', '|', 'u1', 'u0'])

    hover('u1')
    await rerender([...world('f1'), fork('f1')])
    expect(shape()).toEqual(['p0', 's0', 'fork', '|', 'u1', 'u0'])
  })

  it('folder lane: one divider after the pinned run once the pointer leaves', async () => {
    const { rerender } = await renderSidebar(world('f1'), FOLDERS)
    hover('u1')
    await rerender([...world('f1'), fork('f1')])
    leaveSidebar()
    expect(shape()).toEqual(['p0', 's0', 'fork', '|', 'u1', 'u0'])
  })

  it('still holds an unpinned row among unpinned rows that re-sort around it', async () => {
    const { rerender } = await renderSidebar(world('f1'), FOLDERS)
    hover('u1')
    // u0 becomes the newest unpinned row, so the sort wants it above u1.
    await rerender([...world('f1').filter(s => s.key !== 'u0'), row('u0', 'f1', MIN)])
    expect(shape()).toEqual(['p0', 's0', '|', 'u1', 'u0'])
    leaveSidebar()
    expect(shape()).toEqual(['p0', 's0', '|', 'u0', 'u1'])
  })

  // A drag freezes the lane order while pins stay live, so pinning u0 from
  // elsewhere mid-drag leaves the frozen folder lane reading p0 s0 | u1 u0 | u2:
  // two pinned-to-unpinned boundaries in one frame. The drop re-sorts to one.
  //
  // With the shared `pinned-divider-${folder.id}` key, the frozen frame gives
  // React two children with one key, and React warns that such children "may
  // be duplicated and/or omitted". In the browser that left the first divider
  // in the DOM between s0 and the next row until a reload. jsdom draws the
  // two-divider frame and logs the warning, but it removes the extra divider
  // on the drop, so the divider count alone passes on main. The duplicate-key
  // assertion is the part that fails on main.
  it('folder lane: a frame with two dividers heals to one on the next render', async () => {
    const consoleError = vi.spyOn(console, 'error')
    const lane = (pinU0: boolean) => [
      row('p0', 'f1', 10 * MIN, { rank: 0 }),
      row('s0', 'f1', 20 * MIN, { rank: 1 }),
      row('u1', 'f1', 30 * MIN),
      row('u0', 'f1', 40 * MIN, pinU0 ? { rank: 2 } : undefined),
      row('u2', 'f1', 50 * MIN),
    ]
    const { rerender } = await renderSidebar(lane(false), FOLDERS)
    expect(shape()).toEqual(['p0', 's0', '|', 'u1', 'u0', 'u2'])
    // Guard: a missed capture would make the drag steps silent no-ops.
    expect(typeof dnd.handlers.onDragStart).toBe('function')

    const active = { id: 'p0', data: { current: { type: 'session', key: 'p0' } } }
    dnd.active = { id: 'p0' }
    act(() => { dnd.handlers.onDragStart!({ active }) })
    await rerender(lane(true))
    expect(shape()).toEqual(['p0', 's0', '|', 'u1', 'u0', '|', 'u2'])

    dnd.active = null
    act(() => { dnd.handlers.onDragEnd!({ active, over: null }) })
    await settle()
    expect(shape()).toEqual(['p0', 's0', 'u0', '|', 'u1', 'u2'])
    expect(document.querySelectorAll('[data-pinned-divider]')).toHaveLength(1)
    const duplicateKeys = consoleError.mock.calls
      .map(args => args.map(String).join(' '))
      .filter(msg => msg.includes('two children with the same key'))
    consoleError.mockRestore()
    expect(duplicateKeys).toEqual([])
  })
})
