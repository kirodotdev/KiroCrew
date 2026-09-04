import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/react'
import { Slider } from '../components/ui'

describe('Slider', () => {
  it('exposes slider role with aria-value* and valuetext', () => {
    const { getByRole } = render(
      <Slider value={40} onChange={() => {}} min={0} max={100} step={5} label="Volume" formatValue={v => `${v}%`} />,
    )
    const slider = getByRole('slider')
    expect(slider.getAttribute('aria-valuemin')).toBe('0')
    expect(slider.getAttribute('aria-valuemax')).toBe('100')
    expect(slider.getAttribute('aria-valuenow')).toBe('40')
    expect(slider.getAttribute('aria-valuetext')).toBe('40%')
    expect(slider.getAttribute('aria-label')).toBe('Volume')
  })

  it('steps by `step` on arrow keys', () => {
    const onChange = vi.fn()
    const { getByRole } = render(<Slider value={40} onChange={onChange} step={5} />)
    fireEvent.keyDown(getByRole('slider'), { key: 'ArrowRight' })
    expect(onChange).toHaveBeenCalledWith(45)
    fireEvent.keyDown(getByRole('slider'), { key: 'ArrowLeft' })
    expect(onChange).toHaveBeenCalledWith(35)
  })

  it('jumps by ×10 on PageUp/Down and clamps to min/max on Home/End', () => {
    const onChange = vi.fn()
    const { getByRole } = render(<Slider value={50} onChange={onChange} step={5} min={0} max={100} />)
    const slider = getByRole('slider')
    fireEvent.keyDown(slider, { key: 'PageUp' })
    expect(onChange).toHaveBeenCalledWith(100) // 50 + 50, clamped at max
    fireEvent.keyDown(slider, { key: 'Home' })
    expect(onChange).toHaveBeenCalledWith(0)
    fireEvent.keyDown(slider, { key: 'End' })
    expect(onChange).toHaveBeenCalledWith(100)
  })

  it('snaps off-grid values to the nearest step', () => {
    const onChange = vi.fn()
    // value=42, step=5 -> ArrowRight should land on a multiple of 5 (45), not 47
    const { getByRole } = render(<Slider value={42} onChange={onChange} step={5} />)
    fireEvent.keyDown(getByRole('slider'), { key: 'ArrowRight' })
    expect(onChange).toHaveBeenCalledWith(45)
  })

  it('does not fire onChange when disabled', () => {
    const onChange = vi.fn()
    const { getByRole } = render(<Slider value={40} onChange={onChange} disabled />)
    fireEvent.keyDown(getByRole('slider'), { key: 'ArrowRight' })
    expect(onChange).not.toHaveBeenCalled()
    expect(getByRole('slider', { hidden: true }).getAttribute('tabindex')).toBe('-1')
  })

  it('renders one tick mark per step boundary when stepped', () => {
    const { container } = render(<Slider value={0} onChange={() => {}} min={0} max={10} step={2} />)
    // 5 segments -> 6 tick boundaries
    const ticks = container.querySelectorAll('[aria-hidden] > span')
    expect(ticks.length).toBe(6)
  })
})
