import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import { SettingsToggle } from '../components/settings'
import { HOVER_LEAVE_GRACE_MS, HOVER_OPEN_DELAY_MS } from '../components/InfoTip'

describe('InfoTip in a settings row', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('keeps tooltip clicks separate from row activation', () => {
    const onChange = vi.fn()
    const { container } = render(
      <SettingsToggle label="Example setting" hint="Help text" checked={false} onChange={onChange} />
    )

    fireEvent.click(screen.getByRole('button', { name: 'More information' }))
    expect(onChange).not.toHaveBeenCalled()
    const tip = screen.getByRole('tooltip')
    expect(tip).toHaveTextContent('Help text')
    expect(tip.parentElement).toBe(document.body)
    // A press inside the bubble (which pins the tip) and the click that follows
    // reach neither the row's handler nor anything that would close the tip.
    fireEvent.pointerDown(tip, { pointerType: 'mouse', button: 0 })
    fireEvent.mouseDown(tip)
    fireEvent.click(tip)
    expect(onChange).not.toHaveBeenCalled()
    expect(screen.getByRole('tooltip')).toBe(tip)

    fireEvent.click(container.firstElementChild!)
    expect(onChange).toHaveBeenCalledTimes(1)
    expect(onChange).toHaveBeenCalledWith(true)
  })

  it('a press inside a hover-opened bubble pins the tip without flipping the row', () => {
    const onChange = vi.fn()
    render(
      <SettingsToggle label="Example setting" hint="Help text" checked={false} onChange={onChange} />
    )
    const glyph = screen.getByRole('button', { name: 'More information' })
    fireEvent.pointerEnter(glyph, { pointerType: 'mouse' })
    // Hover opens only once the mouse has rested on the glyph for the delay.
    act(() => { vi.advanceTimersByTime(HOVER_OPEN_DELAY_MS + 1) })
    const tip = screen.getByRole('tooltip')
    fireEvent.pointerDown(tip, { pointerType: 'mouse', button: 0 })
    fireEvent.mouseDown(tip)
    fireEvent.click(tip)
    expect(onChange).not.toHaveBeenCalled()
    expect(screen.getByRole('tooltip')).toBe(tip)
    // Pinned by the press: once the pointer has left glyph and bubble, leaving
    // does not close it and an outside press does -- with the row still
    // untouched by anything that happened inside the bubble.
    fireEvent.pointerLeave(glyph, { pointerType: 'mouse' })
    fireEvent.pointerLeave(tip, { pointerType: 'mouse' })
    act(() => { vi.advanceTimersByTime(HOVER_LEAVE_GRACE_MS + 1) })
    expect(screen.getByRole('tooltip')).toBe(tip)
    fireEvent.mouseDown(document.body)
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
    expect(onChange).not.toHaveBeenCalled()
  })

  it('a settings row places its tip above the glyph, never over the control beneath the label', () => {
    // Beside-the-glyph placement top-aligns a tall bubble with the label, so on
    // a field row (control under the label) the bubble sat over the control and
    // its click swallowing left the reader unable to press it. Rows ask for
    // 'top' so the bubble opens over the row above instead.
    Object.defineProperty(window, 'innerHeight', { value: 900, configurable: true })
    render(<SettingsToggle label="Placed" hint="Help text" checked={false} onChange={() => {}} />)
    const btn = screen.getByRole('button', { name: /more information/i })
    btn.getBoundingClientRect = () => ({ left: 100, top: 400, right: 116, bottom: 416, width: 16, height: 16, x: 100, y: 400, toJSON: () => ({}) }) as DOMRect
    fireEvent.click(btn)
    const tip = screen.getByRole('tooltip')
    expect(tip.style.top).toBe('')
    expect(parseFloat(tip.style.bottom)).toBe(900 - 400 + 6)
  })
})
