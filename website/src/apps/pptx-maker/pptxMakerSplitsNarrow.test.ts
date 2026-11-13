/**
 * PPTX Maker's two desktop left/right splits stack at a narrow viewport.
 *
 * Measured at 390px with no narrow-viewport branch, the 240px deck list left the
 * deck viewer 86px (44px once the surrounding Card padding counts), and the 224px
 * template list left the library detail too little to read. Nothing overflowed,
 * which is why an overflow-based check reports them as fine: the list simply took
 * its fixed width and the content took what was left. This is a body-text rule in
 * every language -- the measure is how much width the prose column gets, and script
 * only changes the symptom. The app has no precedent of its own, so it takes the
 * stacked form the rest of the dashboard's narrow-split series uses.
 *
 * Asserted over source: jsdom performs no layout, so a render cannot measure the
 * widths this is about.
 */
import { describe, it, expect } from 'vitest'

import librarySource from './LibraryPanel.tsx?raw'
import pageSource from './PptxMakerPage.tsx?raw'

describe('pptx-maker splits at narrow widths', () => {
  const cases: [string, string, string][] = [
    ['decks', pageSource, 'sm:w-60'],
    ['library', librarySource, 'sm:w-56'],
  ]

  it('stacks both splits and releases both list widths', () => {
    for (const [name, s, gated] of cases) {
      expect(s, `${name}: expected the split to stack while narrow`)
        .toMatch(/className="flex flex-col sm:flex-row gap-4 flex-1 min-h-0"/)
      expect(s, `${name}: the list must take the full width while narrow`)
        .toMatch(new RegExp(`w-full ${gated}`))
      // Bounded when stacked: the list is `shrink-0`, so its natural height would
      // otherwise push the content pane out of the column.
      // A viewport unit, not a percentage: these splits sit inside a Card inside a
      // page scroller, and no ancestor has a definite height, so a percentage
      // max-height never resolves -- measured on the Releases panel, where a 10%
      // cap left a 289px list untouched while 10vh brought it to 84px.
      expect(s, `${name}: the list needs a height bound when stacked`)
        .toContain('max-h-[40vh] sm:max-h-none')
      expect(s, `${name}: the bound must use a definite unit`).not.toContain('max-h-[38%]')
      // Divider on the axis the columns now sit on.
      expect(s, `${name}: the divider must turn with the layout`)
        .toMatch(/border-b sm:border-b-0 sm:border-r border-border/)
    }
  })

  it('leaves no ungated fixed list width behind', () => {
    for (const [name, s, gated] of cases) {
      const bare = gated.replace('sm:', '')
      expect(s, `${name}: no ungated ${bare}`).not.toMatch(new RegExp(`className="${bare}\\b`))
    }
  })
})
