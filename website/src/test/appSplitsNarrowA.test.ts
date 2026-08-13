/**
 * Two builtin apps whose desktop left/right splits had no narrow-viewport branch
 * at all. Measured at a 390px viewport before this change:
 *
 *   meetings MeetingView   340px task sidebar  -> content   49px (or the row
 *                                                 overflowed: the content column
 *                                                 lacked `min-w-0`, so it refused
 *                                                 to shrink and pushed the meeting
 *                                                 off-screen instead)
 *   pptx-maker decks       240px deck list     -> viewer    86px, 44px once the
 *                                                 surrounding Card padding counts
 *   pptx-maker library     224px template list -> detail    too little to read
 *
 * Nothing overflowed in the pptx-maker cases, which is why an overflow-based check
 * reports them as fine: the list simply took its fixed width and the content took
 * what was left. This is a body-text rule in every language -- the measure is how
 * much width the prose column gets, and script only changes the symptom.
 *
 * meetings follows ITS OWN existing shape (`flex-col lg:flex-row`, already used by
 * MeetingWorkspace and TaskReviewView) rather than a new one; only MeetingView's
 * outer split lacked it. pptx-maker has no precedent of its own, so it takes the
 * same stacked form as the rest of this series.
 *
 * Asserted over source: jsdom performs no layout, so a render cannot measure the
 * widths this is about.
 */
import { describe, it, expect } from 'vitest'

const read = async (p: string): Promise<string> =>
  (await import(`../apps/${p}?raw`)).default as string

describe('meetings MeetingView at narrow widths', () => {
  it('stacks the split, using the shape this app already uses twice', async () => {
    const s = await read('meetings/MeetingView.tsx')
    expect(s, 'expected the outer split to stack below lg')
      .toMatch(/className="flex flex-col lg:flex-row h-full overflow-hidden"/)
    // The app's own convention is the `lg` breakpoint, not `sm` -- matching it
    // keeps one app from having two answers to the same question.
    const workspace = await read('meetings/components/MeetingWorkspace.tsx')
    expect(workspace, 'the precedent this copies must still exist').toMatch(/flex flex-col lg:flex-row/)
  })

  it('lets the content column actually shrink', async () => {
    const s = await read('meetings/MeetingView.tsx')
    // Without `min-w-0` a flex child refuses to go below its content width, so the
    // row overflowed sideways instead of resolving to 49px -- a different symptom
    // with the same cause, and the reason stacking alone was not the whole fix.
    expect(s, 'the content column needs min-w-0/min-h-0')
      .toMatch(/className="flex-1 min-w-0 min-h-0 flex flex-col overflow-hidden"/)
  })

  it('gives the task sidebar the full width when stacked, with a bound', async () => {
    const s = await read('meetings/components/TaskSidebar.tsx')
    expect(s, 'sidebar must release its 340px while narrow').toMatch(/w-full[^"]*lg:w-\[340px\]/)
    // Bounded height when stacked, or a `flex-none` sidebar at natural height
    // would push the meeting out of view -- the same shape the transcript pane
    // uses in MeetingWorkspace.
    expect(s, 'sidebar needs a bounded height when stacked').toMatch(/h-\[42%\][^"]*min-h-\[260px\]/)
    // The divider turns with the layout: a left border between two stacked blocks
    // draws a line down the side of the sidebar, not between the two.
    expect(s).toMatch(/border-t border-border[^"]*lg:border-t-0 lg:border-l/)
    expect(s, 'no ungated 340px width').not.toMatch(/className="flex-none w-\[340px\]/)
  })
})

describe('pptx-maker splits at narrow widths', () => {
  const cases: [string, string, string][] = [
    ['decks', 'pptx-maker/PptxMakerPage.tsx', 'sm:w-60'],
    ['library', 'pptx-maker/LibraryPanel.tsx', 'sm:w-56'],
  ]

  it('stacks both splits and releases both list widths', async () => {
    for (const [name, path, gated] of cases) {
      const s = await read(path)
      expect(s, `${name}: expected the split to stack while narrow`)
        .toMatch(/className="flex flex-col sm:flex-row gap-4 flex-1 min-h-0"/)
      expect(s, `${name}: the list must take the full width while narrow`)
        .toMatch(new RegExp(`w-full ${gated.replace(':', ':')}`))
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

  it('leaves no ungated fixed list width behind', async () => {
    for (const [name, path, gated] of cases) {
      const s = await read(path)
      const bare = gated.replace('sm:', '')
      expect(s, `${name}: no ungated ${bare}`).not.toMatch(new RegExp(`className="${bare}\\b`))
    }
  })
})
