/**
 * The chat hero must not eat the first screen on a phone.
 *
 * A desktop-size heading (`text-5xl`, 48px) squeezed into a narrow row measured
 * 5-6 lines at a 320px viewport in English, German and French. The heading now
 * sits BELOW the brand mark in a column, so it gets the full row width, and it
 * starts at 30px and only grows from `sm`. No 64px counterweight spacer beside
 * the heading exists any more, so none can take a phone's width.
 *
 * happy-dom does no layout, so these pin the declaration: the defect is an
 * unconditional large size or a fixed-width spacer in the heading row, and that
 * is what a source assertion can see.
 */
import { describe, it, expect } from 'vitest'

async function source(): Promise<string> {
  return (await import('../components/WelcomeView.tsx?raw')).default as string
}

describe('WelcomeView hero at narrow widths', () => {
  it('scales every heading down on base and up from sm', async () => {
    const src = await source()
    const headings = src.match(/<h2 className="[^"]*"/g)
    expect(headings, 'expected an h2 with a className').not.toBeNull()
    for (const h2 of headings!) {
      expect(h2).toContain('text-3xl')
      expect(h2).toMatch(/sm:text-[45]xl/)
      // An unqualified larger size is the defect: it applies at every width.
      expect(h2).not.toMatch(/(^|\s|")text-[45]xl/)
    }
  })

  it('does not spend 64px of a phone on a centering spacer', async () => {
    const src = await source()
    expect(src).not.toMatch(/className="[^"]*w-\[64px\][^"]*"/)
  })

  it('keeps the brand mark, which is content rather than padding', async () => {
    const src = await source()
    // The mark carries the product identity; only the blank counterweight is
    // dropped. Guards against "fix" by deleting both.
    expect(src).toMatch(/brandMark/)
    expect(src).toMatch(/size=\{64\}|w-16 h-16/)
  })
})
