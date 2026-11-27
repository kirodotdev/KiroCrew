import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { FilterPanel, RANGE_SETTLE_MS } from '../pages/overview/memoryPage/MemoryListParts'
import { DEFAULT_VIEW } from '../pages/overview/memoryPage/memoryListView'

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
})
