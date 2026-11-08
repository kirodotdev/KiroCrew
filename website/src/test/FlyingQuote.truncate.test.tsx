import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, cleanup } from '@testing-library/react'
import FlyingQuote from '../components/FlyingQuote'

// A lone high surrogate with no trailing low surrogate — the broken half an
// emoji leaves behind when a UTF-16 slice cuts through it.
const LONE_HIGH = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])/

/** Keep framer-motion real but inert here: the truncation under test is the
 *  synchronous `text` → displayed string, independent of the flight. */
const rect = { left: 40, top: 100, width: 200, height: 20 } as DOMRect

function composer() {
  const host = document.createElement('div')
  host.appendChild(document.createElement('textarea'))
  document.body.appendChild(host)
  return host
}

/** Render the overlay and read back the one line of quoted text it displays. */
function displayedQuote(text: string): string {
  render(<FlyingQuote from={rect} targetRef={{ current: composer() }} text={text} onComplete={vi.fn()} />)
  // The quote renders in the lone span inside the bubble.
  const span = document.querySelector('span.line-clamp-2')
  return span?.textContent ?? ''
}

// text.length > 60 ? text.slice(0, 57) + '…' : text
describe('FlyingQuote emoji truncation', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks() })

  it('drops an emoji the 60-unit quote cap would cut in half', () => {
    // '🎉' occupies UTF-16 units 56 and 57. The cut at 57 keeps the high
    // surrogate (56) and drops the low (57), so a plain slice leaves U+FFFD.
    const text = 'a'.repeat(56) + '🎉' + 'z'.repeat(5)
    const shown = displayedQuote(text)
    expect(shown).toBe('a'.repeat(56) + '…')
    expect(shown).not.toMatch(LONE_HIGH)
  })

  it('keeps an emoji whole when it ends exactly at the quote cap', () => {
    // '🎉' occupies units 55 and 56, ending at index 57 — the exact cut point,
    // so the whole emoji is kept.
    const text = 'a'.repeat(55) + '🎉' + 'z'.repeat(5)
    const shown = displayedQuote(text)
    expect(shown).toBe('a'.repeat(55) + '🎉' + '…')
    expect(shown).not.toMatch(LONE_HIGH)
  })

  it('leaves a short quote untouched', () => {
    expect(displayedQuote('short 🎉 quote')).toBe('short 🎉 quote')
  })
})
