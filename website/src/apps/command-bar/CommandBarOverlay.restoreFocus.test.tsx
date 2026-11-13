import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

import { useCommandPalette } from '../../hooks/useCommandPalette'
import CommandBarOverlay from './CommandBarOverlay'

/**
 * Where focus goes when the Command Bar closes (#29716).
 *
 * A dismiss (Escape, an outside click) gives focus back to the control the bar was
 * opened from: the header button, or the message box a Ctrl+K came from. A row that closes the bar does
 * not: its action owns focus, which for a session row is that session's composer,
 * in split view too, where the opener can be another pane's composer.
 *
 * The host is the real `useCommandPalette`, which reads the opener at the gesture,
 * and the focus trap and composer handoff are real. Only the store, the palette
 * actions and the viewport are stubbed, as in the other suites.
 */

const dispatch = vi.fn()
const navigate = vi.fn()

const storeState = {
  dashboard: { slots: [] as Record<string, unknown>[], unreadSlots: [] as string[] },
  chat: { slotStatusDetail: {} as Record<string, unknown>, activeSlot: null as string | null },
}
const storeHandle = { getState: () => storeState }

vi.mock('../../store', () => ({
  useAppDispatch: () => dispatch,
  useAppSelector: (fn: (s: unknown) => unknown) => fn(storeState),
  useAppStore: () => storeHandle,
}))
vi.mock('../../store/chatSlice', () => ({
  switchSlot: (arg: { key: string }) => ({ type: 'switchSlot', ...arg }),
}))
vi.mock('../../components/commandPalette/paletteActions', () => ({
  usePaletteActions: () => ({ navigate, enterInsertOrNewSession: vi.fn(), newSessionWithToken: vi.fn() }),
}))
vi.mock('../../components/commandPalette/providers/sessionsProvider', () => ({
  useSessionsProvider: () => ({ search: vi.fn(async () => []) }),
}))
vi.mock('../../components/commandPalette/providers/recentsProvider', async importOriginal => ({
  ...(await importOriginal<typeof import('../../components/commandPalette/providers/recentsProvider')>()),
  useRecentsProvider: () => ({ search: vi.fn(async () => []) }),
}))
vi.mock('../../hooks/useVisualViewport', () => ({ useVisualViewport: () => ({ height: 800 }) }))
vi.mock('../../hooks/useTheme', () => ({ useTheme: () => ({ cycle: vi.fn() }) }))
vi.mock('../../utils/isTouchDevice', () => ({ isTouchDevice: () => false }))

/** The header button and two split-view panes, each with its own composer. */
function Host() {
  const palette = useCommandPalette()
  return (
    <>
      <button type="button" onClick={palette.openPalette}>Run a command</button>
      <div data-chat-pane="" data-pane-slot="slot-a">
        <textarea data-composer-input="" aria-label="Pane A message box" />
      </div>
      <div data-chat-pane="focused" data-pane-slot="slot-b">
        <textarea data-composer-input="" aria-label="Pane B message box" />
      </div>
      {palette.open && <CommandBarOverlay open onClose={palette.close} opener={palette.opener} />}
    </>
  )
}

function renderHost() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <Host />
    </QueryClientProvider>,
  )
}

const bar = () => screen.queryByRole('combobox')

describe('CommandBarOverlay focus on close', () => {
  beforeEach(() => {
    dispatch.mockReset()
    navigate.mockReset()
    storeState.dashboard = { slots: [], unreadSlots: [] }
    storeState.chat = { slotStatusDetail: {}, activeSlot: null }
    localStorage.clear()
  })

  it('gives focus back to the header button after Escape', async () => {
    renderHost()
    const button = screen.getByRole('button', { name: 'Run a command' })
    button.focus()
    fireEvent.click(button)
    const input = await screen.findByRole('combobox')
    fireEvent.change(input, { target: { value: 'dev fle' } })
    fireEvent.keyDown(input, { key: 'Escape' })
    await waitFor(() => expect(bar()).toBeNull())
    expect(button).toHaveFocus()
  })

  it('gives focus back to the header button after an outside click', async () => {
    renderHost()
    const button = screen.getByRole('button', { name: 'Run a command' })
    button.focus()
    fireEvent.click(button)
    await screen.findByRole('combobox')
    // In a browser the press's default action runs after the close; prevented, it
    // cannot clear the focus the close gave back.
    expect(fireEvent.mouseDown(screen.getByRole('dialog').parentElement as HTMLElement)).toBe(false)
    await waitFor(() => expect(bar()).toBeNull())
    expect(button).toHaveFocus()
  })

  it('gives focus back to the message box after Ctrl+K there and Escape', async () => {
    renderHost()
    const composer = screen.getByLabelText('Pane B message box')
    composer.focus()
    fireEvent.keyDown(composer, { key: 'k', ctrlKey: true })
    const input = await screen.findByRole('combobox')
    fireEvent.keyDown(input, { key: 'Escape' })
    await waitFor(() => expect(bar()).toBeNull())
    expect(composer).toHaveFocus()
  })

  it('leaves focus to the picked session, not to the other pane it was opened from', async () => {
    // Split view: A is the active session, B's composer has focus. Picking A's
    // Recent row does not change the active session, and the bar must still not
    // hand focus back to B: the next Enter would send to a session nobody chose.
    storeState.chat.activeSlot = 'slot-a'
    storeState.dashboard.slots = [
      { key: 'slot-a', title: 'Pricing service', messages: 3, last_activity_ts: 2_000 },
      { key: 'slot-b', title: 'Launch email', messages: 2, last_activity_ts: 1_000 },
    ]
    // The switch lands after the bar has closed, as it does over the gateway.
    let land: () => void = () => {}
    dispatch.mockReturnValue({ unwrap: () => new Promise<void>(r => { land = r }) })
    renderHost()
    const opener = screen.getByLabelText('Pane B message box')
    opener.focus()
    fireEvent.keyDown(opener, { key: 'k', ctrlKey: true })
    await screen.findByRole('combobox')
    fireEvent.mouseDown(screen.getByText('Pricing service').closest('[role="option"]') as HTMLElement)
    await waitFor(() => expect(bar()).toBeNull())
    expect(opener).not.toHaveFocus()
    land()
    await waitFor(() => expect(screen.getByLabelText('Pane A message box')).toHaveFocus())
  })
})
