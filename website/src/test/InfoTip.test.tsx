import { describe, it, expect, afterEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import InfoTip from '../components/InfoTip'

const TIP_W = 300 // matches tipW in InfoTip's pos()

const setInnerWidth = (w: number) =>
  Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: w })

const stubRect = (el: HTMLElement, rect: { left: number; top: number; right: number; bottom: number }) => {
  el.getBoundingClientRect = () =>
    ({
      ...rect,
      width: rect.right - rect.left,
      height: rect.bottom - rect.top,
      x: rect.left,
      y: rect.top,
      toJSON: () => ({}),
    }) as DOMRect
}

describe('InfoTip', () => {
  afterEach(() => setInnerWidth(1024))

  it('renders a ? button with title', () => {
    render(<InfoTip text="Help text" />)
    const btn = screen.getByTitle('Help text')
    expect(btn).toBeInTheDocument()
    expect(btn).toHaveTextContent('?')
  })

  it('is named by a short phrase, and describes itself with the tip text', () => {
    // The visible glyph is a bare "?", which assistive technology would
    // otherwise announce as "question mark" — a control with no discoverable
    // purpose, and a blocking a11y rule. The tip prose is the DESCRIPTION, not
    // the name: a name is read on every visit, and it is also the handle every
    // other control is queried by, so a paragraph-length one both talks over
    // the user and collides with real actions named inside that prose.
    render(<InfoTip text="What this binding does" />)
    const btn = screen.getByRole('button', { name: 'More information' })
    expect(btn).toHaveAttribute('aria-expanded', 'false')
    expect(btn).not.toHaveAttribute('aria-describedby')

    fireEvent.click(btn)
    expect(btn).toHaveAttribute('aria-expanded', 'true')
    const tip = screen.getByRole('tooltip')
    expect(tip).toHaveTextContent('What this binding does')
    expect(btn.getAttribute('aria-describedby')).toBe(tip.id)
  })

  it('shows tooltip on click', () => {
    render(<InfoTip text="Detailed help" />)
    fireEvent.click(screen.getByTitle('Detailed help'))
    expect(screen.getByText('Detailed help')).toBeInTheDocument()
  })

  it('hides tooltip on outside click', () => {
    render(<InfoTip text="Tip content" />)
    fireEvent.click(screen.getByTitle('Tip content'))
    expect(screen.getByText('Tip content')).toBeInTheDocument()
    fireEvent.mouseDown(document.body)
    expect(screen.queryByText('Tip content')).not.toBeInTheDocument()
  })

  it('keeps auto placement on-screen on a narrow viewport', () => {
    // Phone-width regression: right-side placement overflows, and the left-flip
    // (r.left - tipW - 6) goes far negative for a button near the left edge.
    // Unclamped, the tip renders mostly past the left viewport edge.
    setInnerWidth(390)
    render(<InfoTip text="Narrow viewport tip" />)
    const btn = screen.getByTitle('Narrow viewport tip')
    stubRect(btn, { left: 100, top: 200, right: 116, bottom: 216 })
    fireEvent.click(btn)
    const tip = screen.getByRole('tooltip')
    const left = parseFloat(tip.style.left)
    expect(left).toBeGreaterThanOrEqual(8)
    expect(left + TIP_W).toBeLessThanOrEqual(390) // fully on-screen
  })

  it('still flips left of the button when the flipped position fits', () => {
    // The clamp must not defeat the flip: a button near the RIGHT edge flips
    // left and the flipped value already fits, so it is used as-is.
    setInnerWidth(390)
    render(<InfoTip text="Right edge tip" />)
    const btn = screen.getByTitle('Right edge tip')
    stubRect(btn, { left: 350, top: 200, right: 366, bottom: 216 })
    fireEvent.click(btn)
    const tip = screen.getByRole('tooltip')
    // flipped: 350 - 300 - 6 = 44; inside [8, 390-300-8=82], so unchanged.
    expect(parseFloat(tip.style.left)).toBe(44)
  })
})
