import { afterEach, describe, expect, it } from 'vitest'
import { render } from '@testing-library/react'

import { FirstRunConfetti } from '../components/FirstRunConfetti'

function setReducedMotion(reduce: boolean) {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    writable: true,
    value: (query: string) => ({
      matches: reduce && query.includes('prefers-reduced-motion'),
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }),
  })
}

const origMatchMedia = window.matchMedia

afterEach(() => {
  Object.defineProperty(window, 'matchMedia', { configurable: true, writable: true, value: origMatchMedia })
})

const pieces = (root: HTMLElement) => Array.from(root.querySelectorAll<HTMLElement>('[data-confetti-piece]'))

describe('FirstRunConfetti', () => {
  it('throws the same pieces, in theme colours, every time', () => {
    setReducedMotion(false)
    const a = render(<FirstRunConfetti />)
    const first = pieces(a.container).map(p => p.getAttribute('style'))
    a.unmount()
    const b = render(<FirstRunConfetti />)
    const second = pieces(b.container).map(p => p.getAttribute('style'))
    expect(first.length).toBeGreaterThan(0)
    expect(second).toEqual(first)
    for (const p of pieces(b.container)) expect(p.style.background).toMatch(/^var\(--/)
  })

  it('renders no confetti under prefers-reduced-motion', () => {
    setReducedMotion(true)
    const { container, getByTestId } = render(<FirstRunConfetti />)
    expect(getByTestId('harness-setup-confetti')).toBeInTheDocument()
    expect(pieces(container)).toHaveLength(0)
  })
})
