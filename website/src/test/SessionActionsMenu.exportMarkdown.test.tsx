/**
 * Regression tests for the session menu's "Export as Markdown" item.
 *
 * Two things can only be checked here. First, the item has to be REACHABLE: it
 * lives in the same group as the self-hiding instance submenu, so a mis-placed
 * entry disappears without any test noticing. Second, the export streams a file
 * built server-side and can fail after the menu has already closed — Radix
 * `onSelect` is sync, so handing it the bare promise would turn a failed export
 * into an unhandled rejection and a menu that looks like it worked.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

vi.mock('../api/client', () => ({
  api: {
    slackChannels: vi.fn().mockResolvedValue([]),
    mcpActive: vi.fn().mockResolvedValue([]),
    setSlotColor: vi.fn().mockResolvedValue({}),
    chatFolders: vi.fn().mockResolvedValue([]),
    exportSessionMarkdown: vi.fn().mockResolvedValue(undefined),
  },
}))

import { api } from '../api/client'
import { ChatHeaderMenu } from '../pages/ChatPage'
import { __resetForTests } from '../utils/chatPopout'

const dashboardState = {
  status: {}, connected: true, slots: [], approvalMode: 'normal',
  channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
  subagentRunning: {}, subagentDetails: {}, subagentText: {},
  sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
} as any

const slot = { key: 'chat-1', title: 'My Session' } as any

function renderMenu() {
  const store = createTestStore({ dashboard: { ...dashboardState, slots: [{ ...slot }] } })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const utils = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatHeaderMenu activeSlot={slot.key} />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  // Radix DropdownMenuTrigger opens on keyboard activation — the path jsdom
  // handles, unlike the PointerEvent-driven mouse open.
  fireEvent.keyDown(utils.container.querySelector('button')!, { key: 'Enter' })
  return utils
}

beforeEach(() => {
  __resetForTests()
  vi.clearAllMocks()
})

afterEach(() => {
  __resetForTests()
  vi.restoreAllMocks()
})

describe('SessionActionsMenu "Export as Markdown"', () => {
  it('renders the item and exports the session the menu is keyed on', async () => {
    renderMenu()
    fireEvent.click(await screen.findByText('Export as Markdown'))
    await waitFor(() => expect(api.exportSessionMarkdown).toHaveBeenCalledWith('chat-1'))
  })

  it('swallows a failed export instead of leaving an unhandled rejection', async () => {
    vi.mocked(api.exportSessionMarkdown).mockRejectedValue(new Error('session not found'))
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    renderMenu()
    fireEvent.click(await screen.findByText('Export as Markdown'))
    await waitFor(() => expect(errSpy).toHaveBeenCalled())
    expect(String(errSpy.mock.calls[0]?.[0])).toContain('Failed to export session as Markdown')
  })
})
