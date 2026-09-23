import { readFile } from 'node:fs/promises'
import { join } from 'node:path'
import { describe, it, expect } from 'vitest'

/* The corner arrows are the ONLY way back to an earlier question once the card
   pages, so on a phone they are the whole navigation. They shipped as `p-1`
   around a 14px glyph — a 22x22px target, under WCAG 2.2 SC 2.5.8's 24px floor
   and well under the 36px the rest of this app's icon buttons hold.

   Scanned from the source and converted to px rather than asserted in the DOM,
   for the same reason `narrowFirstBaseline` does it: jsdom runs no layout, so a
   rendered arrow measures 0x0 and any DOM assertion here would be a class-name
   string match dressed up as a size check. Pinned as a FLOOR: growing the target
   is fine, shrinking it back is the regression. */

const COMPONENT = join(__dirname, '..', 'components', 'QuestionCard.tsx')
const rem = (n: string) => Number(n) * 4

describe('QuestionCard pager arrow tap targets', () => {
  it('keeps both corner arrows at or above a 36px box', async () => {
    const src = await readFile(COMPONENT, 'utf8')

    const arrows = [...src.matchAll(
      /aria-label=\{i18nT\('components\.questionCard\.(previous|next)_question'\)\}\s*\n\s*className="([^"]+)"/g,
    )]
    expect(arrows, 'both pager arrows should be found by their aria-label').toHaveLength(2)

    for (const [, which, className] of arrows) {
      const h = className.match(/min-h-(\d+(?:\.\d+)?)/)
      const w = className.match(/min-w-(\d+(?:\.\d+)?)/)
      expect(h, `the ${which} arrow should size its box with min-h-*`).toBeTruthy()
      expect(w, `the ${which} arrow should size its box with min-w-*`).toBeTruthy()
      expect(
        rem(h![1]),
        `the ${which} arrow is ${rem(h![1])}px tall; 24px is WCAG 2.5.8's floor and 36px `
          + 'is what the rest of the chrome holds',
      ).toBeGreaterThanOrEqual(36)
      expect(rem(w![1]), `the ${which} arrow is ${rem(w![1])}px wide`).toBeGreaterThanOrEqual(36)
    }
  })

  it('grows the hit area without growing the glyph', async () => {
    // A 36px box is reached by padding, not by a bigger chevron: the pager sits in
    // the header beside 13px question text, and a 36px icon would dominate it.
    const src = await readFile(COMPONENT, 'utf8')
    const glyphs = [...src.matchAll(/<Chevron(?:Left|Right) size=\{?(\d+)\}?/g)].map(m => Number(m[1]))
    expect(glyphs.length, 'the pager and the footer jump both render a chevron').toBeGreaterThanOrEqual(2)
    for (const size of glyphs) expect(size).toBeLessThanOrEqual(16)
  })
})
