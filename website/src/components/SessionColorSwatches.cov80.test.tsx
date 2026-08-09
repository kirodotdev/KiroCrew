import { screen, fireEvent, waitFor } from '@testing-library/react'
import type React from 'react'
import { renderWithProviders } from '../test/helpers'
import SessionColorSwatches from './SessionColorSwatches'
import { store } from '../store'
import { sseSlots, sseSlotColor } from '../store/dashboardSlice'
import { api } from '../api/client'
import type { ChatSlot } from '../types'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return { ...mod, api: { ...mod.api, setSlotColor: vi.fn(), clearSlotColor: vi.fn() } }
})
vi.mock('../hooks/useSessionPalette', () => ({
  useSessionPalette: () => ({ paletteColors: ['#111111', '#222222'] }),
}))

const setSlotColor = vi.mocked(api.setSlotColor)
const clearSlotColor = vi.mocked(api.clearSlotColor)

/**
 * The component's optimistic write dispatches through `useAppDispatch` (the
 * Provider store) but its rollback guard reads the singleton `store` directly,
 * so both halves only agree when the Provider IS the real store. Render through
 * it rather than a throwaway test store.
 */
const render = (ui: React.ReactElement) =>
  renderWithProviders(ui, { store: store as never })

function seedStore(colorIndex: number | null) {
  store.dispatch(sseSlots([
    { key: 'zzq-slot', messages: 0, running: false, color_index: colorIndex } as ChatSlot,
  ]))
}

const colorOf = () => store.getState().dashboard.slots[0]?.color_index ?? null

describe('SessionColorSwatches', () => {
  beforeEach(() => {
    setSlotColor.mockReset()
    setSlotColor.mockResolvedValue(undefined as never)
    clearSlotColor.mockReset()
    clearSlotColor.mockResolvedValue(undefined as never)
    seedStore(null)
  })
  afterEach(() => {
    store.dispatch(sseSlots([]))
  })

  it('renders a no-colour swatch plus one per palette colour', () => {
    render(<SessionColorSwatches slotKey="zzq-slot" />)
    expect(screen.getByLabelText('No color')).toBeInTheDocument()
    // no-colour + one per palette colour + the custom-colour trigger
    expect(screen.getAllByRole('button')).toHaveLength(4)
  })

  it('marks the active swatch, and the no-colour one when nothing is set', () => {
    const { unmount } = render(
      <SessionColorSwatches slotKey="zzq-slot" colorIndex={null} />,
    )
    expect(screen.getByLabelText('No color').className).toContain('border-text-strong')
    unmount()

    render(<SessionColorSwatches slotKey="zzq-slot" colorIndex={1} />)
    const swatches = screen.getAllByRole('button')
    expect(swatches[0].className).toContain('border-transparent')
    expect(swatches[2].className).toContain('border-text-strong')
  })

  it('picking a colour writes optimistically and calls the API', async () => {
    const onPicked = vi.fn()
    render(<SessionColorSwatches slotKey="zzq-slot" onPicked={onPicked} />)

    fireEvent.click(screen.getAllByRole('button')[1])
    expect(colorOf()).toBe(0)
    expect(onPicked).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(setSlotColor).toHaveBeenCalledWith('zzq-slot', 0))
  })

  it('picking no-colour clears the index', async () => {
    seedStore(1)
    render(<SessionColorSwatches slotKey="zzq-slot" colorIndex={1} />)
    fireEvent.click(screen.getByLabelText('No color'))
    expect(colorOf()).toBeNull()
    // Clearing goes through the dedicated endpoint since custom hex landed:
    // it clears both color_index and color_hex in one PATCH.
    await waitFor(() => expect(clearSlotColor).toHaveBeenCalledWith('zzq-slot'))
  })

  it('a failed write rolls the optimistic colour back', async () => {
    seedStore(1)
    setSlotColor.mockRejectedValue(new Error('zzq-color-broke'))
    render(<SessionColorSwatches slotKey="zzq-slot" colorIndex={1} />)

    fireEvent.click(screen.getAllByRole('button')[1])
    expect(colorOf()).toBe(0)
    await waitFor(() => expect(colorOf()).toBe(1))
  })

  it('a superseding pick is not clobbered by the earlier failure rolling back', async () => {
    seedStore(1)
    setSlotColor.mockRejectedValue(new Error('zzq-color-broke'))
    render(<SessionColorSwatches slotKey="zzq-slot" colorIndex={1} />)

    fireEvent.click(screen.getAllByRole('button')[1])
    // A later pick lands before the first rejection is handled.
    store.dispatch(sseSlotColor({ key: 'zzq-slot', color_index: 1 }))
    await waitFor(() => expect(setSlotColor).toHaveBeenCalled())
    // The guard sees current !== idx and leaves the newer value alone.
    await waitFor(() => expect(colorOf()).toBe(1))
  })

  it('swallows key events so the surrounding menu does not act on them', () => {
    const onKeyDown = vi.fn()
    render(
      <div onKeyDown={onKeyDown}>
        <SessionColorSwatches slotKey="zzq-slot" />
      </div>,
    )
    fireEvent.keyDown(screen.getByLabelText('No color'), { key: 'a' })
    expect(onKeyDown).not.toHaveBeenCalled()
  })
})
