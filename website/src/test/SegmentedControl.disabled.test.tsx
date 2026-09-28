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
  it('exposes an exclusive selected radio and associates disabled choices with help', () => {
    render(
      <>
        <p id="why-disabled">This choice is unavailable.</p>
        <SegmentedControl<Key>
          segments={SEGMENTS}
          value="live"
          onChange={vi.fn()}
          collapse={false}
          ariaLabel="Release channel"
          ariaDescribedBy="why-disabled"
        />
      </>,
    )
    const group = screen.getByRole('radiogroup', { name: 'Release channel' })
    expect(group).toHaveAttribute('aria-describedby', 'why-disabled')
    expect(screen.getByRole('radio', { name: 'Live' })).toHaveAttribute('aria-checked', 'true')
    const planned = screen.getByRole('radio', { name: 'Planned' })
    expect(planned).toHaveAttribute('aria-checked', 'false')
    expect(planned).toHaveAttribute('aria-describedby', 'why-disabled')
  })

  it('keeps the selected pill on a disabled selected segment and one tab stop on an all-disabled group', () => {
    const ALL_DISABLED: Array<Segment<Key>> = SEGMENTS.map(segment => ({ ...segment, disabled: true }))
    const { container } = render(
      <SegmentedControl<Key> segments={ALL_DISABLED} value="planned" onChange={vi.fn()} collapse={false} />,
    )
    const planned = screen.getByRole('radio', { name: 'Planned' })
    // Greyed out, but still shows which option is in force.
    expect(planned.querySelector('.bg-card')).not.toBeNull()
    expect(container.querySelectorAll('.bg-card')).toHaveLength(1)
    expect(screen.getAllByRole('radio').filter(radio => radio.tabIndex === 0)).toEqual([planned])
  })

  it('arrow keys move focus without persisting a passed-over option', () => {
    const onChange = vi.fn()
    const TWO: Array<Segment<Key>> = [{ key: 'live', label: 'Live' }, { key: 'planned', label: 'Planned' }]
    render(<SegmentedControl<Key> segments={TWO} value="live" onChange={onChange} collapse={false} />)
    const live = screen.getByRole('radio', { name: 'Live' })
    live.focus()
    fireEvent.keyDown(live, { key: 'ArrowRight' })
    expect(screen.getByRole('radio', { name: 'Planned' })).toHaveFocus()
    expect(onChange).not.toHaveBeenCalled()
  })

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

  it('marks disabled segments with ARIA semantics and removes them from the tab sequence', () => {
    // `aria-disabled` exposes the unavailable state and the tooltip keeps the
    // pointer explanation. Roving focus must still skip the segment, so it is
    // programmatically present but not a sequential tab stop.
    render(<SegmentedControl<Key> segments={SEGMENTS} value="live" onChange={vi.fn()} collapse={false} />)
    const planned = screen.getByText('Planned').closest('button')
    expect(planned).toHaveAttribute('aria-disabled', 'true')
    expect(planned).not.toBeDisabled()
    expect(planned).toHaveAttribute('tabindex', '-1')
    expect(planned).toHaveAttribute('title', 'Not wired up yet')
  })

  it('leaves enabled segments unmarked', () => {
    render(<SegmentedControl<Key> segments={SEGMENTS} value="live" onChange={vi.fn()} collapse={false} />)
    expect(screen.getByText('Live').closest('button')).not.toHaveAttribute('aria-disabled')
  })
})
