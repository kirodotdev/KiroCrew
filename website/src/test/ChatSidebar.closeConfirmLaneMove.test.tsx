/**
 * A row's session-menu close asks through the sidebar's one action owner, so
 * its confirmation outlives the row. A busy session that goes idle while the
 * question is open moves to another lane, which unmounts its row; that must not
 * cancel the question.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'
import { sseSlots } from '../store/dashboardSlice'

const mocks = vi.hoisted(() => ({ deleteChatSlot: vi.fn() }))

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ confirmCloseSession: true }),
  saveChatConfig: vi.fn(),
}))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({}, {
    get: (_t, name: string) => name === 'deleteChatSlot' ? mocks.deleteChatSlot : vi.fn().mockResolvedValue([]),
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

const KEY = 'chat-busy-1'
const SLOT = { key: KEY, title: 'Busy session', running: false, messages: 2, created: '', last_ts: '' }

function renderSidebar() {
  const defaults = createTestStore().getState()
  const store = createTestStore({
    dashboard: {
      ...defaults.dashboard, slotsLoaded: true,
      status: {}, connected: true, slots: [SLOT], approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { ...defaults.chat, activeSlot: null } as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  const tree = (slots: typeof SLOT[]) => (
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots as never} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>
  )
  const utils = render(tree([SLOT]))
  return { store, dropRow: () => { store.dispatch(sseSlots([])); utils.rerender(tree([])) } }
}

afterEach(() => vi.clearAllMocks())

describe('session-menu close', () => {
  it('keeps its confirmation open when the row unmounts, and closes on accept', async () => {
    mocks.deleteChatSlot.mockResolvedValue({ ok: true })
    const { dropRow } = renderSidebar()
    const row = await waitFor(() => {
      const el = document.querySelector(`[data-session-row="${KEY}"]`) as HTMLElement | null
      expect(el).toBeTruthy()
      return el!
    })
    const trigger = within(row).getAllByRole('button', { name: 'More options' })[0]
    fireEvent.keyDown(trigger, { key: 'Enter' })
    fireEvent.click(await screen.findByRole('menuitem', { name: /Close session/ }))
    const dialog = await screen.findByRole('dialog', { name: 'Close this session?' })

    // The row unmounts while the question is open, as a lane change does.
    act(() => { dropRow() })
    await waitFor(() => expect(document.querySelector(`[data-session-row="${KEY}"]`)).toBeNull())
    expect(dialog).toBeInTheDocument()
    expect(mocks.deleteChatSlot).not.toHaveBeenCalled()

    fireEvent.click(within(dialog).getByRole('button', { name: 'Close session' }))
    await waitFor(() => expect(mocks.deleteChatSlot).toHaveBeenCalledWith(KEY))
  })
})
