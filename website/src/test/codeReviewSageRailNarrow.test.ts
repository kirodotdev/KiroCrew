/**
 * Code Review Sage's rail must stop competing with the report on a phone.
 *
 * The rail is a resizable column with `MIN_RAIL_WIDTH = 280`, set through an
 * inline `style={{ width }}` on a `flex-shrink-0` box — so at a 390px viewport it
 * held 280-360px and left the report pane roughly 100px. `useColumnResize`
 * already supports a narrow-viewport strip, but it is OPT-IN via the collapse
 * config's `whenNarrow`, and this shell never passed one.
 *
 * Two halves, and the second is the load-bearing one. `ProjectsPage` states the
 * trap directly: "A page that only got the strip would hand the user an expand
 * button that leads straight back into the squeeze." So the strip alone is not
 * the fix — the EXPANDED state has to take the whole viewport, with the report
 * stepping aside, which is what `WebhooksPage` and `ProjectsPage` both do.
 *
 * Asserted over the source: the defect is which declaration each element carries
 * (an inline px width vs `100%`, `flex` vs `hidden`), and jsdom does no layout,
 * so a render could not measure the difference anyway.
 */
import { describe, it, expect } from 'vitest'

import {
  COLLAPSED_RAIL_WIDTH, MIN_RAIL_WIDTH, RAIL_COLLAPSED_KEY,
} from '../apps/code-review-sage/lib/layout'

async function shell(): Promise<string> {
  return (await import('../apps/code-review-sage/Workspace.tsx?raw')).default as string
}

describe('Code Review Sage rail at narrow widths', () => {
  it('opts into the narrow-viewport strip', async () => {
    const src = await shell()
    const cfg = src.match(/const RAIL_COLLAPSE: CollapseConfig = \{[\s\S]*?\}/)
    expect(cfg, 'expected a module-level RAIL_COLLAPSE config').not.toBeNull()
    // Without `whenNarrow` the hook's narrowMode stays false and the rail keeps
    // its px width on a phone — the config alone is not enough.
    expect(cfg![0]).toContain('whenNarrow: true')
    expect(cfg![0]).toContain('COLLAPSED_RAIL_WIDTH')
  })

  it('gives the whole viewport to the rail once expanded while narrow', async () => {
    const src = await shell()
    expect(src).toMatch(/mobileRailOpen = isMobile && !rail\.collapsed/)
    // The anti-squeeze invariant: expanded-while-narrow is 100%, never rail.width.
    // Nested inside the bar-mode branch, which releases the width entirely while
    // COLLAPSED — the two narrow states are disjoint, so this still pins the
    // expanded one and still fails if the 100% is dropped.
    expect(src).toMatch(/mobileRailOpen \? '100%' : rail\.width/)
  })

  it('steps the report aside instead of pushing it off-screen', async () => {
    const src = await shell()
    // A 100%-wide rail beside a flex-1 main would push the report out of view
    // rather than replace it, leaving nothing reachable by scrolling.
    expect(src).toMatch(/mobileRailOpen \? 'hidden' : 'flex'/)
    // And no main pane may keep an unconditional `flex` class.
    expect(src).not.toMatch(/className="flex-1 min-w-0 min-h-0 flex flex-col"/)
  })

  it('leaves the collapsed strip an expand control with an accessible name', async () => {
    const src = await shell()
    expect(src).toMatch(/rail\.collapsed \?/)
    expect(src).toMatch(/onClick=\{rail\.expand\}/)
    // Reuses the app-agnostic catalog key, so this adds no untranslated string.
    expect(src).toContain("i18nT('app.expand_sidebar')")
  })

  it('collapses the rail on select, so the expanded rail is not a one-way door', async () => {
    const src = await shell()
    // The third leg of the pattern. Without it, picking a review from the
    // full-width rail changes nothing visible: the rail keeps the viewport, the
    // report stays hidden, and the drag handle that would collapse it is gone on
    // touch — leaving a reload as the only escape. `whenNarrow` requires it, and
    // both cited shells (ProjectsPage, WebhooksPage) call rail.collapse().
    expect(src).toMatch(/if \(isMobile\) rail\.collapse\(\)/)
    // Keyed on the selection, not on every mobile render, or it would fight the
    // user's own expand and the strip could never be opened at all.
    expect(src).toMatch(/\}, \[selectedPr, activeRun, mainView, isMobile\]\)/)
  })

  it('keeps the drag handle off touch, where it costs width and does nothing', async () => {
    const src = await shell()
    expect(src).toMatch(/\{!isMobile && \(\s*<Splitter/)
  })

  it('has a strip narrower than the column minimum it replaces', async () => {
    // A "collapsed" width at or above the minimum would not free any room.
    expect(COLLAPSED_RAIL_WIDTH).toBeLessThan(MIN_RAIL_WIDTH)
    expect(RAIL_COLLAPSED_KEY).toMatch(/^kc:code-review-sage:/)
  })

  it('lays the collapsed strip ACROSS THE TOP while narrow', async () => {
    const src = await shell()
    // Even a 44px strip is a tenth of the reading column at 390px, and
    // horizontal is the axis a phone cannot spend. Above the report it costs
    // height, which a phone has.
    expect(src, 'expected a railBar derived from narrow AND collapsed')
      .toMatch(/const railBar = isMobile && rail\.collapsed/)
    expect(src, 'expected the shell to stack while the bar is up')
      .toMatch(/\$\{railBar \? 'flex-col' : ''\}/)
    // The strip's own axis has to turn with it: left border and a column of
    // controls make no sense once the element is a row across the top. And the
    // width must be released, or the bar keeps reserving the strip's column.
    expect(src, 'expected the strip to turn into a row').toMatch(/flex-row items-center border-b/)
    expect(src, 'expected the vertical form to survive on a desktop')
      .toMatch(/flex-col items-center border-r/)
    expect(src, 'expected the bar to release the strip width')
      .toMatch(/width: railBar \? undefined :/)
  })
})
