/**
 * A refused close is said, not swallowed.
 *
 * The server can keep a session open, and `deleteSlot` then puts the row back.
 * Without a notice that reads as a missed press. `deleteSlot` records the refusal
 * itself, so every close path (the row X, any session menu, Cmd+W) reaches the
 * sidebar's ErrorNotice. The server's "still saving" refusal gets its own words;
 * any other failure says the close did not go through.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { fireEvent, render, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ApiError } from '../api/apiError'
import { deleteSlot } from '../store/chatSlice'
import { ThemeProvider } from '../hooks/useTheme'

vi.mock('framer-motion', async () => {
  const React = await import('react')
  const SKIP = new Set(['layout', 'layoutId', 'layoutScroll', 'initial', 'animate', 'exit', 'transition', 'variants', 'whileHover', 'whileTap', 'whileInView', 'drag', 'dragConstraints', 'dragElastic', 'onAnimationComplete'])
  const make = (tag: string) =>
    React.forwardRef((props: Record<string, unknown>, ref: React.Ref<unknown>) => {
      const clean: Record<string, unknown> = {}
      for (const k of Object.keys(props)) if (k !== 'children' && !SKIP.has(k)) clean[k] = props[k]
      return React.createElement(tag, { ...clean, ref }, props.children as React.ReactNode)
    })
  return {
    motion: new Proxy({}, { get: (_t, tag: string) => make(tag) }),
    AnimatePresence: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
    LayoutGroup: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
  }
})

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

const mocks = vi.hoisted(() => ({ deleteChatSlot: vi.fn(), chatSlots: vi.fn() }))

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, p: string) => {
      if (p === 'deleteChatSlot') return mocks.deleteChatSlot
      if (p === 'chatSlots') return mocks.chatSlots
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

const SLOTS = [{ key: 'k-notes', title: 'Release notes', messages: 2, running: false }]

function renderSidebar() {
  const defaults = createTestStore().getState()
  const store = createTestStore({
    dashboard: {
      ...defaults.dashboard,
      status: {}, connected: true, slots: SLOTS, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null, slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { ...defaults.chat, activeSlot: null, slotStatusDetail: {}, subagents: {}, slotActivity: {}, revealRequest: null } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], [])
  return { store, ...render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={SLOTS as never} activeSlot={null} unreadSlots={[] as never}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  ) }
}

const rowClose = async (c: HTMLElement) => {
  await waitFor(() => expect(c.querySelector('[data-slot-key="k-notes"] button[aria-label="Close session"]')).not.toBeNull())
  return c.querySelector('[data-slot-key="k-notes"] button[aria-label="Close session"]') as HTMLElement
}

beforeEach(() => {
  localStorage.clear()
  mocks.chatSlots.mockReset()
  mocks.chatSlots.mockResolvedValue(SLOTS)
  mocks.deleteChatSlot.mockReset()
})
afterEach(() => { vi.clearAllMocks() })

const historyWriteRefusal = () => new ApiError(
  500,
  'a history write for this conversation is still running',
  JSON.stringify({ error: 'a history write for this conversation is still running', code: 'history_write_running' }),
)

describe('refused single-session close', () => {
  it("the row's close names the session and says it is still saving", async () => {
    mocks.deleteChatSlot.mockRejectedValue(historyWriteRefusal())
    const { container, findByTestId } = renderSidebar()
    fireEvent.click(await rowClose(container))
    const notice = await findByTestId('close-refused')
    expect(notice.textContent).toContain("Couldn't close the session")
    expect(notice.textContent).toContain('“Release notes” is still saving its last turn.')
    // Waiting is the fix, so there is nothing to hand an agent.
    expect(notice.textContent).not.toContain('Ask the agent')
  })

  it('any other failure says the close did not go through, never "still saving"', async () => {
    mocks.deleteChatSlot.mockRejectedValue(new ApiError(403, 'forbidden', ''))
    const { container, findByTestId } = renderSidebar()
    fireEvent.click(await rowClose(container))
    const notice = await findByTestId('close-refused')
    expect(notice.textContent).toContain("“Release notes” is still open: the close didn't go through.")
    expect(notice.textContent).not.toContain('saving')
  })

  it('a close from outside the sidebar (session menu, Cmd+W) is reported too', async () => {
    // Every other path dispatches the same deleteSlot.
    mocks.deleteChatSlot.mockRejectedValue(historyWriteRefusal())
    const { container, store, findByTestId } = renderSidebar()
    await rowClose(container)
    await store.dispatch(deleteSlot('k-notes')).catch(() => {})
    const notice = await findByTestId('close-refused')
    expect(notice.textContent).toContain('“Release notes” is still saving its last turn.')
  })

  it('a close that succeeds says nothing', async () => {
    mocks.deleteChatSlot.mockResolvedValue({ ok: true })
    const { container, queryByTestId } = renderSidebar()
    fireEvent.click(await rowClose(container))
    await waitFor(() => expect(mocks.deleteChatSlot).toHaveBeenCalledWith('k-notes'))
    await new Promise(r => setTimeout(r, 0))
    expect(queryByTestId('close-refused')).toBeNull()
  })
})

describe('close reach on a card with sessions under it', () => {
  it("names that only this session closes; a card with nothing under it keeps the plain label", async () => {
    // Conductor lane, so the lead nests its worker and knows it has one under it.
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    localStorage.setItem('mc-sidebar-conductor-expanded', JSON.stringify(['k-lead']))
    localStorage.setItem('mc-session-stale-collapse-ms', '0')
    const slots = [
      { key: 'k-lead', title: 'Lead', messages: 2, running: true },
      { key: 'k-worker', title: 'Worker', messages: 2, running: true, parent: { slot: 'k-lead', key: 'k-lead' } },
    ]
    mocks.chatSlots.mockResolvedValue(slots)
    const defaults = createTestStore().getState()
    const store = createTestStore({
      dashboard: {
        ...defaults.dashboard, status: {}, connected: true, slots, approvalMode: 'normal', unreadSlots: [], slotsLoaded: true,
      } as unknown as RootState['dashboard'],
      chat: { ...defaults.chat, activeSlot: null, slotStatusDetail: {} } as unknown as RootState['chat'],
    })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
    qc.setQueryData(['chat-folders'], [])
    const { container } = render(
      <QueryClientProvider client={qc}>
        <Provider store={store}>
          <ThemeProvider>
            <MemoryRouter>
              <ChatSidebar slots={slots as never} activeSlot={null} unreadSlots={[] as never} history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]} />
            </MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    )
    const reach = 'Close this session only. Sessions under it stay open.'
    await waitFor(() => expect(container.querySelector(`[data-slot-key="k-lead"] button[aria-label="${reach}"]`)).not.toBeNull())
    const x = container.querySelector(`[data-slot-key="k-lead"] button[aria-label="${reach}"]`) as HTMLElement
    expect(x.getAttribute('title')).toBe(reach)
    expect(container.querySelector('[data-slot-key="k-worker"] button[aria-label="Close session"]')).not.toBeNull()
  })
})
