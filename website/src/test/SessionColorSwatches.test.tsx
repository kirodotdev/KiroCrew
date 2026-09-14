/**
 * SessionColorSwatches — the shared colour-swatch row used as the colorSlot of
 * SessionActionsMenu on both the session-header dropdown and the sidebar row
 * menus. Tests the render (No-color + palette swatches) and the pick behaviour
 * (optimistic dispatch is exercised against the real store; persistence goes
 * through the mocked api; onPicked fires so a controlled menu can close).
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import type { ReactNode } from 'react'

const mocks = vi.hoisted(() => ({
  setSlotColor: vi.fn(),
  setSlotColorHex: vi.fn(),
  clearSlotColor: vi.fn(),
}))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (t, p: string) => (p in t ? t[p] : vi.fn().mockResolvedValue([])),
  }),
}))
// useSessionPalette reads CSS vars via useTheme (needs a ThemeProvider) — mock it
// to a fixed palette so this stays a focused unit test of the swatch behaviour.
vi.mock('../hooks/useSessionPalette', () => ({
  useSessionPalette: () => ({ paletteColors: ['#ff0000', '#00ff00', '#0000ff'] }),
}))

import { store } from '../store'
import { sseConnected, sseDisconnected } from '../store/dashboardSlice'
import SessionColorSwatches from '../components/SessionColorSwatches'

const SLOT = 'chat-color-1'
// SessionColorSwatches writes via useMutation, so it needs a QueryClientProvider.
const wrap = (ui: ReactNode) => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  store.dispatch(sseConnected())
  return render(<QueryClientProvider client={qc}><Provider store={store}>{ui}</Provider></QueryClientProvider>)
}

beforeEach(() => {
  mocks.setSlotColor.mockResolvedValue({})
  mocks.setSlotColorHex.mockResolvedValue({})
  mocks.clearSlotColor.mockResolvedValue({})
})
afterEach(() => vi.clearAllMocks())

describe('SessionColorSwatches – a panel opened online must not write once the gateway drops', () => {
  // A bare dispatch is never flushed into the tree, so the drop has to happen
  // inside act() exactly as the SSE handler's would.
  const goOffline = () => act(() => { store.dispatch(sseDisconnected()) })

  it('refuses a hex commit on Enter after the connection drops', async () => {
    wrap(<SessionColorSwatches slotKey={SLOT} colorIndex={null} />)
    fireEvent.click(screen.getByLabelText('Custom color'))
    const hexInput = screen.getByLabelText('Hex color code') as HTMLInputElement
    fireEvent.change(hexInput, { target: { value: '#A1B2C3' } })
    // The panel is already open and legitimate; only now does the gateway go.
    goOffline()
    fireEvent.keyDown(screen.getByLabelText('Hex color code'), { key: 'Enter' })
    await waitFor(() => expect(screen.getByLabelText('Hex color code')).toBeDisabled())
    expect(mocks.setSlotColorHex).not.toHaveBeenCalled()
  })

  it('disables both custom inputs while offline, matching the dimmed buttons', () => {
    wrap(<SessionColorSwatches slotKey={SLOT} colorIndex={null} />)
    fireEvent.click(screen.getByLabelText('Custom color'))
    goOffline()
    // Control: the store really is offline, so a still-enabled input below is the
    // component ignoring it rather than the dispatch not landing.
    expect(store.getState().dashboard.connected).toBe(false)
    expect(screen.getByLabelText('Hex color code')).toBeDisabled()
    expect(screen.getByLabelText('Custom color picker')).toBeDisabled()
  })

  it('drops a drag commit already scheduled when the gateway goes mid-debounce', async () => {
    // The one path `disabled` cannot cover: the PATCH is queued behind a 300ms
    // timer, so the drop lands between the gesture and the write.
    vi.useFakeTimers()
    try {
      wrap(<SessionColorSwatches slotKey={SLOT} colorIndex={null} />)
      fireEvent.click(screen.getByLabelText('Custom color'))
      const wheel = document.querySelector('input[type="color"]') as HTMLInputElement
      fireEvent.change(wheel, { target: { value: '#333333' } })
      goOffline()
      await vi.advanceTimersByTimeAsync(350)
      expect(mocks.setSlotColorHex).not.toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
    }
  })

  it('still commits while connected, so the guard is not blanket', async () => {
    wrap(<SessionColorSwatches slotKey={SLOT} colorIndex={null} />)
    fireEvent.click(screen.getByLabelText('Custom color'))
    const hexInput = screen.getByLabelText('Hex color code') as HTMLInputElement
    fireEvent.change(hexInput, { target: { value: '#A1B2C3' } })
    fireEvent.keyDown(hexInput, { key: 'Enter' })
    await waitFor(() => expect(mocks.setSlotColorHex).toHaveBeenCalledWith(SLOT, '#a1b2c3'))
  })
})

describe('SessionColorSwatches', () => {
  it('renders a No-color button plus palette swatches', () => {
    wrap(<SessionColorSwatches slotKey={SLOT} colorIndex={null} />)
    expect(screen.getByLabelText('No color')).toBeTruthy()
    expect(screen.getAllByRole('button').length).toBeGreaterThan(1)
  })

  it('picking a swatch persists via api.setSlotColor(slotKey, index) and fires onPicked', async () => {
    const onPicked = vi.fn()
    wrap(<SessionColorSwatches slotKey={SLOT} colorIndex={null} onPicked={onPicked} />)
    fireEvent.click(screen.getAllByRole('button')[1]) // first palette colour = index 0
    await waitFor(() => expect(mocks.setSlotColor).toHaveBeenCalledWith(SLOT, 0))
    expect(onPicked).toHaveBeenCalled()
  })

  it('picking "No color" clears BOTH fields via api.clearSlotColor', async () => {
    wrap(<SessionColorSwatches slotKey={SLOT} colorIndex={2} />)
    fireEvent.click(screen.getByLabelText('No color'))
    await waitFor(() => expect(mocks.clearSlotColor).toHaveBeenCalledWith(SLOT))
  })

  it('renders the custom-color cell and toggles the hex panel', () => {
    wrap(<SessionColorSwatches slotKey={SLOT} colorIndex={null} />)
    const cell = screen.getByLabelText('Custom color')
    expect(cell.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(cell)
    expect(cell.getAttribute('aria-expanded')).toBe('true')
    expect(screen.getByLabelText('Hex color code')).toBeTruthy()
  })

  it('the panel toggle and the colour wheel do NOT share one accessible name', () => {
    // On base both resolved components.sessionColorSwatches.custom_color, so a
    // screen reader announced two different controls identically.
    wrap(<SessionColorSwatches slotKey={SLOT} colorIndex={null} />)
    fireEvent.click(screen.getByLabelText('Custom color'))
    const toggle = screen.getByLabelText('Custom color')
    const wheel = screen.getByLabelText('Custom color picker')
    expect(toggle).not.toBe(wheel)
    expect(screen.getAllByLabelText('Custom color')).toHaveLength(1)
  })

  it('committing a hex via Enter persists lowercase through api.setSlotColorHex', async () => {
    wrap(<SessionColorSwatches slotKey={SLOT} colorIndex={null} />)
    fireEvent.click(screen.getByLabelText('Custom color'))
    const hexInput = screen.getByLabelText('Hex color code') as HTMLInputElement
    fireEvent.change(hexInput, { target: { value: '#A1B2C3' } })
    fireEvent.keyDown(hexInput, { key: 'Enter' })
    await waitFor(() => expect(mocks.setSlotColorHex).toHaveBeenCalledWith(SLOT, '#a1b2c3'))
  })

  it('does not persist a malformed hex', async () => {
    wrap(<SessionColorSwatches slotKey={SLOT} colorIndex={null} />)
    fireEvent.click(screen.getByLabelText('Custom color'))
    const hexInput = screen.getByLabelText('Hex color code') as HTMLInputElement
    fireEvent.change(hexInput, { target: { value: '#GGG' } })
    fireEvent.keyDown(hexInput, { key: 'Enter' })
    fireEvent.blur(hexInput)
    await new Promise(r => setTimeout(r, 20))
    expect(mocks.setSlotColorHex).not.toHaveBeenCalled()
  })

  it('debounces the native colour-input drag into a single PATCH', async () => {
    vi.useFakeTimers()
    try {
      wrap(<SessionColorSwatches slotKey={SLOT} colorIndex={null} />)
      fireEvent.click(screen.getByLabelText('Custom color'))
      const wheel = document.querySelector('input[type="color"]') as HTMLInputElement
      fireEvent.change(wheel, { target: { value: '#111111' } })
      fireEvent.change(wheel, { target: { value: '#222222' } })
      fireEvent.change(wheel, { target: { value: '#333333' } })
      // Async advance: flushes the debounce timer AND the react-query
      // microtask chain that carries mutate() -> mutationFn.
      await vi.advanceTimersByTimeAsync(350)
      expect(mocks.setSlotColorHex).toHaveBeenCalledTimes(1)
      expect(mocks.setSlotColorHex).toHaveBeenCalledWith(SLOT, '#333333')
    } finally {
      vi.useRealTimers()
    }
  })

  it('marks the custom cell active when a colorHex is set', () => {
    wrap(<SessionColorSwatches slotKey={SLOT} colorIndex={null} colorHex="#a1b2c3" />)
    const cell = screen.getByLabelText('Custom color')
    expect(cell.className).toContain('border-text-strong')
    // And the No-color cell is NOT marked active.
    expect(screen.getByLabelText('No color').className).toContain('border-transparent')
  })

  it('a palette pick within the wheel debounce window cancels the pending hex commit', async () => {
    // Blocker regression: wheel drag -> palette click within 300ms. The
    // delayed hex PATCH must NOT run after (and overwrite) the palette pick.
    vi.useFakeTimers()
    try {
      wrap(<SessionColorSwatches slotKey={SLOT} colorIndex={null} />)
      fireEvent.click(screen.getByLabelText('Custom color'))
      const wheel = document.querySelector('input[type="color"]') as HTMLInputElement
      fireEvent.change(wheel, { target: { value: '#111111' } })
      // Immediate palette selection 100ms into the debounce window.
      await vi.advanceTimersByTimeAsync(100)
      fireEvent.click(screen.getAllByRole('button')[1])
      await vi.advanceTimersByTimeAsync(500)
      expect(mocks.setSlotColor).toHaveBeenCalledWith(SLOT, 0)
      expect(mocks.setSlotColorHex).not.toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
    }
  })

  it('blur without editing does not commit the seeded placeholder hex', async () => {
    wrap(<SessionColorSwatches slotKey={SLOT} colorIndex={null} />)
    fireEvent.click(screen.getByLabelText('Custom color'))
    const hexInput = screen.getByLabelText('Hex color code') as HTMLInputElement
    hexInput.focus()
    fireEvent.blur(hexInput)
    await new Promise(r => setTimeout(r, 20))
    // Merely focusing and clicking away must not paint the session the
    // placeholder blue the draft is seeded with.
    expect(mocks.setSlotColorHex).not.toHaveBeenCalled()
  })

  it('failed clear does not roll back over a custom hex that landed afterwards', async () => {
    // Blocker regression: clear PATCH fails slowly; a custom-hex PATCH
    // succeeds in between. Both color fields being (index=null, hex=set), the
    // late clear rollback must NOT restore the pre-clear color: after the
    // dust settles the store must show the custom hex, not the old index.
    const { sseSlots, sseSlotColor } = await import('../store/dashboardSlice')
    // Seed the slot: sseSlotColor (and the component's rollback guard reading
    // the store) both no-op unless the slot exists in dashboard.slots.
    store.dispatch(sseSlots([{ key: SLOT, color_index: 2, color_hex: null } as never]))
    store.dispatch(sseSlotColor({ key: SLOT, color_index: 2, color_hex: null }))
    let rejectClear: (e: Error) => void = () => {}
    mocks.clearSlotColor.mockImplementation(() => new Promise((_r, rej) => { rejectClear = rej }))
    wrap(<SessionColorSwatches slotKey={SLOT} colorIndex={2} />)
    fireEvent.click(screen.getByLabelText('No color'))
    // Superseding custom pick lands while the clear is in flight.
    fireEvent.click(screen.getByLabelText('Custom color'))
    const hexInput = screen.getByLabelText('Hex color code') as HTMLInputElement
    fireEvent.change(hexInput, { target: { value: '#a1b2c3' } })
    fireEvent.keyDown(hexInput, { key: 'Enter' })
    await waitFor(() => expect(mocks.setSlotColorHex).toHaveBeenCalledWith(SLOT, '#a1b2c3'))
    rejectClear(new Error('boom'))
    await new Promise(r => setTimeout(r, 30))
    const slot = store.getState().dashboard.slots.find(s => s.key === SLOT)
    expect(slot?.color_hex).toBe('#a1b2c3')
    expect(slot?.color_index ?? null).toBe(null)
  })

  it('a superseded hex write does not roll back over a later write of the SAME value', async () => {
    // Blocker regression: two PATCHes target the same hex. The first fails
    // slowly, the second succeeds. A value-only guard cannot tell them apart
    // (the store shows exactly that hex either way), so the late failure would
    // revert a successful write. Only the LATEST write may roll back.
    const { sseSlots, sseSlotColor } = await import('../store/dashboardSlice')
    store.dispatch(sseSlots([{ key: SLOT, color_index: 2, color_hex: null } as never]))
    store.dispatch(sseSlotColor({ key: SLOT, color_index: 2, color_hex: null }))
    const rejects: Array<(e: Error) => void> = []
    const resolves: Array<(v: unknown) => void> = []
    mocks.setSlotColorHex.mockImplementation(
      () => new Promise((res, rej) => { resolves.push(res); rejects.push(rej) }),
    )
    wrap(<SessionColorSwatches slotKey={SLOT} colorIndex={2} />)
    fireEvent.click(screen.getByLabelText('Custom color'))
    const hexInput = screen.getByLabelText('Hex color code') as HTMLInputElement
    fireEvent.change(hexInput, { target: { value: '#a1b2c3' } })
    fireEvent.keyDown(hexInput, { key: 'Enter' })
    // Second commit of the same value while the first is still in flight.
    fireEvent.keyDown(hexInput, { key: 'Enter' })
    await waitFor(() => expect(mocks.setSlotColorHex).toHaveBeenCalledTimes(2))
    resolves[1]({})
    await new Promise(r => setTimeout(r, 10))
    rejects[0](new Error('boom'))
    await new Promise(r => setTimeout(r, 30))
    const after = store.getState().dashboard.slots.find(s => s.key === SLOT)
    expect(after?.color_hex).toBe('#a1b2c3')
    expect(after?.color_index ?? null).toBe(null)
  })
})
