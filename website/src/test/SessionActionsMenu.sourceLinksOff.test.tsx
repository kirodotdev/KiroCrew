/**
 * The per-session PR and Issue chip switch in the shared session menu: the row
 * reads "Don't show …" while the session shows chips and "Show …" while it does
 * not, selecting it writes THAT row's slot (never the active tab's), and a failed
 * write rolls the row back and says which direction failed.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { ThemeProvider } from '../hooks/useTheme'
import { DropdownMenu, DropdownMenuContent, DropdownMenuTrigger } from '../components/ui/dropdown-menu'

const { setSlotSourceLinksOff, setSlotMuted } = vi.hoisted(() => ({
  setSlotSourceLinksOff: vi.fn(),
  setSlotMuted: vi.fn(),
}))

vi.mock('../api/client', () => ({
  api: {
    slackChannels: vi.fn().mockResolvedValue([]),
    mcpActive: vi.fn().mockResolvedValue([]),
    setSlotColor: vi.fn().mockResolvedValue({}),
    chatFolders: vi.fn().mockResolvedValue([]),
    setSlotMuted,
    setSlotSourceLinksOff,
  },
  ApiError: class ApiError extends Error {},
}))

import SessionActionsMenu from '../components/SessionActionsMenu'
import { store } from '../store'
import { sseSlots, sseConnected, sseDisconnected } from '../store/dashboardSlice'
import { setActiveSlot } from '../store/chatSlice'

const HIDE = "Don't show PR and Issue chips on this session"
const SHOW = 'Show PR and Issue chips on this session'

function renderMenu(off: boolean, connected = true) {
  // useSessionActions reads the app store directly, so drive that store.
  store.dispatch(connected ? sseConnected() : sseDisconnected())
  store.dispatch(sseSlots([
    { key: 'chat-active', title: 'Active', messages: 0, running: false },
    // No chips on the card: the entry must still be there, so a session can be
    // switched off before its first link and switched back on while it has none.
    { key: 'chat-row', title: 'Row', messages: 0, running: false, source_links_off: off },
  ]))
  store.dispatch(setActiveSlot('chat-active'))
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const utils = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <DropdownMenu>
              <DropdownMenuTrigger>menu</DropdownMenuTrigger>
              <DropdownMenuContent>
                <SessionActionsMenu variant="dropdown" slotKey="chat-row" />
              </DropdownMenuContent>
            </DropdownMenu>
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  fireEvent.keyDown(utils.getByText('menu'), { key: 'Enter' })
  const rowOff = () => store.getState().dashboard.slots.find(s => s.key === 'chat-row')?.source_links_off
  return { ...utils, rowOff }
}

describe('SessionActionsMenu per-session chip switch', () => {
  beforeEach(() => { setSlotSourceLinksOff.mockReset(); setSlotMuted.mockReset() })
  afterEach(() => { store.dispatch(sseSlots([])); store.dispatch(setActiveSlot(null)) })

  it('turns the chips off for the row it was opened on, not the active tab', async () => {
    setSlotSourceLinksOff.mockResolvedValue({ ok: true, source_links_off: true, changed: true })
    const { rowOff } = renderMenu(false)
    fireEvent.click(await screen.findByText(HIDE))
    await waitFor(() => expect(setSlotSourceLinksOff).toHaveBeenCalledWith('chat-row', true))
    expect(rowOff()).toBe(true)
    expect(setSlotMuted).not.toHaveBeenCalled()
  })

  it('a session with its chips off offers Show, which turns them back on', async () => {
    setSlotSourceLinksOff.mockResolvedValue({ ok: true, source_links_off: false, changed: true })
    const { rowOff } = renderMenu(true)
    fireEvent.click(await screen.findByText(SHOW))
    expect(screen.queryByText(HIDE)).toBeNull()
    await waitFor(() => expect(setSlotSourceLinksOff).toHaveBeenCalledWith('chat-row', false))
    expect(rowOff()).toBe(false)
  })

  it('a failed turn-off rolls the row back and says so, on its own error record', async () => {
    setSlotSourceLinksOff.mockRejectedValue(new Error('could not persist the setting; please retry'))
    const { rowOff } = renderMenu(false)
    fireEvent.click(await screen.findByText(HIDE))
    await waitFor(() => expect(
      store.getState().dashboard.slotMuteFlagError?.['source_links_off:chat-row'],
    ).toBe('could not persist the setting; please retry'))
    expect(rowOff()).toBe(false)
    expect(store.getState().dashboard.slotMuteFlagError?.['muted:chat-row']).toBeUndefined()
    // Selecting closed the menu; reopen it to read the store-backed notice.
    fireEvent.keyDown(screen.getByText('menu'), { key: 'Enter' })
    expect(await screen.findByText("Couldn't turn off this session's PR and Issue chips")).toBeTruthy()
  })

  it('a failed turn-on names the turn-on', async () => {
    setSlotSourceLinksOff.mockRejectedValue(new Error('nope'))
    const { rowOff } = renderMenu(true)
    fireEvent.click(await screen.findByText(SHOW))
    await waitFor(() => expect(store.getState().dashboard.slotMuteFlagError?.['source_links_off:chat-row']).toBe('nope'))
    expect(rowOff()).toBe(true)
    fireEvent.keyDown(screen.getByText('menu'), { key: 'Enter' })
    expect(await screen.findByText("Couldn't turn on this session's PR and Issue chips")).toBeTruthy()
  })

  it('refuses the write while offline, dimmed and named', async () => {
    const { rowOff } = renderMenu(false, false)
    const row = await screen.findByRole('menuitem', { name: new RegExp(HIDE) })
    expect(row).toHaveAttribute('aria-disabled', 'true')
    expect(row.title).toMatch(/Gateway offline/)
    fireEvent.click(row)
    expect(setSlotSourceLinksOff).not.toHaveBeenCalled()
    expect(rowOff()).toBeFalsy()
  })
})
