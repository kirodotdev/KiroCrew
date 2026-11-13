/**
 * Test: a failed Older Sessions search drops the previous query's rows and says so.
 *
 * Regression for the stale-result bug in `useDebouncedSessionSearch`: its `catch`
 * kept the previous result, and HistoryPane still counted the search as active,
 * so query A's rows rendered under query B with nothing saying B had failed.
 *
 * The hook-level cases pin the race guard: only the newest request may write the
 * result or the failure, so a slow, older response never lands over a newer one.
 *
 * Mock scaffolding mirrors ChatSidebar.historySearchOrder.test.tsx.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, renderHook, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

const { sessionsSearchMock } = vi.hoisted(() => ({ sessionsSearchMock: vi.fn() }))

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return {
    ...actual,
    api: {
      ...Object.fromEntries(
        [
          'sessions', 'chatSlots', 'chatSlotDetail', 'createChatSlot', 'deleteChatSlot',
          'resumeChatSlot', 'deleteSession', 'agentDetail', 'spawnList', 'fetchHistory',
          'renameSlot', 'forkSession',
        ].map(k => [k, vi.fn().mockResolvedValue({})]),
      ),
      chatFolders: vi.fn().mockResolvedValue([]),
      sessionsSearch: sessionsSearchMock,
    },
  }
})

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
import { useDebouncedSessionSearchState } from '../pages/chat-sidebar/search'
import type { ChatSlot, ChatHistoryItem } from '../types'
import type { RootState } from '../store'

const QUERY_A = 'alpha notes'
const QUERY_B = 'bravo notes'
const ROW_A = { key: 'dashboard_chat-a', title: 'Alpha result row', modified: 1_000_000 }
const ROW_B = { key: 'dashboard_chat-b', title: 'Bravo result row', modified: 2_000_000 }

const slot = (key: string, title: string): ChatSlot => ({
  key, title, messages: 1, running: false, mode: '', created: '', last_ts: '2026-01-01T00:00:00Z',
} as ChatSlot)

const histItem = (key: string, title: string): ChatHistoryItem => ({
  key, title, last_ts: '2026-01-01T00:00:00Z',
} as unknown as ChatHistoryItem)

function renderSidebar() {
  const slots = [slot('s1', 'Session 1')]
  const history = [histItem('h1', 'Placeholder history row')]
  const store = createTestStore({
    dashboard: {
      status: { platform: 'darwin' },
      connected: true,
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

/** A promise the test settles by hand, so it decides which response lands first. */
function deferred<T>() {
  let resolve!: (v: T) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

describe('ChatSidebar – a failed Older Sessions search', () => {
  beforeEach(() => {
    sessionsSearchMock.mockReset()
    localStorage.clear()
  })

  it('drops the previous query rows and shows the error line', async () => {
    sessionsSearchMock.mockImplementation((q: string) =>
      q === QUERY_A ? Promise.resolve({ sessions: [ROW_A] }) : Promise.reject(new Error('HTTP 500')))
    renderSidebar()

    fireEvent.click(screen.getByRole('button', { name: /^older sessions$/i }))
    const box = screen.getByPlaceholderText(/search older sessions/i)

    fireEvent.change(box, { target: { value: QUERY_A } })
    await screen.findByText('Alpha result row')
    expect(screen.queryByText('Search failed. Showing title matches only.')).toBeNull()

    fireEvent.change(box, { target: { value: QUERY_B } })
    await waitFor(() => expect(sessionsSearchMock).toHaveBeenCalledWith(QUERY_B))
    await screen.findByText('Search failed. Showing title matches only.')
    expect(screen.queryByText('Alpha result row')).toBeNull()
  })

  it('clears the error line once a later search succeeds', async () => {
    sessionsSearchMock.mockImplementation((q: string) =>
      q === QUERY_B ? Promise.reject(new Error('HTTP 500')) : Promise.resolve({ sessions: [ROW_A] }))
    renderSidebar()

    fireEvent.click(screen.getByRole('button', { name: /^older sessions$/i }))
    const box = screen.getByPlaceholderText(/search older sessions/i)

    fireEvent.change(box, { target: { value: QUERY_B } })
    await screen.findByText('Search failed. Showing title matches only.')

    fireEvent.change(box, { target: { value: QUERY_A } })
    await screen.findByText('Alpha result row')
    expect(screen.queryByText('Search failed. Showing title matches only.')).toBeNull()
  })
})

describe('useDebouncedSessionSearchState – only the newest request writes', () => {
  beforeEach(() => {
    sessionsSearchMock.mockReset()
  })

  const identity = <T,>(s: T) => s

  it('a slow, older success does not overwrite the newer result', async () => {
    const slowA = deferred<{ sessions: typeof ROW_A[] }>()
    sessionsSearchMock.mockImplementation((q: string) =>
      q === QUERY_A ? slowA.promise : Promise.resolve({ sessions: [ROW_B] }))
    const { result, rerender } = renderHook(({ q }) => useDebouncedSessionSearchState(q, identity), {
      initialProps: { q: QUERY_A },
    })

    await waitFor(() => expect(sessionsSearchMock).toHaveBeenCalledWith(QUERY_A))
    rerender({ q: QUERY_B })
    await waitFor(() => expect(result.current.result).toEqual([ROW_B]))

    await act(async () => { slowA.resolve({ sessions: [ROW_A] }); await slowA.promise })
    expect(result.current).toEqual({ result: [ROW_B], failed: false })
  })

  it('drops the failure flag as soon as a new query is typed', async () => {
    const pendingA = deferred<{ sessions: typeof ROW_A[] }>()
    sessionsSearchMock.mockImplementation((q: string) =>
      q === QUERY_B ? Promise.reject(new Error('HTTP 500')) : pendingA.promise)
    const { result, rerender } = renderHook(({ q }) => useDebouncedSessionSearchState(q, identity), {
      initialProps: { q: QUERY_B },
    })

    await waitFor(() => expect(result.current.failed).toBe(true))
    rerender({ q: QUERY_A })
    // A's request has not answered, so nothing has failed for A yet.
    expect(result.current).toEqual({ result: null, failed: false })

    await act(async () => { pendingA.resolve({ sessions: [ROW_A] }); await pendingA.promise })
    await waitFor(() => expect(result.current).toEqual({ result: [ROW_A], failed: false }))
  })

  it('a slow, older failure does not wipe the newer result', async () => {
    const slowA = deferred<{ sessions: typeof ROW_A[] }>()
    sessionsSearchMock.mockImplementation((q: string) =>
      q === QUERY_A ? slowA.promise : Promise.resolve({ sessions: [ROW_B] }))
    const { result, rerender } = renderHook(({ q }) => useDebouncedSessionSearchState(q, identity), {
      initialProps: { q: QUERY_A },
    })

    await waitFor(() => expect(sessionsSearchMock).toHaveBeenCalledWith(QUERY_A))
    rerender({ q: QUERY_B })
    await waitFor(() => expect(result.current.result).toEqual([ROW_B]))

    await act(async () => { slowA.reject(new Error('HTTP 500')); await slowA.promise.catch(() => {}) })
    expect(result.current).toEqual({ result: [ROW_B], failed: false })
  })
})
