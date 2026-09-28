import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, screen } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { copyToClipboard } from '../utils/clipboard'

vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn(async () => undefined) }))

beforeEach(() => { vi.mocked(copyToClipboard).mockClear() })

/** Dispatch a real cancelable click and report whether the default action
 *  survived. `preventDefault` from ANY listener in propagation cancels an
 *  anchor's navigation, so this is the mechanism under test — not a spy on
 *  window.location, which jsdom does not implement. */
function clickAndReadDefault(el: Element): boolean {
  const ev = new MouseEvent('click', { bubbles: true, cancelable: true })
  el.dispatchEvent(ev)
  return ev.defaultPrevented
}

describe('inline code used as a link label', () => {
  it('keeps the anchor navigable instead of copying', async () => {
    render(<MarkdownRenderer content={'See [`https://example.com/x`](https://example.com/x) now.'} />)
    const code = await screen.findByText('https://example.com/x')

    expect(code.tagName).toBe('CODE')
    expect(code.closest('a')).toHaveAttribute('href', 'https://example.com/x')

    // The defect: the span's copy handler called preventDefault, which cancels
    // the enclosing anchor's navigation from anywhere in propagation.
    expect(clickAndReadDefault(code)).toBe(false)
    expect(copyToClipboard).not.toHaveBeenCalled()

    // Not a control either: the anchor owns the click, so the span must not
    // advertise itself as a button or take the keyboard.
    expect(code).not.toHaveAttribute('role', 'button')
    expect(code).not.toHaveAttribute('tabindex')
  })

  it('still copies a backticked span that is NOT inside a link', async () => {
    render(<MarkdownRenderer content={'Open `https://example.com/x` now.'} />)
    const code = await screen.findByText('https://example.com/x')

    expect(code).toHaveAttribute('role', 'button')
    fireEvent.click(code)
    expect(copyToClipboard).toHaveBeenCalledWith('https://example.com/x')
  })

  it('still copies a backticked command in prose', async () => {
    render(<MarkdownRenderer content={'Run `npm test` please.'} />)
    const code = await screen.findByText('npm test')
    fireEvent.click(code)
    expect(copyToClipboard).toHaveBeenCalledWith('npm test')
  })

  it('leaves a plain-text link label navigable', async () => {
    render(<MarkdownRenderer content={'See [the docs](https://example.com/y) now.'} />)
    const anchor = await screen.findByText('the docs')
    expect(anchor.tagName).toBe('A')
    expect(clickAndReadDefault(anchor)).toBe(false)
  })
})
