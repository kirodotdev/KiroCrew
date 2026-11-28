import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { useState } from 'react'
import { act, fireEvent, screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { FilterPanel, RANGE_SETTLE_MS } from '../pages/overview/memoryPage/MemoryListParts'
import { DEFAULT_VIEW, setConfidenceBound, type ListView } from '../pages/overview/memoryPage/memoryListView'

/** The confidence sliders reload the list once per resting value, not once per step. */
describe('FilterPanel confidence range', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('reports a run of steps as one range change once the slider rests', () => {
    const onRange = vi.fn()
    renderWithProviders(<FilterPanel view={DEFAULT_VIEW} onToggle={() => {}} onRange={onRange} />)
    const lowest = screen.getAllByRole('slider')[0]
    for (let i = 0; i < 5; i++) fireEvent.keyDown(lowest, { key: 'ArrowRight' })
    // Each step shows at once, for sight and for a screen reader.
    expect(lowest).toHaveAttribute('aria-valuenow', '0.25')
    act(() => { vi.advanceTimersByTime(RANGE_SETTLE_MS - 1) })
    expect(onRange).not.toHaveBeenCalled()
    act(() => { vi.advanceTimersByTime(1) })
    expect(onRange).toHaveBeenCalledTimes(1)
    expect(onRange).toHaveBeenCalledWith('conf_min', 0.25)
  })

  it('reports a value still pending when the panel closes', () => {
    const onRange = vi.fn()
    const { unmount } = renderWithProviders(<FilterPanel view={DEFAULT_VIEW} onToggle={() => {}} onRange={onRange} />)
    fireEvent.keyDown(screen.getAllByRole('slider')[1], { key: 'ArrowLeft' })
    unmount()
    expect(onRange).toHaveBeenCalledTimes(1)
    expect(onRange).toHaveBeenCalledWith('conf_max', 0.95)
  })

  /** The page's wiring: the bound is clamped inside the state update, and Clear all bumps the reset. */
  function Page({ seen }: { seen: (view: ListView) => void }) {
    const [view, setView] = useState<ListView>(DEFAULT_VIEW)
    const [reset, setReset] = useState(0)
    seen(view)
    return <>
      <button type="button" onClick={() => { setReset(n => n + 1); setView(old => ({ ...old, filters: {} })) }}>Clear all</button>
      <FilterPanel view={view} rangeReset={reset} onToggle={() => {}} onRange={(bound, value) => setView(old => ({ ...old, filters: setConfidenceBound(old.filters, bound, value) }))} />
    </>
  }

  it('drops a value still settling when Clear all is pressed', () => {
    let last = DEFAULT_VIEW
    renderWithProviders(<Page seen={view => { last = view }} />)
    const lowest = screen.getAllByRole('slider')[0]
    fireEvent.keyDown(lowest, { key: 'ArrowRight' })
    fireEvent.keyDown(lowest, { key: 'ArrowRight' })
    fireEvent.click(screen.getByRole('button', { name: 'Clear all' }))
    act(() => { vi.advanceTimersByTime(RANGE_SETTLE_MS * 2) })
    expect(last.filters).toEqual({})
    expect(lowest).toHaveAttribute('aria-valuenow', '0')
  })

  it('never leaves the lowest bound above the highest when both settle in one window', () => {
    let last = DEFAULT_VIEW
    renderWithProviders(<Page seen={view => { last = view }} />)
    const [lowest, highest] = screen.getAllByRole('slider')
    fireEvent.keyDown(lowest, { key: 'End' })
    fireEvent.keyDown(highest, { key: 'Home' })
    act(() => { vi.advanceTimersByTime(RANGE_SETTLE_MS * 2) })
    const low = last.filters.conf_min ?? 0
    const high = last.filters.conf_max ?? 1
    expect(low).toBeLessThanOrEqual(high)
    // And in the other order.
    fireEvent.click(screen.getByRole('button', { name: 'Clear all' }))
    fireEvent.keyDown(highest, { key: 'Home' })
    fireEvent.keyDown(lowest, { key: 'End' })
    act(() => { vi.advanceTimersByTime(RANGE_SETTLE_MS * 2) })
    expect(last.filters.conf_min ?? 0).toBeLessThanOrEqual(last.filters.conf_max ?? 1)
  })
})
