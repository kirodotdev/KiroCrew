import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import HoverTip from '../components/HoverTip'
import { OPEN_DELAY_MS } from '../components/InstantTip'
import ReadingWidthToggle from '../components/ReadingWidthToggle'

describe('HoverTip', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('shows the bubble after the short intent delay, not the OS title delay', () => {
    render(<HoverTip label="Download"><button type="button" aria-label="Download">D</button></HoverTip>)
    const btn = screen.getByRole('button', { name: 'Download' })
    fireEvent.mouseEnter(btn.parentElement as HTMLElement)
    expect(screen.queryByRole('tooltip')).toBeNull()
    act(() => { vi.advanceTimersByTime(OPEN_DELAY_MS) })
    expect(screen.getByRole('tooltip')).toHaveTextContent('Download')
    fireEvent.mouseLeave(btn.parentElement as HTMLElement)
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('keeps the description on the control while the bubble is closed', () => {
    render(<HoverTip label="Publish this artifact"><button type="button">Publish</button></HoverTip>)
    expect(screen.getByRole('button', { name: 'Publish', description: 'Publish this artifact' })).not.toHaveAttribute('title')
  })

  it('keeps the description out of the reading order, so it is not read twice as page text', () => {
    render(<h1><span>Notes</span><HoverTip label="Rename this artifact"><button type="button">Rename</button></HoverTip></h1>)
    expect(screen.getByRole('heading', { name: 'Notes Rename' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Rename', description: 'Rename this artifact' })).toBeInTheDocument()
  })

  it('opens synchronously on keyboard focus', () => {
    render(<HoverTip label="Copy"><button type="button">C</button></HoverTip>)
    act(() => { screen.getByRole('button').focus() })
    expect(screen.getByRole('tooltip')).toHaveTextContent('Copy')
  })

  it('wraps a long hint inside the window instead of running it off the edge', () => {
    const long = 'This hint explains at length why no balance is shown here and whose account the credits are billed to instead.'
    render(<HoverTip label={long}><button type="button">K</button></HoverTip>)
    fireEvent.mouseEnter(screen.getByRole('button').parentElement as HTMLElement)
    act(() => { vi.advanceTimersByTime(OPEN_DELAY_MS) })
    const tip = screen.getByRole('tooltip')
    expect(tip).toHaveTextContent(long)
    expect(tip).toHaveClass('max-w-[min(26rem,calc(100vw-1rem))]')
    expect(tip).not.toHaveClass('whitespace-nowrap')
  })

  it('moves a hint that grows while open back inside the window', () => {
    // A pill near the right edge opens on a short hint, then its reading
    // settles to a longer one while the bubble stays open: the fit to the
    // window must run for the new text, not stay where the short one fit.
    const short = 'Checking credits'
    const long = 'Credits for this sign-in are billed to the account that owns the API key'
    const saved = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetWidth')
    const savedInnerWidth = Object.getOwnPropertyDescriptor(window, 'innerWidth')
    Object.defineProperty(HTMLElement.prototype, 'offsetWidth', {
      configurable: true,
      get() { return (this as HTMLElement).textContent === long ? 300 : 100 },
    })
    Object.defineProperty(window, 'innerWidth', { value: 1024, configurable: true })
    try {
      const { rerender } = render(<HoverTip label={short}><button type="button">K</button></HoverTip>)
      const btn = screen.getByRole('button')
      const wrapper = btn.parentElement as HTMLElement
      // Wrapper at 900: the 100px hint fits (900 + 100 <= 1016), the 300px one does not.
      wrapper.getBoundingClientRect = () => ({ top: 0, left: 900, right: 910, bottom: 24, width: 10, height: 24, x: 900, y: 0, toJSON: () => ({}) }) as DOMRect
      fireEvent.mouseEnter(wrapper)
      act(() => { vi.advanceTimersByTime(OPEN_DELAY_MS) })
      expect(parseFloat(screen.getByRole('tooltip').style.left)).toBe(900)
      rerender(<HoverTip label={long}><button type="button">K</button></HoverTip>)
      expect(screen.getByRole('button')).toBe(btn)
      const tip = screen.getByRole('tooltip')
      expect(tip).toHaveTextContent(long)
      const left = parseFloat(tip.style.left)
      expect(left + 300).toBeLessThanOrEqual(1024 - 8)
      expect(left).toBe(1024 - 8 - 300)
    } finally {
      if (saved) Object.defineProperty(HTMLElement.prototype, 'offsetWidth', saved)
      else delete (HTMLElement.prototype as unknown as Record<string, unknown>).offsetWidth
      if (savedInnerWidth) Object.defineProperty(window, 'innerWidth', savedInnerWidth)
    }
  })

  it('paints nothing on a focus the browser would not ring, such as a dialog handing focus back after a mouse close', () => {
    render(<HoverTip label="Kiro account & credits"><button type="button">K</button></HoverTip>)
    const btn = screen.getByRole('button')
    const matches = btn.matches.bind(btn)
    vi.spyOn(btn, 'matches').mockImplementation(sel => sel === ':focus-visible' ? false : matches(sel))
    act(() => { btn.focus() })
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('without a label, leaves the control as it is inside the same wrapper', () => {
    const { rerender } = render(<HoverTip label="Click to hide"><button type="button">M</button></HoverTip>)
    const btn = screen.getByRole('button')
    fireEvent.mouseEnter(btn.parentElement as HTMLElement)
    act(() => { vi.advanceTimersByTime(OPEN_DELAY_MS) })
    expect(screen.getByRole('tooltip')).toHaveTextContent('Click to hide')
    rerender(<HoverTip><button type="button" aria-describedby="card">M</button></HoverTip>)
    expect(screen.getByRole('button')).toBe(btn)
    expect(btn).toHaveAttribute('aria-describedby', 'card')
    expect(btn.parentElement).toHaveAttribute('data-hover-tip')
    expect(screen.queryByRole('tooltip')).toBeNull()
    // The pointer leaves while no hint shows, so the hint coming back stays
    // closed instead of reopening where the pointer no longer is.
    fireEvent.mouseLeave(btn.parentElement as HTMLElement)
    rerender(<HoverTip label="Click to hide"><button type="button">M</button></HoverTip>)
    expect(screen.queryByRole('tooltip')).toBeNull()
  })
})

describe('ReadingWidthToggle', () => {
  it('renders an icon, not a letter, and no native title', () => {
    render(<ReadingWidthToggle value="md" onToggle={() => {}} />)
    const btn = screen.getByRole('button', { name: 'Medium width' })
    expect(btn).toHaveTextContent('')
    expect(btn.querySelector('svg')).not.toBeNull()
    expect(btn).not.toHaveAttribute('title')
  })
})
