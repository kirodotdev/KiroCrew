import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, waitFor, act } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { setActiveSlot, replaceMessages, sseChatMessage } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

/* `slotHydrated` means "a pane shows this slot now" (#11139): a ChatPane flags
 * its slot at mount and releases it when the last pane showing the slot
 * unmounts, which re-opens the background retention bound for a monitor pane
 * that was opened and then left. The release also drops the slot's cached
 * hydrate page, so a remount reads the server again rather than replaying the
 * first mount's snapshot (`staleTime: Infinity` would never refetch it). */

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    sendChat: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    uploadFiles: vi.fn().mockResolvedValue({ paths: [] }),
    screenshot: vi.fn().mockResolvedValue({ path: null }),
    fileSearch: vi.fn().mockResolvedValue({ root: '/repo', results: [] }),
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'
import { api } from '../api/client'

const SLOT = 'pane-release-1'

function makeStore() {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: SLOT, messages: 0, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        slotsLoaded: true,
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
}
type Store = ReturnType<typeof makeStore>

function Panes({ store, qc, count }: { store: Store; qc: QueryClient; count: number }) {
  return (
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter>
            {Array.from({ length: count }, (_, i) => <ChatPane key={i} slotKey={SLOT} />)}
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>
  )
}

const detail = api.chatSlotDetail as ReturnType<typeof vi.fn>
/** Let the deferred release run. */
const settle = () => act(async () => { await Promise.resolve() })

beforeEach(() => {
  vi.clearAllMocks()
  detail.mockResolvedValue({
    messages: [{ role: 'assistant', content: 'hello', cls: 'msg msg-a', ts: '2026-01-01T00:00:00Z', meta: { mid: 'm1' } }],
    running: false, has_more: true, total: 120, next_before: 119,
  })
})

describe('ChatPane releases its slot on unmount', () => {
  it('clears slotHydrated when the pane unmounts and re-reads the server on remount', async () => {
    const store = makeStore()
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const view = render(<Panes store={store} qc={qc} count={1} />)
    await waitFor(() => expect(store.getState().chat.slotHydrated?.[SLOT]).toBe(true))
    expect(detail).toHaveBeenCalledTimes(1)

    view.unmount()
    await settle()
    expect(store.getState().chat.slotHydrated?.[SLOT]).toBeFalsy()
    expect(qc.getQueryCache().findAll({ queryKey: ['slot-messages', SLOT] })).toEqual([])

    render(<Panes store={store} qc={qc} count={1} />)
    // Flagged at mount, before the fresh page answers.
    expect(store.getState().chat.slotHydrated?.[SLOT]).toBe(true)
    await waitFor(() => expect(detail).toHaveBeenCalledTimes(2))
  })

  it('keeps the slot flagged while another pane still shows it', async () => {
    const store = makeStore()
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const view = render(<Panes store={store} qc={qc} count={2} />)
    await waitFor(() => expect(store.getState().chat.slotHydrated?.[SLOT]).toBe(true))

    view.rerender(<Panes store={store} qc={qc} count={1} />)
    await settle()
    expect(store.getState().chat.slotHydrated?.[SLOT]).toBe(true)
    expect(qc.getQueryCache().findAll({ queryKey: ['slot-messages', SLOT] }).length).toBeGreaterThan(0)
  })

  it('flags a split pane left on screen when its slot stops being active, so a background turn trims nothing', async () => {
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    // Well past the background retention window, so an unheld slot is trimmed.
    const history = Array.from({ length: 250 }, (_, i) => ({
      role: i % 2 ? 'assistant' : 'user', content: `h${i}`, cls: i % 2 ? 'msg msg-a' : 'msg msg-u',
      ts: new Date(Date.UTC(2026, 0, 1, 0, 0, i)).toISOString(), meta: { mid: `h-${i}` },
    }))
    store.dispatch(replaceMessages(history))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<Panes store={store} qc={qc} count={1} />)
    // The pane's hydrate has landed (and was refused: the slot is active) before
    // the switch, so only the hold can flag the slot afterwards.
    await waitFor(() => expect(qc.getQueryCache().findAll({ queryKey: ['slot-messages', SLOT] })[0]?.state.status).toBe('success'))
    await settle()
    // The pane shows the active slot, whose mirror is live, so nothing is held.
    expect(store.getState().chat.slotHydrated?.[SLOT]).toBeFalsy()

    // The reader moves to another chat with the grid still mounted.
    await act(async () => { store.dispatch(setActiveSlot('other-chat')) })
    await settle()
    expect(store.getState().chat.slotHydrated?.[SLOT]).toBe(true)

    act(() => {
      store.dispatch(sseChatMessage({ slot: SLOT, role: 'assistant', content: 'live', meta: { mid: 'live-1' } }))
      store.dispatch(sseChatMessage({ slot: SLOT, role: '_done', content: '', turn: 1, turn_gen: 'gen-a' }))
    })
    const held = store.getState().chat.slotMessages[SLOT] ?? []
    expect(held.map(m => m.meta?.mid)).toEqual([...history.map(m => m.meta.mid), 'live-1'])
    // The re-run's cleanup released nothing: this pane still observes the slot.
    expect(qc.getQueryCache().findAll({ queryKey: ['slot-messages', SLOT] }).length).toBeGreaterThan(0)
  })
})
