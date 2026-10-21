/**
 * Two discoverability actions on the shared session menu (#11823): "Copy session
 * ID" (always present — the bare key, not a URL) and "Reference in chat" (present
 * only when the row passes a handler, the way "Fork chat" is sidebar-only because
 * the header's own session would be a self-reference).
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

// copyId copies the bare key through this; stub it so the test asserts the key,
// not the jsdom clipboard.
const copyToClipboard = vi.hoisted(() => vi.fn(() => Promise.resolve(true)))
vi.mock('../utils/clipboard', () => ({ copyToClipboard }))

import SessionActionsMenu from '../components/SessionActionsMenu'
import type { RootState } from '../store'

const dashboardState = {
  status: {}, connected: true, slots: [{ key: 'chat-1', title: 'My Session' }], approvalMode: 'normal',
  channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
  subagentRunning: {}, subagentDetails: {}, subagentText: {},
  sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
} as unknown as RootState['dashboard']

function renderMenu(onReferenceInChat?: () => void) {
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
                <SessionActionsMenu variant="dropdown" slotKey="chat-1" onReferenceInChat={onReferenceInChat} />
              </DropdownMenuContent>
            </DropdownMenu>
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  fireEvent.keyDown(utils.getByText('menu'), { key: 'Enter' })
  return utils
}

describe('SessionActionsMenu copy-session-id item', () => {
  it('always renders Copy session ID and copies the bare key (not a URL)', async () => {
    renderMenu()
    fireEvent.click(await screen.findByText('Copy session ID'))
    expect(copyToClipboard).toHaveBeenCalledWith('chat-1')
  })

  it('surfaces a failure through ErrorNotice when both clipboard layers refuse', async () => {
    copyToClipboard.mockResolvedValueOnce(false)
    renderMenu()
    fireEvent.click(await screen.findByText('Copy session ID'))
    // The failure is recorded in the store and rendered through the menu's
    // ErrorNotice (not swallowed into a silent no-op).
    expect(await screen.findByTestId('session-menu-copy-id-error')).toBeTruthy()
  })
})

describe('SessionActionsMenu reference-in-chat item', () => {
  it('renders Reference in chat when the row passes a handler, and selecting it calls it', async () => {
    const onReferenceInChat = vi.fn()
    renderMenu(onReferenceInChat)
    fireEvent.click(await screen.findByText('Reference in chat'))
    expect(onReferenceInChat).toHaveBeenCalledTimes(1)
  })

  it('renders no Reference in chat item when no handler is passed', async () => {
    renderMenu()
    expect(await screen.findByText('Pin')).toBeTruthy()
    expect(screen.queryByText('Reference in chat')).toBeNull()
  })
})
