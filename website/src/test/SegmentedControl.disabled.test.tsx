// A segment for a capability the surface cannot serve yet must be visibly
// unavailable. Omitting it says "does not exist" and accepting the click says
// "broken", so the disabled state is the only honest option — and it is only
// honest if the click genuinely does nothing.
import { describe, it, expect, vi, beforeAll } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import SegmentedControl, { type Segment } from '../components/SegmentedControl'

type Key = 'live' | 'planned'

const SEGMENTS: Array<Segment<Key>> = [
  { key: 'live', label: 'Live' },
  { key: 'planned', label: 'Planned', disabled: true, tooltip: 'Not wired up yet' },
]

beforeAll(() => {
  // SegmentedControl measures its parent to decide whether to collapse.
  class RO {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  ;(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = RO
})

describe('SegmentedControl disabled segments', () => {
  it('renders a disabled segment instead of hiding it', () => {
    render(<SegmentedControl<Key> segments={SEGMENTS} value="live" onChange={vi.fn()} collapse={false} />)
    expect(screen.getByText('Planned')).toBeInTheDocument()
  })

  it('refuses selection, so the caller never sees the disabled key', () => {
    const onChange = vi.fn()
    render(<SegmentedControl<Key> segments={SEGMENTS} value="live" onChange={onChange} collapse={false} />)
    fireEvent.click(screen.getByText('Planned'))
    expect(onChange).not.toHaveBeenCalled()
  })

  it('still selects the enabled segments', () => {
    const onChange = vi.fn()
    render(<SegmentedControl<Key> segments={SEGMENTS} value="planned" onChange={onChange} collapse={false} />)
    fireEvent.click(screen.getByText('Live'))
    expect(onChange).toHaveBeenCalledWith('live')
  })

  it('marks the segment aria-disabled rather than using the disabled attribute', () => {
    // `aria-disabled` keeps the control focusable, so a keyboard or
    // screen-reader user can still reach it and read the tooltip that explains
    // WHY it is unavailable. The `disabled` attribute would remove it from the
    // tab order and take that explanation with it.
    render(<SegmentedControl<Key> segments={SEGMENTS} value="live" onChange={vi.fn()} collapse={false} />)
    const planned = screen.getByText('Planned').closest('button')
    expect(planned).toHaveAttribute('aria-disabled', 'true')
    expect(planned).not.toBeDisabled()
    expect(planned).toHaveAttribute('title', 'Not wired up yet')
  })

  it('leaves enabled segments unmarked', () => {
    render(<SegmentedControl<Key> segments={SEGMENTS} value="live" onChange={vi.fn()} collapse={false} />)
    expect(screen.getByText('Live').closest('button')).not.toHaveAttribute('aria-disabled')
  })
})
