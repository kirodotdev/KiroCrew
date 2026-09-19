/**
 * Regression test: a backend pick on the welcome screen must apply to the
 * chat it is made in.
 *
 * The welcome surface renders on an ALREADY-CREATED empty slot (the new-chat
 * click creates it before the picker is ever shown). The first wiring parked
 * the pick in the `pendingBackend` ref, which only the NEXT slot creation
 * reads — so the trigger showed the pick while this chat kept `acp_backend ==
 * ''` and its first message spawned the global default (kiro-cli). Found on
 * the pod hands-test: every chat's backend chip read "Kiro CLI" regardless of
 * the pick.
 *
 * The fix mirrors `switchModel`: with an active slot the pick goes through the
 * per-slot mutation endpoint (`POST /api/chat/slots/{slot}/backend`) and the
 * store's slot row is written with the stored value, so the composer chip and
 * the backend-keyed model list follow it. These tests pin that contract and
 * that the picker's displayed value is the slot's STORED pick, not the ref.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, waitFor, fireEvent } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'

interface VirtuosoMockProps {
  data?: unknown[]
  itemContent: (index: number, item: unknown) => ReactNode
}
vi.mock('react-virtuoso', () => ({ Virtuoso: ({ data, itemContent }: VirtuosoMockProps) => <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div> }))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([{ model_name: 'auto', description: 'Auto' }]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({ model: 'auto' }),
    agentResolvedModel: vi.fn().mockResolvedValue({ model: 'auto' }),
    chatSlotModel: vi.fn().mockResolvedValue({ ok: true }),
    // The endpoint under test. Echoes the stored value like the server does.
    chatSlotBackend: vi.fn().mockImplementation(async (_slot: string, backend: string) => ({ ok: true, backend })),
    backends: vi.fn().mockResolvedValue({
      backends: [
        { id: '', label: 'Kiro CLI', is_global_default: true },
        { id: 'acme', label: 'Acme Agent', is_global_default: false },
      ],
      invalid: [],
      unroutable: [],
    }),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    slackChannels: vi.fn().mockResolvedValue([]),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [{ name: 'kirocrew' }], defaultAgent: 'kirocrew' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
// A stub WelcomeView that exposes exactly the two props under test: the
// displayed backend value and a button that fires the pick, so the test drives
// the ChatPage handoff without rendering the real picker's listbox.
vi.mock('../components/WelcomeView', () => ({
  default: ({ backend, onSelectBackend }: { backend?: string; onSelectBackend?: (id: string) => void }) => (
    <div>
      <span data-testid="welcome-backend-value">{backend ?? '(undefined)'}</span>
      <button data-testid="pick-acme" onClick={() => onSelectBackend?.('acme')}>pick acme</button>
    </div>
  ),
}))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPage from '../pages/ChatPage'

function makeStore(acp_backend?: string) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null,
        // An EMPTY slot (messages: 0) is the welcome state — exactly the surface
        // the picker renders on.
        slots: [{ key: 'slot-a', messages: 0, running: false, mode: '', agent: 'kirocrew', model: 'auto', acp_backend, pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot: 'slot-a', messages: [],
        slotRunning: false, slotStopping: false, slotState: 'idle',
        history: [], historyHasMore: false, pendingInput: null,
        subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools',
        slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
        slotStatusDetail: {}, slotContextPct: {}, slotActivity: {}, slotHistory: [],
        historyOffset: 0, _wsChunkedDuringFetch: false,
        slotMessages: {}, slotLoading: false,
      } as unknown as RootState['chat'],
      notifications: { items: [] } as unknown as RootState['notifications'],
    },
  })
}

async function renderChat(acp_backend?: string) {
  const store = makeStore(acp_backend)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => {
    render(
      <QueryClientProvider client={qc}>
        <Provider store={store}>
          <ThemeProvider>
            <MemoryRouter><ChatPage /></MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    )
  })
  await waitFor(() => expect(screen.getByTestId('pick-acme')).toBeTruthy())
  return store
}

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  vi.clearAllMocks()
})

describe('ChatPage — welcome-screen backend pick applies to the current slot', { timeout: 15_000 }, () => {
  it('writes the pick to the existing slot through the mutation endpoint', async () => {
    const { api } = await import('../api/client')
    const store = await renderChat('')
    await act(async () => { fireEvent.click(screen.getByTestId('pick-acme')) })

    // THE bug: this call never happened — the pick only parked in a ref.
    await waitFor(() => expect(api.chatSlotBackend).toHaveBeenCalledWith('slot-a', 'acme'))
    // The store's slot row carries the stored value, so the composer chip and
    // the backend-keyed model list follow the pick without a slots rebroadcast.
    await waitFor(() => {
      const slot = (store.getState() as RootState).dashboard.slots.find(s => s.key === 'slot-a')
      expect(slot?.acp_backend).toBe('acme')
    })
  })

  it('shows the slot\'s STORED pick on the picker, not a stale pending value', async () => {
    await renderChat('acme')
    // A slot created elsewhere with a pin: switching onto it must display that
    // pin (the ref was never set on this surface).
    expect(screen.getByTestId('welcome-backend-value').textContent).toBe('acme')
  })

  it('displays the inherit-default state for a slot with no pin', async () => {
    await renderChat(undefined)
    expect(screen.getByTestId('welcome-backend-value').textContent).toBe('')
  })
})
