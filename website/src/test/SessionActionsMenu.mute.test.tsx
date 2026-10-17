/**
 * The per-row Mute item (#12828): the shared session menu offers Mute / Unmute
 * for the row it was opened on, and selecting it writes THAT row's slot, never
 * the active tab. A failed write rolls the row back and says so in the menu.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { ThemeProvider } from '../hooks/useTheme'
import { DropdownMenu, DropdownMenuContent, DropdownMenuTrigger } from '../components/ui/dropdown-menu'

const { setSlotMuted } = vi.hoisted(() => ({ setSlotMuted: vi.fn() }))

vi.mock('../api/client', () => ({
  api: {
    slackChannels: vi.fn().mockResolvedValue([]),
    mcpActive: vi.fn().mockResolvedValue([]),
    setSlotColor: vi.fn().mockResolvedValue({}),
    chatFolders: vi.fn().mockResolvedValue([]),
    setSlotMuted,
  },
  ApiError: class ApiError extends Error {},
}))

import SessionActionsMenu from '../components/SessionActionsMenu'
import { store } from '../store'
import { sseSlots } from '../store/dashboardSlice'
import { setActiveSlot } from '../store/chatSlice'

function renderMenu(muted: boolean) {
  // useSessionActions reads the app store directly, so drive that store.
  store.dispatch(sseSlots([
    { key: 'chat-active', title: 'Active', messages: 0, running: false },
    { key: 'chat-row', title: 'Row', messages: 0, running: false, muted },
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
  const rowMuted = () => store.getState().dashboard.slots.find(s => s.key === 'chat-row')?.muted
  return { ...utils, store, rowMuted }
}

describe('SessionActionsMenu per-row Mute', () => {
  beforeEach(() => { setSlotMuted.mockReset() })
  afterEach(() => { store.dispatch(sseSlots([])); store.dispatch(setActiveSlot(null)) })

  it('Mute writes the row it was opened on, not the active tab', async () => {
    setSlotMuted.mockResolvedValue({ ok: true, muted: true, changed: true })
    const { rowMuted } = renderMenu(false)
    fireEvent.click(await screen.findByText('Mute this session'))
    await waitFor(() => expect(setSlotMuted).toHaveBeenCalledWith('chat-row', true))
    expect(rowMuted()).toBe(true)
  })

  it('a muted row offers Unmute, which clears it', async () => {
    setSlotMuted.mockResolvedValue({ ok: true, muted: false, changed: true })
    const { rowMuted } = renderMenu(true)
    fireEvent.click(await screen.findByText('Unmute this session'))
    await waitFor(() => expect(setSlotMuted).toHaveBeenCalledWith('chat-row', false))
    expect(rowMuted()).toBe(false)
  })

  it('a failed write rolls the row back and records the error for the menu', async () => {
    setSlotMuted.mockRejectedValue(new Error('could not persist'))
    const { store, rowMuted } = renderMenu(false)
    fireEvent.click(await screen.findByText('Mute this session'))
    await waitFor(() => expect(store.getState().dashboard.slotMuteFlagError?.['muted:chat-row']).toBe('could not persist'))
    expect(rowMuted()).toBe(false)
  })

  it('a failed unmute says the unmute failed, and the two flags keep separate error records', async () => {
    setSlotMuted.mockRejectedValue(new Error('nope'))
    const { rowMuted } = renderMenu(true)
    fireEvent.click(await screen.findByText('Unmute this session'))
    await waitFor(() => expect(store.getState().dashboard.slotMuteFlagError?.['muted:chat-row']).toBe('nope'))
    expect(rowMuted()).toBe(true)
    // Selecting closed the menu; reopen it to read the store-backed notice.
    fireEvent.keyDown(screen.getByText('menu'), { key: 'Enter' })
    expect(await screen.findByText("Couldn't unmute this session")).toBeTruthy()
    const errors = store.getState().dashboard.slotMuteFlagError
    expect(errors['muted:chat-row']).toBe('nope')
    expect(errors['mutes_opened:chat-row']).toBeUndefined()
  })
})
