/**
 * Chat sidebar folder order follows `dashboard.folder_sort`.
 *
 * The reporter numbered folders `01.`, `02.`, ..., `98.`, `99.` and the sidebar
 * drew them scrambled, because a placed folder's stored `order` beats its name.
 * The fix is a per-user VIEW preference with three modes -- Custom (the stored
 * positions, today's order and the default), Name (natural order, so 01. < 02. <
 * 10.) and Created (newest first) -- stored server-side so the MCP tree agrees, and
 * chosen from a "Folder order" section of the sidebar's sort-and-filter menu.
 * Choosing a mode never rewrites a stored position.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

// Captures the sidebar's drag-end handler so a case can script a folder drop
// without driving dnd-kit's sensors through jsdom (the technique of
// ChatSidebar.dragFreezeOrder.test.tsx). Every DndContext in the sidebar is
// wired to the same handler, so the last one rendered is as good as any. The
// per-row `useSortable` arguments are captured too: whether a folder row is a
// reorder TARGET is decided there (`disabled.droppable`), which no DOM query
// can see.
const dnd = vi.hoisted(() => ({
  handlers: {} as Record<string, ((e: unknown) => void) | undefined>,
  sortables: new Map<string, { disabled?: boolean | { draggable?: boolean; droppable?: boolean } }>(),
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
  }
})
vi.mock('@dnd-kit/sortable', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@dnd-kit/sortable')>()
  return {
    ...actual,
    useSortable: (args: Parameters<typeof actual.useSortable>[0]) => {
      const data = args.data as { type?: string } | undefined
      if (data?.type === 'folder') dnd.sortables.set(String(args.id), { disabled: args.disabled })
      return actual.useSortable(args)
    },
  }
})

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
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

/** The gateway's config as the test's fake server holds it. `patchConfig` writes
 *  into it and `kirocrewConfig` reads it back, so the settle-time refetch the
 *  optimistic overlay triggers after a save sees the value the save landed --
 *  exactly the round trip the real endpoint pair performs. */
const serverConfig: { dashboard: Record<string, unknown> } = { dashboard: {} }
const patchConfig = vi.fn(async (path: string, value: unknown) => {
  if (path === 'dashboard.folder_sort') serverConfig.dashboard.folder_sort = value
  return { ok: true }
})
const kirocrewConfig = vi.fn(async () => ({ dashboard: { ...serverConfig.dashboard } }))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, name: string) => {
      if (name === 'patchConfig') return patchConfig
      if (name === 'kirocrewConfig') return kirocrewConfig
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
import type { ChatFolder, ChatSlot } from '../types'
import { bySidebarOrder } from '../utils/folderTree'

/** The reporter's scheme with the stored positions a few drags left behind:
 *  the stored order reads 10, 99, 98, 02, 03, 01 -- the sidebar he screenshotted. */
const NUMBERED: ChatFolder[] = [
  { id: 'f10', name: '10. Zulu', order: 0, collapsed: true, created_at: 1_000 },
  { id: 'f99', name: '99. Omega', order: 1, collapsed: true, created_at: 6_000 },
  { id: 'f98', name: '98. Tango', order: 2, collapsed: true, created_at: 5_000 },
  { id: 'f02', name: '02. Mike', order: 3, collapsed: true, created_at: 2_000 },
  { id: 'f03', name: '03. Kilo', order: 4, collapsed: true, created_at: 3_000 },
  { id: 'f01', name: '01. Alpha', order: 5, collapsed: true, created_at: 4_000 },
]

function renderSidebar(folders: ChatFolder[], folderSort: unknown, opts: { configReadFails?: Error; configPending?: boolean } = {}) {
  const slots: ChatSlot[] = []
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null, slotStatusDetail: {}, subagents: {}, slotActivity: {}, workflowRuns: {} } as unknown as RootState['chat'],
  })
  // staleTime keeps both seeded caches authoritative: the blanket api mock
  // resolves every read to [], so an on-mount refetch would wipe the folders and
  // the config out from under the rows we are asserting on.
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity, refetchOnMount: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], folders)
  serverConfig.dashboard = folderSort === undefined ? {} : { folder_sort: folderSort }
  let settleConfig: (body: { dashboard: Record<string, unknown> }) => void = () => {}
  if (opts.configReadFails) {
    // No seeded config: the mount fetch is the read, and it fails -- the query
    // settles in its error state (retry is off) with no data to fall back on.
    kirocrewConfig.mockRejectedValueOnce(opts.configReadFails)
  } else if (opts.configPending) {
    // No seeded config and a read that does not settle until the test says so:
    // the first-load window, with the folder list already on screen.
    kirocrewConfig.mockImplementationOnce(() => new Promise(resolve => { settleConfig = resolve }))
  } else {
    qc.setQueryData(['kirocrewConfig'], { dashboard: { ...serverConfig.dashboard } })
  }
  const utils = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return { ...utils, qc, settleConfig: (body: { dashboard: Record<string, unknown> }) => settleConfig(body) }
}

/** Folder ids in the order the tree draws their header rows. */
function drawnFolderIds(container: HTMLElement): string[] {
  return [...container.querySelectorAll('[data-folder-row]')].map(el => el.getAttribute('data-folder-row') ?? '')
}

beforeEach(() => { localStorage.clear(); patchConfig.mockClear(); kirocrewConfig.mockClear(); dnd.sortables.clear() })
afterEach(() => vi.clearAllMocks())

describe('chat sidebar — folder order follows dashboard.folder_sort', () => {
  it('draws the stored order in custom mode -- byte-identical to bySidebarOrder', () => {
    const { container } = renderSidebar(NUMBERED, 'custom')
    expect(drawnFolderIds(container)).toEqual([...NUMBERED].sort(bySidebarOrder).map(f => f.id))
    expect(drawnFolderIds(container)).toEqual(['f10', 'f99', 'f98', 'f02', 'f03', 'f01'])
  })

  it('treats an absent or unknown stored value as custom, so an upgrade changes nothing', () => {
    expect(drawnFolderIds(renderSidebar(NUMBERED, undefined).container)).toEqual(['f10', 'f99', 'f98', 'f02', 'f03', 'f01'])
    expect(drawnFolderIds(renderSidebar(NUMBERED, 'alphabetical').container)).toEqual(['f10', 'f99', 'f98', 'f02', 'f03', 'f01'])
  })

  it('draws the numbered folders in natural order in name mode', () => {
    const { container } = renderSidebar(NUMBERED, 'name')
    expect(drawnFolderIds(container)).toEqual(['f01', 'f02', 'f03', 'f10', 'f98', 'f99'])
  })

  it('draws newest first in created mode', () => {
    const { container } = renderSidebar(NUMBERED, 'created')
    expect(drawnFolderIds(container)).toEqual(['f99', 'f98', 'f01', 'f03', 'f02', 'f10'])
  })

  it('applies the mode at every depth, not only to root folders', () => {
    const nested: ChatFolder[] = [
      { id: 'root', name: 'Projects', order: 0, collapsed: false },
      { id: 'c10', name: '10. late', order: 0, parent_id: 'root', collapsed: true },
      { id: 'c2', name: '2. early', order: 1, parent_id: 'root', collapsed: true },
    ]
    expect(drawnFolderIds(renderSidebar(nested, 'custom').container)).toEqual(['root', 'c10', 'c2'])
    expect(drawnFolderIds(renderSidebar(nested, 'name').container)).toEqual(['root', 'c2', 'c10'])
  })

  it('offers the three modes under "Folder order" in the sort-and-filter menu and marks the active one', async () => {
    const { getByLabelText, findByTestId, getByTestId, queryByTestId } = renderSidebar(NUMBERED, 'custom')
    fireEvent.keyDown(getByLabelText('Sort and filter sessions'), { key: 'Enter' })
    const custom = await findByTestId('folder-order-custom')
    // "Custom", not "Custom order": under the "Folder order" heading the row's
    // own "order" was the third in three lines and had to be read around.
    expect(custom.textContent?.trim()).toBe('Custom')
    expect(getByTestId('folder-order-name').textContent).toContain('By name')
    // "By date created (newest first)": names the field AND the direction --
    // shown newest-first with no direction in the label, a reader guessed
    // "reversed"; the session rows two sections up spell theirs. What the rows
    // must not do is take a session row's own shape ("Created (Newest)"): the
    // two headings now name their objects, so a shared word is not a shared
    // setting, but an identical label would be.
    expect(getByTestId('folder-order-created').textContent?.trim()).toBe('By date created (newest first)')
    for (const mode of ['custom', 'name', 'created']) expect(getByTestId(`folder-order-${mode}`).textContent).not.toMatch(/Created \(/)
    // The check mark sits on the active row only.
    expect(custom.querySelector('svg.text-accent')).toBeTruthy()
    expect(getByTestId('folder-order-name').querySelector('svg.text-accent')).toBeNull()
    // In Custom every drag reorders, so there is nothing to say under the rows.
    expect(queryByTestId('folder-order-reorder-note')).toBeNull()
  })

  it('says under the rows that reordering needs Custom, whenever another mode is active', async () => {
    const { getByLabelText, findByTestId, queryByTestId } = renderSidebar(NUMBERED, 'name')
    fireEvent.keyDown(getByLabelText('Sort and filter sessions'), { key: 'Enter' })
    const note = await findByTestId('folder-order-reorder-note')
    // A fact about the modes, not the sidebar hint's "Switch to Custom ..."
    // sentence: that one sits beside a button that does the switching, and
    // the same words here would read as an action that does nothing.
    expect(note.textContent).toBe('Folders can be dragged into place in Custom order only')
    expect(note.textContent).not.toContain('Switch to')
    // No unstamped-folder line here: every NUMBERED folder carries a stamp, and
    // the mode is Name -- the line is about what the created comparator cannot
    // separate, and only while that comparator is the one drawing the tree.
    expect(queryByTestId('folder-order-unstamped-note')).toBeNull()
  })

  it('heads the sessions sort with its object, so it does not read as a twin of Folder order', async () => {
    const { getByLabelText, findByTestId, getByText, queryByText } = renderSidebar(NUMBERED, 'custom')
    fireEvent.keyDown(getByLabelText('Sort and filter sessions'), { key: 'Enter' })
    await findByTestId('folder-order-custom')
    // Two orderings in one menu: "Sort by" over one list and "Folder order" over
    // another read as sorting twice, with a guess about which list each changes.
    expect(getByText('Sort sessions by')).toBeInTheDocument()
    expect(queryByText('Sort by')).toBeNull()
    expect(getByText('Folder order')).toBeInTheDocument()
  })

  it('in created mode says under the rows that folders from before the stamp have no date and come last, only while such a folder is in the list', async () => {
    // A folder from before `created_at` existed: the comparator puts it after
    // every stamped row, in the stored order. On an existing tree that is the
    // order the person already had, and picking the mode looks like nothing
    // happened unless the menu says why.
    const legacy: ChatFolder = { id: 'f00', name: 'Legacy', order: 6, collapsed: true }
    const withLegacy = [...NUMBERED, legacy]
    const { container, getByLabelText, findByTestId } = renderSidebar(withLegacy, 'created')
    expect(drawnFolderIds(container)).toEqual(['f99', 'f98', 'f01', 'f03', 'f02', 'f10', 'f00'])
    fireEvent.keyDown(getByLabelText('Sort and filter sessions'), { key: 'Enter' })
    const note = await findByTestId('folder-order-unstamped-note')
    expect(note.textContent).toBe('Folders made before dates were recorded have no date; they come last, in your Custom order')
    // Beside the drag note, not instead of it: two facts about this mode.
    expect(await findByTestId('folder-order-reorder-note')).toBeInTheDocument()
  })

  it('the unstamped-folder line is withheld when every folder is stamped, and outside created mode', async () => {
    const stamped = renderSidebar(NUMBERED, 'created')
    fireEvent.keyDown(stamped.getByLabelText('Sort and filter sessions'), { key: 'Enter' })
    await stamped.findByTestId('folder-order-created')
    expect(stamped.queryByTestId('folder-order-unstamped-note')).toBeNull()
    stamped.unmount()
    const legacy: ChatFolder = { id: 'f00', name: 'Legacy', order: 6, collapsed: true }
    const byName = renderSidebar([...NUMBERED, legacy], 'name')
    fireEvent.keyDown(byName.getByLabelText('Sort and filter sessions'), { key: 'Enter' })
    await byName.findByTestId('folder-order-name')
    // In Name mode the unstamped folder sorts by its name like any other: the
    // fact is about dates, so it is not said where dates are not the order.
    expect(byName.queryByTestId('folder-order-unstamped-note')).toBeNull()
  })

  it('offers the Folder order rows in the flat lane too, where the mode still orders every folder picker', async () => {
    // The flat lane draws no folder tree, but the mode is not idle there: every
    // row menu's "Move to folder" picker, the history search's folder groups, the
    // Command Bar, the job form and the MCP tree all list in it. A person who
    // picked Name in the tree lane must be able to change it from here, so the
    // rows are lane-independent -- the control lives wherever the mode is read.
    localStorage.setItem('mc-sidebar-lane', 'flat')
    const { getByLabelText, findByTestId, getByTestId, queryByTestId } = renderSidebar(NUMBERED, 'name')
    expect(getByTestId('flat-view-lane')).toBeInTheDocument()
    fireEvent.keyDown(getByLabelText('Sort and filter sessions'), { key: 'Enter' })
    const name = await findByTestId('folder-order-name')
    expect(name.querySelector('svg.text-accent')).toBeTruthy()
    // The reorder note is about a folder DRAG, and this lane draws no folder row
    // to drag: a note promising a reorder that cannot happen here would mislead.
    expect(queryByTestId('folder-order-reorder-note')).toBeNull()
    fireEvent.click(getByTestId('folder-order-custom'))
    await waitFor(() => expect(patchConfig).toHaveBeenCalledWith('dashboard.folder_sort', 'custom'))
    expect(patchConfig).toHaveBeenCalledTimes(1)
  })

  it('picking Name PATCHes dashboard.folder_sort once, re-sorts the tree when the save lands, and rewrites no folder', async () => {
    // The save is held until the test lets it land: the tree must NOT switch on
    // the pick alone. The card, the menu, the Command Bar and the MCP tree all
    // read the shared cache, and a sidebar drawing a picked mode one round-trip
    // before them would show two orders for one tree. The menu's success write
    // puts the accepted value into that cache, and every reader switches on it.
    let land: () => void = () => {}
    patchConfig.mockImplementationOnce((path: string, value: unknown) => new Promise(resolve => {
      land = () => { if (path === 'dashboard.folder_sort') serverConfig.dashboard.folder_sort = value; resolve({ ok: true }) }
    }))
    const { getByLabelText, findByTestId, container, qc } = renderSidebar(NUMBERED, 'custom')
    fireEvent.keyDown(getByLabelText('Sort and filter sessions'), { key: 'Enter' })
    fireEvent.click(await findByTestId('folder-order-name'))
    await waitFor(() => expect(patchConfig).toHaveBeenCalledWith('dashboard.folder_sort', 'name'))
    expect(patchConfig).toHaveBeenCalledTimes(1)
    // In flight: still the stored order, and the shared cache still says Custom.
    expect(drawnFolderIds(container)).toEqual(['f10', 'f99', 'f98', 'f02', 'f03', 'f01'])
    expect(qc.getQueryData<{ dashboard?: { folder_sort?: string } }>(['kirocrewConfig'])?.dashboard?.folder_sort).toBe('custom')
    await act(async () => { land() })
    await waitFor(() => expect(drawnFolderIds(container)).toEqual(['f01', 'f02', 'f03', 'f10', 'f98', 'f99']))
    // ...and it switched because the CACHE did, which is what every other reader sees.
    expect(qc.getQueryData<{ dashboard?: { folder_sort?: string } }>(['kirocrewConfig'])?.dashboard?.folder_sort).toBe('name')
    // A VIEW change: the folder rows themselves are untouched, so switching back to
    // Custom restores exactly the arrangement the person had.
    expect(qc.getQueryData<ChatFolder[]>(['chat-folders'])).toEqual(NUMBERED)
    expect(patchConfig.mock.calls.every(([path]) => path === 'dashboard.folder_sort')).toBe(true)
  })

  it('picking the active mode again writes nothing', async () => {
    const { getByLabelText, findByTestId } = renderSidebar(NUMBERED, 'name')
    fireEvent.keyDown(getByLabelText('Sort and filter sessions'), { key: 'Enter' })
    fireEvent.click(await findByTestId('folder-order-name'))
    expect(patchConfig).not.toHaveBeenCalled()
  })

  it('two quick picks go out one at a time, and the LAST one is what the server holds even when the first response is delayed', async () => {
    // Two concurrent PATCHes race on the server: a delayed first one can commit
    // after the second and restore the earlier choice. So one save is on the
    // wire at a time -- a pick made while a save is in flight is queued and
    // sent when that save settles. Here Name's response is held until after
    // Custom is picked; Custom must go out only once Name has landed, and end
    // as the value on the server and in the tree.
    let landName: () => void = () => {}
    patchConfig.mockImplementationOnce((path: string, value: unknown) => new Promise<{ ok: boolean }>(resolve => {
      landName = () => { if (path === 'dashboard.folder_sort') serverConfig.dashboard.folder_sort = value; resolve({ ok: true }) }
    }))
    const { getByLabelText, findByTestId, container } = renderSidebar(NUMBERED, 'custom')
    fireEvent.keyDown(getByLabelText('Sort and filter sessions'), { key: 'Enter' })
    fireEvent.click(await findByTestId('folder-order-name'))
    await waitFor(() => expect(patchConfig).toHaveBeenCalledWith('dashboard.folder_sort', 'name'))
    fireEvent.keyDown(getByLabelText('Sort and filter sessions'), { key: 'Enter' })
    fireEvent.click(await findByTestId('folder-order-custom'))
    // Queued, not sent: Name is still in flight.
    await act(async () => { await Promise.resolve() })
    expect(patchConfig).toHaveBeenCalledTimes(1)
    // The delayed first response lands AFTER the second pick was made.
    await act(async () => { landName() })
    await waitFor(() => expect(patchConfig).toHaveBeenCalledWith('dashboard.folder_sort', 'custom'))
    expect(patchConfig.mock.calls.map(([, v]) => v)).toEqual(['name', 'custom'])
    // The last pick is what the server holds and what the tree draws.
    await waitFor(() => expect(serverConfig.dashboard.folder_sort).toBe('custom'))
    await waitFor(() => expect(drawnFolderIds(container)).toEqual(['f10', 'f99', 'f98', 'f02', 'f03', 'f01']))
    fireEvent.keyDown(getByLabelText('Sort and filter sessions'), { key: 'Enter' })
    expect((await findByTestId('folder-order-custom')).querySelector('svg.text-accent')).toBeTruthy()
  })

  it('queued picks are sent in pick order, judged against the pick in flight, and a refusal is reported only for the latest pick', async () => {
    // Pick Name, then Custom, then Name again while the first Name is still
    // saving: each pick is judged against the value in flight (Custom after
    // Name, Name after Custom -- both real changes), the queue drains in that
    // order, and the server ends on the last one.
    let landName: () => void = () => {}
    patchConfig.mockImplementationOnce((path: string, value: unknown) => new Promise<{ ok: boolean }>(resolve => {
      landName = () => { if (path === 'dashboard.folder_sort') serverConfig.dashboard.folder_sort = value; resolve({ ok: true }) }
    }))
    const { getByLabelText, findByTestId } = renderSidebar(NUMBERED, 'custom')
    const open = () => fireEvent.keyDown(getByLabelText('Sort and filter sessions'), { key: 'Enter' })
    open(); fireEvent.click(await findByTestId('folder-order-name'))
    await waitFor(() => expect(patchConfig).toHaveBeenCalledTimes(1))
    open(); fireEvent.click(await findByTestId('folder-order-custom'))
    open(); fireEvent.click(await findByTestId('folder-order-name'))
    await act(async () => { await Promise.resolve() })
    expect(patchConfig).toHaveBeenCalledTimes(1)
    await act(async () => { landName() })
    await waitFor(() => expect(patchConfig).toHaveBeenCalledTimes(3))
    expect(patchConfig.mock.calls.map(([, v]) => v)).toEqual(['name', 'custom', 'name'])
    await waitFor(() => expect(serverConfig.dashboard.folder_sort).toBe('name'))
    // A refused save with a newer pick queued behind it: the newer pick still
    // goes out, and the refusal is not said -- the newer save's own outcome is
    // the one that matters. When THAT one is refused too, it is said.
    let refuseCreated: () => void = () => {}
    patchConfig.mockImplementationOnce(() => new Promise<{ ok: boolean }>((_resolve, reject) => {
      refuseCreated = () => reject(new Error('created was refused'))
    }))
    patchConfig.mockImplementationOnce(async () => { throw new Error('custom was refused too') })
    open(); fireEvent.click(await findByTestId('folder-order-created'))
    await waitFor(() => expect(patchConfig).toHaveBeenCalledWith('dashboard.folder_sort', 'created'))
    open(); fireEvent.click(await findByTestId('folder-order-custom'))
    await act(async () => { refuseCreated() })
    await waitFor(() => expect(patchConfig).toHaveBeenCalledWith('dashboard.folder_sort', 'custom'))
    const notice = await findByTestId('folder-action-error')
    expect(notice.textContent).toContain('custom was refused too')
    expect(notice.textContent).not.toContain('created was refused')
    expect(patchConfig).toHaveBeenCalledTimes(5)
    // Neither refused write reached the server's copy.
    expect(serverConfig.dashboard.folder_sort).toBe('name')
  })

  it('a refused save is said on the folder-action notice and the tree never leaves the stored order', async () => {
    patchConfig.mockRejectedValueOnce(new Error('governance refused this write'))
    const { getByLabelText, findByTestId, container } = renderSidebar(NUMBERED, 'custom')
    fireEvent.keyDown(getByLabelText('Sort and filter sessions'), { key: 'Enter' })
    fireEvent.click(await findByTestId('folder-order-name'))
    // Nothing on screen changed, so the notice is what tells the person their
    // pick did not take.
    const notice = await findByTestId('folder-action-error')
    expect(notice.textContent).toContain('governance refused this write')
    expect(drawnFolderIds(container)).toEqual(['f10', 'f99', 'f98', 'f02', 'f03', 'f01'])
    // The refused write never reached the server's copy.
    expect(serverConfig.dashboard.folder_sort).toBe('custom')
  })

  it('outside Custom a folder row is no reorder target, and a scripted sibling drop writes nothing but says why', async () => {
    const first = renderSidebar(NUMBERED, 'name')
    // The affordance is withdrawn at the row: with the droppable side off, no
    // pointer drag can resolve a sibling as `over`, so no slot ever opens.
    expect(dnd.sortables.get('f10')?.disabled).toEqual({ droppable: true })
    expect(dnd.sortables.get('f99')?.disabled).toEqual({ droppable: true })
    // The belt for the keyboard/scripted path: the handler declines the write,
    // and says at the drop where reordering comes back -- a status line, not a
    // failed-update notice, because nothing failed.
    expect(typeof dnd.handlers.onDragEnd).toBe('function')
    const drop = {
      active: { id: 'f10', data: { current: { type: 'folder' } } },
      over: { id: 'f99', data: { current: { type: 'folder' } } },
    }
    act(() => { dnd.handlers.onDragEnd!(drop) })
    expect(first.queryByTestId('folder-action-error')).toBeNull()
    expect(first.qc.getQueryData<ChatFolder[]>(['chat-folders'])).toEqual(NUMBERED)
    const hint = first.getByTestId('folder-reorder-hint')
    expect(hint).toHaveAttribute('role', 'status')
    // The line, and beside it the way there -- the action is a real button, so
    // the status text itself stays the lane's sentence.
    expect(hint.querySelector('span')?.textContent).toBe('Switch to Custom to reorder folders')
    expect(first.getByTestId('folder-reorder-hint-switch').textContent).toBe('Switch to Custom')
    first.unmount()

    // In Custom the rows are ordinary sortables again.
    dnd.sortables.clear()
    renderSidebar(NUMBERED, 'custom')
    expect(dnd.sortables.get('f10')?.disabled).toBeUndefined()
  })

  it('outside Custom a folder drag that finds no target is answered too, and a re-parent drop is not', async () => {
    const view = renderSidebar(NUMBERED, 'name')
    // The pointer path: with the sibling droppables off, a reorder attempt ends
    // with `over` null -- exactly the drag that used to die with zero feedback.
    act(() => { dnd.handlers.onDragEnd!({ active: { id: 'f10', data: { current: { type: 'folder' } } }, over: null }) })
    expect(view.getByTestId('folder-reorder-hint')).toBeInTheDocument()
    // A re-parent gesture (the header's nest band, a folder-drop hit) still
    // works in every mode, so it earns no hint; and its START retires the last
    // one, the way every new drag does.
    const reparent = { active: { id: 'f10', data: { current: { type: 'folder' } } } }
    act(() => { dnd.handlers.onDragStart!(reparent) })
    expect(view.queryByTestId('folder-reorder-hint')).toBeNull()
    act(() => { dnd.handlers.onDragEnd!({ ...reparent, over: { id: 'drop-f99', data: { current: { type: 'folder-drop', folderId: 'f99' } } } }) })
    expect(view.queryByTestId('folder-reorder-hint')).toBeNull()
  })

  it('the drop hint clears when the mode goes back to Custom', async () => {
    const view = renderSidebar(NUMBERED, 'name')
    act(() => { dnd.handlers.onDragEnd!({ active: { id: 'f10', data: { current: { type: 'folder' } } }, over: null }) })
    expect(view.getByTestId('folder-reorder-hint')).toBeInTheDocument()
    fireEvent.keyDown(view.getByLabelText('Sort and filter sessions'), { key: 'Enter' })
    fireEvent.click(await view.findByTestId('folder-order-custom'))
    await waitFor(() => expect(view.queryByTestId('folder-reorder-hint')).toBeNull())
  })

  it('while the folder order cannot be read, the tree draws the stored order, says so, and withdraws reordering', async () => {
    const { container, findByTestId, queryByTestId } = renderSidebar(NUMBERED, 'name', { configReadFails: new Error('gateway restarting') })
    // The stored order is the fallback -- every earlier build drew it -- and an
    // unknown mode is never treated as a writable Custom. Until the read settles
    // no drag is offered at all (see the first-load case below).
    expect(drawnFolderIds(container)).toEqual(['f10', 'f99', 'f98', 'f02', 'f03', 'f01'])
    expect(dnd.sortables.get('f10')?.disabled).toEqual({ draggable: true, droppable: true })
    const notice = await findByTestId('folder-order-unavailable')
    expect(notice.textContent).toContain('Folder order could not be read')
    expect(notice.textContent).toContain('gateway restarting')
    // The server's words stay the message (the journal key) but read as the
    // detail, not the lead: their own smaller line under the plain title.
    const raw = [...notice.querySelectorAll('span')].find(el => el.textContent === 'gateway restarting')
    expect(raw?.className).toContain('block')
    expect(raw?.className).toContain('text-[12px]')
    expect(notice.textContent?.indexOf('could not be read')).toBeLessThan(notice.textContent?.indexOf('gateway restarting') ?? -1)
    // The plain line under it: what is shown, and that nothing is asked of the
    // person -- the read retries on its own. ONE phrase for this failure on
    // every surface (sidebar, pickers, card): a reader seeing two of them at
    // once must not have to work out whether they are one fallback or two.
    expect(notice.textContent).toContain('All folders are shown, in your Custom order; retries automatically')
    expect(notice.textContent).not.toContain('arrangement')
    // The hand-off is stacked UNDER the text, inside the text column, not a
    // sibling column beside it: in this ~300px panel a sibling column left the
    // title and server string wrapping one or two words per line.
    const handoff = notice.querySelector('button')
    expect(handoff?.textContent).toContain('Ask the agent')
    const textColumn = notice.querySelector('.flex-1')
    expect(textColumn?.contains(handoff)).toBe(true)
    expect(notice.textContent?.indexOf('retries automatically')).toBeLessThan(notice.textContent?.indexOf('Ask the agent') ?? -1)
    expect(dnd.sortables.get('f10')?.disabled).toEqual({ droppable: true })
    expect(queryByTestId('folder-action-error')).toBeNull()
  })

  it('a failed BACKGROUND refetch after a Custom read is silent: the cached body is drawn and acted on', async () => {
    // React Query keeps the previous body as `data` when a refetch fails and
    // retries it on its own. The sidebar knows the mode -- it has the body -- so
    // it neither says "could not be read" beside a list it is drawing with
    // confidence nor withdraws the drag it was offering a moment ago.
    const { qc, container, queryByTestId } = renderSidebar(NUMBERED, 'custom')
    expect(dnd.sortables.get('f10')?.disabled).toBeUndefined()
    kirocrewConfig.mockRejectedValueOnce(new Error('config store unavailable'))
    await act(async () => { await qc.refetchQueries({ queryKey: ['kirocrewConfig'] }) })
    expect(qc.getQueryState(['kirocrewConfig'])?.status).toBe('error')
    expect(queryByTestId('folder-order-unavailable')).toBeNull()
    expect(drawnFolderIds(container)).toEqual(['f10', 'f99', 'f98', 'f02', 'f03', 'f01'])
    expect(dnd.sortables.get('f10')?.disabled).toBeUndefined()
  })

  it('a failure with no body is LATCHED through the retry, so the banner does not blink and the drag stays off', async () => {
    // The first read fails with nothing to fall back on. The retry that follows
    // puts the query back in `pending` -- still no body, and now no error either
    // -- and a banner keyed on the status would unmount for its duration and
    // remount on the next failure. Keyed on what is known, it stays put.
    const { qc, findByTestId, queryByTestId } = renderSidebar(NUMBERED, 'custom', { configReadFails: new Error('gateway restarting') })
    await findByTestId('folder-order-unavailable')
    expect(dnd.sortables.get('f10')?.disabled).toEqual({ droppable: true })
    // The retry: a read that never settles while we look.
    let settle: (v: { dashboard: Record<string, unknown> }) => void = () => {}
    kirocrewConfig.mockImplementationOnce(() => new Promise(resolve => { settle = resolve }))
    act(() => { void qc.refetchQueries({ queryKey: ['kirocrewConfig'] }) })
    await waitFor(() => expect(qc.getQueryState(['kirocrewConfig'])?.fetchStatus).toBe('fetching'))
    expect(qc.getQueryState(['kirocrewConfig'])?.status).toBe('pending')
    // Mid-retry: banner still up, with the failure it latched; drag still off.
    expect(queryByTestId('folder-order-unavailable')?.textContent).toContain('gateway restarting')
    expect(dnd.sortables.get('f10')?.disabled).toEqual({ droppable: true })
    // The retry succeeds: a body arrives, the banner clears, the drag comes back.
    await act(async () => { settle({ dashboard: { folder_sort: 'custom' } }) })
    await waitFor(() => expect(queryByTestId('folder-order-unavailable')).toBeNull())
    await waitFor(() => expect(dnd.sortables.get('f10')?.disabled).toBeUndefined())
  })

  it('before the first read, folder rows offer NO drag at all -- no lift to die silently, no grab cursor -- and the drag returns with the mode', async () => {
    // The first-load window: the folder list is on screen (its own query resolved
    // first) while the settings read is still in flight, so the mode is not known
    // and there is no error to say. The rows' droppable side is off in that
    // state, and a lift that then dies at the drop with nothing on screen to
    // explain it is the failure the hint was added against. Consistent with the
    // read-failed state's rule -- no refusal after the fact -- the affordance is
    // withheld: both sides of the sortable off, so dnd-kit hands the header no
    // listeners and the row shows no grab cursor.
    const { container, settleConfig } = renderSidebar(NUMBERED, 'custom', { configPending: true })
    expect(drawnFolderIds(container)).toEqual(['f10', 'f99', 'f98', 'f02', 'f03', 'f01'])
    expect(dnd.sortables.get('f10')?.disabled).toEqual({ draggable: true, droppable: true })
    expect(container.querySelector('[data-folder-row="f10"]')?.className).not.toContain('cursor-grab')
    // The mode arrives: Custom, so the rows are ordinary sortables again.
    await act(async () => { settleConfig({ dashboard: { folder_sort: 'custom' } }) })
    await waitFor(() => expect(dnd.sortables.get('f10')?.disabled).toBeUndefined())
    expect(container.querySelector('[data-folder-row="f10"]')?.className).toContain('cursor-grab')
  })

  it('the drop hint offers Switch to Custom, which writes the mode the way the menu row does', async () => {
    const view = renderSidebar(NUMBERED, 'name')
    act(() => { dnd.handlers.onDragEnd!({ active: { id: 'f10', data: { current: { type: 'folder' } } }, over: null }) })
    const hint = view.getByTestId('folder-reorder-hint')
    expect(hint.textContent).toContain('Switch to Custom to reorder folders')
    fireEvent.click(view.getByTestId('folder-reorder-hint-switch'))
    // One PATCH of the same path the menu writes, no folder rewritten, and the
    // tree returns to the stored order -- exactly what the person was after.
    await waitFor(() => expect(patchConfig).toHaveBeenCalledWith('dashboard.folder_sort', 'custom'))
    expect(patchConfig).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(drawnFolderIds(view.container)).toEqual(['f10', 'f99', 'f98', 'f02', 'f03', 'f01']))
    // Reordering is back, so the line that said where it comes back is gone.
    await waitFor(() => expect(view.queryByTestId('folder-reorder-hint')).toBeNull())
    await waitFor(() => expect(dnd.sortables.get('f10')?.disabled).toBeUndefined())
  })

  it('the drop hint stays until the next interaction away from it -- never on a clock -- and a press on its own action is not one', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      const view = renderSidebar(NUMBERED, 'name')
      act(() => { dnd.handlers.onDragEnd!({ active: { id: 'f10', data: { current: { type: 'folder' } } }, over: null }) })
      const hint = view.getByTestId('folder-reorder-hint')
      // A minute later it is still there: a timer took the "Switch to Custom"
      // action away from under a hand reaching for it.
      act(() => { vi.advanceTimersByTime(60_000) })
      expect(view.getByTestId('folder-reorder-hint')).toBe(hint)
      // Pressing on the line itself (its action) is not an interaction away.
      fireEvent.pointerDown(view.getByTestId('folder-reorder-hint-switch'))
      expect(view.getByTestId('folder-reorder-hint')).toBe(hint)
      // A pointer landing anywhere else -- here the chat pane, outside the
      // sidebar -- retires it.
      fireEvent.pointerDown(document.body)
      expect(view.queryByTestId('folder-reorder-hint')).toBeNull()
      // A key does too.
      act(() => { dnd.handlers.onDragEnd!({ active: { id: 'f10', data: { current: { type: 'folder' } } }, over: null }) })
      expect(view.getByTestId('folder-reorder-hint')).toBeInTheDocument()
      fireEvent.keyDown(document.body, { key: 'ArrowDown' })
      expect(view.queryByTestId('folder-reorder-hint')).toBeNull()
    } finally {
      vi.useRealTimers()
    }
  })
})
