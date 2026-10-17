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
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
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
      expect(api.mcpActive).toHaveBeenCalledWith('default')
      expect(api.mcpServers).toHaveBeenCalled()
      expect(api.kirocrewConfig).toHaveBeenCalled()
    })
    // The per-server tool list the panel derives from the full server list.
    expect(await screen.findByText('github')).toBeInTheDocument()
  })

  it('closes the menu after a swatch pick', async () => {
    renderMenu()
    const swatch = await screen.findByRole('button', { name: 'No color' })
    fireEvent.click(swatch)
    await waitFor(() => expect(screen.queryByText('MCP servers')).not.toBeInTheDocument())
    expect(api.clearSlotColor).toHaveBeenCalledWith('chat-1-100')
  })
})
