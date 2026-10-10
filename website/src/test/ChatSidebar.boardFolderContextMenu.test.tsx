/**
 * Board view (tag-columns) folder right-click: the header row of a column
 * folder opens the same items its ⋯ button does, at the pointer, matching the
 * list-view row (renderFolderHeader).
 *
 * Radix DropdownMenu can't be opened in jsdom (needs PointerEvent), so the ⋯
 * copy is only asserted closed; the context menu opens on a plain
 * `contextmenu` event and is driven directly.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import type { RootState } from '../store'
import { ThemeProvider } from '../hooks/useTheme'
import type { ChatTag, TagColumn, ChatFolder } from '../types'

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
  loadChatConfig: () => ({ tagColumnsEnabled: true, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

const mocks = vi.hoisted(() => ({ updateChatFolder: vi.fn(), deleteChatFolder: vi.fn() }))

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as Record<string, unknown>, {
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

import ChatSidebar from '../pages/ChatSidebar'

const REVIEW = '22222222-2222-2222-2222-222222222222'
const ONCALL = '33333333-3333-3333-3333-333333333333'
const COL_A = 'col-aaaa'
const COL_B = 'col-bbbb'
const FOLDER_ID = 'folder-zzzz'

const tags: ChatTag[] = [
  { id: REVIEW, name: 'Review', color: '#1a1', order: 0, status: true },
  { id: ONCALL, name: 'Oncall', color: '#a11', order: 1, status: true },
]
// Two columns: every root folder renders in BOTH, so each test can tell the
// clicked column's copy from the other one.
const columns: TagColumn[] = [
  { id: COL_A, name: 'Review', tag_ids: [REVIEW], mode: 'any', order: 0 },
  { id: COL_B, name: 'Oncall', tag_ids: [ONCALL], mode: 'any', order: 1 },
]
const folders: ChatFolder[] = [{ id: FOLDER_ID, name: 'CDF', order: 0 }]

async function renderSidebar() {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: false, slots: [], approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null } as unknown as RootState['chat'],
  })
  // `staleTime: Infinity` keeps the seeded lists: the mocked api answers every
  // refetch with `[]`, which would empty the board once the mount settles.
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } } })
  qc.setQueryData(['chat-tags'], tags)
  qc.setQueryData(['tag-columns'], columns)
  qc.setQueryData(['chat-folders'], folders)
  const view = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={[]} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  // ThemeProvider applies its boot query after mount; settle it here so no
  // state update lands after a test's last assertion.
  await waitFor(() => expect(qc.getQueryState(['theme-boot'])?.status).toBe('success'))
  return view
}

function colFolder(container: HTMLElement, colId: string): HTMLElement {
  const el = container.querySelector(`[data-testid="col-${colId}-folder-${FOLDER_ID}"]`)
  expect(el).toBeTruthy()
  return el as HTMLElement
}

/** The header row inside the folder block: the right-click target. The
 *  data-testid wrapper around it is the drop zone and also holds the body. */
function colFolderRow(container: HTMLElement, colId: string): HTMLElement {
  const row = colFolder(container, colId).querySelector('[role="button"][aria-expanded]')
  expect(row).toBeTruthy()
  return row as HTMLElement
}

const ctxItem = (colId: string, name: string) => `col-${colId}-folder-${FOLDER_ID}-${name}-ctx`

beforeEach(() => {
  localStorage.clear()
  mocks.updateChatFolder.mockResolvedValue({})
  mocks.deleteChatFolder.mockResolvedValue({})
})
afterEach(() => {
  vi.clearAllMocks()
  vi.unstubAllGlobals()
})

describe('board view: folder right-click menu', () => {
  it('opens the ⋯ menu items on a right-click of the header row', async () => {
    const { container } = await renderSidebar()
    expect(screen.queryByTestId(ctxItem(COL_A, 'rename'))).toBeNull()

    fireEvent.contextMenu(colFolderRow(container, COL_A), { clientX: 40, clientY: 20 })

    for (const name of ['rename', 'new-subfolder', 'new-ephemeral', 'settings', 'delete']) {
      expect(screen.getByTestId(ctxItem(COL_A, name))).toBeTruthy()
    }
    // The ⋯ copy stays closed, and the other column's copy does not open.
    expect(screen.queryByTestId(`col-${COL_A}-folder-${FOLDER_ID}-settings`)).toBeNull()
    expect(screen.queryByTestId(ctxItem(COL_B, 'rename'))).toBeNull()
    // The two hide items belong to the list-view row only.
    expect(screen.queryByTestId(ctxItem(COL_A, 'visibility'))).toBeNull()
    expect(screen.queryByTestId(ctxItem(COL_A, 'hide'))).toBeNull()
  })

  it('renames from the right-click menu in the clicked column only', async () => {
    const { container } = await renderSidebar()
    fireEvent.contextMenu(colFolderRow(container, COL_B), { clientX: 40, clientY: 20 })
    fireEvent.click(screen.getByTestId(ctxItem(COL_B, 'rename')))

    const input = within(colFolder(container, COL_B)).getByRole('textbox')
    expect(within(colFolder(container, COL_A)).queryByRole('textbox')).toBeNull()
    fireEvent.change(input, { target: { value: 'ViaRightClick' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(mocks.updateChatFolder).toHaveBeenCalledWith(FOLDER_ID, { name: 'ViaRightClick' }))
  })

  it('keeps the browser menu for the row while its name is being edited', async () => {
    const { container } = await renderSidebar()
    fireEvent.doubleClick(within(colFolder(container, COL_A)).getByText('CDF'))

    const input = within(colFolder(container, COL_A)).getByRole('textbox')
    expect(fireEvent.contextMenu(input, { clientX: 40, clientY: 20 })).toBe(true)
    expect(screen.queryByTestId(ctxItem(COL_A, 'rename'))).toBeNull()
  })

  it('deletes only through the same confirm as the ⋯ menu', async () => {
    const confirmFn = vi.fn().mockReturnValue(false)
    vi.stubGlobal('confirm', confirmFn)
    const { container } = await renderSidebar()

    fireEvent.contextMenu(colFolderRow(container, COL_A), { clientX: 40, clientY: 20 })
    fireEvent.click(screen.getByTestId(ctxItem(COL_A, 'delete')))
    expect(confirmFn).toHaveBeenCalledWith('Delete “CDF”? Sessions will be ungrouped.')
    expect(mocks.deleteChatFolder).not.toHaveBeenCalled()

    confirmFn.mockReturnValue(true)
    fireEvent.contextMenu(colFolderRow(container, COL_A), { clientX: 40, clientY: 20 })
    fireEvent.click(screen.getByTestId(ctxItem(COL_A, 'delete')))
    await waitFor(() => expect(mocks.deleteChatFolder).toHaveBeenCalledWith(FOLDER_ID))
  })
})
