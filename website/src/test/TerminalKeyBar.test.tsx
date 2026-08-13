import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { Terminal } from '@xterm/xterm'
import TerminalKeyBar from '../components/TerminalKeyBar'
import { SOFT_KEYS } from '../utils/terminalKeys'

function makeTerm() {
  const textarea = document.createElement('textarea')
  document.body.appendChild(textarea)
  const seen: KeyboardEvent[] = []
  textarea.addEventListener('keydown', e => seen.push(e))
  return { term: { textarea } as unknown as Terminal, textarea, seen }
}

afterEach(cleanup)

describe('TerminalKeyBar', () => {
  it('renders one labelled button per soft key', () => {
    const { term } = makeTerm()
    render(<TerminalKeyBar term={term} />)
    for (const k of SOFT_KEYS) {
      expect(screen.getByRole('button', { name: k.aria })).toBeTruthy()
    }
  })

  it('sends the key to the terminal when tapped', async () => {
    const { term, seen } = makeTerm()
    render(<TerminalKeyBar term={term} />)
    await userEvent.click(screen.getByRole('button', { name: 'Tab' }))
    expect(seen.map(e => e.key)).toEqual(['Tab'])
  })

  /**
   * The four arrows are drawn as icons, not characters. U+2190..U+2193 are not
   * all present in the terminal font stack, so the browser fell back per glyph
   * and the arrows rendered at visibly different sizes next to each other.
   */
  it('draws the arrows as icons and the named keys as text', () => {
    const { term } = makeTerm()
    render(<TerminalKeyBar term={term} />)
    for (const name of ['Left arrow', 'Down arrow', 'Up arrow', 'Right arrow']) {
      const btn = screen.getByRole('button', { name })
      expect(btn.querySelector('svg')).toBeTruthy()
      expect(btn.textContent).toBe('')
    }
    for (const [name, text] of [['Escape', 'esc'], ['Tab', 'tab'], ['Control C', 'ctrl-c']]) {
      const btn = screen.getByRole('button', { name })
      expect(btn.querySelector('svg')).toBeNull()
      expect(btn.textContent).toBe(text)
    }
  })

  /**
   * All four arrow icons share one box size — that is the whole point. Only the
   * sizing classes are compared: lucide appends a per-icon class of its own
   * (`lucide-arrow-left`), so the full class strings legitimately differ.
   */
  it('sizes every arrow icon identically', () => {
    const { term } = makeTerm()
    render(<TerminalKeyBar term={term} />)
    const sizes = ['Left arrow', 'Down arrow', 'Up arrow', 'Right arrow'].map(name => {
      const cls = screen.getByRole('button', { name }).querySelector('svg')!.getAttribute('class') ?? ''
      return cls.split(/\s+/).filter(c => /^[hw]-/.test(c)).sort().join(' ')
    })
    expect(sizes).toEqual(['h-4 w-4', 'h-4 w-4', 'h-4 w-4', 'h-4 w-4'])
  })

  /**
   * The whole point of the bar is to be usable while typing. A tap that blurs
   * xterm's textarea dismisses the on-screen keyboard, so pressing Tab would
   * cost the user the keyboard they were using.
   */
  it('cancels pointerdown so the press never moves focus off the terminal', async () => {
    const { term, textarea } = makeTerm()
    render(<TerminalKeyBar term={term} />)
    const btn = screen.getByRole('button', { name: 'Tab' })
    const blur = vi.fn()
    textarea.addEventListener('blur', blur)
    textarea.focus()

    const ev = new PointerEvent('pointerdown', { bubbles: true, cancelable: true })
    btn.dispatchEvent(ev)

    expect(ev.defaultPrevented).toBe(true)
    expect(blur).not.toHaveBeenCalled()
  })
})
