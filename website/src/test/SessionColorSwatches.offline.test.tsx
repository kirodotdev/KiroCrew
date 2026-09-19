/**
 * The colour swatch row is a gateway write sitting inside the session menu whose
 * other write rows dim offline. Left at full weight it reads as the one action
 * that still works, and a click was accepted and dropped.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { createTestStore } from './helpers'
import { sseConnected, sseDisconnected } from '../store/dashboardSlice'

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
vi.mock('../hooks/useSessionPalette', () => ({
  useSessionPalette: () => ({ paletteColors: ['#ff0000', '#00ff00', '#0000ff'] }),
}))

import SessionColorSwatches from '../components/SessionColorSwatches'

const SLOT = 'chat-color-1'

function mount(connected: boolean) {
  const store = createTestStore()
  if (connected) store.dispatch(sseConnected())
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const utils = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <SessionColorSwatches slotKey={SLOT} colorIndex={null} />
      </Provider>
    </QueryClientProvider>,
  )
  return { ...utils, store }
}

beforeEach(() => {
  mocks.setSlotColor.mockResolvedValue({})
  mocks.setSlotColorHex.mockResolvedValue({})
  mocks.clearSlotColor.mockResolvedValue({})
})
afterEach(() => vi.clearAllMocks())

describe('SessionColorSwatches (gateway offline)', () => {
  it('refuses every swatch and says why, instead of taking a PATCH that cannot land', () => {
    mount(false)
    const noColor = screen.getByLabelText('No color')
    expect(noColor).toBeDisabled()
    fireEvent.click(noColor)
    expect(mocks.clearSlotColor).not.toHaveBeenCalled()
    for (const b of screen.getAllByRole('button')) expect(b).toBeDisabled()
  })

  it('dims the row and names the refused action, matching its dimmed menu siblings', () => {
    const { container } = mount(false)
    const row = container.querySelector('[aria-disabled="true"]') as HTMLElement
    expect(row).not.toBeNull()
    expect(row.className).toContain('opacity-40')
    expect(row.getAttribute('title')).toMatch(/Gateway offline/)
  })

  it('takes the same picks at full weight when connected — the control', async () => {
    const { container } = mount(true)
    const noColor = screen.getByLabelText('No color')
    expect(noColor).not.toBeDisabled()
    expect(container.querySelector('[aria-disabled="true"]')).toBeNull()
    fireEvent.click(noColor)
    await waitFor(() => expect(mocks.clearSlotColor).toHaveBeenCalledWith(SLOT))
  })

  it('commits a debounced hex while connected — the control for the drop below', async () => {
    vi.useFakeTimers()
    try {
      mount(true)
      fireEvent.click(screen.getByLabelText('Custom color'))
      const wheel = document.querySelector('input[type="color"]') as HTMLInputElement
      fireEvent.change(wheel, { target: { value: '#123456' } })
      // Async advance: flushes the debounce timer AND the react-query microtask
      // chain that carries mutate() -> mutationFn.
      await vi.advanceTimersByTimeAsync(400)
      expect(mocks.setSlotColorHex).toHaveBeenCalledWith(SLOT, '#123456')
    } finally {
      vi.useRealTimers()
    }
  })

  it('drops a debounced hex commit when the gateway goes away mid-drag', async () => {
    vi.useFakeTimers()
    try {
      const { store } = mount(true)
      fireEvent.click(screen.getByLabelText('Custom color'))
      const wheel = document.querySelector('input[type="color"]') as HTMLInputElement
      fireEvent.change(wheel, { target: { value: '#123456' } })
      act(() => { store.dispatch(sseDisconnected()) })
      await vi.advanceTimersByTimeAsync(400)
      expect(mocks.setSlotColorHex).not.toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
    }
  })
})
