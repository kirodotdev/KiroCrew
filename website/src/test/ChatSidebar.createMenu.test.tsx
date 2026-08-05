/**
 * The split create-button's caret menu must list the ORDINARY chat, not only
 * the alternative ways to create one.
 *
 * Two load-bearing assertions:
 *   (1) "New chat" renders in the menu alongside "New autopilot chat" — a menu
 *       that offers only autopilot + folder entries reads as if the caret could
 *       not make a plain chat at all;
 *   (2) it creates a PLAIN chat even when `defaultAutopilot` is on. The main
 *       segment honours that preference; this entry names its mode, so it must
 *       pin it — otherwise the one control that says "New chat" hands back an
 *       autopilot session.
 *
 * Radix DropdownMenu cannot be opened by mouse in jsdom (needs PointerEvent),
 * so the trigger is activated by keyboard — the path jsdom does handle.
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

// `defaultAutopilot` is the whole point of assertion (2), so the config mock is
// a mutable box the tests flip between renders.
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

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})

import ChatSidebar from '../pages/ChatSidebar'

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
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return view
}

function openCreateMenu() {
  const caret = screen.getByLabelText('More create options')
  fireEvent.keyDown(caret, { key: 'Enter' })
}

beforeEach(() => {
  localStorage.clear()
  cfg.value = { tagColumnsEnabled: false, confirmCloseSession: false, defaultAutopilot: false }
  mocks.createChatSlot.mockResolvedValue({ key: 'chat-new-1' })
})
afterEach(() => vi.clearAllMocks())

describe('create-button caret menu', () => {
  it('lists "New chat" next to "New autopilot chat"', async () => {
    renderSidebar()
    openCreateMenu()
    expect(await screen.findByText('New chat')).toBeTruthy()
    expect(screen.getByText('New autopilot chat')).toBeTruthy()
  })

  it('"New chat" creates a plain session even when defaultAutopilot is on', async () => {
    cfg.value = { tagColumnsEnabled: false, confirmCloseSession: false, defaultAutopilot: true }
    renderSidebar()
    openCreateMenu()
    fireEvent.click(await screen.findByText('New chat'))
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalled())
    // createSlot passes the mode positionally; assert no call carried 'orchestrator'.
    for (const call of mocks.createChatSlot.mock.calls) {
      expect(call).not.toContain('orchestrator')
    }
  })

  it('"New autopilot chat" still creates an orchestrator session', async () => {
    renderSidebar()
    openCreateMenu()
    fireEvent.click(await screen.findByText('New autopilot chat'))
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalled())
    expect(mocks.createChatSlot.mock.calls.some(c => c.includes('orchestrator'))).toBe(true)
  })
})
