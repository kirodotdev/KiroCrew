/**
 * The caret menu lists "New incognito chat" and "New temporary chat" beside
 * "New chat", the only place in the sidebar header that starts a session whose
 * chat teaches memory nothing. Both memory modes already exist on the create
 * endpoint; before the caret menu they were reachable only from the welcome
 * view, so a user already inside a session had to leave it to start one.
 *
 * Load-bearing assertions:
 *   (1) both modes are top-level rows with their one-line memory hint, on
 *       desktop and phone alike -- no grouping submenu (it opens off-screen at
 *       390px) and no caption (at phone width it read as a button);
 *   (2) Incognito creates with memory_mode 'incognito', and
 *   (3) Temporary with 'temporary' -- the memory mode is the entire feature, and
 *       it must ride the CREATE call: a session that starts persistent and is
 *       corrected afterwards has already written to memory by then;
 *   (4) both entries create in the default run mode ('') -- they name a memory
 *       mode, not a run mode -- and the plain "New chat" entry carries the
 *       configured default memory mode while the explicit choices remain
 *       pinned.
 *
 *   (5) at phone width the inline folder list sits under a text-only caption
 *       that is not a menu item, so it cannot be mistaken for a row.
 *
 * Radix DropdownMenu cannot be opened by mouse in jsdom (needs PointerEvent),
 * so the trigger is activated by keyboard -- the path jsdom does handle.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
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

// A mutable box so a test can flip the config between renders.
const cfg = vi.hoisted(() => ({ value: { tagColumnsEnabled: false, confirmCloseSession: false } as Record<string, unknown> }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => cfg.value,
  saveChatConfig: vi.fn(),
}))

const mocks = vi.hoisted(() => ({ createChatSlot: vi.fn(), chatFolders: vi.fn() }))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (target, prop: string) => (prop in target ? target[prop] : vi.fn().mockResolvedValue([])),
  }),
}))

/* `useIsMobile` resolves its media query at MODULE LOAD, so a matchMedia stub
   installed in this file's body would land after the hoisted import. Mocking the
   hook itself is both deterministic and flippable per test. Default is DESKTOP,
   so the flyout tests below read exactly as they did before the phone branch
   existed. */
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
// api.createChatSlot(name, agent, model, mode, memory_mode, title, artifact, folder_id)
const ARG_AGENT = 1
const ARG_MODE = 3
const ARG_MEMORY_MODE = 4

function renderSidebar(folders: ChatFolder[] = []) {
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
  // The sidebar refetches folders on mount; answer with the same list.
  mocks.chatFolders.mockResolvedValue(folders)
  const view = render(
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
  return view
}

function openCreateMenu() {
  fireEvent.keyDown(screen.getByLabelText('More create options'), { key: 'Enter' })
}

beforeEach(() => {
  localStorage.clear()
  mobile.value = false
  cfg.value = { tagColumnsEnabled: false, confirmCloseSession: false }
  mocks.createChatSlot.mockResolvedValue({ key: 'chat-new-1' })
})
afterEach(() => vi.clearAllMocks())

describe('create-button caret menu: private chats', () => {
  it.each([false, true])('lists both modes as flat rows with their memory hint (mobile=%s)', async (isPhone) => {
    mobile.value = isPhone
    renderSidebar()
    openCreateMenu()
    // No ArrowRight anywhere: both rows must already be in the one open menu.
    const incognito = await screen.findByTestId('new-incognito-chat')
    const temporary = screen.getByTestId('new-temporary-chat')
    expect(incognito).toHaveTextContent('New incognito chat')
    expect(incognito).toHaveTextContent('Uses memory, learns nothing new')
    expect(temporary).toHaveTextContent('New temporary chat')
    expect(temporary).toHaveTextContent('No memory, learns nothing new')
    // The rows sit in the root menu, not behind a sub-trigger, and no caption
    // names them with a word the rest of the product does not use.
    expect(incognito.closest('[role="menu"]')).toBe(screen.getByRole('menuitem', { name: 'New chat' }).closest('[role="menu"]'))
    expect(screen.queryByText(/ephemeral/i)).toBeNull()
  })

  it('Incognito creates with memory_mode "incognito"', async () => {
    renderSidebar()
    openCreateMenu()
    fireEvent.click(await screen.findByTestId('new-incognito-chat'))
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    const call = mocks.createChatSlot.mock.calls[0]
    expect(call[ARG_MEMORY_MODE]).toBe('incognito')
    expect(call[ARG_MODE]).toBe('')
    expect(call[ARG_AGENT]).toBe(DEFAULT_AGENT)
  })

  it('Temporary creates with memory_mode "temporary"', async () => {
    renderSidebar()
    openCreateMenu()
    fireEvent.click(await screen.findByTestId('new-temporary-chat'))
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    const call = mocks.createChatSlot.mock.calls[0]
    expect(call[ARG_MEMORY_MODE]).toBe('temporary')
    expect(call[ARG_MODE]).toBe('')
    expect(call[ARG_AGENT]).toBe(DEFAULT_AGENT)
  })

  it('applies the configured fallback to the plain "New chat" entry', async () => {
    // This file's API proxy returns no configured value, so the shared thunk
    // must preserve the factory default rather than omit the mode.
    renderSidebar()
    openCreateMenu()
    // By role, not by text: the split button's main segment is titled "New
    // chat" too, so a text query is one relabel away from matching two nodes.
    fireEvent.click(await screen.findByRole('menuitem', { name: 'New chat' }))
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    expect(mocks.createChatSlot.mock.calls[0][ARG_MEMORY_MODE]).toBe('persistent')
  })

  it('captions the inline folder list with text alone at phone width', async () => {
    // The caption once carried the same Folder glyph as the rows under it and
    // read as one more tappable row. It is a menu LABEL with no icon; the folder
    // rows below it stay the tappable items.
    mobile.value = true
    renderSidebar([{ id: 'f1', name: 'Research', order: 0, collapsed: true }])
    openCreateMenu()
    const caption = await screen.findByTestId('new-chat-in-folder-caption')
    expect(caption).toHaveTextContent('New chat in folder')
    expect(caption.closest('[role="menuitem"]')).toBeNull()
    expect(caption.querySelector('svg')).toBeNull()
    expect(within(screen.getByRole('menu')).getByText('Research').closest('[role="menuitem"]')).not.toBeNull()
  })

  it('creates with memory_mode "temporary" from the row at phone width', async () => {
    // The memory mode is the whole feature and must still ride the CREATE on a
    // phone, where the menu is rendered by the same rows.
    mobile.value = true
    renderSidebar()
    openCreateMenu()

    fireEvent.click(await screen.findByTestId('new-temporary-chat'))
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    const call = mocks.createChatSlot.mock.calls[0]
    expect(call[ARG_MEMORY_MODE]).toBe('temporary')
    expect(call[ARG_MODE]).toBe('')
    expect(call[ARG_AGENT]).toBe(DEFAULT_AGENT)
  })
})
