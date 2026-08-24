/**
 * Regression test for #4523 on the split-pane surface: ChatPane's model chip
 * reads `paneSlot.model` from the Redux store, so the pane's switchModel must
 * write the store as soon as the API call resolves — the pane must not depend
 * on the server's coalesced slots rebroadcast (which never arrives when the
 * websocket is down) to see its own pick. No websocket exists in this
 * harness, so the store can only move if the callback writes it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, act, fireEvent, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([
      { model_name: 'auto', description: 'Models chosen by task' },
      { model_name: 'claude-opus-5', description: 'Claude Opus 5' },
      { model_name: 'claude-sonnet-5', description: 'Claude Sonnet 5' },
    ]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    chatSlotModel: vi.fn().mockResolvedValue({ ok: true, model: 'claude-sonnet-5' }),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    // The agent switch also names the re-resolved workspace binding.
    chatSlotAgent: vi.fn().mockResolvedValue({ ok: true, agent: 'writer', workspace: 'writing-ws' }),
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [{ name: 'default' }, { name: 'writer' }], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'
import { api } from '../api/client'

function makeStore(slotKey: string) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: slotKey, messages: 0, running: false, mode: '', agent: 'default', model: 'claude-opus-5', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
}

function renderPane(slotKey: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = makeStore(slotKey)
  return Object.assign(render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatPane slotKey={slotKey} />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>,
  ), { store })
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('ChatPane — model switch updates the pane label without a slot-list round trip (#4523)', () => {
  it('writes the store on API success so the chip follows the pick', async () => {
    const { store } = renderPane('pane-1')
    const chip = await waitFor(() => screen.getByTitle('Model: claude-opus-5'))
    const slotsFetchesBeforePick = vi.mocked(api.chatSlots).mock.calls.length

    await act(async () => { fireEvent.click(chip) })
    const option = await waitFor(() => screen.getByRole('option', { name: /claude-sonnet-5/ }))
    await act(async () => { fireEvent.click(option) })

    await waitFor(() => expect(api.chatSlotModel).toHaveBeenCalledWith('pane-1', 'claude-sonnet-5'))
    await waitFor(() => expect(store.getState().dashboard.slots.find(s => s.key === 'pane-1')?.model).toBe('claude-sonnet-5'))
    // The label moved because the store was written, not because anything
    // re-fetched the slot list (and no websocket exists in this harness).
    expect(vi.mocked(api.chatSlots).mock.calls.length).toBe(slotsFetchesBeforePick)
    expect(await waitFor(() => screen.getByTitle('Model: claude-sonnet-5'))).toBeTruthy()
  })

  it('keeps the pre-switch model when the call fails', async () => {
    vi.mocked(api.chatSlotModel).mockRejectedValueOnce(new Error('boom'))
    const { store } = renderPane('pane-2')
    const chip = await waitFor(() => screen.getByTitle('Model: claude-opus-5'))
    await act(async () => { fireEvent.click(chip) })
    const option = await waitFor(() => screen.getByRole('option', { name: /claude-sonnet-5/ }))
    await act(async () => { fireEvent.click(option) })

    await waitFor(() => expect(api.chatSlotModel).toHaveBeenCalled())
    expect(store.getState().dashboard.slots.find(s => s.key === 'pane-2')?.model).toBe('claude-opus-5')
  })

  it('agent pick writes agent AND workspace to the store on API success (#5120)', async () => {
    const { store } = renderPane('pane-3')
    const chip = await waitFor(() => screen.getByTitle('Agent: default'))
    const slotsFetchesBeforePick = vi.mocked(api.chatSlots).mock.calls.length
    await act(async () => { fireEvent.click(chip) })
    const option = await waitFor(() => screen.getByRole('option', { name: /writer/ }))
    await act(async () => { fireEvent.click(option) })

    await waitFor(() => expect(api.chatSlotAgent).toHaveBeenCalledWith('pane-3', 'writer'))
    // The write mirrors exactly what the response names: the stored agent
    // plus the re-resolved workspace binding, as one pair — and it happens
    // without a slot-list refetch (no websocket exists in this harness).
    const slot = () => store.getState().dashboard.slots.find(s => s.key === 'pane-3')
    await waitFor(() => expect(slot()?.agent).toBe('writer'))
    expect(slot()?.workspace).toBe('writing-ws')
    expect(vi.mocked(api.chatSlots).mock.calls.length).toBe(slotsFetchesBeforePick)
  })

  it('keeps the pre-switch agent when the agent switch fails (#5120)', async () => {
    vi.mocked(api.chatSlotAgent).mockRejectedValueOnce(new Error('boom'))
    const { store } = renderPane('pane-4')
    const chip = await waitFor(() => screen.getByTitle('Agent: default'))
    await act(async () => { fireEvent.click(chip) })
    const option = await waitFor(() => screen.getByRole('option', { name: /writer/ }))
    await act(async () => { fireEvent.click(option) })

    await waitFor(() => expect(api.chatSlotAgent).toHaveBeenCalled())
    expect(store.getState().dashboard.slots.find(s => s.key === 'pane-4')?.agent).toBe('default')
  })
})
