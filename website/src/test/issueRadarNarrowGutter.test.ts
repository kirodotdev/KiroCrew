/**
 * Issue Radar's own page gutters, narrow-first.
 *
 * Every full-width surface in this app ran an unconditional desktop gutter while
 * the issue and PR list columns beside them had run 8px at every width all along,
 * so the app disagreed with itself about how far content sits from a phone's edge.
 * Measured on a pod with the real repo cache, at 390px / 320px, as the width of
 * the longest body-text line:
 *
 *   Overview   282 -> 314  /  212 -> 244
 *   Tagging    308 -> 340  /  238 -> 270
 *   Detail     342 -> 374  /  272 -> 304
 *   Settings   326 -> 374  /  256 -> 304   (its gutter was 32px, not 24px)
 *
 * Asserted over source: happy-dom performs no layout, so a render cannot measure
 * the width this is about. The value itself is a recommendation, not a gate — what
 * these tests pin is the SHAPE (a narrow baseline with an `md:` desktop half) and
 * the two places where several boxes must carry the SAME gutter to stay aligned.
 */
import { describe, it, expect } from 'vitest'

const read = async (p: string): Promise<string> =>
  (await import(`../apps/issue-radar/${p}?raw`)).default as string

/** Surfaces converted to a narrow-first gutter, with the EXACT paired spelling each
 * carries. Pairing is asserted as one token rather than as "has px-2 and has md:px-N"
 * because chips and buttons in these same files legitimately use a bare `px-2` for
 * their own touch padding — a file-wide check for an unpaired `px-2` reports those
 * as gutter misses (it did, on the first run of this test). */
const CONVERTED: [string, string][] = [
  ['views/OverviewView.tsx', 'px-2 md:px-6'],
  ['views/TaggingView.tsx', 'px-2 md:px-6'],
  ['views/CrewPageView.tsx', 'px-2 md:px-6'],
  ['components/DetailHeader.tsx', 'px-2 md:px-6'],
  ['components/IssueDetail.tsx', 'px-2 md:px-6'],
  ['components/PrDetail.tsx', 'px-2 md:px-6'],
  ['views/settings/GeneralSettings.tsx', 'px-2 py-8 md:px-8'],
  ['views/settings/RepoSettings.tsx', 'px-2 py-8 md:px-8'],
]

describe('issue-radar page gutters are narrow-first', () => {
  it('leaves no unconditional desktop gutter on a converted surface', async () => {
    for (const [file] of CONVERTED) {
      const s = await read(file)
      // A bare `px-6`/`px-8` in one of these files means a gutter was half-swept:
      // the surface would then hold two different left edges at the same width,
      // which is the defect, not a smaller version of it. Two earlier sweeps in
      // this repo each missed sites by grepping ONE spelling, so this is checked
      // per file rather than trusted to a hand-run search.
      expect(s, `${file} still pins a gutter to a single width`)
        .not.toMatch(/className=(?:"|\{`)[^"`]*(?<![-:])px-[68]\b/)
    }
  })

  it('keeps the desktop half paired with the narrow one', async () => {
    for (const [file, paired] of CONVERTED) {
      const s = await read(file)
      // Settings deliberately stays at 32px while the pages stay at 24px: that
      // difference predates this change, and folding it in here would be a
      // desktop redesign smuggled into a narrow-viewport fix. Asserting the exact
      // paired token is also what proves the desktop rendering is untouched —
      // drop the `md:` half and the desktop value silently becomes the phone one.
      expect(s, `${file} must carry its narrow gutter paired with its desktop half`)
        .toContain(paired)
    }
  })

  it('gives all three parts of the detail header ONE gutter', async () => {
    // The sticky back row, the tall title block and the toolbar are three separate
    // boxes stacked into one visual header. Give them different insets and the
    // title stops sharing a left edge with the state pill under it — nothing
    // overflows, so only an assertion catches it.
    const s = await read('components/DetailHeader.tsx')
    const gutters = s.match(/px-2 md:px-6/g) ?? []
    expect(gutters, 'expected back row + title block + toolbar to share one gutter')
      .toHaveLength(3)
  })

  it('gives a crew page the same gutter while loading, on error, and loaded', async () => {
    // A gutter that changed as the read landed would shift the content sideways
    // on arrival.
    const s = await read('views/CrewPageView.tsx')
    expect(s.match(/px-2 md:px-6/g) ?? [], 'loading, error and loaded must agree')
      .toHaveLength(3)
  })

  it('leaves the centered placeholders alone', async () => {
    // These two are NOT page gutters: the text is centered and `px-6` is its only
    // inset, so flushing it would push centered copy against the screen edge for
    // no width gain. Named here so a future sweep does not read them as misses.
    for (const f of ['components/ListEmptyState.tsx', 'Workspace.tsx']) {
      const s = await read(f)
      expect(s, `${f} is a centered placeholder and keeps its own inset`)
        .toMatch(/items-center justify-center[^"]*px-6|px-6 text-center/)
    }
  })
})
