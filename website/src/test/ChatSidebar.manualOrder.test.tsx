/**
 * The manual session order (sort "Manual"), end to end through the sidebar.
 *
 * A drop between two unpinned rows, in any sort, switches the sort to Manual,
 * writes the order on the gateway and offers "Sorted by hand" with Undo. The
 * first drag seeds the order on screen; later drags move one row; Undo puts
 * back the sort and the row's old place without undoing another browser.
 *
 * The drag lifecycle is driven through the real handlers with the DndContext
 * stubbed (see ChatSidebar.dragFreezeOrder.test.tsx), and the gateway is a
 * small fake below that applies seed / move / clear the way the route does
 * (the route's own rules are pinned in test/test_manual_session_order.py).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, fireEvent, waitFor, within } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

const dnd = vi.hoisted(() => ({
  handlers: {} as Record<string, ((e: unknown) => void) | undefined>,
  overId: null as string | null,
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
    useDroppable: (args: Parameters<typeof actual.useDroppable>[0]) => ({
      ...actual.useDroppable(args),
      isOver: dnd.overId === String(args.id),
    }),
  }
})

/** The fake gateway: the config file, the slot rows and the manual order. */
const gw = vi.hoisted(() => ({
  config: { dashboard: {} as Record<string, unknown> },
  slots: [] as unknown[],
  manual: { order: [] as string[], stored: false, rev: 100 },
  search: [] as { key: string; title: string }[],
}))

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return {
    ...actual,
    api: {
      ...Object.fromEntries(
        [
          'sessions', 'chatSlotDetail', 'createChatSlot', 'deleteChatSlot',
          'resumeChatSlot', 'deleteSession', 'agentDetail', 'spawnList', 'fetchHistory',
          'renameSlot', 'forkSession',
        ].map(k => [k, vi.fn().mockResolvedValue({})]),
      ),
      // A write that fails re-reads the rows.
      chatSlots: vi.fn(async () => structuredClone(gw.slots)),
      chatFolders: vi.fn().mockResolvedValue([{ id: 'f1', name: 'Work', order: 0 }]),
      kirocrewConfig: vi.fn(async () => structuredClone(gw.config)),
      patchConfig: vi.fn(async (path: string, value: unknown) => {
        if (path === 'dashboard.session_sort') gw.config.dashboard.session_sort = value
        return { ok: true }
      }),
      sessionsSearch: vi.fn(async () => ({ sessions: gw.search })),
      setPinnedOrder: vi.fn(async (keys: string[]) => ({ ok: true, order: keys, rev: 1 })),
      setSlotFolder: vi.fn().mockResolvedValue({ ok: true }),
      setManualOrder: vi.fn(async (body: ManualBody) => fakeManualWrite(body)),
    },
  }
})

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
import { api, ApiError } from '../api/client'
import type { ChatSlot } from '../types'
import type { RootState } from '../store'

type ManualBody =
  | { op: 'seed'; keys: string[] }
  | { op: 'move'; key: string; after?: string | null; before?: string; seat?: string[] }
  | { op: 'clear'; rev: number }

/** Seed / move / clear, as `POST /api/chat/manual-order` applies them. */
function fakeManualWrite(body: ManualBody) {
  const m = gw.manual
  const refuse = (code: string) => {
    throw new ApiError(409, code, JSON.stringify({ code, order: [...m.order], rev: m.rev }))
  }
  if (body.op === 'seed') {
    if (m.stored) refuse('manual_order_exists')
    m.order = [...body.keys]
    m.stored = true
  } else if (body.op === 'clear') {
    if (body.rev !== m.rev) refuse('manual_order_changed')
    m.order = []
    m.stored = false
  } else {
    const placed = new Set(m.order)
    const order = [...(body.seat ?? []).filter(k => !placed.has(k) && k !== body.key), ...m.order.filter(k => k !== body.key)]
    const anchor = 'before' in body && body.before !== undefined ? body.before : body.after ?? null
    if (anchor !== null && !order.includes(anchor)) refuse('anchor_gone')
    const at = anchor === null ? 0 : order.indexOf(anchor) + ('before' in body && body.before !== undefined ? 0 : 1)
    order.splice(at, 0, body.key)
    m.order = order
    m.stored = true
  }
  m.rev += 1
  return { ok: true, order: [...m.order], rev: m.rev }
}

/** Another browser's move, straight on the fake gateway. */
function moveElsewhere(key: string, after: string | null) {
  fakeManualWrite({ op: 'move', key, after })
}

const slot = (key: string, ts: string, extra: Partial<ChatSlot> = {}): ChatSlot => ({
  key, title: `${key} title`, messages: 1, running: false, mode: '', created: ts, last_ts: ts, ...extra,
} as ChatSlot)

// Last activity, newest first: a, b, c.
const ABC = [
  slot('a', '2026-03-01T00:00:00Z'),
  slot('b', '2026-02-01T00:00:00Z'),
  slot('c', '2026-01-01T00:00:00Z'),
]

function renderSidebar(slots: ChatSlot[], sort?: string) {
  if (sort) gw.config.dashboard.session_sort = sort
  gw.slots = slots
  const store = createTestStore({
    dashboard: {
      status: { platform: 'darwin' }, connected: true, slots,
      approvalMode: 'normal', channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
      slotsLoaded: true,
    } as unknown as RootState['dashboard'],
    chat: {
      activeSlot: null, messages: [], slotStatusDetail: {}, history: [], historyHasMore: false,
      subagents: {}, toolLog: [], slotActivity: {}, slotHistory: [], slotMessages: {},
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['kirocrewConfig'], structuredClone(gw.config))
  const tree = (s: ChatSlot[]) => (
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar slots={s} activeSlot={null} unreadSlots={[]} history={[]} historyHasMore={false}
              defaultAgent="default" installedAgents={[]} />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>
  )
  const view = render(tree(slots))
  return {
    store, qc,
    rerender: (s: ChatSlot[]) => view.rerender(tree(s)),
    // The app feeds the sidebar from the store, where answers paint the ranks.
    rerenderFromStore: () => view.rerender(tree(store.getState().dashboard.slots)),
  }
}

/** Rendered order of the given keys' rows, top to bottom. */
function order(keys: string[]): string[] {
  return keys
    .map(k => ({ k, el: screen.getByText(`${k} title`) }))
    .sort((x, y) => (x.el.compareDocumentPosition(y.el) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1))
    .map(n => n.k)
}

function drop(key: string, over: { id: string; data: Record<string, unknown> } | null, container = 'root', pinned = false) {
  const active = { id: `session:${key}`, data: { current: { type: 'session', key, pinned, container } } }
  act(() => {
    dnd.active = { id: active.id }
    dnd.handlers.onDragStart!({ active })
    dnd.active = null
    dnd.handlers.onDragEnd!({ active, over: over && { id: over.id, data: { current: over.data } } })
  })
}
const onRow = (key: string, container = 'root') =>
  ({ id: `manual-session:list:${key}`, data: { type: 'manual-session', key, container } })

const manualWrites = () => vi.mocked(api.setManualOrder).mock.calls.map(([body]) => body)
const sortWrites = () => vi.mocked(api.patchConfig).mock.calls
  .filter(([path]) => path === 'dashboard.session_sort').map(([, value]) => value)

beforeEach(() => {
  localStorage.clear()
  localStorage.setItem('mc-session-stale-collapse-ms', '0')
  gw.config = { dashboard: {} }
  gw.manual = { order: [], stored: false, rev: 100 }
  gw.search = []
  vi.mocked(api.setManualOrder).mockClear()
  vi.mocked(api.patchConfig).mockClear()
  vi.mocked(api.setPinnedOrder).mockClear()
  vi.mocked(api.setSlotFolder).mockClear()
  dnd.handlers = {}
  dnd.overId = null
  dnd.active = null
})

describe('a drop between two rows, in any sort', () => {
  it('seeds the order on screen, switches to Manual, and Undo puts both back', async () => {
    const { rerenderFromStore } = renderSidebar(ABC)
    expect(order(['a', 'b', 'c'])).toEqual(['a', 'b', 'c'])

    // c dropped onto a: it moved up, so it lands above a.
    drop('c', onRow('a'))
    await waitFor(() => expect(manualWrites()).toEqual([{ op: 'seed', keys: ['c', 'a', 'b'] }]))
    await waitFor(() => expect(sortWrites()).toEqual(['manual']))
    const bar = await screen.findByTestId('manual-sort-undo')
    expect(within(bar).getByText('Sorted by hand')).toBeTruthy()
    rerenderFromStore()
    expect(order(['a', 'b', 'c'])).toEqual(['c', 'a', 'b'])

    // The very first drag leaves no saved order behind once undone.
    fireEvent.click(within(bar).getByTestId('manual-sort-undo-button'))
    await waitFor(() => expect(manualWrites().at(-1)).toEqual({ op: 'clear', rev: 101 }))
    await waitFor(() => expect(sortWrites()).toEqual(['manual', 'date-desc']))
    expect(gw.manual).toMatchObject({ order: [], stored: false })
    rerenderFromStore()
    expect(order(['a', 'b', 'c'])).toEqual(['a', 'b', 'c'])
  })

  it('under Manual moves only the dragged row, seating the new sessions drawn beside it', async () => {
    gw.manual = { order: ['a', 'b'], stored: true, rev: 100 }
    // c is new since the order was saved, so it shows on top.
    const slots = [
      slot('a', '2026-03-01T00:00:00Z', { manual_rank: 0, manual_rev: 100 }),
      slot('b', '2026-02-01T00:00:00Z', { manual_rank: 1, manual_rev: 100 }),
      slot('c', '2026-01-01T00:00:00Z', { manual_rank: null, manual_rev: 100 }),
    ]
    const { rerenderFromStore } = renderSidebar(slots, 'manual')
    expect(order(['a', 'b', 'c'])).toEqual(['c', 'a', 'b'])

    drop('b', onRow('c'))
    await waitFor(() => expect(manualWrites()).toEqual([
      { op: 'move', key: 'b', before: 'c', seat: ['c'], expected_created: '2026-02-01T00:00:00Z' },
    ]))
    expect(sortWrites()).toEqual([])
    rerenderFromStore()
    expect(order(['a', 'b', 'c'])).toEqual(['b', 'c', 'a'])

    // Undo puts b back after the row it followed.
    fireEvent.click(await screen.findByTestId('manual-sort-undo-button'))
    await waitFor(() => expect(manualWrites().at(-1)).toMatchObject({ op: 'move', key: 'b', after: 'a' }))
    rerenderFromStore()
    expect(order(['a', 'b', 'c'])).toEqual(['c', 'a', 'b'])
  })

  it('Undo after another browser moved a row puts back only the dragged one', async () => {
    const { rerenderFromStore } = renderSidebar(ABC)
    drop('c', onRow('a'))
    await screen.findByTestId('manual-sort-undo')
    // Elsewhere: a goes to the bottom, after b.
    moveElsewhere('a', 'b')
    fireEvent.click(screen.getByTestId('manual-sort-undo-button'))
    // The clear is refused (the order changed), so only c moves back: after b,
    // the row it followed on screen when it was dropped.
    await waitFor(() => expect(manualWrites().at(-1)).toMatchObject({ op: 'move', key: 'c', after: 'b' }))
    expect(gw.manual.order).toEqual(['b', 'c', 'a'])
    expect(gw.manual.stored).toBe(true)
    rerenderFromStore()
    await waitFor(() => expect(sortWrites()).toEqual(['manual', 'date-desc']))
  })

  it.each([
    { target: 'b', expected: ['a', 'b', 'c'] },
    { target: 'a', expected: ['a', 'c', 'b'] },
  ])('finishes a seed-conflict move before a later drag onto $target', async ({ target, expected }) => {
    // Another browser saved these ranks, but this browser still draws ABC.
    gw.manual = { order: ['a', 'b', 'c'], stored: true, rev: 100 }
    let releaseSeed!: () => void
    const seedHeld = new Promise<void>(resolve => { releaseSeed = resolve })
    vi.mocked(api.setManualOrder).mockImplementationOnce(body => seedHeld.then(() => fakeManualWrite(body)))
    const { store, rerenderFromStore } = renderSidebar(ABC)
    drop('c', onRow('a'))
    await waitFor(() => expect(manualWrites()).toEqual([{ op: 'seed', keys: ['c', 'a', 'b'] }]))
    await waitFor(() => expect(order(['a', 'b', 'c'])).toEqual(['c', 'a', 'b']))

    drop('c', onRow(target))
    expect(store.getState().dashboard.pendingManualOrder).toEqual(expected)
    expect(manualWrites()).toHaveLength(1)
    await act(async () => { releaseSeed(); await seedHeld })
    await waitFor(() => expect(store.getState().dashboard.pendingManualOrder).toBeNull())

    // The fake removes c then inserts it before a: [c,a,b]. The later drag
    // removes c again and inserts it after target: [a,b,c] or [a,c,b].
    expect(gw.manual.order).toEqual(expected)
    expect(manualWrites()).toEqual([
      { op: 'seed', keys: ['c', 'a', 'b'] },
      { op: 'move', key: 'c', before: 'a', seat: [], expected_created: '2026-01-01T00:00:00Z' },
      { op: 'move', key: 'c', after: target, seat: [], expected_created: '2026-01-01T00:00:00Z' },
    ])
    rerenderFromStore()
    expect(order(['a', 'b', 'c'])).toEqual(expected)
  })

  it('finishes an Undo conflict move before a later drag seeds again', async () => {
    const { store, rerenderFromStore } = renderSidebar(ABC)
    drop('c', onRow('a'))
    await screen.findByTestId('manual-sort-undo')
    rerenderFromStore()
    moveElsewhere('a', 'b')
    let releaseClear!: () => void
    const clearHeld = new Promise<void>(resolve => { releaseClear = resolve })
    vi.mocked(api.setManualOrder).mockImplementationOnce(body => clearHeld.then(() => fakeManualWrite(body)))
    fireEvent.click(screen.getByTestId('manual-sort-undo-button'))
    await waitFor(() => expect(manualWrites().at(-1)).toEqual({ op: 'clear', rev: 101 }))
    await waitFor(() => expect(sortWrites()).toEqual(['manual', 'date-desc']))
    expect(order(['a', 'b', 'c'])).toEqual(['a', 'b', 'c'])

    drop('c', onRow('b'))
    expect(store.getState().dashboard.pendingManualOrder).toEqual(['a', 'c', 'b'])
    expect(manualWrites()).toHaveLength(2)
    await act(async () => { releaseClear(); await clearHeld })
    await waitFor(() => expect(store.getState().dashboard.pendingManualOrder).toBeNull())

    // Elsewhere left [c,b,a]. Undo inserts c after b: [b,c,a]. The later
    // drag's seed is refused, then its move inserts c before b: [c,b,a].
    expect(gw.manual.order).toEqual(['c', 'b', 'a'])
    expect(manualWrites()).toEqual([
      { op: 'seed', keys: ['c', 'a', 'b'] },
      { op: 'clear', rev: 101 },
      { op: 'move', key: 'c', after: 'b', seat: [], expected_created: '2026-01-01T00:00:00Z' },
      { op: 'seed', keys: ['a', 'c', 'b'] },
      { op: 'move', key: 'c', before: 'b', seat: [], expected_created: '2026-01-01T00:00:00Z' },
    ])
    rerenderFromStore()
    expect(order(['a', 'b', 'c'])).toEqual(['c', 'b', 'a'])
  })

  it('a first drag that loses the seed race moves the row in the order another browser saved', async () => {
    // Another browser seeded a moment ago; this one has not seen the ranks yet.
    gw.manual = { order: ['b', 'a', 'c'], stored: true, rev: 100 }
    const { rerenderFromStore } = renderSidebar(ABC)
    drop('c', onRow('a'))
    await waitFor(() => expect(manualWrites()).toEqual([
      { op: 'seed', keys: ['c', 'a', 'b'] },
      { op: 'move', key: 'c', before: 'a', seat: [], expected_created: '2026-01-01T00:00:00Z' },
    ]))
    expect(gw.manual.order).toEqual(['b', 'c', 'a'])
    await screen.findByTestId('manual-sort-undo')
    rerenderFromStore()
    expect(order(['a', 'b', 'c'])).toEqual(['b', 'c', 'a'])
  })
})

/** The gateway's answer when its order file cannot be written. */
const writeFailed = () => new ApiError(500, 'manual_order_write_failed',
  JSON.stringify({ code: 'manual_order_write_failed' }))
const RANKED_ABC = [
  slot('a', '2026-03-01T00:00:00Z', { manual_rank: 0, manual_rev: 100 }),
  slot('b', '2026-02-01T00:00:00Z', { manual_rank: 1, manual_rev: 100 }),
  slot('c', '2026-01-01T00:00:00Z', { manual_rank: 2, manual_rev: 100 }),
]
/** Picks a sort from the menu. Sort saves go out one at a time in pick
 *  order, so once this one is on the wire no earlier save is still to come. */
async function pickSortByLabel(label: string) {
  fireEvent.keyDown(screen.getByLabelText('Sort and filter sessions'), { key: 'Enter' })
  fireEvent.click(await screen.findByText(label))
}
const pickNameDesc = () => pickSortByLabel('Z → A')
/** The notice's text, naming the sort the list shows after the failure. */
const failedBackIn = (sort: string) => `The move wasn't saved, so the list is back in ${sort} order. Try the move again.`
const FAILED_SAVED = "The move wasn't saved, so your saved manual order is back. Try the move again."
const conflictBackIn = (sort: string) =>
  `Your sessions changed while you were moving one, so the list is back in ${sort} order. Try the move again.`
const CONFLICT_SAVED = 'Your sessions changed while you were moving one, so your saved manual order is back. Try the move again.'
const UNSAVED = 'Nothing is saved in Manual yet, so new sessions show first. Drag a session to try again.'
const pickManual = () => pickSortByLabel('Manual')

describe('a drop whose write does not take', () => {
  it('in Newest with nothing saved: the seed fails, the notice shows and the sort goes back', async () => {
    vi.mocked(api.setManualOrder).mockImplementationOnce(async () => { throw writeFailed() })
    renderSidebar(ABC)
    drop('c', onRow('a'))
    expect(await screen.findByText(failedBackIn('Newest'))).toBeTruthy()
    await waitFor(() => expect(sortWrites()).toEqual(['manual', 'date-desc']))
    expect(manualWrites()).toEqual([{ op: 'seed', keys: ['c', 'a', 'b'] }])
    expect(screen.queryByTestId('manual-sort-undo')).toBeNull()
  })

  it('in Newest with an order saved: the move fails and the sort goes back', async () => {
    gw.manual = { order: ['a', 'b', 'c'], stored: true, rev: 100 }
    vi.mocked(api.setManualOrder).mockImplementationOnce(async () => { throw writeFailed() })
    renderSidebar(RANKED_ABC, 'date-desc')
    drop('c', onRow('a'))
    expect(await screen.findByText(failedBackIn('Newest'))).toBeTruthy()
    await waitFor(() => expect(sortWrites()).toEqual(['manual', 'date-desc']))
    expect(manualWrites()).toMatchObject([{ op: 'move', key: 'c', before: 'a' }])
    expect(screen.queryByTestId('manual-sort-undo')).toBeNull()
  })

  it('in Newest with an order saved: a move refused as session_gone says so and the sort goes back', async () => {
    gw.manual = { order: ['a', 'b', 'c'], stored: true, rev: 100 }
    // The route's answer when the moved session closed: no order in the body.
    vi.mocked(api.setManualOrder).mockImplementationOnce(async () => {
      throw new ApiError(409, 'session_gone',
        JSON.stringify({ error: 'the moved session is gone or was replaced', code: 'session_gone' }))
    })
    renderSidebar(RANKED_ABC, 'date-desc')
    drop('c', onRow('a'))
    expect(await screen.findByText(conflictBackIn('Newest'))).toBeTruthy()
    await waitFor(() => expect(sortWrites()).toEqual(['manual', 'date-desc']))
    expect(screen.queryByTestId('manual-sort-undo')).toBeNull()
  })

  it('under Manual: the move fails and no sort is written', async () => {
    gw.manual = { order: ['a', 'b', 'c'], stored: true, rev: 100 }
    vi.mocked(api.setManualOrder).mockImplementationOnce(async () => { throw writeFailed() })
    renderSidebar(RANKED_ABC, 'manual')
    drop('c', onRow('a'))
    expect(await screen.findByText(FAILED_SAVED)).toBeTruthy()
    expect(sortWrites()).toEqual([])
    await pickNameDesc()
    await waitFor(() => expect(sortWrites()).toEqual(['name-desc']))
  })

  it('under Manual: a move refused as anchor_gone says the sessions changed, and the saved order is back', async () => {
    // Another browser's order no longer holds c; this browser still ranks it.
    gw.manual = { order: ['a', 'b'], stored: true, rev: 100 }
    renderSidebar(RANKED_ABC, 'manual')
    drop('a', onRow('c'))
    expect(await screen.findByText(CONFLICT_SAVED)).toBeTruthy()
    expect(manualWrites()).toMatchObject([{ op: 'move', key: 'a', after: 'c' }])
    expect(sortWrites()).toEqual([])
  })

  it('two drops made in Newest before either lands, both failing: Newest comes back', async () => {
    let releaseSeed!: () => void
    const seedHeld = new Promise<void>(resolve => { releaseSeed = resolve })
    vi.mocked(api.setManualOrder).mockImplementationOnce(() => seedHeld.then(() => { throw writeFailed() }))
    renderSidebar(ABC)
    drop('c', onRow('a'))
    await waitFor(() => expect(sortWrites()).toEqual(['manual']))
    // Made under the Manual drop 1 switched to, while its seed is held: a move.
    drop('a', onRow('b'))
    expect(manualWrites()).toHaveLength(1)
    await act(async () => { releaseSeed(); await seedHeld.catch(() => undefined) })
    // Nothing is stored, so the gateway refuses the move as anchor_gone.
    await waitFor(() => expect(manualWrites()).toMatchObject([{ op: 'seed' }, { op: 'move', key: 'a', after: 'b' }]))
    await waitFor(() => expect(sortWrites()).toEqual(['manual', 'date-desc']))
    expect(await screen.findByText(conflictBackIn('Newest'))).toBeTruthy()
    expect(screen.queryByTestId('manual-sort-undo')).toBeNull()
    expect(gw.manual).toMatchObject({ order: [], stored: false })
  })

  it('a later drop that fails after the first one landed keeps Manual, and the first Undo still works', async () => {
    let releaseSeed!: () => void
    const seedHeld = new Promise<void>(resolve => { releaseSeed = resolve })
    vi.mocked(api.setManualOrder)
      .mockImplementationOnce(body => seedHeld.then(() => fakeManualWrite(body)))
      .mockImplementationOnce(async () => { throw writeFailed() })
    renderSidebar(ABC)
    drop('c', onRow('a'))
    await waitFor(() => expect(sortWrites()).toEqual(['manual']))
    drop('a', onRow('b'))
    await act(async () => { releaseSeed(); await seedHeld })
    expect(await screen.findByText(FAILED_SAVED)).toBeTruthy()
    expect(gw.manual).toMatchObject({ order: ['c', 'a', 'b'], stored: true, rev: 101 })
    fireEvent.click(await screen.findByTestId('manual-sort-undo-button'))
    await waitFor(() => expect(manualWrites().at(-1)).toEqual({ op: 'clear', rev: 101 }))
    await waitFor(() => expect(gw.manual).toMatchObject({ order: [], stored: false }))
    await pickNameDesc()
    // Undo's Newest is the only sort written between the drop and the pick.
    await waitFor(() => expect(sortWrites()).toEqual(['manual', 'date-desc', 'name-desc']))
  })

  it('a sort picked while a drop is pending stands when that drop fails', async () => {
    let releaseSeed!: () => void
    const seedHeld = new Promise<void>(resolve => { releaseSeed = resolve })
    vi.mocked(api.setManualOrder).mockImplementationOnce(() => seedHeld.then(() => { throw writeFailed() }))
    const { store } = renderSidebar(ABC)
    drop('c', onRow('a'))
    await waitFor(() => expect(sortWrites()).toEqual(['manual']))
    await pickNameDesc()
    await waitFor(() => expect(sortWrites()).toEqual(['manual', 'name-desc']))
    await act(async () => { releaseSeed(); await seedHeld.catch(() => undefined) })
    expect(await screen.findByText(failedBackIn('Z → A'))).toBeTruthy()
    await waitFor(() => expect(store.getState().dashboard.pendingManualOrder).toBeNull())
    await pickSortByLabel('A → Z')
    // Nothing put Newest back between the pick and this one.
    await waitFor(() => expect(sortWrites()).toEqual(['manual', 'name-desc', 'name-asc']))
  })
})

describe('a Manual with nothing saved, after a write does not take', () => {
  it('picking Manual whose seed fails says nothing is saved', async () => {
    vi.mocked(api.setManualOrder).mockImplementationOnce(async () => { throw writeFailed() })
    renderSidebar(ABC, 'name-desc')
    await pickManual()
    expect(await screen.findByText(UNSAVED)).toBeTruthy()
    expect(manualWrites()).toEqual([{ op: 'seed', keys: ['c', 'b', 'a'] }])
    await waitFor(() => expect(sortWrites()).toEqual(['manual']))
  })

  it('a first drag whose seed fails after Manual was picked says nothing is saved, and keeps Manual', async () => {
    let releaseSeed!: () => void
    const seedHeld = new Promise<void>(resolve => { releaseSeed = resolve })
    vi.mocked(api.setManualOrder).mockImplementationOnce(() => seedHeld.then(() => { throw writeFailed() }))
    const { store } = renderSidebar(ABC)
    drop('c', onRow('a'))
    await waitFor(() => expect(sortWrites()).toEqual(['manual']))
    await pickManual()
    await waitFor(() => expect(sortWrites()).toEqual(['manual', 'manual']))
    await act(async () => { releaseSeed(); await seedHeld.catch(() => undefined) })
    expect(await screen.findByText(UNSAVED)).toBeTruthy()
    await waitFor(() => expect(store.getState().dashboard.pendingManualOrder).toBeNull())
    await pickNameDesc()
    // Nothing put Newest back between the pick and this one.
    await waitFor(() => expect(sortWrites()).toEqual(['manual', 'manual', 'name-desc']))
  })

  it('picking Manual over an order of closed sessions whose follow-up move fails says nothing is saved', async () => {
    gw.manual = { order: ['x', 'y'], stored: true, rev: 100 }
    vi.mocked(api.setManualOrder)
      .mockImplementationOnce(async body => fakeManualWrite(body))
      .mockImplementationOnce(async () => { throw writeFailed() })
    renderSidebar(ABC, 'name-desc')
    await pickManual()
    expect(await screen.findByText(UNSAVED)).toBeTruthy()
    expect(manualWrites()).toMatchObject([{ op: 'seed' }, { op: 'move', key: 'c', after: null }])
    expect(gw.manual.order).toEqual(['x', 'y'])
  })
})

describe('the insertion line', () => {
  it('shows where an unpinned row will land: below a row it moves down past, above one it moves up past', () => {
    renderSidebar(ABC)
    const start = (key: string) => {
      const active = { id: `session:${key}`, data: { current: { type: 'session', key, pinned: false, container: 'root' } } }
      act(() => {
        dnd.active = { id: active.id }
        dnd.handlers.onDragStart!({ active })
      })
    }
    dnd.overId = 'manual-session:list:c'
    start('a')
    const down = screen.getByTestId('manual-session-insertion')
    expect(down.className).toContain('-bottom-[1px]')
    expect(screen.getByText('c title').closest('[data-session-row]')!.contains(down)).toBe(true)
    act(() => { dnd.handlers.onDragEnd!({ active: { id: 'session:a', data: { current: { type: 'session', key: 'a' } } }, over: null }) })
    dnd.overId = 'manual-session:list:a'
    start('c')
    expect(screen.getByTestId('manual-session-insertion').className).toContain('-top-[1px]')
  })
})

describe('drops that are not between two rows', () => {
  it('a drop on a folder files the session there and leaves the order and the sort alone', async () => {
    renderSidebar(ABC)
    await screen.findByText('Work')
    drop('c', { id: 'folder-drop:f1', data: { type: 'folder-drop', folderId: 'f1' } })
    await waitFor(() => expect(api.setSlotFolder).toHaveBeenCalledWith('c', 'f1'))
    expect(manualWrites()).toEqual([])
    expect(sortWrites()).toEqual([])
  })

  it('a pinned row reorders the pinned order only, in its own sort', async () => {
    renderSidebar([
      slot('a', '2026-03-01T00:00:00Z', { pinned: true }),
      slot('b', '2026-02-01T00:00:00Z', { pinned: true }),
      slot('c', '2026-01-01T00:00:00Z'),
    ])
    drop('a', { id: 'pinned-session:list:b', data: { type: 'pinned-session', key: 'b', container: 'root' } }, 'root', true)
    await waitFor(() => expect(api.setPinnedOrder).toHaveBeenCalled())
    expect(manualWrites()).toEqual([])
    expect(sortWrites()).toEqual([])
  })

  it('a drop while a search ranks the list writes nothing', async () => {
    gw.search = [{ key: 'dashboard_c', title: 'c title' }, { key: 'dashboard_a', title: 'a title' }]
    renderSidebar(ABC)
    fireEvent.change(screen.getByPlaceholderText(/search sessions/i), { target: { value: 'title' } })
    await waitFor(() => expect(api.sessionsSearch).toHaveBeenCalled())
    await waitFor(() => expect(order(['a', 'c'])).toEqual(['c', 'a']))
    drop('c', onRow('a'))
    // A write is several promise hops deep: run every pending chain first.
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
    expect(manualWrites()).toEqual([])
    expect(sortWrites()).toEqual([])
  })
})

describe('the keyboard', () => {
  it('in another sort Alt+ArrowDown on an unpinned row stays the page\'s, and nothing is written', async () => {
    renderSidebar(ABC)
    const row = screen.getByText('a title').closest('[data-session-row]') as HTMLElement
    expect(row.getAttribute('aria-keyshortcuts')).toBeNull()
    row.focus()
    // fireEvent returns false when the handler called preventDefault.
    expect(fireEvent.keyDown(row, { key: 'ArrowDown', altKey: true })).toBe(true)
    // A write is several promise hops deep: run every pending chain first.
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
    expect(manualWrites()).toEqual([])
    expect(sortWrites()).toEqual([])
  })

  it('under Manual Alt+ArrowDown moves an unpinned row the way a drop does and keeps it focused', async () => {
    // Manual with nothing saved yet: the rows draw newest first, as a, b, c.
    const { rerenderFromStore } = renderSidebar(ABC, 'manual')
    const row = screen.getByText('a title').closest('[data-session-row]') as HTMLElement
    expect(row.getAttribute('aria-keyshortcuts')).toBe('Alt+ArrowUp Alt+ArrowDown')
    row.focus()
    fireEvent.keyDown(row, { key: 'ArrowDown', altKey: true })
    // a moved down past b, so it lands below it.
    await waitFor(() => expect(manualWrites()).toEqual([{ op: 'seed', keys: ['b', 'a', 'c'] }]))
    await screen.findByTestId('manual-sort-undo')
    expect(sortWrites()).toEqual([])
    // A browser drops focus from a row it re-inserts lower in the list; jsdom
    // does not, so drop it here. The next commit puts it back on the row.
    act(() => { row.blur() })
    expect(document.activeElement).toBe(document.body)
    rerenderFromStore()
    expect(order(['a', 'b', 'c'])).toEqual(['b', 'a', 'c'])
    await waitFor(() => expect(document.activeElement).toBe(
      screen.getByText('a title').closest('[data-session-row]')))
  })
})

describe('the Manual sort', () => {
  it('is a menu row under Name; picking it with nothing saved seeds the order on screen', async () => {
    renderSidebar(ABC, 'name-desc')
    expect(order(['a', 'b', 'c'])).toEqual(['c', 'b', 'a'])
    fireEvent.keyDown(screen.getByLabelText('Sort and filter sessions'), { key: 'Enter' })
    const name = await screen.findByText('Z → A')
    const manual = screen.getByText('Manual')
    expect(name.compareDocumentPosition(manual) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    fireEvent.click(manual)
    await waitFor(() => expect(manualWrites()).toEqual([{ op: 'seed', keys: ['c', 'b', 'a'] }]))
    await waitFor(() => expect(sortWrites()).toEqual(['manual']))
    // Checked once the list is in that order.
    fireEvent.keyDown(screen.getByLabelText('Sort and filter sessions'), { key: 'Enter' })
    const row = (await screen.findByText('Manual')).closest('[role="menuitemradio"]') as HTMLElement
    expect(row.getAttribute('aria-checked')).toBe('true')
    expect(row.querySelector('svg')).toBeTruthy()
  })

  it('picking it while the saved order ranks only closed sessions puts the order on screen on top', async () => {
    // x and y have closed; the gateway keeps their places.
    gw.manual = { order: ['x', 'y'], stored: true, rev: 100 }
    const { store, rerenderFromStore } = renderSidebar(ABC, 'name-desc')
    expect(order(['a', 'b', 'c'])).toEqual(['c', 'b', 'a'])
    fireEvent.keyDown(screen.getByLabelText('Sort and filter sessions'), { key: 'Enter' })
    fireEvent.click(await screen.findByText('Manual'))
    await waitFor(() => expect(manualWrites()).toEqual([
      { op: 'seed', keys: ['c', 'b', 'a'] },
      { op: 'move', key: 'c', after: null, seat: ['b', 'a'], expected_created: '2026-01-01T00:00:00Z' },
    ]))
    await waitFor(() => expect(store.getState().dashboard.pendingManualOrder).toBeNull())
    expect(gw.manual.order).toEqual(['c', 'b', 'a', 'x', 'y'])
    expect(screen.queryByTestId('manual-order-error')).toBeNull()
    rerenderFromStore()
    expect(order(['a', 'b', 'c'])).toEqual(['c', 'b', 'a'])
  })

  it('another sort keeps the saved order, and Manual brings it back without a new seed', async () => {
    gw.manual = { order: ['b', 'c', 'a'], stored: true, rev: 100 }
    const ranked = [
      slot('a', '2026-03-01T00:00:00Z', { manual_rank: 2, manual_rev: 100 }),
      slot('b', '2026-02-01T00:00:00Z', { manual_rank: 0, manual_rev: 100 }),
      slot('c', '2026-01-01T00:00:00Z', { manual_rank: 1, manual_rev: 100 }),
    ]
    renderSidebar(ranked, 'date-desc')
    expect(order(['a', 'b', 'c'])).toEqual(['a', 'b', 'c'])
    fireEvent.keyDown(screen.getByLabelText('Sort and filter sessions'), { key: 'Enter' })
    fireEvent.click(await screen.findByText('Manual'))
    await waitFor(() => expect(sortWrites()).toEqual(['manual']))
    await waitFor(() => expect(order(['a', 'b', 'c'])).toEqual(['b', 'c', 'a']))
    expect(manualWrites()).toEqual([])
  })

  it('keeps pinned rows on top, shows a handle on unpinned rows, puts new sessions first and draws no date headers', () => {
    localStorage.setItem('mc-sidebar-lane', 'flat')
    renderSidebar([
      slot('p', '2026-01-05T00:00:00Z', { pinned: true, manual_rank: null }),
      slot('a', '2026-03-01T00:00:00Z', { manual_rank: 1, manual_rev: 100 }),
      slot('b', '2026-02-01T00:00:00Z', { manual_rank: 0, manual_rev: 100 }),
      slot('n', '2026-04-01T00:00:00Z', { manual_rank: null, manual_rev: 100 }),
    ], 'manual')
    expect(order(['p', 'a', 'b', 'n'])).toEqual(['p', 'n', 'b', 'a'])
    expect(screen.queryAllByTestId('date-segment-header')).toEqual([])
    const handleOf = (k: string) => screen.getByText(`${k} title`).closest('[data-session-row]')!
      .querySelector('[data-testid="session-row-drag-handle"]')
    expect(handleOf('p')).toBeNull()
    expect(handleOf('a')).toBeTruthy()
    expect(handleOf('n')).toBeTruthy()
  })

  it('draws the handle in the row\'s left pad, out of the text column, apart from the ⋮ menu and the right-end group', () => {
    renderSidebar([slot('a', '2026-03-01T00:00:00Z', { manual_rank: 0, manual_rev: 100 })], 'manual')
    const row = screen.getByText('a title').closest('[data-session-row]') as HTMLElement
    const handle = row.querySelector('[data-testid="session-row-drag-handle"]') as HTMLElement
    const classes = (el: Element) => el.className.split(/\s+/)
    // A child of the row itself, out of flow, so it moves no x in the content
    // column: the agent name, title and status line keep the folder name's left
    // edge (docs/decisions/2026-10-09-folder-glyph-sits-on-its-connector-line.md).
    expect(handle.parentElement).toBe(row)
    expect(classes(handle)).toContain('absolute')
    const column = row.querySelector('[data-session-title]')!.parentElement!
    expect(column.contains(handle)).toBe(false)
    const meta = row.querySelector('.session-agent-label')!
    expect(meta.firstElementChild!.textContent).toBe('default')
    // Inside the row's 14px left pad: 4px in, 10px wide, so it ends where the
    // content column starts.
    expect(classes(row)).toContain('pl-3.5')
    expect(classes(handle)).toEqual(expect.arrayContaining(['left-1', 'w-2.5']))
    // Apart from the right end, which the ⋮ menu and the time group hold.
    const menu = within(row).getByLabelText('More options').parentElement!
    expect(menu.contains(handle)).toBe(false)
    const end = within(row).getByTestId('session-row-time').parentElement!
    expect(end.className).toContain('ml-auto')
    expect(end.contains(handle)).toBe(false)
  })

  it('an unpinned session takes back its manual place', () => {
    const { rerender } = renderSidebar([
      slot('a', '2026-03-01T00:00:00Z', { manual_rank: 0, manual_rev: 100 }),
      slot('b', '2026-02-01T00:00:00Z', { manual_rank: 1, manual_rev: 100, pinned: true }),
      slot('c', '2026-01-01T00:00:00Z', { manual_rank: 2, manual_rev: 100 }),
    ], 'manual')
    expect(order(['a', 'b', 'c'])).toEqual(['b', 'a', 'c'])
    rerender([
      slot('a', '2026-03-01T00:00:00Z', { manual_rank: 0, manual_rev: 100 }),
      slot('b', '2026-02-01T00:00:00Z', { manual_rank: 1, manual_rev: 100, pinned: false }),
      slot('c', '2026-01-01T00:00:00Z', { manual_rank: 2, manual_rev: 100 }),
    ])
    expect(order(['a', 'b', 'c'])).toEqual(['a', 'b', 'c'])
  })

  it('shows the gateway order from a frame at least as new as the last one applied', () => {
    const { store, rerenderFromStore } = renderSidebar([], 'manual')
    act(() => {
      store.dispatch({ type: 'dashboard/sseSlots', payload: [
        slot('a', '2026-03-01T00:00:00Z', { manual_rank: 0, manual_rev: 200 }),
        slot('b', '2026-02-01T00:00:00Z', { manual_rank: 1, manual_rev: 200 }),
      ] })
    })
    rerenderFromStore()
    expect(order(['a', 'b'])).toEqual(['a', 'b'])
    act(() => {
      store.dispatch({ type: 'dashboard/sseSlots', payload: [
        slot('a', '2026-03-01T00:00:00Z', { manual_rank: 1, manual_rev: 150 }),
        slot('b', '2026-02-01T00:00:00Z', { manual_rank: 0, manual_rev: 150 }),
      ] })
    })
    rerenderFromStore()
    // Serialized before the order the rows already hold: ignored.
    expect(order(['a', 'b'])).toEqual(['a', 'b'])
    act(() => {
      store.dispatch({ type: 'dashboard/sseSlots', payload: [
        slot('a', '2026-03-01T00:00:00Z', { manual_rank: 1, manual_rev: 250 }),
        slot('b', '2026-02-01T00:00:00Z', { manual_rank: 0, manual_rev: 250 }),
      ] })
    })
    rerenderFromStore()
    expect(order(['a', 'b'])).toEqual(['b', 'a'])
  })
})

describe('with "Group by kind" on', () => {
  // a and c are normal chats, b a Crew Mode chat: the list draws b | a, c.
  const MIXED_KINDS = [
    slot('a', '2026-03-01T00:00:00Z'),
    slot('b', '2026-02-01T00:00:00Z', { crew_mode_root: true } as Partial<ChatSlot>),
    slot('c', '2026-01-01T00:00:00Z'),
  ]
  beforeEach(() => { localStorage.setItem('mc-session-group-by-kind', '1') })

  it('takes no manual move on an unpinned row: no Alt+Arrow, no drop target, nothing written', async () => {
    renderSidebar(MIXED_KINDS, 'manual')
    expect(order(['a', 'b', 'c'])).toEqual(['b', 'a', 'c'])
    const row = screen.getByText('a title').closest('[data-session-row]') as HTMLElement
    // The row does not offer the chord, and pressing it is left to the page.
    expect(row.getAttribute('aria-keyshortcuts')).toBeNull()
    row.focus()
    expect(fireEvent.keyDown(row, { key: 'ArrowDown', altKey: true })).toBe(true)
    // Same-kind and cross-kind drops alike: the grouped order is not a manual one.
    drop('a', onRow('c'))
    drop('b', onRow('a'))
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
    expect(manualWrites()).toEqual([])
    expect(sortWrites()).toEqual([])
  })
})
