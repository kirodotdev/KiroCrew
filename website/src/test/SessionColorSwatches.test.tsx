/**
 * SessionColorSwatches — the shared colour-swatch row used as the colorSlot of
 * SessionActionsMenu on both the session-header dropdown and the sidebar row
 * menus. Tests the render (No-color + palette swatches) and the pick behaviour
 * (optimistic dispatch is exercised against the real store; persistence goes
 * through the mocked api; onPicked fires so a controlled menu can close).
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
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
import SessionColorSwatches from '../components/SessionColorSwatches'

const SLOT = 'chat-color-1'
// SessionColorSwatches writes via useMutation, so it needs a QueryClientProvider.
const wrap = (ui: ReactNode) => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={qc}><Provider store={store}>{ui}</Provider></QueryClientProvider>)
}

beforeEach(() => {
  mocks.setSlotColor.mockResolvedValue({})
  mocks.setSlotColorHex.mockResolvedValue({})
  mocks.clearSlotColor.mockResolvedValue({})
})
afterEach(() => vi.clearAllMocks())

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
