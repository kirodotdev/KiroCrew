/**
 * The per-folder menu lists "New incognito chat" and "New temporary chat" as
 * two flat rows so a mode-pinned session can be started INSIDE a folder in one
 * step. The create call must carry both the memory mode and the folder
 * membership, since correcting either afterwards is already too late — the
 * mode gates the first memory access and the folder placement gates the first
 * paint.
 *
 * Load-bearing assertions:
 *   (1) incognito / temporary create with their memory_mode, and every one
 *       rides the CREATE call with the folder id;
 *   (2) they create in the default run mode ('') — they name a memory type,
 *       not a run mode;
 *   (3) the rows are top-level items with their one-line memory hint, on
 *       desktop and phone alike: no grouping caption (it read as a button at
 *       phone width) and no submenu (one opens off-screen at 390px).
 *
 * Radix menus cannot be opened by mouse in jsdom (needs PointerEvent), so the
 * trigger is activated by keyboard — the path jsdom handles (see
 * ChatSidebar.ephemeralCreate).
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'
import type { ChatFolder } from '../types'

// Render framer-motion elements as plain DOM because jsdom cannot run projection.
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

// List view (not board) so the single list-view folder header renders.
// Mutable box, flipped per test.
const cfg = vi.hoisted(() => ({ value: { tagColumnsEnabled: false, confirmCloseSession: false } as Record<string, unknown> }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => cfg.value,
  saveChatConfig: vi.fn(),
}))

const mocks = vi.hoisted(() => ({ createChatSlot: vi.fn() }))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (target, prop: string) => (prop in target ? target[prop] : vi.fn().mockResolvedValue([])),
  }),
}))

// useIsMobile resolves at module load, so mock the hook itself; default DESKTOP.
const mobile = vi.hoisted(() => ({ value: false }))
vi.mock('../hooks/useIsMobile', () => ({
  MOBILE_BREAKPOINT: 768,
  useIsMobile: () => mobile.value,
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

const DEFAULT_AGENT = 'kirocrew'
const FOLDER_ID = 'folder-zzzz'
// api.createChatSlot(name, agent, model, mode, memory_mode, title, artifact, folder_id)
const ARG_AGENT = 1
const ARG_MODE = 3
const ARG_MEMORY_MODE = 4
const ARG_FOLDER_ID = 7

const folders: ChatFolder[] = [{ id: FOLDER_ID, name: 'CDF', order: 0, collapsed: true }]

function renderSidebar() {
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
  qc.setQueryData(['chat-folders'], folders)
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={[]} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent={DEFAULT_AGENT} installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

// The folder ⋯ menu survives only for the current tick — Radix tears it down on
// the first macrotask because nothing in jsdom holds the focus it grabs — so
// this helper is SYNCHRONOUS and every caller must drive the item it wants in
// the same tick, with no await in between (see ChatSidebarW3Coverage).
function openFolderMenu() {
  fireEvent.keyDown(screen.getByTestId(`folder-menu-${FOLDER_ID}`), { key: 'Enter' })
  // Confirm the menu is up (via an item present in both layouts) so a silent
  // no-open cannot pass a later query.
  expect(screen.getByTestId(`folder-settings-${FOLDER_ID}`)).toBeTruthy()
}

beforeEach(() => {
  localStorage.clear()
  mobile.value = false
  cfg.value = { tagColumnsEnabled: false, confirmCloseSession: false }
  mocks.createChatSlot.mockImplementation((...args: unknown[]) =>
    Promise.resolve({ key: 'chat-new-1', folder_id: (args[ARG_FOLDER_ID] as string) || '' }),
  )
})
afterEach(() => vi.clearAllMocks())

describe('folder menu: private chat creation', () => {
  it('incognito creates with memory_mode "incognito" in the folder', async () => {
    renderSidebar()
    openFolderMenu()
    fireEvent.click(screen.getByTestId(`folder-new-incognito-${FOLDER_ID}`))
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    const call = mocks.createChatSlot.mock.calls[0]
    expect(call[ARG_MEMORY_MODE]).toBe('incognito')
    expect(call[ARG_FOLDER_ID]).toBe(FOLDER_ID)
    expect(call[ARG_MODE]).toBe('')
    expect(call[ARG_AGENT]).toBe(DEFAULT_AGENT)
  })

  it('temporary creates with memory_mode "temporary" in the folder', async () => {
    renderSidebar()
    openFolderMenu()
    fireEvent.click(screen.getByTestId(`folder-new-temporary-${FOLDER_ID}`))
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    const call = mocks.createChatSlot.mock.calls[0]
    expect(call[ARG_MEMORY_MODE]).toBe('temporary')
    expect(call[ARG_FOLDER_ID]).toBe(FOLDER_ID)
  })

  it.each([false, true])('lists both modes as flat rows with their memory hint (mobile=%s)', (isPhone) => {
    mobile.value = isPhone
    renderSidebar()
    openFolderMenu()
    const incognito = screen.getByTestId(`folder-new-incognito-${FOLDER_ID}`)
    const temporary = screen.getByTestId(`folder-new-temporary-${FOLDER_ID}`)
    expect(incognito).toHaveTextContent('New incognito chat')
    expect(incognito).toHaveTextContent('Uses memory, learns nothing new')
    expect(temporary).toHaveTextContent('New temporary chat')
    expect(temporary).toHaveTextContent('No memory, learns nothing new')
    // Neither a submenu trigger nor a grouping caption stands in front of them.
    expect(screen.queryByTestId(`folder-new-ephemeral-${FOLDER_ID}`)).toBeNull()
    expect(screen.queryByText(/ephemeral/i)).toBeNull()
  })

  it('creates from the row at phone width', async () => {
    mobile.value = true
    renderSidebar()
    openFolderMenu()
    fireEvent.click(screen.getByTestId(`folder-new-temporary-${FOLDER_ID}`))
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    const call = mocks.createChatSlot.mock.calls[0]
    expect(call[ARG_MEMORY_MODE]).toBe('temporary')
    expect(call[ARG_FOLDER_ID]).toBe(FOLDER_ID)
  })
})
