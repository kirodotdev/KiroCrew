import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

import CommandBarOverlay from './CommandBarOverlay'

/**
 * IME guard on the dialog's Escape branch (#5481). The overlay is one of the two
 * `handleEscape: false` consumers (the other, Modal, is guarded upstream): its
 * dialog `onKeyDown` used to `preventDefault()` every Escape before the
 * scope-pop-or-close decision, so an Escape pressed to cancel an in-flight IME
 * candidate list was consumed by the overlay instead of the IME. The branch now
 * claims the key through the shared latch (`ime.claimKey`) first — a declined
 * composing Escape keeps its default action for the IME and neither pops the
 * scope nor closes the bar. Coverage mirrors
 * `useImeGuard.documentLatch.test.tsx` (PR #5505 lineage): live composition,
 * the WebKit post-compositionend grace window, and recovery.
 */

const dispatch = vi.fn()
const navigate = vi.fn()
const newSessionWithToken = vi.fn()
const enterInsertOrNewSession = vi.fn()

const storeState = {
  dashboard: { slots: [] as Record<string, unknown>[], unreadSlots: [] as string[] },
  chat: { slotStatusDetail: {} as Record<string, unknown>, activeSlot: null as string | null },
}

vi.mock('../../store', () => ({
  useAppDispatch: () => dispatch,
  useAppSelector: (fn: (s: unknown) => unknown) => fn(storeState),
}))
vi.mock('../../store/chatSlice', () => ({
  createSlot: (arg: unknown) => ({ type: 'createSlot', arg }),
  setPendingInput: (text: string) => ({ type: 'setPendingInput', text }),
  switchSlot: (key: string) => ({ type: 'switchSlot', key }),
}))
vi.mock('../../components/commandPalette/paletteActions', () => ({
  usePaletteActions: () => ({ navigate, enterInsertOrNewSession, newSessionWithToken }),
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
vi.mock('../../hooks/useTheme', () => ({ useTheme: () => ({ cycle: vi.fn() }) }))

function mount(onClose = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <CommandBarOverlay open onClose={onClose} />
    </QueryClientProvider>,
  )
  return onClose
}

const rowByText = (text: string) =>
  screen.getByText(text).closest('[role="option"]') as HTMLElement

/** Enter the sessions scope the same way the row tests do. */
async function enterScope() {
  fireEvent.mouseDown(rowByText('Search Sessions'))
  await waitFor(() =>
    expect(screen.getByRole('button', { name: 'Back to all commands' })).toBeTruthy(),
  )
}

const inScope = () =>
  screen.queryByRole('button', { name: 'Back to all commands' }) !== null

describe('CommandBarOverlay IME Escape guard', () => {
  beforeEach(() => {
    dispatch.mockReset()
    storeState.dashboard = { slots: [], unreadSlots: [] }
    storeState.chat = { slotStatusDetail: {}, activeSlot: null }
    localStorage.clear()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('declines an Escape during live composition: default kept, bar stays open', () => {
    const onClose = mount()
    const input = screen.getByRole('combobox')
    fireEvent.compositionStart(input)
    // The decline contract has two halves and this repo's drift history is of
    // one being dropped: the default is KEPT (the IME needs it to cancel the
    // candidate list) while propagation is STOPPED (a declined Escape must not
    // leak to outer layers' own Escape handling). The bubble listener pins the
    // second half, as useDialogFocusTrap.imeGuard.test.tsx does.
    const bubbleListener = vi.fn()
    window.addEventListener('keydown', bubbleListener)
    try {
      // A live composition's keydown carries the native `isComposing` flag —
      // fireEvent returns false when preventDefault was called.
      const defaultKept = fireEvent.keyDown(input, { key: 'Escape', isComposing: true })
      expect(defaultKept).toBe(true)
      expect(bubbleListener).not.toHaveBeenCalled()
    } finally {
      window.removeEventListener('keydown', bubbleListener)
    }
    expect(onClose).not.toHaveBeenCalled()
  })

  it('does not pop the scope on a composing Escape', async () => {
    const onClose = mount()
    const input = screen.getByRole('combobox')
    await enterScope()
    fireEvent.compositionStart(input)
    fireEvent.keyDown(input, { key: 'Escape', isComposing: true })
    // The scope chip is still there: the Escape belonged to the IME, not the
    // scope-pop-before-close chain.
    expect(inScope()).toBe(true)
    expect(onClose).not.toHaveBeenCalled()
  })

  it('still declines inside the WebKit post-compositionend grace window', () => {
    // On WebKit the keydown that cancels a candidate can arrive AFTER
    // compositionend with `isComposing` already false — only the tracked latch
    // window identifies it. Inside that window both native signals read clear,
    // so the claim consumes the key it declines (preventDefault) to stop the
    // browser acting on a key the overlay decided was not its own. Fake timers
    // hold the 50ms window open: on a loaded runner real timers could let it
    // lapse between the two fireEvent calls and fail the assertion.
    vi.useFakeTimers()
    const onClose = mount()
    const input = screen.getByRole('combobox')
    fireEvent.compositionStart(input)
    fireEvent.compositionEnd(input)
    const defaultKept = fireEvent.keyDown(input, { key: 'Escape' })
    expect(defaultKept).toBe(false)
    expect(onClose).not.toHaveBeenCalled()
  })

  it('recovers: a non-composing Escape after the window closes the bar', () => {
    vi.useFakeTimers()
    const onClose = mount()
    const input = screen.getByRole('combobox')
    fireEvent.compositionStart(input)
    fireEvent.compositionEnd(input)
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(onClose).not.toHaveBeenCalled()
    // Past the post-composition window the latch releases and Escape is the
    // dialog's again.
    vi.advanceTimersByTime(60)
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(onClose).toHaveBeenCalled()
  })

  it('recovers inside a scope too: scope-pop-then-close ordering is untouched', async () => {
    const onClose = mount()
    const input = screen.getByRole('combobox')
    // Enter the scope on real timers (the scope entry awaits a render), then
    // switch to fake ones for the latch window.
    await enterScope()
    vi.useFakeTimers()
    fireEvent.compositionStart(input)
    fireEvent.compositionEnd(input)
    vi.advanceTimersByTime(60)
    // First clear Escape pops the scope, the second closes — the deliberate
    // ordering the guard must not change.
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(inScope()).toBe(false)
    expect(onClose).not.toHaveBeenCalled()
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(onClose).toHaveBeenCalled()
  })
})
