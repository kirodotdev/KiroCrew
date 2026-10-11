import { describe, it, expect, vi, beforeEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import SideChat from '../pages/chat/SideChat'
import { api } from '../api/client'

vi.mock('../api/client', () => ({
  api: {
    sideOpen: vi.fn().mockResolvedValue({ ok: true, open: true, messages: 0, last_run_id: '', created_at: '' }),
    sideTurn: vi.fn(),
    sideClose: vi.fn(),
    sideStop: vi.fn(),
    kirocrewConfig: vi.fn(),
    acpBackends: vi.fn(),
  },
}))

function renderSide() {
  const store = configureStore({
    reducer: { chat: chatReducer, dashboard: dashboardReducer },
    preloadedState: {
      chat: {
        ...chatReducer(undefined, { type: '@@INIT' }),
        activeSlot: 'slot-1',
        slotSide: { 'slot-1': { messages: [], openedAtTurnCount: 0, createdAt: '', lastRunId: '', pending: false, streaming: false, queue: [] } },
      },
      dashboard: { ...dashboardReducer(undefined, { type: '@@INIT' }), connected: true },
    } as never,
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <SideChat slot="slot-1" />
      </Provider>
    </QueryClientProvider>,
  )
}

/** The card rows the gateway sends: the side_chat_tools line per backend. */
function serveBackends(withTools: string[]) {
  const ids = ['', 'kas', 'claude']
  vi.mocked(api.acpBackends).mockResolvedValue({
    backends: ids.map(id => ({
      id,
      capabilities: [{ id: 'side_chat_tools', available: withTools.includes(id), measured: true, unmeasured_reason: '' }],
    })),
  } as never)
}

describe('SideChat footer follows the side_chat_tools line of the configured backend', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    serveBackends(['', 'kas'])
  })

  it.each([
    ['kiro', ''],
    ['kas', 'kas'],
  ])('says lookups work on %s', async (_name, backend) => {
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: backend } } as never)
    renderSide()
    expect(await screen.findByText(/Lookups work here, but changes don't/)).toBeTruthy()
    expect(screen.queryByText(/Tools are unavailable here/)).toBeNull()
  })

  it('says tools are unavailable on a backend outside the set', async () => {
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'claude' } } as never)
    renderSide()
    expect(await screen.findByText(/Tools are unavailable here/)).toBeTruthy()
    expect(screen.queryByText(/Lookups work here, but changes don't/)).toBeNull()
  })

  it('follows the server line, not a frontend list', async () => {
    serveBackends([''])
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'kas' } } as never)
    renderSide()
    expect(await screen.findByText(/Tools are unavailable here/)).toBeTruthy()
  })

  it('says only that the tools are unknown when the backend card cannot load', async () => {
    vi.mocked(api.acpBackends).mockRejectedValue(new Error('down'))
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: '' } } as never)
    renderSide()
    expect((await screen.findByTestId('side-chat-tools-unknown')).textContent).toMatch(
      /Couldn't check which tools this agent backend allows here/,
    )
    expect(screen.queryByRole('note')).toBeNull()
    expect(screen.queryByTestId('side-chat-config-error')).toBeNull()
  })

  it('says the config failed only when the config is what failed', async () => {
    vi.mocked(api.kirocrewConfig).mockRejectedValue(new Error('down'))
    renderSide()
    expect(await screen.findByTestId('side-chat-config-error')).toBeTruthy()
    expect(screen.queryByTestId('side-chat-tools-unknown')).toBeNull()
  })
})
