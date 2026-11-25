/**
 * ChatHeaderMenu owns two things SessionActionsMenu does not: the lazily
 * fetched MCP servers submenu (three queries gated on the submenu's open
 * state, so a closed menu costs no request) and the controlled open state that
 * the colour swatch row closes after a pick (a swatch is not a Radix menu item,
 * so Radix would not close the menu by itself).
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

vi.mock('../api/client', () => ({
  ApiError: class ApiError extends Error {
    status: number
    body: string
    constructor(message: string, status = 500, body = '') {
      super(message)
      this.status = status
      this.body = body
    }
  },
  api: {
    mcpActive: vi.fn().mockResolvedValue([{ name: 'github', enabled: true }]),
    mcpServers: vi.fn().mockResolvedValue([{ name: 'github', tools: ['search_issues'], disabledTools: [] }]),
    kirocrewConfig: vi.fn().mockResolvedValue({ agent: { tool_search: false } }),
    channelTargets: vi.fn().mockResolvedValue([]),
    slackChannels: vi.fn().mockResolvedValue([]),
    chatFolders: vi.fn().mockResolvedValue([]),
    setSlotColor: vi.fn().mockResolvedValue({}),
    clearSlotColor: vi.fn().mockResolvedValue({}),
  },
}))

import type { RootState } from '../store'
import { api } from '../api/client'
import { ChatHeaderMenu } from '../pages/chat/ChatHeaderMenu'

const dashboardState = {
  status: {}, connected: true, slots: [], approvalMode: 'normal',
  channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
  subagentRunning: {}, subagentDetails: {}, subagentText: {},
  sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
} as RootState['dashboard']

function renderMenu() {
  const store = createTestStore({ dashboard: { ...dashboardState, slots: [{ key: 'chat-1-100' }] } })
  // The app client's `staleTime: Infinity` default, so a query that does not opt
  // out keeps serving its first answer here as it does in the app.
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  const utils = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatHeaderMenu activeSlot="chat-1-100" agent="default" />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  // Radix opens the trigger on keyboard activation, a path jsdom handles.
  fireEvent.keyDown(utils.container.querySelector('button')!, { key: 'Enter' })
  return utils
}

beforeEach(() => vi.clearAllMocks())

describe('ChatHeaderMenu — MCP submenu and colour-pick close', () => {
  it('fetches the MCP server lists only once the submenu opens', async () => {
    renderMenu()
    const sub = await screen.findByText('MCP servers')
    // Menu open, submenu shut: no MCP server request yet. The config read is
    // a shared ['kirocrewConfig'] cache other menu rows also fill, so only the
    // two MCP-keyed queries are pinned here.
    expect(api.mcpActive).not.toHaveBeenCalled()
    expect(api.mcpServers).not.toHaveBeenCalled()

    // A Radix sub-trigger opens on ArrowRight.
    fireEvent.keyDown(sub.closest('[role="menuitem"]')!, { key: 'ArrowRight' })

    await waitFor(() => {
      expect(api.mcpActive).toHaveBeenCalledWith('default', 'chat-1-100')
      expect(api.mcpServers).toHaveBeenCalled()
      expect(api.kirocrewConfig).toHaveBeenCalled()
    })
    // The per-server tool list the panel derives from the full server list.
    expect(await screen.findByText('github')).toBeInTheDocument()
  })

  it('asks the server again each time the submenu opens', async () => {
    const utils = renderMenu()
    const sub = await screen.findByText('MCP servers')
    const trigger = sub.closest('[role="menuitem"]')!
    fireEvent.keyDown(trigger, { key: 'ArrowRight' })
    await waitFor(() => expect(api.mcpActive).toHaveBeenCalledTimes(1))
    // The answer depends on the chat's live session, which can start in between:
    // close the whole menu, open it and the submenu again.
    fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByText('MCP servers')).not.toBeInTheDocument())
    fireEvent.keyDown(utils.container.querySelector('button')!, { key: 'Enter' })
    const again = await screen.findByText('MCP servers')
    fireEvent.keyDown(again.closest('[role="menuitem"]')!, { key: 'ArrowRight' })
    await waitFor(() => expect(api.mcpActive).toHaveBeenCalledTimes(2))
  })

  it('says a refused server read failed instead of staying on Loading', async () => {
    vi.mocked(api.mcpActive).mockRejectedValueOnce(new Error('mcp_config_busy'))
    renderMenu()
    const sub = await screen.findByText('MCP servers')
    fireEvent.keyDown(sub.closest('[role="menuitem"]')!, { key: 'ArrowRight' })
    expect(await screen.findByText("Couldn't load the MCP servers")).toBeInTheDocument()
    expect(screen.queryByText('Loading...')).not.toBeInTheDocument()
  })

  it('says there are no servers when the answer is empty', async () => {
    vi.mocked(api.mcpActive).mockResolvedValueOnce([])
    renderMenu()
    const sub = await screen.findByText('MCP servers')
    fireEvent.keyDown(sub.closest('[role="menuitem"]')!, { key: 'ArrowRight' })
    expect(await screen.findByText('None loaded')).toBeInTheDocument()
    expect(screen.queryByText('Loading...')).not.toBeInTheDocument()
  })

  it('drops the earlier list when a later read fails', async () => {
    const utils = renderMenu()
    const sub = await screen.findByText('MCP servers')
    fireEvent.keyDown(sub.closest('[role="menuitem"]')!, { key: 'ArrowRight' })
    expect(await screen.findByText('github')).toBeInTheDocument()
    vi.mocked(api.mcpActive).mockRejectedValueOnce(new Error('mcp_config_busy'))
    fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByText('MCP servers')).not.toBeInTheDocument())
    fireEvent.keyDown(utils.container.querySelector('button')!, { key: 'Enter' })
    const again = await screen.findByText('MCP servers')
    fireEvent.keyDown(again.closest('[role="menuitem"]')!, { key: 'ArrowRight' })
    expect(await screen.findByText("Couldn't load the MCP servers")).toBeInTheDocument()
    expect(screen.queryByText('github')).not.toBeInTheDocument()
  })

  it('closes the menu after a swatch pick', async () => {
    renderMenu()
    const swatch = await screen.findByRole('button', { name: 'No color' })
    fireEvent.click(swatch)
    await waitFor(() => expect(screen.queryByText('MCP servers')).not.toBeInTheDocument())
    expect(api.clearSlotColor).toHaveBeenCalledWith('chat-1-100')
  })
})
