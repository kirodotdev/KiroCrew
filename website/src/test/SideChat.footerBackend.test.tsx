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
    sideTools: vi.fn(),
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

/** The answer GET /api/chat/side/tools sends: what a side turn runs right now. */
function serveSideTools(answer: { read_only_tools: boolean; claude_adapter_outdated?: boolean; config_unavailable?: boolean }) {
  vi.mocked(api.sideTools).mockResolvedValue({
    claude_adapter_outdated: false,
    config_unavailable: false,
    ...answer,
  })
}

describe('SideChat footer follows the gateway answer for the side turn', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('says lookups work when the side turn runs read-only tools', async () => {
    serveSideTools({ read_only_tools: true })
    renderSide()
    expect(await screen.findByText(/Lookups work here, but changes don't/)).toBeTruthy()
    expect(screen.queryByText(/Tools are unavailable here/)).toBeNull()
  })

  it('says tools are unavailable on a backend outside the set', async () => {
    serveSideTools({ read_only_tools: false })
    renderSide()
    expect(await screen.findByText(/Tools are unavailable here/)).toBeTruthy()
    expect(screen.queryByText(/Lookups work here, but changes don't/)).toBeNull()
  })

  it('says to update claude-agent-acp when only the adapter is too old', async () => {
    serveSideTools({ read_only_tools: false, claude_adapter_outdated: true })
    renderSide()
    expect(await screen.findByText(/Update claude-agent-acp/)).toBeTruthy()
    expect(screen.queryByText(/Lookups work here, but changes don't/)).toBeNull()
  })

  it('says only that the tools are unknown when the answer cannot load', async () => {
    vi.mocked(api.sideTools).mockRejectedValue(new Error('down'))
    renderSide()
    expect((await screen.findByTestId('side-chat-tools-unknown')).textContent).toMatch(
      /Couldn't check which tools this agent backend allows here/,
    )
    expect(screen.queryByRole('note')).toBeNull()
    expect(screen.queryByTestId('side-chat-config-error')).toBeNull()
  })

  it('says the config failed only when the config is what failed', async () => {
    serveSideTools({ read_only_tools: false, config_unavailable: true })
    renderSide()
    expect(await screen.findByTestId('side-chat-config-error')).toBeTruthy()
    expect(screen.queryByTestId('side-chat-tools-unknown')).toBeNull()
  })
})
