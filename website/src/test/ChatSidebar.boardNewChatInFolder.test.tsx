/**
 * Board view (tag-columns) previously had no way to start a session inside a
 * folder — the list-view folder header has a "New chat in folder" + button
 * (renderFolderHeader) but the compact column folder header (renderColumnFolder)
 * only exposed the ⋯ menu. This adds the same + button to board view.
 *
 * Two load-bearing assertions:
 *   (1) the + button renders inside every column's copy of the folder header
 *       (the feature now exists in board view), and
 *   (2) clicking it creates a slot, assigns it to the folder, AND drops it
 *       into the column it was clicked from — so a status-lane column shows
 *       the new session immediately instead of the untagged slot vanishing
 *       from a tag-filtered column.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { KiroReadinessProvider } from '../providers/KiroReadinessContext'
import type { RootState } from '../store'
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

// Hoisted spies shared between the api mock factory and the test body.
const NEW_KEY = 'chat-new-1'
const mocks = vi.hoisted(() => ({
  createChatSlot: vi.fn(),
  setSlotFolder: vi.fn(),
  dropSlotToColumn: vi.fn(),
  chatSlotProject: vi.fn(),
  setSlotColor: vi.fn(),
}))

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  // Named spies for the create→assign→drop path; everything else resolves [].
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

const BLOCKED = '11111111-1111-1111-1111-111111111111'
const REVIEW = '22222222-2222-2222-2222-222222222222'
const COL_A = 'col-aaaa'
const COL_B = 'col-bbbb'
const FOLDER_ID = 'folder-zzzz'

const tags: ChatTag[] = [
  { id: BLOCKED, name: 'Blocked', color: '#e11', order: 0, status: true },
  { id: REVIEW, name: 'Review', color: '#1a1', order: 1, status: true },
]
const columns: TagColumn[] = [
  { id: COL_A, name: 'Planned/Blocked', tag_ids: [BLOCKED], mode: 'any', order: 0 },
  { id: COL_B, name: 'Review', tag_ids: [REVIEW], mode: 'any', order: 1 },
]
const folders: ChatFolder[] = [{ id: FOLDER_ID, name: 'CDF', order: 0 }]

function renderSidebar(kiroReady = true) {
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
  qc.setQueryData(['tag-columns'], columns)
  qc.setQueryData(['chat-folders'], folders)
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <KiroReadinessProvider ready={kiroReady}>
              <ChatSidebar
                slots={[]} activeSlot={null} unreadSlots={[]}
                history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
              />
            </KiroReadinessProvider>
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  localStorage.clear()
  mocks.createChatSlot.mockResolvedValue({ key: NEW_KEY })
  mocks.setSlotFolder.mockResolvedValue({})
  mocks.dropSlotToColumn.mockResolvedValue({ ok: true })
  mocks.chatSlotProject.mockResolvedValue({})
  mocks.setSlotColor.mockResolvedValue({})
})
afterEach(() => vi.clearAllMocks())

describe('board view: new chat in folder', () => {
  it('renders a "new chat in folder" button in every column copy of the folder', () => {
    const { container } = renderSidebar()
    // The empty folder still renders in each column as a drop target, so the
    // + button must be present under both columns.
    expect(container.querySelector(`[data-testid="col-${COL_A}-folder-${FOLDER_ID}-new-chat"]`)).toBeTruthy()
    expect(container.querySelector(`[data-testid="col-${COL_B}-folder-${FOLDER_ID}-new-chat"]`)).toBeTruthy()
  })

  it('creates a session, assigns it to the folder, and drops it into the clicked column', async () => {
    const { container } = renderSidebar()
    const btn = container.querySelector(`[data-testid="col-${COL_A}-folder-${FOLDER_ID}-new-chat"]`) as HTMLElement
    expect(btn).toBeTruthy()
    fireEvent.click(btn)

    // create → assign folder → drop into the column it was created from.
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(mocks.setSlotFolder).toHaveBeenCalledWith(NEW_KEY, FOLDER_ID))
    await waitFor(() => expect(mocks.dropSlotToColumn).toHaveBeenCalledWith(NEW_KEY, COL_A))
  })

  it('disables every board folder session action until Kiro is ready', () => {
    const { container } = renderSidebar(false)
    const buttons = container.querySelectorAll<HTMLButtonElement>(
      `[data-testid$="-folder-${FOLDER_ID}-new-chat"], button[aria-label="New chat in CDF"]`,
    )
    expect(buttons.length).toBe(4)
    for (const button of buttons) expect(button).toBeDisabled()
  })
})
