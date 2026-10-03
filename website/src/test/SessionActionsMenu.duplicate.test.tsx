/**
 * The sidebar row's single-menu form (phones, and any touch screen) replaces the
 * hover cluster that hosts the fork button, so the menu must carry "Fork chat"
 * itself when the row passes it (#16101 relabelled pages.chatSidebar.duplicate
 * from "Duplicate" to "Fork chat"). Surfaces that pass no handler get no item.
 */
import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { DropdownMenu, DropdownMenuContent, DropdownMenuTrigger } from '../components/ui/dropdown-menu'

vi.mock('../api/client', () => ({
  api: {
    slackChannels: vi.fn().mockResolvedValue([]),
    mcpActive: vi.fn().mockResolvedValue([]),
    setSlotColor: vi.fn().mockResolvedValue({}),
    chatFolders: vi.fn().mockResolvedValue([]),
  },
}))

import SessionActionsMenu from '../components/SessionActionsMenu'
import type { RootState } from '../store'

const dashboardState = {
  status: {}, connected: true, slots: [{ key: 'chat-1', title: 'My Session' }], approvalMode: 'normal',
  channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
  subagentRunning: {}, subagentDetails: {}, subagentText: {},
  sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
} as unknown as RootState['dashboard']

function renderMenu(onDuplicate?: () => void) {
  const store = createTestStore({ dashboard: dashboardState })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const utils = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <DropdownMenu>
              <DropdownMenuTrigger>menu</DropdownMenuTrigger>
              <DropdownMenuContent>
                <SessionActionsMenu variant="dropdown" slotKey="chat-1" onDuplicate={onDuplicate} />
              </DropdownMenuContent>
            </DropdownMenu>
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  // Radix opens a dropdown on keyboard activation, the path jsdom handles.
  fireEvent.keyDown(utils.getByText('menu'), { key: 'Enter' })
  return utils
}

describe('SessionActionsMenu Fork chat item', () => {
  it('renders Fork chat when the row passes a handler, and selecting it calls the handler', async () => {
    const onDuplicate = vi.fn()
    renderMenu(onDuplicate)
    fireEvent.click(await screen.findByText('Fork chat'))
    expect(onDuplicate).toHaveBeenCalledTimes(1)
  })

  it('renders no Fork chat item when no handler is passed', async () => {
    renderMenu()
    expect(await screen.findByText('Pin')).toBeTruthy()
    expect(screen.queryByText('Fork chat')).toBeNull()
  })
})
