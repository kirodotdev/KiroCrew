/**
 * Test: ChatSidebar offline session-row + history click blocking.
 * Covers the offline-UX session-row / history click-blocking guards:
 *   - L760-761: active-list onClick early-returns when `connected=false && !isActive`
 *     and skips dispatching `switchSlot`.
 *   - L1392, L1395: history-list onMouseDown early-returns when `connected=false`
 *     and skips dispatching `resumeFromHistory`.
 *
 * No existing ChatSidebar tests in src/test/, so this file owns the mock setup
 * for the component. Mocks the chat slice's switchSlot / resumeFromHistory thunks
 * so we can assert they were NOT called when offline. Renders ChatSidebar
 * directly (no router needed beyond MemoryRouter) and simulates user clicks.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

// Mock the chat slice thunks BEFORE importing the component so we can
// observe whether they are dispatched. switchSlot + resumeFromHistory are
// the two functions that the offline guards block in production.
// vi.hoisted() ensures these vi.fn() instances exist by the time the hoisted
// vi.mock() factory runs (factories execute before top-level statements).
const { switchSlotMock, resumeFromHistoryMock } = vi.hoisted(() => ({
  switchSlotMock: vi.fn(() => ({ type: 'chat/switchSlot/pending', meta: {} })),
  resumeFromHistoryMock: vi.fn(() => ({ type: 'chat/resumeFromHistory/pending', meta: {} })),
}))

vi.mock('../store/chatSlice', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../store/chatSlice')>()
  return {
    ...actual,
    switchSlot: (...args: unknown[]) => switchSlotMock(...args),
    resumeFromHistory: (...args: unknown[]) => resumeFromHistoryMock(...args),
  }
})

vi.mock('../hooks/useTheme', async () => {
  const actual = await vi.importActual<typeof import('../hooks/useTheme')>('../hooks/useTheme')
  return actual
})

// Mock API client (sidebar fires fetch calls on mount). Use importOriginal so
// that non-`api` exports (e.g. SEARCH_MIN_CHARS constants used by sidebar
// search debounce) keep their real values.
vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return {
    ...actual,
    api: Object.fromEntries(
      [
        'sessions', 'chatSlots', 'chatSlotDetail', 'createChatSlot', 'deleteChatSlot',
        'resumeChatSlot', 'deleteSession', 'agentDetail', 'spawnList', 'fetchHistory',
        'renameSlot', 'forkSession',
      ].map(k => [k, vi.fn().mockResolvedValue({})]),
    ),
  }
})

// Browser API stubs
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})
globalThis.fetch = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }) as unknown as typeof fetch

import ChatSidebar from '../pages/ChatSidebar'
import type { ChatSlot, ChatHistoryItem } from '../types'
import type { RootState } from '../store'

const slot = (key: string, title?: string): ChatSlot => ({
  key, title: title ?? key, messages: 1, running: false, mode: '', created: '', last_ts: '2026-01-01T00:00:00Z',
} as ChatSlot)

const histItem = (key: string, title: string): ChatHistoryItem => ({
  key, title, last_ts: '2026-01-01T00:00:00Z',
} as unknown as ChatHistoryItem)

function renderSidebar(connected: boolean, opts: { withHistory?: boolean } = {}) {
  const slots = [slot('s1', 'Session 1'), slot('s2', 'Session 2')]
  const history = opts.withHistory ? [histItem('h1', 'History 1')] : []
  const store = createTestStore({
    dashboard: {
      status: { platform: 'darwin' },
      connected,
      slots,
      approvalMode: 'normal', channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
      slotsLoaded: true,
    } as unknown as RootState['dashboard'],
    chat: {
      activeSlot: 's1',
      messages: [], slotRunning: false, slotStopping: false, slotState: 'idle',
      slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
      lastChunkSeq: undefined,
      history, historyHasMore: false, historyOffset: history.length,
      pendingInput: null, slotContextPct: {}, voicePlaying: false, voiceAudio: null,
      subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools', slotActivity: {}, slotHistory: [],
      slotMessages: {}, slotLoading: false,
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots}
              activeSlot={'s1'}
              unreadSlots={[]}
              history={history}
              historyHasMore={false}
              defaultAgent={'default'}
              installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

describe('ChatSidebar – offline guards', () => {
  beforeEach(() => {
    switchSlotMock.mockClear()
    resumeFromHistoryMock.mockClear()
  })

  it('clicking a non-active session row when offline does NOT dispatch switchSlot', () => {
    renderSidebar(false)
    // The clickable element is the inner div with class "session-row" — the
    // outer motion.div with data-slot-key just wraps for animation/layout.
    const wrapper = screen.getByText('Session 2').closest('[data-slot-key]') as HTMLElement | null
    expect(wrapper).not.toBeNull()
    const row = wrapper!.querySelector('.session-row') as HTMLElement | null
    expect(row).not.toBeNull()
    fireEvent.click(row!)
    expect(switchSlotMock).not.toHaveBeenCalled()
  })

  it('clicking the ACTIVE session row when offline does NOT dispatch switchSlot', () => {
    // Without the guard, re-clicking the active row while offline dispatches
    // switchSlot, fetchSlotDetail fails with the gateway down, switchSlot.rejected
    // clears messages to [], and the ChatPage WelcomeView fallback kicks in
    // (activeSlot truthy + messages empty → "What can I do for you?"). The
    // "if (!connected && !isActive) return" guard covers the active row explicitly.
    renderSidebar(false)
    const wrapper = screen.getByText('Session 1').closest('[data-slot-key]') as HTMLElement | null
    expect(wrapper).not.toBeNull()
    const row = wrapper!.querySelector('.session-row') as HTMLElement | null
    expect(row).not.toBeNull()
    fireEvent.click(row!)
    expect(switchSlotMock).not.toHaveBeenCalled()
  })

  it('clicking a non-active session row when connected DOES dispatch switchSlot', () => {
    renderSidebar(true)
    const wrapper = screen.getByText('Session 2').closest('[data-slot-key]') as HTMLElement | null
    expect(wrapper).not.toBeNull()
    const row = wrapper!.querySelector('.session-row') as HTMLElement | null
    expect(row).not.toBeNull()
    fireEvent.click(row!)
    expect(switchSlotMock).toHaveBeenCalled()
  })

  it('mousedown on a history row when offline does NOT dispatch resumeFromHistory', () => {
    renderSidebar(false, { withHistory: true })
    // History section is collapsed by default — expand it first
    fireEvent.click(screen.getByRole('button', { name: /^older sessions$/i }))
    const histRow = screen.getByText('History 1').closest('div') as HTMLElement | null
    expect(histRow).not.toBeNull()
    fireEvent.mouseDown(histRow!)
    expect(resumeFromHistoryMock).not.toHaveBeenCalled()
  })

  it('mousedown on a history row when connected DOES dispatch resumeFromHistory', () => {
    renderSidebar(true, { withHistory: true })
    fireEvent.click(screen.getByRole('button', { name: /^older sessions$/i }))
    const histRow = screen.getByText('History 1').closest('div') as HTMLElement | null
    expect(histRow).not.toBeNull()
    fireEvent.mouseDown(histRow!)
    expect(resumeFromHistoryMock).toHaveBeenCalled()
  })

  it('offline session rows expose aria-disabled=true for screen readers', () => {
    // Beyond visual cursor-not-allowed + opacity-50, screen-reader users
    // need an explicit aria-disabled to know the rows aren't actionable
    // while the gateway is offline. Without it the row is announced as a
    // plain interactive element (no semantic disabled state).
    renderSidebar(false)
    // Active row
    const activeWrapper = screen.getByText('Session 1').closest('[data-slot-key]') as HTMLElement
    const activeRow = activeWrapper.querySelector('.session-row') as HTMLElement
    expect(activeRow.getAttribute('aria-disabled')).toBe('true')
    // Non-active row
    const otherWrapper = screen.getByText('Session 2').closest('[data-slot-key]') as HTMLElement
    const otherRow = otherWrapper.querySelector('.session-row') as HTMLElement
    expect(otherRow.getAttribute('aria-disabled')).toBe('true')
  })

  it('connected session rows expose aria-disabled=false', () => {
    renderSidebar(true)
    const activeWrapper = screen.getByText('Session 1').closest('[data-slot-key]') as HTMLElement
    const activeRow = activeWrapper.querySelector('.session-row') as HTMLElement
    expect(activeRow.getAttribute('aria-disabled')).toBe('false')
  })

  it('offline history rows expose aria-disabled=true for screen readers', () => {
    renderSidebar(false, { withHistory: true })
    fireEvent.click(screen.getByRole('button', { name: /^older sessions$/i }))
    // The history row is the outermost flex container with the offline title.
    // closest('[aria-disabled]') walks up to the element that owns the attr.
    const histRow = screen.getByText('History 1').closest('[aria-disabled]') as HTMLElement
    expect(histRow).not.toBeNull()
    expect(histRow.getAttribute('aria-disabled')).toBe('true')
  })
})
