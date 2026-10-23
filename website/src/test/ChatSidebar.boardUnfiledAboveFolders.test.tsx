/**
 * Board view: unfiled cards above the folder blocks, on request.
 *
 * `dashboard.unfiled_sessions_above_folders` (default off) moves a column's
 * cards that are filed in no folder from below its folder blocks to above
 * them. With it off the column is today's (the characterization case was green
 * before the lane changed). The unfile strip at the column foot and the column
 * body's own drop are untouched either way. The flat board draws no folders, so
 * the setting is inert there and its menu row is hidden.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { useAppSelector } from '../store'
import { ThemeProvider } from '../hooks/useTheme'
import type { ChatTag, TagColumn, ChatFolder, Slot } from '../types'
import type { RootState } from '../store'

const mocks = vi.hoisted(() => ({
  setSlotFolder: vi.fn(),
  dropSlotToColumn: vi.fn(),
  tagColumns: vi.fn(),
  chatTags: vi.fn(),
  chatFolders: vi.fn(),
  kirocrewConfig: vi.fn(),
  patchConfig: vi.fn(),
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

const TAG = '22222222-2222-2222-2222-222222222222'
const COL = 'col-blocked'
const FILED_KEY = 'chat-filed-1'
const UNFILED_KEY = 'chat-unfiled-1'
const ARCHIVE = 'folder-archive'
const UNFILE_TEXT = 'Drop here to remove from folder'

const tags: ChatTag[] = [{ id: TAG, name: 'Blocked', color: '#e11', order: 0, status: true }]
const columns: TagColumn[] = [{ id: COL, name: 'Blocked', tag_ids: [TAG], mode: 'any', order: 0 }]
const folders: ChatFolder[] = [{ id: ARCHIVE, name: 'Archive', order: 0 }]

/** The gateway's config as the fake server holds it. The board's queries refetch
 *  on mount, so the mock must answer with the value the cache was seeded with. */
const serverConfig: { dashboard: Record<string, unknown> } = { dashboard: {} }

async function renderSidebar(unfiledAbove: unknown, opts: { flat?: boolean } = {}) {
  if (opts.flat) localStorage.setItem('mc-sidebar-lane', 'flat')
  const filed = {
    key: FILED_KEY, title: 'A session that lives in Archive', messages: 0,
    running: false, tags: [TAG], created: '', last_ts: '', folder_id: ARCHIVE,
  } as Slot
  const unfiled = {
    key: UNFILED_KEY, title: 'A session in no folder', messages: 0,
    running: false, tags: [TAG], created: '', last_ts: '', folder_id: undefined,
  } as Slot
  const store = createTestStore({
    dashboard: {
      status: {}, connected: false, slots: [filed, unfiled], slotsLoaded: true, approvalMode: 'normal',
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
  serverConfig.dashboard = unfiledAbove === undefined ? {} : { unfiled_sessions_above_folders: unfiledAbove }
  qc.setQueryData(['kirocrewConfig'], { dashboard: { ...serverConfig.dashboard } })
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
  // The theme provider sets state when its boot read resolves through the api
  // mock. Those microtasks land here, inside act, so no update follows a
  // case's last barrier.
  await act(async () => { for (let i = 0; i < 10; i += 1) await Promise.resolve() })
  return { ...utils, store, qc }
}

/** A native-DnD stub for one session card: the key rides as text/plain. */
function sessionTransfer(slotKey = FILED_KEY) {
  return {
    types: ['text/plain'],
    effectAllowed: 'uninitialized',
    dropEffect: 'none',
    setData: vi.fn(),
    getData: (t: string) => (t === 'text/plain' ? slotKey : ''),
  }
}

const before = (a: Element, b: Element) => !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING)
const cardIn = (c: HTMLElement, slotKey: string) =>
  c.querySelector(`[data-session-row="${slotKey}"]`) as HTMLElement
const folderBlockIn = (c: HTMLElement) =>
  c.querySelector(`[data-col-folder-sortable="${ARCHIVE}"]`) as HTMLElement | null
const stripIn = (c: HTMLElement) =>
  c.querySelector(`[data-testid="column-unfile-drop-${COL}"]`) as HTMLElement | null
const columnIn = (c: HTMLElement) =>
  c.querySelector(`[data-testid="column-${COL}"]`) as HTMLElement
const toggle = () => document.querySelector('[data-testid="folder-order-unfiled-above"]') as HTMLElement | null

/** The column's structure as a comparable value: every element's tag, classes
 *  and `data-*` attributes in document order. Ids and `aria-*` wiring are left
 *  out because dnd-kit and Radix mint fresh counters per mount. */
function columnSignature(c: HTMLElement): string[] {
  return [...columnIn(c).querySelectorAll('*')].map(el => {
    const data = [...el.attributes].filter(a => a.name.startsWith('data-')).map(a => `${a.name}=${a.value}`).sort()
    return `${el.tagName}|${el.className}|${data.join(',')}`
  })
}

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
  mocks.kirocrewConfig.mockImplementation(async () => ({ dashboard: { ...serverConfig.dashboard } }))
  mocks.patchConfig.mockResolvedValue({ ok: true })
})
afterEach(() => { vi.clearAllMocks() })

describe('board column with the setting off (characterization)', () => {
  it('draws the folder block above the unfiled card', async () => {
    const { container } = await renderSidebar(undefined)
    const block = folderBlockIn(container)
    expect(block, 'the Archive block renders in the column').toBeTruthy()
    const unfiledCard = cardIn(container, UNFILED_KEY)
    expect(unfiledCard).toBeTruthy()
    expect(block!.contains(cardIn(container, FILED_KEY))).toBe(true)
    expect(before(block!, unfiledCard)).toBe(true)
    expect(columnIn(container).contains(unfiledCard)).toBe(true)
  })

  it('renders an explicit false, and any stored value other than true, exactly as an absent key', async () => {
    const absent = columnSignature((await renderSidebar(undefined)).container)
    expect(absent.length).toBeGreaterThan(0)
    for (const stored of [false, 'true', 1, null]) {
      expect(columnSignature((await renderSidebar(stored)).container), String(stored)).toEqual(absent)
    }
  })
})

describe('board column with the setting on', () => {
  it('draws the unfiled card above the folder block', async () => {
    const { container } = await renderSidebar(true)
    const block = folderBlockIn(container)
    const unfiledCard = cardIn(container, UNFILED_KEY)
    expect(block).toBeTruthy()
    expect(unfiledCard).toBeTruthy()
    expect(before(unfiledCard, block!)).toBe(true)
    expect(block!.contains(cardIn(container, FILED_KEY))).toBe(true)
    expect(columnIn(container).contains(unfiledCard)).toBe(true)
  })

  it('still shows the unfile strip for a filed card and unfiles on a drop there', async () => {
    const { container } = await renderSidebar(true)
    expect(stripIn(container)).toBeNull()
    fireEvent.dragStart(cardIn(container, FILED_KEY), { dataTransfer: sessionTransfer() })
    const strip = stripIn(container)
    expect(strip, 'the column renders its unfile strip while the filed card drags').toBeTruthy()
    expect(strip!.textContent).toContain(UNFILE_TEXT)
    const dataTransfer = sessionTransfer()
    fireEvent.dragOver(strip!, { dataTransfer })
    fireEvent.drop(strip!, { dataTransfer })
    await waitFor(() => expect(mocks.setSlotFolder).toHaveBeenCalledWith(FILED_KEY, null))
    expect(mocks.dropSlotToColumn).not.toHaveBeenCalled()
  })

  it('offers the menu row on the board, and hides it on the flat board, which draws no folders', async () => {
    const board = await renderSidebar(true)
    fireEvent.keyDown(board.getByLabelText('Sort and filter sessions'), { key: 'Enter' })
    await waitFor(() => expect(toggle()).toBeTruthy())
    expect(toggle()?.getAttribute('aria-checked')).toBe('true')
    board.unmount()
    localStorage.clear()

    const flat = await renderSidebar(true, { flat: true })
    expect(columnIn(flat.container)).toBeTruthy()
    expect(folderBlockIn(flat.container)).toBeNull()
    expect(cardIn(flat.container, FILED_KEY)).toBeTruthy()
    expect(cardIn(flat.container, UNFILED_KEY)).toBeTruthy()
    fireEvent.keyDown(flat.getByLabelText('Sort and filter sessions'), { key: 'Enter' })
    await flat.findByTestId('folder-order-custom')
    expect(toggle()).toBeNull()
  })
})
