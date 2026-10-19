/**
 * Chat sidebar tree lane: unfiled sessions above the folders, on request.
 *
 * `dashboard.unfiled_sessions_above_folders` (default off) moves the bucket of
 * sessions filed in no folder from below the folder list to above it. With it
 * off the lane is today's, element for element. The characterization cases
 * here were green before the lane changed and stay as the pin. With it on the
 * bucket renders first at natural height, the folders follow, and a lane tail
 * (another null-folder droppable) fills the space below them. Dropping on the
 * empty area still removes a session from its folder, and the explicit "Drop
 * here to remove from folder" hint still renders below the folders.
 *
 * The setting is a gateway config value written from the Folder order section
 * of the sort-and-filter menu as a checkbox row. It is drawn optimistically. A
 * refused save is put back and said on the setting's own notice. The next
 * toggle clears that notice.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import type { ClientRect, Collision, CollisionDetection, DroppableContainer } from '@dnd-kit/core'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

// Captures the sidebar's drag handlers so a case can script a session drag
// without driving dnd-kit's sensors through jsdom (the technique of
// ChatSidebar.folderSortMode.test.tsx). `active` stands in for dnd-kit's own
// store. The sidebar reconciles its drag mirror against `useDndContext().active`,
// so a scripted start must set it and a scripted end must clear it. The
// passthrough context collects no droppables. `mounted` counts the nodes
// registered under each droppable id while they are mounted, and `data` holds
// the data the drop handler is handed for that id. A scripted drag-end reads
// its target's data from `data`. Which droppable a pointer resolves to is
// pinned separately below, against `sidebarCollision` with measured
// rectangles.
const dnd = vi.hoisted(() => ({
  handlers: {} as Record<string, ((e: unknown) => void) | undefined>,
  active: null as { id: string } | null,
  mounted: new Map<string, number>(),
  data: new Map<string, Record<string, unknown> | undefined>(),
}))
vi.mock('@dnd-kit/core', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@dnd-kit/core')>()
  const React = await import('react')
  return {
    ...actual,
    DndContext: (props: { children?: unknown; onDragStart?: (e: unknown) => void; onDragEnd?: (e: unknown) => void }) => {
      dnd.handlers.onDragStart = props.onDragStart
      dnd.handlers.onDragEnd = props.onDragEnd
      return props.children as never
    },
    useDndContext: () => ({ ...actual.useDndContext(), active: dnd.active }),
    useDroppable: (args: Parameters<typeof actual.useDroppable>[0]) => {
      const id = String(args.id)
      const shape = JSON.stringify(args.data ?? null)
      React.useEffect(() => {
        dnd.mounted.set(id, (dnd.mounted.get(id) ?? 0) + 1)
        dnd.data.set(id, JSON.parse(shape) ?? undefined)
        return () => {
          const left = (dnd.mounted.get(id) ?? 1) - 1
          if (left <= 0) { dnd.mounted.delete(id); dnd.data.delete(id) } else dnd.mounted.set(id, left)
        }
      }, [id, shape])
      return actual.useDroppable(args)
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

const PATH = 'dashboard.unfiled_sessions_above_folders'

/** The gateway's config as the test's fake server holds it. `patchConfig` writes
 *  into it and `kirocrewConfig` reads it back, so the settle-time refetch the
 *  optimistic overlay triggers after a save sees the value the save landed.
 *  That is the round trip the real endpoint pair performs. A case that needs a
 *  refused or delayed save replaces `patchConfig` once with a promise it settles
 *  by hand. */
const serverConfig: { dashboard: Record<string, unknown> } = { dashboard: {} }
const patchConfig = vi.fn(async (path: string, value: unknown) => {
  if (path === PATH || path === 'dashboard.folder_sort') serverConfig.dashboard[path.slice('dashboard.'.length)] = value
  return { ok: true }
})
const kirocrewConfig = vi.fn(async () => ({ dashboard: { ...serverConfig.dashboard } }))
const setSlotFolder = vi.fn(async () => ({}))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, name: string) => {
      if (name === 'patchConfig') return patchConfig
      if (name === 'kirocrewConfig') return kirocrewConfig
      if (name === 'setSlotFolder') return setSlotFolder
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

import ChatSidebar, { sidebarCollision } from '../pages/ChatSidebar'
import type { RootState } from '../store'
import type { ChatFolder, ChatSlot } from '../types'

// The one instant the fixtures and the lane's clock share. The dormant split
// compares `last_turn_ts` with the current time, so both must come from it.
const NOW = new Date('2026-10-19T12:00:00Z')
const hoursAgo = (h: number) => new Date(NOW.getTime() - h * 3600_000).toISOString()
const slot = (key: string, title: string, ageHours: number, folderId?: string): ChatSlot => ({
  key, title, running: false, messages: 2,
  created: hoursAgo(ageHours + 1), last_turn_ts: hoursAgo(ageHours),
  folder_id: folderId,
} as unknown as ChatSlot)

const FOLDERS: ChatFolder[] = [
  { id: 'fa', name: 'Alpha work', order: 0, collapsed: false, created_at: 1_000 },
  { id: 'fb', name: 'Beta work', order: 1, collapsed: false, created_at: 2_000 },
]
const UNFILED = [slot('u1', 'unfiled one', 1), slot('u2', 'unfiled two', 2)]
// Older than every dormant-collapse preset, so it folds behind the root expander.
const DORMANT = slot('old', 'unfiled dormant', 30 * 24)
const FILED = [slot('f1', 'filed in alpha', 3, 'fa'), slot('f2', 'filed in beta', 4, 'fb')]
const EVERYTHING = [...UNFILED, DORMANT, ...FILED]

async function renderSidebar(slots: ChatSlot[], folders: ChatFolder[], unfiledAbove: unknown, opts: { lane?: 'flat' | 'conductor' } = {}) {
  if (opts.lane) localStorage.setItem('mc-sidebar-lane', opts.lane)
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
  // resolves every read to [], so an on-mount refetch would wipe the folders
  // out from under the rows we are asserting on.
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity, refetchOnMount: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], folders)
  serverConfig.dashboard = unfiledAbove === undefined ? {} : { unfiled_sessions_above_folders: unfiledAbove }
  qc.setQueryData(['kirocrewConfig'], { dashboard: { ...serverConfig.dashboard } })
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
  // The theme provider sets state when its boot read resolves through the api
  // mock. Those microtasks land here, inside act, so no update follows a
  // case's last barrier.
  await act(async () => { for (let i = 0; i < 10; i += 1) await Promise.resolve() })
  return { ...utils, qc }
}

const before = (a: Element, b: Element) => !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING)
const row = (c: HTMLElement, key: string) => c.querySelector(`[data-session-row="${key}"]`) as HTMLElement
const folderBlock = (c: HTMLElement, id: string) => c.querySelector(`[data-folder-sortable="${id}"]`) as HTMLElement
const lane = (c: HTMLElement) => c.querySelector('[data-testid="tree-view-lane"]') as HTMLElement
const hint = (c: HTMLElement) => c.querySelector('[data-testid="older-sessions-hint-root"]') as HTMLElement
const expander = (c: HTMLElement) => c.querySelector('[data-testid="stale-expander-root"]') as HTMLElement
const toggle = (c: HTMLElement) => c.ownerDocument.querySelector('[data-testid="folder-order-unfiled-above"]') as HTMLElement | null
const openMenu = (r: ReturnType<typeof renderSidebar>) => fireEvent.keyDown(r.getByLabelText('Sort and filter sessions'), { key: 'Enter' })

/** The lane's structure as a comparable value: every element's tag, classes and
 *  `data-*` attributes in document order. Ids and `aria-*` wiring are left out.
 *  dnd-kit and Radix mint fresh counters per mount, so two identical renders
 *  would differ there without differing in what is drawn. */
function laneSignature(c: HTMLElement): string[] {
  return [...lane(c).querySelectorAll('*')].map(el => {
    const data = [...el.attributes].filter(a => a.name.startsWith('data-')).map(a => `${a.name}=${a.value}`).sort()
    return `${el.tagName}|${el.className}|${data.join(',')}`
  })
}

const startSessionDrag = (key: string) => act(() => {
  dnd.active = { id: key }
  dnd.handlers.onDragStart?.({ active: { id: key, data: { current: { type: 'session', key } } } })
})
const startFolderDrag = (id: string) => act(() => {
  dnd.active = { id }
  dnd.handlers.onDragStart?.({ active: { id, data: { current: { type: 'folder' } } } })
})
const endSessionDrag = (key: string, overId: string, overData: Record<string, unknown>) => act(() => {
  dnd.active = null
  dnd.handlers.onDragEnd?.({
    active: { id: key, data: { current: { type: 'session', key } } },
    over: { id: overId, data: { current: overData } },
  })
})

beforeEach(() => { vi.setSystemTime(NOW); localStorage.clear(); dnd.active = null; dnd.mounted.clear(); dnd.data.clear(); patchConfig.mockClear(); kirocrewConfig.mockClear(); setSlotFolder.mockClear() })
afterEach(() => { vi.useRealTimers(); vi.clearAllMocks() })

describe('with the setting off the tree lane is unchanged (characterization)', () => {
  it('draws the folders first and the ungrouped bucket last, filling the lane, with the rows, dormant expander and Older Sessions hint inside it', async () => {
    const { container } = await renderSidebar(EVERYTHING, FOLDERS, undefined)
    const treeLane = lane(container)
    expect(treeLane.firstElementChild?.hasAttribute('data-collapse-floor')).toBe(true)
    const rootLane = treeLane.children[1] as HTMLElement
    expect(rootLane.className.split(/\s+/)).toEqual(expect.arrayContaining(['flex', 'flex-col', 'flex-1', 'min-h-0']))
    const bucket = hint(container).parentElement as HTMLElement
    expect([...rootLane.children]).toEqual([folderBlock(container, 'fa'), folderBlock(container, 'fb'), bucket])
    // `flex-1` is what makes the bucket the whole-lower-area drop target.
    expect(bucket.className.split(/\s+/)).toEqual(expect.arrayContaining(['flex-1', 'min-h-0']))
    // The off-mode bucket carries exactly one attribute, its class list.
    expect([...bucket.attributes].map(a => a.name)).toEqual(['class'])
    expect(bucket.className).toBe('flex flex-col flex-1 min-h-0 rounded-md transition-all ')
    expect(dnd.mounted.get('root-group')).toBe(1)
    expect(dnd.data.get('root-group')).toEqual({ type: 'folder-drop', folderId: null })
    expect(dnd.mounted.has('root-lane-tail')).toBe(false)
    expect(before(row(container, 'u1'), row(container, 'u2'))).toBe(true)
    expect(before(row(container, 'u2'), expander(container))).toBe(true)
    expect(before(expander(container), hint(container))).toBe(true)
    expect(bucket.contains(row(container, 'u1'))).toBe(true)
    expect(bucket.contains(expander(container))).toBe(true)
    expect(bucket.lastElementChild).toBe(hint(container))
    expect(folderBlock(container, 'fa').contains(row(container, 'f1'))).toBe(true)
    expect(before(row(container, 'f2'), row(container, 'u1'))).toBe(true)
  })

  it('renders an explicit false, and any stored value other than true, exactly as an absent key', async () => {
    const absent = laneSignature((await renderSidebar(EVERYTHING, FOLDERS, undefined)).container)
    expect(absent.length).toBeGreaterThan(0)
    for (const stored of [false, 'true', 1, null]) {
      expect(laneSignature((await renderSidebar(EVERYTHING, FOLDERS, stored)).container), String(stored)).toEqual(absent)
    }
  })
})

describe('with the setting on, unfiled sessions sit above the folders', () => {
  it('draws the bucket first at natural height, the folders next, and a lane tail below them holding the Older Sessions hint', async () => {
    const { container } = await renderSidebar(EVERYTHING, FOLDERS, true)
    const bucket = container.querySelector('[data-testid="root-group"]') as HTMLElement
    const tail = container.querySelector('[data-testid="root-lane-tail"]') as HTMLElement
    expect(bucket).toBeTruthy()
    expect(tail).toBeTruthy()
    expect(dnd.mounted.get('root-group')).toBe(1)
    expect(dnd.mounted.get('root-lane-tail')).toBe(1)
    expect(dnd.data.get('root-group')).toEqual({ type: 'folder-drop', folderId: null })
    expect(dnd.data.get('root-lane-tail')).toEqual({ type: 'folder-drop', folderId: null })
    expect(before(row(container, 'u1'), folderBlock(container, 'fa'))).toBe(true)
    expect(before(row(container, 'u2'), folderBlock(container, 'fa'))).toBe(true)
    expect(before(folderBlock(container, 'fb'), tail)).toBe(true)
    expect(bucket.className.split(/\s+/)).not.toContain('flex-1')
    expect(tail.className.split(/\s+/)).toEqual(expect.arrayContaining(['flex-1', 'min-h-0']))
    // The dormant section stays with the unfiled rows. The hint moves to the
    // tail, still after the dormant expander and still the lane's last row
    // (no crew groups in this fixture).
    expect(bucket.contains(expander(container))).toBe(true)
    expect(tail.contains(hint(container))).toBe(true)
    expect(before(expander(container), hint(container))).toBe(true)
    expect(tail.lastElementChild).toBe(hint(container))
    const rootLane = lane(container).children[1] as HTMLElement
    expect([...rootLane.children]).toEqual([bucket, folderBlock(container, 'fa'), folderBlock(container, 'fb'), tail])
  })

  it('renders no bucket when nothing is unfiled, and the tail still carries the lane end', async () => {
    const { container } = await renderSidebar(FILED, FOLDERS, true)
    expect(container.querySelector('[data-testid="root-group"]')).toBeNull()
    expect(dnd.mounted.has('root-group')).toBe(false)
    expect(dnd.mounted.get('root-lane-tail')).toBe(1)
    const tail = container.querySelector('[data-testid="root-lane-tail"]') as HTMLElement
    expect(tail).toBeTruthy()
    expect(tail.contains(hint(container))).toBe(true)
    expect(before(folderBlock(container, 'fb'), tail)).toBe(true)
  })

  it('removes the bucket while a search matches no unfiled session, and draws it again when the search clears', async () => {
    const r = await renderSidebar(EVERYTHING, FOLDERS, true)
    const { container } = r
    expect(container.querySelector('[data-testid="root-group"]')).toBeTruthy()
    // One character stays below the backend search threshold. The lane then
    // narrows by its local match and needs no debounce. 'p' matches only
    // "filed in alpha" and its folder, "Alpha work".
    const search = r.getByPlaceholderText('Search sessions…')
    fireEvent.change(search, { target: { value: 'p' } })
    expect(row(container, 'f1')).toBeTruthy()
    expect(row(container, 'u1')).toBeNull()
    expect(container.querySelector('[data-testid="root-group"]')).toBeNull()
    expect(dnd.mounted.has('root-group')).toBe(false)
    expect(container.querySelector('[data-testid="root-lane-tail"]')).toBeTruthy()

    fireEvent.change(search, { target: { value: '' } })
    expect(row(container, 'u1')).toBeTruthy()
    expect(container.querySelector('[data-testid="root-group"]')).toBeTruthy()
    expect(dnd.mounted.get('root-group')).toBe(1)
  })
})

describe('the Folder order section offers the setting as a checkbox row', () => {
  it('shows the row unchecked, flips it and reorders the lane on the click before the save lands, and keeps the menu open', async () => {
    let land: () => void = () => {}
    patchConfig.mockImplementationOnce((path: string, value: unknown) => new Promise(resolve => {
      land = () => { serverConfig.dashboard.unfiled_sessions_above_folders = value; resolve({ ok: true }) }
    }))
    const r = await renderSidebar(EVERYTHING, FOLDERS, undefined)
    const { container } = r
    expect(before(folderBlock(container, 'fb'), row(container, 'u1'))).toBe(true)
    openMenu(r)
    const rowEl = await waitFor(() => { const t = toggle(container); expect(t).toBeTruthy(); return t as HTMLElement })
    expect(rowEl.getAttribute('role')).toBe('menuitemcheckbox')
    expect(rowEl.getAttribute('aria-checked')).toBe('false')
    expect(rowEl.textContent).toContain('Unfiled sessions above folders')
    fireEvent.click(rowEl)
    await waitFor(() => expect(patchConfig).toHaveBeenCalledWith(PATH, true))
    await waitFor(() => expect(before(row(container, 'u1'), folderBlock(container, 'fa'))).toBe(true))
    expect(toggle(container)?.getAttribute('aria-checked')).toBe('true')
    // A checkbox row keeps the menu open, as the tag and model rows do.
    expect(toggle(container)).toBeTruthy()
    await act(async () => { land() })
    await waitFor(() => expect(r.qc.getQueryData<{ dashboard?: Record<string, unknown> }>(['kirocrewConfig'])?.dashboard?.unfiled_sessions_above_folders).toBe(true))
    expect(before(row(container, 'u1'), folderBlock(container, 'fa'))).toBe(true)
  })

  it('two clicks during one in-flight save end with the setting off, stored and shown', async () => {
    let land: () => void = () => {}
    patchConfig.mockImplementationOnce((path: string, value: unknown) => new Promise(resolve => {
      land = () => { serverConfig.dashboard.unfiled_sessions_above_folders = value; resolve({ ok: true }) }
    }))
    const r = await renderSidebar(EVERYTHING, FOLDERS, false)
    const { container } = r
    openMenu(r)
    const rowEl = await waitFor(() => { const t = toggle(container); expect(t).toBeTruthy(); return t as HTMLElement })
    fireEvent.click(rowEl)
    await waitFor(() => expect(patchConfig).toHaveBeenCalledWith(PATH, true))
    // The second click negates the SHOWN value (now true), not the committed one.
    fireEvent.click(toggle(container) as HTMLElement)
    await act(async () => { await Promise.resolve() })
    expect(patchConfig).toHaveBeenCalledTimes(1)
    expect(toggle(container)?.getAttribute('aria-checked')).toBe('false')
    await act(async () => { land() })
    await waitFor(() => expect(patchConfig).toHaveBeenCalledWith(PATH, false))
    expect(patchConfig.mock.calls.map(([, v]) => v)).toEqual([true, false])
    await waitFor(() => expect(serverConfig.dashboard.unfiled_sessions_above_folders).toBe(false))
    await waitFor(() => expect(before(folderBlock(container, 'fb'), row(container, 'u1'))).toBe(true))
    expect(toggle(container)?.getAttribute('aria-checked')).toBe('false')
  })

  it('a refused save puts the layout and the row back, says so on its own notice, and the next toggle clears it', async () => {
    const r = await renderSidebar(EVERYTHING, FOLDERS, undefined)
    const { container } = r
    let refuse: () => void = () => {}
    patchConfig.mockImplementationOnce(() => new Promise((_resolve, reject) => {
      refuse = () => reject(new Error('the gateway said no'))
    }))
    openMenu(r)
    const rowEl = await waitFor(() => { const t = toggle(container); expect(t).toBeTruthy(); return t as HTMLElement })
    fireEvent.click(rowEl)
    await waitFor(() => expect(patchConfig).toHaveBeenCalledWith(PATH, true))
    // Flipped and redrawn while the save is in flight.
    await waitFor(() => expect(before(row(container, 'u1'), folderBlock(container, 'fa'))).toBe(true))
    expect(toggle(container)?.getAttribute('aria-checked')).toBe('true')
    await act(async () => { refuse() })
    const notice = await r.findByTestId('unfiled-above-folders-error')
    expect(notice.textContent).toContain("Couldn't save this setting. Nothing changed. Try again.")
    expect(notice.textContent).toContain('the gateway said no')
    expect(r.queryByTestId('folder-action-error')).toBeNull()
    await waitFor(() => expect(before(folderBlock(container, 'fb'), row(container, 'u1'))).toBe(true))
    expect(toggle(container)?.getAttribute('aria-checked')).toBe('false')
    expect(serverConfig.dashboard.unfiled_sessions_above_folders).toBeUndefined()
    fireEvent.click(toggle(container) as HTMLElement)
    await waitFor(() => expect(r.queryByTestId('unfiled-above-folders-error')).toBeNull())
    await waitFor(() => expect(serverConfig.dashboard.unfiled_sessions_above_folders).toBe(true))
  })

  it('is present in a tree with no folders yet, and absent in the flat and conductor lanes', async () => {
    const bare = await renderSidebar(UNFILED, [], undefined)
    openMenu(bare)
    await waitFor(() => expect(toggle(bare.container)).toBeTruthy())
    bare.unmount()
    localStorage.clear()

    const flat = await renderSidebar(EVERYTHING, FOLDERS, true, { lane: 'flat' })
    expect(flat.getByTestId('flat-view-lane')).toBeInTheDocument()
    openMenu(flat)
    await flat.findByTestId('folder-order-custom')
    expect(toggle(flat.container)).toBeNull()
    flat.unmount()
    localStorage.clear()

    // The conductor lane renders only when some row carries a parent.
    const nested = [...UNFILED, { ...slot('w1', 'worker', 1), parent: { key: 'u1', slot: 'u1' } } as unknown as ChatSlot]
    const conductor = await renderSidebar(nested, FOLDERS, true, { lane: 'conductor' })
    expect(conductor.getByTestId('conductor-view-lane')).toBeInTheDocument()
    openMenu(conductor)
    await conductor.findByTestId('folder-order-custom')
    expect(toggle(conductor.container)).toBeNull()
  })
})

describe('the ungroup drop stays reachable with the setting on', () => {
  it('shows the "remove from folder" hint inside the lane tail, below the folders, while a filed session drags and nothing is unfiled', async () => {
    const { container } = await renderSidebar(FILED, FOLDERS, true)
    expect(container.textContent).not.toContain('Drop here to remove from folder')
    startSessionDrag('f1')
    const tail = container.querySelector('[data-testid="root-lane-tail"]') as HTMLElement
    await waitFor(() => expect(tail.textContent).toContain('Drop here to remove from folder'))
    expect(before(folderBlock(container, 'fb'), tail)).toBe(true)
    expect(container.querySelector('[data-testid="root-group"]')).toBeNull()
  })

  it('keeps the nested-folder hint in the lane tail during a folder drag, and draws no hint above the folders while a filed session drags past unfiled rows', async () => {
    const HINT = 'Drop here to remove from folder'
    const nested: ChatFolder[] = [...FOLDERS, { id: 'fc', name: 'Gamma work', order: 0, collapsed: false, created_at: 3_000, parent_id: 'fa' }]
    const { container } = await renderSidebar(EVERYTHING, nested, true)
    const bucket = () => container.querySelector('[data-testid="root-group"]') as HTMLElement
    const tail = () => container.querySelector('[data-testid="root-lane-tail"]') as HTMLElement
    expect(container.textContent).not.toContain(HINT)
    startFolderDrag('fc')
    await waitFor(() => expect(container.textContent).toContain(HINT))
    expect(tail().textContent).toContain(HINT)
    expect(bucket().textContent).not.toContain(HINT)
    expect(before(folderBlock(container, 'fb'), tail())).toBe(true)
    act(() => { dnd.active = null; dnd.handlers.onDragEnd?.({ active: { id: 'fc', data: { current: { type: 'folder' } } }, over: null }) })
    await waitFor(() => expect(container.textContent).not.toContain(HINT))
    startSessionDrag('f1')
    await act(async () => { await Promise.resolve() })
    expect(container.textContent).not.toContain(HINT)
    expect(bucket().textContent).not.toContain(HINT)
  })

  it('a drop on the tail or on the bucket, with the data each registered, removes the session from its folder', async () => {
    const dropOn = (key: string, targetId: string) => {
      expect(dnd.mounted.get(targetId)).toBe(1)
      const data = dnd.data.get(targetId)
      expect(data).toBeTruthy()
      endSessionDrag(key, targetId, data as Record<string, unknown>)
    }
    await renderSidebar(EVERYTHING, FOLDERS, true)
    startSessionDrag('f1')
    dropOn('f1', 'root-lane-tail')
    await waitFor(() => expect(setSlotFolder).toHaveBeenCalledWith('f1', null))
    setSlotFolder.mockClear()
    startSessionDrag('f2')
    dropOn('f2', 'root-group')
    await waitFor(() => expect(setSlotFolder).toHaveBeenCalledWith('f2', null))
  })
})

function rect(top: number, bottom: number, left = 245, right = 487): ClientRect {
  return { top, bottom, left, right, width: right - left, height: bottom - top } as ClientRect
}

/** A droppable whose node is nested under `host`, so containment ranking sees the
 *  real ancestor/descendant relationship (the shape of dndCollisionDepth.test.tsx). */
function droppable(id: string, r: ClientRect, host: HTMLElement, data: Record<string, unknown>): DroppableContainer {
  const node = document.createElement('div')
  host.appendChild(node)
  return {
    id, key: id, data: { current: data }, rect: { current: r }, node: { current: node }, disabled: false,
  } as unknown as DroppableContainer
}

const winner = (collisions: Collision[]) => (collisions[0] ? String(collisions[0].id) : null)

describe('sidebarCollision with the bucket above the folders', () => {
  it('resolves a session pointer over the top rows to the bucket, on a folder to that folder, and below the folders to the tail', () => {
    // Pins only the ranking `sidebarCollision` gives these four targets at this
    // layout's measured geometry. The rendered lane's own ids and drop data are
    // pinned by the DOM cases above.
    const laneEl = document.createElement('div')
    document.body.appendChild(laneEl)
    // The lane (139..838) wraps, in order, the bucket (139..260), one folder
    // block (260..600) and the tail (600..838). All four are DOM descendants of
    // the lane node, as in the rendered tree.
    const laneC = droppable('root-lane', rect(139, 838), laneEl, { type: 'folder-drop', folderId: null })
    const laneNode = laneC.node.current as HTMLElement
    const bucketHost = document.createElement('div'); laneNode.appendChild(bucketHost)
    const bucket = droppable('root-group', rect(139, 260), bucketHost, { type: 'folder-drop', folderId: null })
    const folderHost = document.createElement('div'); laneNode.appendChild(folderHost)
    const folder = droppable('folder-drop:fa', rect(260, 600), folderHost, { type: 'folder-drop', folderId: 'fa' })
    const tailHost = document.createElement('div'); laneNode.appendChild(tailHost)
    const tail = droppable('root-lane-tail', rect(600, 838), tailHost, { type: 'folder-drop', folderId: null })
    const containers = [laneC, bucket, folder, tail]
    const droppableRects = new Map<string, ClientRect>(containers.map(c => [c.id as string, c.rect.current as ClientRect]))
    const args = (pointer: { x: number; y: number }): Parameters<CollisionDetection>[0] => ({
      active: { id: 'f1', data: { current: { type: 'session', key: 'f1' } }, rect: { current: { initial: null, translated: null } } },
      collisionRect: rect(pointer.y, pointer.y + 20, pointer.x, pointer.x + 40),
      droppableRects,
      droppableContainers: containers,
      pointerCoordinates: pointer,
    } as unknown as Parameters<CollisionDetection>[0])
    expect(winner(sidebarCollision(args({ x: 366, y: 200 })))).toBe('root-group')
    expect(winner(sidebarCollision(args({ x: 366, y: 420 })))).toBe('folder-drop:fa')
    expect(winner(sidebarCollision(args({ x: 366, y: 720 })))).toBe('root-lane-tail')
    laneEl.remove()
  })
})
