/**
 * Board view (tag-columns) folder rename: renderColumnFolder renders the folder
 * name with an editingId branch so both the ⋯-menu "Rename" item and a
 * double-click reveal an inline edit input, matching the list-view header
 * (renderFolderHeader).
 *
 * Radix DropdownMenu can't be opened in jsdom (needs PointerEvent), so the
 * load-bearing path here is double-click → inline input → Enter commit.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent, waitFor, within } from '@testing-library/react'
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

const mocks = vi.hoisted(() => ({ updateChatFolder: vi.fn() }))

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
const columns: TagColumn[] = [{ id: COL_A, name: 'Review', tag_ids: [REVIEW], mode: 'any', order: 0 }]
const folders: ChatFolder[] = [{ id: FOLDER_ID, name: 'CDF', order: 0 }]

function renderWith(cols: TagColumn[]) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: false, slots: [], approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-tags'], tags)
  qc.setQueryData(['tag-columns'], cols)
  qc.setQueryData(['chat-folders'], folders)
  return render(
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
}

function renderSidebar() {
  return renderWith(columns)
}

// Two columns: every root folder renders in BOTH, which is what exposes the
// per-column edit-scope bug.
function renderMultiColumn() {
  return renderWith([
    { id: COL_A, name: 'Review', tag_ids: [REVIEW], mode: 'any', order: 0 },
    { id: COL_B, name: 'Oncall', tag_ids: [ONCALL], mode: 'any', order: 1 },
  ])
}

function colHeader(container: HTMLElement, colId: string): HTMLElement {
  const el = container.querySelector(`[data-testid="col-${colId}-folder-${FOLDER_ID}"]`)
  expect(el).toBeTruthy()
  return el as HTMLElement
}

function colFolderHeader(container: HTMLElement): HTMLElement {
  return colHeader(container, COL_A)
}

beforeEach(() => {
  localStorage.clear()
  mocks.updateChatFolder.mockResolvedValue({})
})
afterEach(() => vi.clearAllMocks())

describe('board view: folder rename', () => {
  it('shows no rename input until the folder name is double-clicked', () => {
    const { container } = renderSidebar()
    const header = colFolderHeader(container)
    expect(within(header).queryByRole('textbox')).toBeNull()
  })

  it('reveals an inline input pre-filled with the folder name on double-click', () => {
    const { container } = renderSidebar()
    const header = colFolderHeader(container)
    fireEvent.doubleClick(within(header).getByText('CDF'))
    const input = within(colFolderHeader(container)).getByRole('textbox') as HTMLInputElement
    expect(input).toBeTruthy()
    expect(input.value).toBe('CDF')
  })

  it('commits the new name via Enter, persisting it through updateChatFolder', async () => {
    const { container } = renderSidebar()
    fireEvent.doubleClick(within(colFolderHeader(container)).getByText('CDF'))
    const input = within(colFolderHeader(container)).getByRole('textbox')
    fireEvent.change(input, { target: { value: 'Renamed Folder' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(mocks.updateChatFolder).toHaveBeenCalledWith(FOLDER_ID, { name: 'Renamed Folder' }))
  })

  // Regression: every root folder renders in EVERY column, so an edit gated only
  // by folder.id opened the rename input in all columns at once and the shared
  // ref bound to the last one — the caret landed in the wrong column. The edit
  // is scoped to the clicked column (editScope === columnId), so exactly one
  // input mounts, in the column that was clicked.
  it('scopes the rename input to the clicked column, not every column', () => {
    const { container } = renderMultiColumn()
    // Confidence check: the folder renders in both columns before editing.
    expect(colHeader(container, COL_A)).toBeTruthy()
    expect(colHeader(container, COL_B)).toBeTruthy()

    fireEvent.doubleClick(within(colHeader(container, COL_A)).getByText('CDF'))

    // Column A (clicked) shows the input; column B does not.
    expect(within(colHeader(container, COL_A)).getByRole('textbox')).toBeTruthy()
    expect(within(colHeader(container, COL_B)).queryByRole('textbox')).toBeNull()
    // And exactly one rename input (value 'CDF') exists across the whole board —
    // the pre-fix bug rendered it in every column at once.
    const renameInputs = Array.from(container.querySelectorAll('input'))
      .filter(i => (i as HTMLInputElement).value === 'CDF')
    expect(renameInputs.length).toBe(1)
  })
})
