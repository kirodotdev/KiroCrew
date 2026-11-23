import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'

import CommandBarOverlay from './CommandBarOverlay'

/**
 * The Recent sessions row of a session whose chat is compacting (#37300).
 *
 * A compaction moves only the slot's run state; its status detail keeps the phase
 * from before it. So the row says "Compacting…", the chat footer's word, only when
 * the run state is read, and an open bar relabels it only when that read is part of
 * what rebuilds the rows. The selector is the real one; the store, the palette
 * actions and the viewport are stubbed, as in the other suites.
 */

const dispatch = vi.fn()
const navigate = vi.fn()
const cycle = vi.fn()
const onClose = vi.fn()

const storeState = {
  dashboard: { slots: [] as Record<string, unknown>[], unreadSlots: [] as string[] },
  chat: {
    slotStatusDetail: {} as Record<string, unknown>,
    activeSlot: null as string | null,
    slotRun: {} as Record<string, { state: string }>,
  },
}
// Stable identities, as the real hooks return: the rows' memo depends on these, and a
// fresh value on every render would rebuild the rows whatever the compacting keys say.
const storeHandle = { getState: () => storeState }
const paletteActions = { navigate, enterInsertOrNewSession: vi.fn(), newSessionWithToken: vi.fn() }
const theme = { cycle }

vi.mock('../../store', () => ({
  useAppDispatch: () => dispatch,
  useAppSelector: (fn: (s: unknown) => unknown) => fn(storeState),
  useAppStore: () => storeHandle,
}))
vi.mock('../../store/chatSlice', async importOriginal => ({
  selectSlotStreamState: (await importOriginal<typeof import('../../store/chatSlice')>()).selectSlotStreamState,
  switchSlot: (arg: { key: string }) => ({ type: 'switchSlot', ...arg }),
}))
vi.mock('../../components/commandPalette/paletteActions', () => ({
  usePaletteActions: () => paletteActions,
}))
vi.mock('../../components/commandPalette/providers/sessionsProvider', () => ({
  useSessionsProvider: () => ({ search: vi.fn(async () => []) }),
}))
vi.mock('../../components/commandPalette/providers/recentsProvider', async importOriginal => ({
  ...(await importOriginal<typeof import('../../components/commandPalette/providers/recentsProvider')>()),
  useRecentsProvider: () => ({ search: vi.fn(async () => []) }),
}))
vi.mock('../../hooks/useVisualViewport', () => ({ useVisualViewport: () => ({ height: 800 }) }))
vi.mock('../../hooks/useDialogFocusTrap', () => ({ useDialogFocusTrap: () => {} }))
vi.mock('../../hooks/useTheme', () => ({ useTheme: () => theme }))

const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
// A fresh element each call: React skips a re-render handed the element it already has.
const bar = () => (
  <QueryClientProvider client={client}>
    <CommandBarOverlay open onClose={onClose} />
  </QueryClientProvider>
)
const rowText = (title: string) => screen.getByText(title).closest('[role="option"]')?.textContent

describe('CommandBarOverlay Recent row while a chat compacts', () => {
  beforeEach(() => {
    // Two running sessions, both still holding a thinking phase as their detail.
    storeState.dashboard.slots = [
      { key: 'slot-c', title: 'Compacting thread', running: true, messages: 9, last_activity_ts: 2_000 },
      { key: 'slot-t', title: 'Thinking thread', running: true, messages: 4, last_activity_ts: 1_000 },
    ]
    storeState.chat.slotStatusDetail = { 'slot-c': { kind: 'thinking', ts: 1 }, 'slot-t': { kind: 'thinking', ts: 1 } }
    storeState.chat.slotRun = { 'slot-c': { state: 'compacting' }, 'slot-t': { state: 'streaming' } }
    localStorage.clear()
  })

  it('says Compacting… for the compacting session and Thinking… for the other', () => {
    render(bar())
    expect(rowText('Compacting thread')).toContain('Compacting…')
    expect(rowText('Thinking thread')).toContain('Thinking…')
    expect(rowText('Thinking thread')).not.toContain('Compacting…')
  })

  it('relabels an open bar when the compaction ends', () => {
    const { rerender } = render(bar())
    expect(rowText('Compacting thread')).toContain('Compacting…')
    storeState.chat.slotRun = { 'slot-c': { state: 'streaming' }, 'slot-t': { state: 'streaming' } }
    rerender(bar())
    expect(rowText('Compacting thread')).not.toContain('Compacting…')
    expect(rowText('Compacting thread')).toContain('Thinking…')
  })
})
