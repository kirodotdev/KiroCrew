/**
 * The chat hero must not eat the first screen on a phone.
 *
 * `text-5xl` (48px) is a desktop size, and the heading sits between a 64px brand
 * mark and a 64px optical-centering spacer in a `gap-4` row inside `px-8`. At a
 * 320px viewport that leaves the heading 189px, which measured 5 lines in
 * English and 6 in German and French — 260-325px of hero. Dropping to 30px and
 * releasing the decorative spacer below `sm` holds it to 2 lines in every locale
 * measured (82px).
 *
 * happy-dom does no layout, so these pin the declaration: the defect is that the
 * size and the spacer were unconditional, and that is what a source assertion
 * can see.
 */
import { describe, it, expect } from 'vitest'

async function source(): Promise<string> {
  return (await import('../components/WelcomeView.tsx?raw')).default as string
}

describe('WelcomeView hero at narrow widths', () => {
  it('scales the heading down on base and up from sm', async () => {
    const src = await source()
    const h2 = src.match(/<h2 className="[^"]*"/)
    expect(h2, 'expected an h2 with a className').not.toBeNull()
    expect(h2![0]).toContain('text-3xl')
    expect(h2![0]).toContain('sm:text-5xl')
    // An unqualified text-5xl is the defect: it applies at every width.
    expect(h2![0]).not.toMatch(/(^|\s)text-5xl/)
  })

  it('does not spend 64px of a phone on the centering spacer', async () => {
    const src = await source()
    const spacer = src.match(/className="[^"]*w-\[64px\][^"]*"/)
    expect(spacer, 'expected the optical-centering spacer').not.toBeNull()
    expect(spacer![0]).toContain('hidden')
    expect(spacer![0]).toContain('sm:block')
  })

  it('keeps the brand mark, which is content rather than padding', async () => {
    const src = await source()
    // The mark carries the product identity; only the blank counterweight is
    // dropped. Guards against "fix" by deleting both.
    expect(src).toMatch(/brandMark/)
    expect(src).toMatch(/size=\{64\}|w-16 h-16/)
  })
})
