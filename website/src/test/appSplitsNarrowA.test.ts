/**
 * A builtin app whose desktop left/right split had no narrow-viewport branch at
 * all. Measured at a 390px viewport before this change:
 *
 *   meetings MeetingView   340px task sidebar  -> content   49px (or the row
 *                                                 overflowed: the content column
 *                                                 lacked `min-w-0`, so it refused
 *                                                 to shrink and pushed the meeting
 *                                                 off-screen instead)
 *
 * This is a body-text rule in every language -- the measure is how much width the
 * prose column gets, and script only changes the symptom. The pptx-maker splits of
 * the same series are pinned beside that app, in
 * `apps/pptx-maker/pptxMakerSplitsNarrow.test.ts`.
 *
 * meetings follows ITS OWN existing shape (`flex-col lg:flex-row`, already used by
 * MeetingWorkspace and TaskReviewView) rather than a new one; only MeetingView's
 * outer split lacked it.
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
