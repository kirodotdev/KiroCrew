/**
 * The per-folder menu carries a "New ephemeral chat" submenu (incognito,
 * temporary) so a mode-pinned session can be started INSIDE a folder in
 * one step. The create call must carry both the memory mode and the
 * folder membership, since correcting either afterwards is already too late —
 * the mode gates the first memory access and the folder placement gates the
 * first paint.
 *
 * Load-bearing assertions:
 *   (1) incognito / temporary create with their memory_mode, and every one
 *       rides the CREATE call with the folder id;
 *   (2) none of them smuggle the `defaultAutopilot` preference in as
 *       'orchestrator' — they name a memory type, not a run mode;
 *   (3) at phone width the rows are listed inline under a caption (a Radix
 *       submenu pins to the trigger's side and opens off-screen at 390px), and
 *       the inline row still creates with its mode and folder id.
 *
 * Radix menus cannot be opened by mouse in jsdom (needs PointerEvent), so the
 * trigger is activated by keyboard and the submenu opens on ArrowRight at its
 * sub-trigger — the paths jsdom handles (see ChatSidebar.ephemeralCreate).
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

// `defaultAutopilot` is load-bearing for assertion (2); list view (not board) so
// the single list-view folder header renders. Mutable box, flipped per test.
const cfg = vi.hoisted(() => ({ value: { tagColumnsEnabled: false, confirmCloseSession: false, defaultAutopilot: false } as Record<string, unknown> }))
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
// api.createChatSlot(name, agent, model, mode, memory_mode, title, clean_mode, artifact, folder_id)
const ARG_AGENT = 1
const ARG_MODE = 3
const ARG_MEMORY_MODE = 4
const ARG_CLEAN_MODE = 6
const ARG_FOLDER_ID = 8

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

// Submenus open on ArrowRight at their sub-trigger, in the same tick.
function openEphemeralSubmenu() {
  fireEvent.keyDown(screen.getByTestId(`folder-new-ephemeral-${FOLDER_ID}`), { key: 'ArrowRight' })
}

beforeEach(() => {
  localStorage.clear()
  mobile.value = false
  cfg.value = { tagColumnsEnabled: false, confirmCloseSession: false, defaultAutopilot: false }
  mocks.createChatSlot.mockImplementation((...args: unknown[]) =>
    Promise.resolve({ key: 'chat-new-1', folder_id: (args[ARG_FOLDER_ID] as string) || '' }),
  )
})
afterEach(() => vi.clearAllMocks())

describe('folder menu: ephemeral chat creation', () => {
  it('incognito creates with memory_mode "incognito" in the folder', async () => {
    // Preference ON to prove the entry pins the run mode rather than smuggling
    // an autopilot session onto a choice that named a memory mode.
    cfg.value = { tagColumnsEnabled: false, confirmCloseSession: false, defaultAutopilot: true }
    renderSidebar()
    openFolderMenu()
    openEphemeralSubmenu()
    fireEvent.click(screen.getByTestId(`folder-new-incognito-${FOLDER_ID}`))
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    const call = mocks.createChatSlot.mock.calls[0]
    expect(call[ARG_MEMORY_MODE]).toBe('incognito')
    expect(call[ARG_CLEAN_MODE]).toBeFalsy()
    expect(call[ARG_FOLDER_ID]).toBe(FOLDER_ID)
    expect(call[ARG_MODE]).not.toBe('orchestrator')
    expect(call[ARG_AGENT]).toBe(DEFAULT_AGENT)
  })

  it('temporary creates with memory_mode "temporary" in the folder', async () => {
    renderSidebar()
    openFolderMenu()
    openEphemeralSubmenu()
    fireEvent.click(screen.getByTestId(`folder-new-temporary-${FOLDER_ID}`))
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    const call = mocks.createChatSlot.mock.calls[0]
    expect(call[ARG_MEMORY_MODE]).toBe('temporary')
    expect(call[ARG_FOLDER_ID]).toBe(FOLDER_ID)
  })

  it('lists the modes inline under a caption at phone width, with no flyout', () => {
    // No submenu step: at phone width all three rows are already in the one open
    // menu (a Radix submenu opens off-screen there), so they resolve
    // synchronously right after the menu opens.
    mobile.value = true
    renderSidebar()
    openFolderMenu()
    expect(screen.getByTestId(`folder-new-incognito-${FOLDER_ID}`)).toBeInTheDocument()
    expect(screen.getByTestId(`folder-new-temporary-${FOLDER_ID}`)).toBeInTheDocument()
    // The label is a caption here, not a sub-trigger.
    expect(screen.queryByTestId(`folder-new-ephemeral-${FOLDER_ID}`)).toBeNull()
  })

  it('creates from the inline row at phone width', async () => {
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
