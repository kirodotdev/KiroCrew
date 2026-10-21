/**
 * The session menu's Rename while the gateway is down. `MobileTopBar` renders
 * this menu until Rename fires and only then mounts the title editor, so the
 * editor's own open-time gate never sees the phone's rename: on a phone this
 * menu is the only rename entry there is.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { ThemeProvider } from '../hooks/useTheme'
import { DropdownMenu, DropdownMenuContent, DropdownMenuTrigger } from '../components/ui/dropdown-menu'

vi.mock('../api/client', () => ({
  api: {
    slackChannels: vi.fn().mockResolvedValue([]),
    mcpActive: vi.fn().mockResolvedValue([]),
    setSlotColor: vi.fn().mockResolvedValue({}),
    chatFolders: vi.fn().mockResolvedValue([]),
  },
  ApiError: class ApiError extends Error {},
}))

import SessionActionsMenu from '../components/SessionActionsMenu'
import { store } from '../store'
import { sseSlots, sseConnected, sseDisconnected } from '../store/dashboardSlice'
import { setActiveSlot } from '../store/chatSlice'

function renderMenu(connected: boolean) {
  store.dispatch(sseSlots([{ key: 'chat-row', title: 'Row', messages: 0, running: false }]))
  store.dispatch(setActiveSlot('chat-row'))
  store.dispatch(connected ? sseConnected() : sseDisconnected())
  const onRename = vi.fn()
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const utils = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <DropdownMenu>
              <DropdownMenuTrigger>menu</DropdownMenuTrigger>
              <DropdownMenuContent>
                <SessionActionsMenu variant="dropdown" slotKey="chat-row" onRename={onRename} />
              </DropdownMenuContent>
            </DropdownMenu>
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  fireEvent.keyDown(utils.getByText('menu'), { key: 'Enter' })
  return { ...utils, onRename, item: () => screen.getByText('Rename').closest('[role="menuitem"]') as HTMLElement }
}

beforeEach(() => { vi.clearAllMocks() })

describe('the session menu Rename while the gateway is offline', () => {
  it('refuses it and names the reason', () => {
    const { onRename, item } = renderMenu(false)
    expect(item().getAttribute('aria-disabled')).toBe('true')
    expect(item().getAttribute('title')).toContain('rename sessions')
    fireEvent.click(item())
    expect(onRename).not.toHaveBeenCalled()
  })

  it('shows the reason as visible text, since a touch screen never shows the tooltip', () => {
    renderMenu(false)
    expect(screen.getByTestId('session-menu-offline-reason').textContent).toBeTruthy()
  })

  it('runs it while connected', () => {
    const { onRename, item } = renderMenu(true)
    // `offlineMenuRow` adds the offline props ONLY while offline, so an online
    // row carries no `aria-disabled` to contradict its own `disabled` state.
    expect(item().getAttribute('aria-disabled')).toBeNull()
    expect(screen.queryByTestId('session-menu-offline-reason')).toBeNull()
    fireEvent.click(item())
    expect(onRename).toHaveBeenCalled()
  })
})
