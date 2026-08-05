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

const mocks = vi.hoisted(() => ({ setSlotColor: vi.fn() }))
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

beforeEach(() => mocks.setSlotColor.mockResolvedValue({}))
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

  it('picking "No color" persists null', async () => {
    wrap(<SessionColorSwatches slotKey={SLOT} colorIndex={2} />)
    fireEvent.click(screen.getByLabelText('No color'))
    await waitFor(() => expect(mocks.setSlotColor).toHaveBeenCalledWith(SLOT, null))
  })
})
