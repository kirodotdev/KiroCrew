import { useRef, useState } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'

import { SETTINGS_REGISTRY } from '../../components/commandPalette/settingsRegistry.gen'
import { localizedSettingLabel } from '../../components/commandPalette/settingsSearchCore'
import CommandBarOverlay from './CommandBarOverlay'

const renderedTitle = (entry: (typeof SETTINGS_REGISTRY)[number]) => localizedSettingLabel(entry)
const channelsEntry = SETTINGS_REGISTRY.find(e => e.tab === 'channels')!

/**
 * The bar returns focus to the control it was opened from on every close -- a
 * dismiss (Escape, backdrop) and a row that navigates or acts in place -- except
 * a row that switches chats, where the new chat's composer keeps focus.
 *
 * The opener is captured by the code that opens the bar (the host here, as
 * `useCommandPalette` does in the app) at the instant of the gesture, and handed
 * to the overlay as a prop. That is what makes it the real opener rather than
 * the loading fallback's input, which holds focus by the time the overlay first
 * renders -- the cold-open case the first test pins. The shared focus trap keeps
 * `restoreFocus: false`; the bar's own unmount effect does the return, and reads
 * the active chat slot to tell a switch (skip) from any other close (restore).
 *
 * These run against the REAL focus trap -- unlike the other CommandBarOverlay
 * suites, this file deliberately does NOT mock `useDialogFocusTrap`. Only the
 * store, palette providers and viewport are stubbed, as elsewhere, so the
 * component mounts without a live gateway.
 */

const dispatch = vi.fn()
const navigate = vi.fn()
const newSessionWithToken = vi.fn()
const enterInsertOrNewSession = vi.fn()

const storeState = {
  dashboard: { slots: [] as Record<string, unknown>[], unreadSlots: [] as string[] },
  chat: { slotStatusDetail: {} as Record<string, unknown>, activeSlot: null as string | null },
}

// A STABLE store handle, as the real `useAppStore` returns: a fresh object each
// render would make the overlay's restore effect (keyed on the store) re-run and
// its cleanup fire every render, restoring focus mid-open.
const storeHandle = { getState: () => storeState }
vi.mock('../../store', () => ({
  useAppDispatch: () => dispatch,
  useAppSelector: (fn: (s: unknown) => unknown) => fn(storeState),
  useAppStore: () => storeHandle,
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
vi.mock('../../hooks/useTheme', () => ({ useTheme: () => ({ cycle: vi.fn() }) }))
// The composer-fallback target for the one case where the opener is gone by the
// time the bar closes. Spied so a test can assert the fallback fired.
const focusComposerNow = vi.fn()
vi.mock('../../pages/chat/composerFocus', () => ({ focusComposerNow: () => focusComposerNow() }))

/**
 * A host that mirrors the real one: it captures `document.activeElement` at the
 * instant the gesture fires -- before mounting the overlay (and, in the app, its
 * loading fallback) -- and hands it to the overlay as `opener`. The overlay is
 * mounted ONLY while open, so a close unmounts it and runs the return.
 *
 * `withFallback` reproduces the cold-open HANDOFF: a loading field mounts first
 * and takes focus (the Suspense fallback), then on the next commit it is removed
 * and the overlay mounts in its place. So at the overlay's first render the
 * active element is the loading field, exactly as on a cold open -- the ordering
 * that makes a capture read INSIDE the overlay grab the wrong element, and that
 * the gesture-time capture here is immune to.
 */
let removeOpenerSignal: (() => void) | null = null
let resolveChunkSignal: (() => void) | null = null

function Host({ opener, withFallback = false }: { opener: 'button' | 'composer'; withFallback?: boolean }) {
  const [open, setOpen] = useState(false)
  const [chunkReady, setChunkReady] = useState(!withFallback)
  const [openerGone, setOpenerGone] = useState(false)
  removeOpenerSignal = () => setOpenerGone(true)
  resolveChunkSignal = () => setChunkReady(true)
  const openerRef = useRef<HTMLElement | null>(null)
  const openBar = () => {
    const el = document.activeElement
    openerRef.current = el instanceof HTMLElement && el !== document.body ? el : null
    setOpen(true)
  }
  return (
    <>
      {openerGone ? null : opener === 'button' ? (
        <button data-testid="opener" onClick={openBar}>
          Run a command
        </button>
      ) : (
        <textarea
          data-testid="opener"
          aria-label="Message box"
          onKeyDown={e => {
            if ((e.ctrlKey || e.metaKey) && e.key === 'k') openBar()
          }}
        />
      )}
      {open && !chunkReady ? (
        // The loading fallback, focused at mount, standing in for TypeAheadInput
        // while the overlay chunk loads.
        <input data-testid="loading" autoFocus aria-label="loading" />
      ) : null}
      {open && chunkReady ? (
        <CommandBarOverlay open onClose={() => setOpen(false)} opener={openerRef.current} />
      ) : null}
    </>
  )
}

function renderHost(opener: 'button' | 'composer', withFallback = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <Host opener={opener} withFallback={withFallback} />
    </QueryClientProvider>,
  )
}

describe('CommandBarOverlay focus restore', () => {
  beforeEach(() => {
    dispatch.mockReset()
    navigate.mockReset()
    focusComposerNow.mockReset()
    storeState.dashboard = { slots: [], unreadSlots: [] }
    storeState.chat = { slotStatusDetail: {}, activeSlot: null }
    localStorage.clear()
  })

  it('returns focus to the header button after Escape', async () => {
    renderHost('button')
    const opener = screen.getByTestId('opener')
    opener.focus()
    fireEvent.click(opener)

    const input = await screen.findByRole('combobox')
    expect(input).toHaveFocus()

    fireEvent.keyDown(input, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('combobox')).toBeNull())
    expect(opener).toHaveFocus()
  })

  it('returns focus to the message box after Ctrl+K opens the bar and Escape closes it', async () => {
    renderHost('composer')
    const opener = screen.getByTestId('opener')
    opener.focus()
    fireEvent.keyDown(opener, { key: 'k', ctrlKey: true })

    const input = await screen.findByRole('combobox')
    expect(input).toHaveFocus()

    fireEvent.keyDown(input, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('combobox')).toBeNull())
    expect(opener).toHaveFocus()
  })

  it('returns focus to the opener on the cold first open, when a loading field held focus at mount', async () => {
    // The first open of a page load: the chunk is not cached, so a loading field
    // is on screen and focused when the overlay first renders. Because the host
    // captured the opener at the gesture (not the overlay after mount), Escape
    // still returns focus to the real opener, not the vanished loading field.
    renderHost('button', true)
    const opener = screen.getByTestId('opener')
    opener.focus()
    fireEvent.click(opener)

    // While the chunk loads, the fallback field holds focus -- so the active
    // element when the overlay mounts next is the loading field, not the opener.
    expect(screen.getByTestId('loading')).toHaveFocus()

    // Chunk resolves: the fallback is removed and the overlay mounts in its place.
    act(() => resolveChunkSignal?.())
    const input = await screen.findByRole('combobox')

    fireEvent.keyDown(input, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('combobox')).toBeNull())
    // The opener recorded at the gesture -- not the vanished loading field -- is
    // where focus returns.
    expect(opener).toHaveFocus()
    expect(focusComposerNow).not.toHaveBeenCalled()
  })

  it('returns focus to the opener when a row that does not switch chats is picked', async () => {
    // A navigate row (a settings row) closes the bar without changing the active
    // chat slot. That is not a switch, so focus goes back to the opener -- the
    // same as a dismiss.
    renderHost('composer')
    const opener = screen.getByTestId('opener')
    opener.focus()
    fireEvent.keyDown(opener, { key: 'k', ctrlKey: true })

    const input = await screen.findByRole('combobox')
    fireEvent.change(input, { target: { value: channelsEntry.label } })
    const settingsRow = (await screen.findByText(renderedTitle(channelsEntry))).closest('[role="option"]') as HTMLElement
    fireEvent.mouseDown(settingsRow)

    await waitFor(() => expect(navigate).toHaveBeenCalled())
    await waitFor(() => expect(screen.queryByRole('combobox')).toBeNull())
    expect(opener).toHaveFocus()
  })

  it('does NOT return focus to the opener when the close carried a chat switch', async () => {
    // When the active slot has changed by the time the bar closes, a switch
    // happened and the new chat's composer owns focus; the bar must not pull it
    // back to the opener. Driven directly: the store reports a different active
    // slot at close than at open.
    renderHost('button')
    const opener = screen.getByTestId('opener')
    opener.focus()
    fireEvent.click(opener)
    await screen.findByRole('combobox')

    // Model the switch the activation would have performed, then dismiss.
    storeState.chat.activeSlot = 'slot-after-switch'
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Escape' })

    await waitFor(() => expect(screen.queryByRole('combobox')).toBeNull())
    expect(opener).not.toHaveFocus()
    expect(focusComposerNow).not.toHaveBeenCalled()
  })

  it('falls back to the composer when the opener is gone by the time the bar closes', async () => {
    // The opener can be removed while the bar is open (its pane re-rendered). The
    // return then has nowhere to land, so it falls back to the composer, as the
    // quick-search surfaces do.
    renderHost('button')
    const opener = screen.getByTestId('opener')
    opener.focus()
    fireEvent.click(opener)
    await screen.findByRole('combobox')

    // The opener's own pane re-renders it away while the bar is open; React
    // unmounts it, so the captured node is disconnected. Then dismiss, no switch.
    act(() => removeOpenerSignal?.())
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Escape' })

    await waitFor(() => expect(screen.queryByRole('combobox')).toBeNull())
    expect(focusComposerNow).toHaveBeenCalled()
  })
})
