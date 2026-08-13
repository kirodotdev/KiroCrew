/**
 * Settings -> Releases is a desktop left/right split: a `w-48` version list beside
 * the release notes. Nothing overflowed -- the list simply took 192px of a 390px
 * viewport and the notes, which are the reason the panel exists, got what was
 * left. Measured before this change: 146px for the notes column, with 192 showing
 * up in the probe's fixed-sibling list.
 *
 * The rule: a desktop left/right split must hand the information pane the FULL
 * width on a phone. This is a body-text rule in every language -- the measure is
 * how much width the prose column gets, and script only changes the symptom (Latin
 * overflows and clips, per-character-breaking scripts collapse into a ribbon and
 * report no overflow at all, which is why `scrollWidth` cannot be the test).
 *
 * Asserted over source: jsdom performs no layout, so a render could not measure
 * the width this is about.
 */
import { describe, it, expect } from 'vitest'

async function src(): Promise<string> {
  return (await import('../pages/settings/ReleasesPanel.tsx?raw')).default as string
}

describe('Releases panel at narrow widths', () => {
  it('stacks the version list above the notes while narrow', async () => {
    const s = await src()
    // Direction flip on the row: without it the list stays a column beside the
    // notes no matter how narrow the pane gets.
    expect(s, 'expected the split row to stack while narrow')
      .toMatch(/flex flex-col sm:flex-row min-h-0 flex-1 gap-5/)
  })

  it('releases the list column width while narrow', async () => {
    const s = await src()
    // `w-48` is what took the 192px. Stacking alone is not enough: a 192px-wide
    // list stacked above the notes would leave a short column and dead space.
    expect(s, 'the list must take the full width while narrow')
      .toMatch(/w-full sm:w-48/)
    expect(s, 'no ungated w-48 on the list').not.toMatch(/className="w-48\b/)
  })

  it('moves the divider to the axis the columns now sit on', async () => {
    const s = await src()
    // A right border between two stacked blocks draws a line down the side of the
    // list instead of between the two; the divider has to turn with the layout.
    expect(s, 'expected a bottom border while narrow and a right border on desktop')
      .toMatch(/border-b sm:border-b-0 sm:border-r border-border/)
  })

  it('caps the list height and keeps it scrolling at every width', async () => {
    const s = await src()
    // Stacking alone traded one defect for another: the list is `shrink-0` and a
    // changelog grows without limit, so at its natural height it pushed the notes
    // heading below the fold -- a too-narrow notes column became an off-screen one.
    expect(s, 'the list needs a height cap when stacked')
      .toContain('max-h-[40vh] sm:max-h-none')
    // A PERCENTAGE cap is inert here: no ancestor has a definite height, so it
    // never resolves. Measured on the live page -- a 10% cap left the list at its
    // full 289px, while 10vh brought it to 84px and made it scroll.
    expect(s, 'the cap must use a definite unit').not.toContain('max-h-[38%]')
    // Scroll containment must NOT be gated behind `sm:`, or the capped list has no
    // way to reach its own overflowing rows.
    expect(s, 'the list must scroll at every width').toMatch(/max-h-none overflow-y-auto/)
    expect(s, 'no sm:-gated scroll containment').not.toMatch(/\bsm:overflow-y-auto/)
  })
})
