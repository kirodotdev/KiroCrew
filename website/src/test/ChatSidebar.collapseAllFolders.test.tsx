/**
 * Collapse-all in the sessions sidebar: one filter-menu row that closes every
 * open folder, so a tree taller than the sidebar can be reset without walking
 * it.
 *
 * Four things are worth pinning, and each has a way of silently regressing:
 *
 *  1. It writes one `PATCH {collapsed: true}` per OPEN folder and leaves the
 *     already-closed ones alone. There is no bulk `collapsed` endpoint, so the
 *     N-request shape is deliberate; what must not drift is re-closing folders
 *     that were already shut, which costs a store write and an audit row each.
 *  2. The row flips which way it points: collapse while anything is open,
 *     expand once everything is shut. A collapse-all with no way back is a
 *     trap, and two rows would mean one of them is always a dead click.
 *  3. It lives in the MENU, not the search field's control row, because
 *     `max-two-buttons-per-row` (`website/AUTOSDE.yaml`, blocking) caps that
 *     row at two and the lane toggle plus this menu's trigger already fill it.
 *  4. Board columns keep per-column overrides in localStorage, layered over
 *     the server flag. The override sweep covers EVERY folder, not just the
 *     ones written: a folder whose flag already says collapsed can still be
 *     held open in a column by an expanded override, needs no PATCH, and would
 *     be skipped by a loop over the open set — leaving it open with the row
 *     offering only expand, so nothing could shut it.
 *
 * `staleTime: Infinity` + `refetchOnMount: false` are load-bearing: the api mock
 * resolves `chatFolders()` to `[]`, so an on-mount refetch would empty the tree
 * and let an assertion pass for the wrong reason.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

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

const chatConfig = { tagColumnsEnabled: false, confirmCloseSession: false }
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => chatConfig,
  saveChatConfig: vi.fn(),
}))

/** Records the folder PATCHes the action issues. */
const updateChatFolder = vi.fn().mockResolvedValue({})

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, prop: string) => {
      if (prop === 'updateChatFolder') return updateChatFolder
      if (prop === 'chatTags') return vi.fn().mockResolvedValue([])
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
import type { ChatFolder, ChatSlot, TagColumn } from '../types'

/** Two open folders with a nested one between them, and one already closed —
 *  the closed one is what proves the action does not re-write what is done. */
const FOLDERS: ChatFolder[] = [
  { id: 'f-open', name: 'Sydney Property', collapsed: false, order: 0 },
  { id: 'f-nested', name: 'Inspections', collapsed: false, order: 0, parent_id: 'f-open' },
  { id: 'f-shut', name: 'Archive', collapsed: true, order: 1 },
]

/** One board column, so an override can sit in a column that is actually drawn. */
const BOARD_COLUMNS: TagColumn[] = [
  { id: 'col-a', name: 'Planned', tag_ids: [], mode: 'any', order: 0 } as unknown as TagColumn,
]

const SLOTS: ChatSlot[] = [
  { key: 'k-open', title: 'mortgage numbers', running: false, messages: 2, folder_id: 'f-open' },
  { key: 'k-nested', title: 'saturday walkthrough', running: false, messages: 2, folder_id: 'f-nested' },
] as unknown as ChatSlot[]

function renderSidebar(folders: ChatFolder[] = FOLDERS, columns: TagColumn[] = []) {
  const defaults = createTestStore().getState()
  const store = createTestStore({
    dashboard: {
      ...defaults.dashboard,
      status: {}, connected: true, slots: SLOTS, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: {
      ...defaults.chat,
      activeSlot: null, slotStatusDetail: {},
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: {
    queries: { retry: false, staleTime: Infinity, refetchOnMount: false }, mutations: { retry: false },
  } })
  qc.setQueryData(['chat-folders'], folders)
  chatConfig.tagColumnsEnabled = columns.length > 0
  qc.setQueryData(['tag-columns'], columns)
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={SLOTS} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

async function openFilterMenu(utils: ReturnType<typeof renderSidebar>) {
  fireEvent.keyDown(utils.getByLabelText('Sort and filter sessions'), { key: 'Enter' })
  await utils.findByText('Filter')
}

beforeEach(() => { localStorage.clear(); updateChatFolder.mockClear() })
afterEach(() => vi.clearAllMocks())

describe('collapse all folders', () => {
  it('closes every open folder and leaves an already-closed one alone', async () => {
    const utils = renderSidebar()
    await openFilterMenu(utils)
    fireEvent.click(await utils.findByTestId('folder-collapse-all'))
    await waitFor(() => expect(updateChatFolder).toHaveBeenCalledWith('f-open', { collapsed: true }))
    expect(updateChatFolder).toHaveBeenCalledWith('f-nested', { collapsed: true })
    // The closed folder is not re-written: each PATCH costs a folder-store write
    // and an audit row, so a no-op write is a real cost, not just noise.
    expect(updateChatFolder).not.toHaveBeenCalledWith('f-shut', { collapsed: true })
    expect(updateChatFolder).toHaveBeenCalledTimes(2)
  })

  it('is a labelled menu row, not a third button in the search field', async () => {
    // `max-two-buttons-per-row` (website/AUTOSDE.yaml, blocking) caps that row
    // at two, and the lane toggle plus this menu's own trigger already fill it.
    // The label is what the menu buys: the icon never has to be read alone.
    const utils = renderSidebar()
    await openFilterMenu(utils)
    const row = await utils.findByTestId('folder-collapse-all')
    expect(row.getAttribute('role')).toBe('menuitem')
    expect(row.textContent).toBe('Collapse all folders')
    expect(row.getAttribute('data-folder-disclosure')).toBe('collapse')
    // The field keeps its two-control inset, unchanged from base.
    expect(utils.getByPlaceholderText('Search sessions…').style.paddingRight).toBe('56px')
  })

  it('points the other way, and expands, once every folder is closed', async () => {
    const utils = renderSidebar(FOLDERS.map(f => ({ ...f, collapsed: true })))
    await openFilterMenu(utils)
    const row = await utils.findByTestId('folder-collapse-all')
    // Still offered — a collapse-all with no way back is a trap — but now the
    // opposite action, and the label follows the action, not the tree.
    expect(row.textContent).toBe('Expand all folders')
    expect(row.getAttribute('data-folder-disclosure')).toBe('expand')

    fireEvent.click(row)
    await waitFor(() => expect(updateChatFolder).toHaveBeenCalledWith('f-open', { collapsed: false }))
    expect(updateChatFolder).toHaveBeenCalledWith('f-nested', { collapsed: false })
    expect(updateChatFolder).toHaveBeenCalledWith('f-shut', { collapsed: false })
    expect(updateChatFolder).toHaveBeenCalledTimes(3)
  })

  it('offers collapse while anything is open, even with most of the tree closed', async () => {
    // One open folder among closed ones still means the useful press is the
    // collapse: the row follows "is there anything to close", not a majority.
    const utils = renderSidebar([
      { id: 'f-open', name: 'Sydney Property', collapsed: false, order: 0 },
      { id: 'f-shut', name: 'Archive', collapsed: true, order: 1 },
      { id: 'f-shut2', name: 'Old', collapsed: true, order: 2 },
    ])
    await openFilterMenu(utils)
    const row = await utils.findByTestId('folder-collapse-all')
    expect(row.getAttribute('data-folder-disclosure')).toBe('collapse')
    fireEvent.click(row)
    await waitFor(() => expect(updateChatFolder).toHaveBeenCalledWith('f-open', { collapsed: true }))
    expect(updateChatFolder).toHaveBeenCalledTimes(1)
  })

  it('is not offered when the tree has no folders', async () => {
    const utils = renderSidebar([])
    await openFilterMenu(utils)
    expect(utils.queryByTestId('folder-collapse-all')).toBeNull()
    expect(updateChatFolder).not.toHaveBeenCalled()
  })

  it('is not offered in the flat lane, which draws no folder disclosures', async () => {
    // Flat view explodes every chat out of its folder, so there is nothing on
    // screen for a collapse to move. Pressing it would rewrite stored state the
    // user cannot see change — a row that looks broken.
    localStorage.setItem('mc-sidebar-lane', 'flat')
    const utils = renderSidebar()
    await openFilterMenu(utils)
    expect(utils.queryByTestId('folder-collapse-all')).toBeNull()
  })

  it('comes back when the lane returns to the tree', async () => {
    // The same render path, one key apart: this is what proves the flat-lane
    // absence above is the lane gate and not some unrelated render failure.
    localStorage.setItem('mc-sidebar-lane', 'tree')
    const utils = renderSidebar()
    await openFilterMenu(utils)
    expect(await utils.findByTestId('folder-collapse-all')).toBeTruthy()
  })

  it('clears the override holding a folder open even when that folder needs no PATCH', async () => {
    // The case a loop over the OPEN set misses: `f-shut`'s server flag already
    // says collapsed, so collapse-all writes nothing for it, but a column's
    // expanded override still renders it open. Skipping it would leave it open
    // with the row now offering only expand, so nothing could shut it.
    localStorage.setItem('kc-board-folder-collapsed:col-a:f-shut', '0')
    localStorage.setItem('kc-board-folder-collapsed:col-b:f-open', '0')
    localStorage.setItem('kc-board-folder-collapsed:col-c:f-nested', '1')
    const utils = renderSidebar()
    await openFilterMenu(utils)
    fireEvent.click(await utils.findByTestId('folder-collapse-all'))
    await waitFor(() => expect(localStorage.getItem('kc-board-folder-collapsed:col-a:f-shut')).toBeNull())
    expect(localStorage.getItem('kc-board-folder-collapsed:col-b:f-open')).toBeNull()
    // A collapsed override already agrees with the write, so it is left alone:
    // dropping it would hand the column back to a flag that may yet roll back.
    expect(localStorage.getItem('kc-board-folder-collapsed:col-c:f-nested')).toBe('1')
  })

  it('offers collapse when a RENDERED column holds a folder open against a collapsed flag', async () => {
    // Every server flag says collapsed, so the flag count is 0 — but the board
    // column on screen draws one folder OPEN via its override, and that is what
    // the person sees. A flag-only reading would offer to expand everything at
    // the moment that one folder is what they want shut.
    localStorage.setItem('kc-board-folder-collapsed:col-a:f-open', '0')
    const utils = renderSidebar(FOLDERS.map(f => ({ ...f, collapsed: true })), BOARD_COLUMNS)
    await openFilterMenu(utils)
    const row = await utils.findByTestId('folder-collapse-all')
    expect(row.getAttribute('data-folder-disclosure')).toBe('collapse')
    expect(row.textContent).toBe('Collapse all folders')
  })

  it('ignores an override whose column is no longer rendered', async () => {
    // Overrides are keyed by (column, folder) and nothing prunes them when a
    // column or folder is deleted. Reading the stored map instead of the drawn
    // columns would latch this row on "collapse" forever and make expand
    // unreachable — worse than the bug that reading drew, because it never clears.
    localStorage.setItem('kc-board-folder-collapsed:col-deleted:f-open', '0')
    const utils = renderSidebar(FOLDERS.map(f => ({ ...f, collapsed: true })), BOARD_COLUMNS)
    await openFilterMenu(utils)
    const row = await utils.findByTestId('folder-collapse-all')
    expect(row.getAttribute('data-folder-disclosure')).toBe('expand')
  })

  it('expanding clears the collapsed overrides holding folders shut', async () => {
    // Only reachable with NO expanded override anywhere: one would mean a column
    // draws some folder open, which makes the row offer collapse instead. So
    // every flag here is collapsed and both seeded overrides are the collapsed
    // kind — the ones `clearBoardCollapse` has to drop for the expand to show.
    localStorage.setItem('kc-board-folder-collapsed:col-a:f-open', '1')
    localStorage.setItem('kc-board-folder-collapsed:col-b:f-shut', '1')
    const utils = renderSidebar(FOLDERS.map(f => ({ ...f, collapsed: true })))
    await openFilterMenu(utils)
    const row = await utils.findByTestId('folder-collapse-all')
    expect(row.getAttribute('data-folder-disclosure')).toBe('expand')

    fireEvent.click(row)
    await waitFor(() => expect(localStorage.getItem('kc-board-folder-collapsed:col-a:f-open')).toBeNull())
    expect(localStorage.getItem('kc-board-folder-collapsed:col-b:f-shut')).toBeNull()
  })
})
