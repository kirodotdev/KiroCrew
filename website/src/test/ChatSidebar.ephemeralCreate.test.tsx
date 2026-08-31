/**
 * The caret menu's "New ephemeral chat" submenu is the only place the sidebar
 * can start a session that does not persist. Both memory modes already exist
 * on the create endpoint; before this they were reachable only from the welcome
 * view, so a user already inside a session had to leave it to start one.
 *
 * Four load-bearing assertions:
 *   (1) the submenu lists BOTH modes — a submenu that opened onto one entry
 *       would be a worse affordance than a plain menu item;
 *   (2) Incognito creates with memory_mode 'incognito', and
 *   (3) Temporary with 'temporary' — the memory mode is the entire feature, and
 *       it must ride the CREATE call: a session that starts persistent and is
 *       corrected afterwards has already written to memory by then;
 *   (4) neither entry smuggles the `defaultAutopilot` preference in as
 *       'orchestrator' — these entries name a memory mode, not a run mode — and
 *       the plain "New chat" entry still carries NO memory_mode, so adding the
 *       submenu cannot make the ordinary path ephemeral.
 *
 * Radix DropdownMenu cannot be opened by mouse in jsdom (needs PointerEvent),
 * so the trigger is activated by keyboard — the path jsdom does handle. Submenus
 * open on ArrowRight at their sub-trigger (see ChatSidebarW3Coverage).
 *
 * Two more at PHONE width, where the submenu is not a flyout at all: a Radix
 * submenu pins to the trigger's side and only shifts vertically, so at 390px it
 * opens past the left viewport edge. The rows are listed inline under a caption
 * instead — (5) both rows are in the one open menu and the label is no longer a
 * sub-trigger, and (6) the inline row still creates with its memory mode.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'

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

// `defaultAutopilot` is load-bearing for assertion (4), so the config mock is a
// mutable box the tests flip between renders.
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
// api.createChatSlot(name, agent, model, mode, memory_mode, title, clean_mode, artifact, folder_id)
const ARG_AGENT = 1
const ARG_MODE = 3
const ARG_MEMORY_MODE = 4

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
  qc.setQueryData(['chat-folders'], [])
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

// The submenu opens from its SUB-TRIGGER, which is the role=menuitem carrying
// the label — not necessarily the node holding the text, since a sub-trigger may
// wrap its label in a span for icon alignment. Resolve upward to the menuitem so
// the ArrowRight lands on the element Radix listens on.
async function openEphemeralSubmenu() {
  const label = await screen.findByText('New ephemeral chat')
  const trigger = (label.closest('[role="menuitem"]') ?? label) as HTMLElement
  fireEvent.keyDown(trigger, { key: 'ArrowRight' })
}

beforeEach(() => {
  localStorage.clear()
  mobile.value = false
  cfg.value = { tagColumnsEnabled: false, confirmCloseSession: false, defaultAutopilot: false }
  mocks.createChatSlot.mockResolvedValue({ key: 'chat-new-1' })
})
afterEach(() => vi.clearAllMocks())

describe('create-button caret menu: ephemeral chats', () => {
  it('offers both ephemeral modes under one submenu', async () => {
    renderSidebar()
    openCreateMenu()
    await openEphemeralSubmenu()
    const incognito = await screen.findByTestId('new-incognito-chat')
    const temporary = screen.getByTestId('new-temporary-chat')
    // Labels come from the welcome view's catalog keys, so the two surfaces that
    // create these sessions name the modes identically.
    expect(incognito.textContent).toContain('Incognito')
    expect(temporary.textContent).toContain('Temporary')
  })

  it('Incognito creates with memory_mode "incognito"', async () => {
    // The preference is ON to prove the entry pins the run mode: it names a
    // MEMORY mode, so silently returning an autopilot session would be a second,
    // unasked-for choice.
    cfg.value = { tagColumnsEnabled: false, confirmCloseSession: false, defaultAutopilot: true }
    renderSidebar()
    openCreateMenu()
    await openEphemeralSubmenu()
    fireEvent.click(await screen.findByTestId('new-incognito-chat'))
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    const call = mocks.createChatSlot.mock.calls[0]
    expect(call[ARG_MEMORY_MODE]).toBe('incognito')
    expect(call[ARG_MODE]).not.toBe('orchestrator')
    expect(call[ARG_AGENT]).toBe(DEFAULT_AGENT)
  })

  it('Temporary creates with memory_mode "temporary"', async () => {
    cfg.value = { tagColumnsEnabled: false, confirmCloseSession: false, defaultAutopilot: true }
    renderSidebar()
    openCreateMenu()
    await openEphemeralSubmenu()
    fireEvent.click(await screen.findByTestId('new-temporary-chat'))
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    const call = mocks.createChatSlot.mock.calls[0]
    expect(call[ARG_MEMORY_MODE]).toBe('temporary')
    expect(call[ARG_MODE]).not.toBe('orchestrator')
    expect(call[ARG_AGENT]).toBe(DEFAULT_AGENT)
  })

  it('leaves the plain "New chat" entry persistent', async () => {
    // Regression guard: an ephemeral mode leaking onto the ordinary entry would
    // silently stop writing the user's sessions to memory, with nothing on the
    // menu to say so.
    renderSidebar()
    openCreateMenu()
    fireEvent.click(await screen.findByText('New chat'))
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    expect(mocks.createChatSlot.mock.calls[0][ARG_MEMORY_MODE]).toBeUndefined()
  })

  it('lists both modes inline under a caption at phone width, with no flyout to open', async () => {
    mobile.value = true
    renderSidebar()
    openCreateMenu()

    // No ArrowRight anywhere: at phone width both rows must already be in the one
    // open menu, because a Radix submenu pins to the trigger's side and only
    // shifts vertically — it opens past the left viewport edge and is unreadable.
    expect(await screen.findByTestId('new-incognito-chat')).toBeInTheDocument()
    expect(screen.getByTestId('new-temporary-chat')).toBeInTheDocument()

    // The row carrying the label is a CAPTION here, not a sub-trigger. Asserting
    // only the rows above would stay green if the flyout were left in place with
    // the rows duplicated inline.
    const caption = screen.getByText('New ephemeral chat')
    expect(caption.closest('[role="menuitem"]')).toBeNull()
    expect(caption.getAttribute('aria-haspopup')).toBeNull()
  })

  it('creates with memory_mode "temporary" from the inline row at phone width', async () => {
    // The handlers are shared with the flyout, so this pins that the phone branch
    // reuses them rather than re-deriving the create call — the memory mode is the
    // whole feature and must still ride the CREATE.
    mobile.value = true
    cfg.value = { tagColumnsEnabled: false, confirmCloseSession: false, defaultAutopilot: true }
    renderSidebar()
    openCreateMenu()

    fireEvent.click(await screen.findByTestId('new-temporary-chat'))
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    const call = mocks.createChatSlot.mock.calls[0]
    expect(call[ARG_MEMORY_MODE]).toBe('temporary')
    expect(call[ARG_MODE]).not.toBe('orchestrator')
    expect(call[ARG_AGENT]).toBe(DEFAULT_AGENT)
  })
})
