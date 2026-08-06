/**
 * SegmentedControl must not animate a CLIPPED width when the reader asked for
 * reduced motion.
 *
 * The label is `overflow-hidden whitespace-nowrap` and animates `width` from `0`
 * to `auto`, so for the length of the spring the text is genuinely cut off. That
 * makes the animation more than a preference violation: anything that measures
 * layout during the window reads the label as truncated when it is not. The
 * repository's i18n render gate launches Chromium with
 * `reducedMotion: 'reduce'` for exactly this reason and allows the surface only
 * ~250ms to settle, so a slower host samples mid-flight and reports truncation on
 * a page nobody changed.
 *
 * framer-motion does not consult `prefers-reduced-motion` by itself — the other
 * components in this repo that respect it all call `useReducedMotion()`
 * explicitly — so the preference has to be honoured here.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'

const SEGMENTS = [
  { key: 'grid' as const, label: 'Gallery' },
  { key: 'table' as const, label: 'Table' },
]

/** Stub `matchMedia`, which jsdom does not implement, for one preference value. */
function setReducedMotion(reduce: boolean): void {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: reduce && query.includes('prefers-reduced-motion'),
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }))
}

/**
 * Load the component AFTER the preference is stubbed. framer-motion resolves
 * `prefers-reduced-motion` once into module state that a test-local reset does not
 * clear, so the opposite case lives in its own file (one module registry per test
 * file) rather than here.
 */
async function mountWith(reduce: boolean) {
  setReducedMotion(reduce)
  const { default: SegmentedControl } = await import('../components/SegmentedControl')
  render(<SegmentedControl segments={SEGMENTS} value="grid" onChange={() => {}} collapse={false} />)
  return screen.getByText('Gallery')
}

describe('SegmentedControl — reduced motion honoured', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('mounts the label at its natural width when motion is reduced', async () => {
    const label = await mountWith(true)
    // `initial={false}` means no starting width is written, so the label is never
    // laid out at 0 and never reads as clipped.
    expect(label.style.width).not.toBe('0px')
  })
})
