/**
 * DrivePage lays out ALL of its card grids with one mechanism: the CSS
 * `repeat(auto-fill, minmax(258px, 1fr))` template. The browser derives the
 * column count natively from the container width, so no grid carries a
 * ResizeObserver subscription, a JavaScript measurement, or a resize
 * re-render for it.
 *
 * `useColumnCount` stays a legitimate hook: `VirtuosoMasonry` in ArtifactsPage
 * takes a column COUNT as a value and cannot lay itself out from CSS. What this
 * file pins is only that DrivePage — whose grids are plain CSS grid — never
 * reaches for the measured form again.
 *
 * These are source-level assertions, in the same family as
 * ArtifactsPage.narrowSingleAxis.test.tsx's pins, because jsdom does not lay
 * out CSS grid: `clientWidth` is 0 and no track resolution happens, so a
 * rendered-column assertion would pass vacuously under either mechanism.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const src = () => readFileSync(join(__dirname, '..', 'apps', 'aws-control', 'DrivePage.tsx'), 'utf8')

/** The exact template the root grid uses; the other three must match it
 *  byte-for-byte (same 258px min, same 1fr max) so all four resolve
 *  identically at every width. */
const AUTO_FILL = "gridTemplateColumns: 'repeat(auto-fill, minmax(258px, 1fr))'"

describe('DrivePage grid layout mechanism', () => {
  it('derives no grid column count in JavaScript', () => {
    const text = src()
    expect(text).not.toMatch(/useColumnCount\(/)
    // The import must go with the call sites: an unused import is a lint
    // failure, and a kept one invites the measured form back.
    expect(text).not.toMatch(/hooks\/useColumnCount/)
  })

  it('lays out every card grid with the shared auto-fill template', () => {
    const text = src()
    // Library grid, Add-from-Artifacts picker, Files grid: three grids today,
    // one template. (The Drive-root folder cards were the fourth until the
    // flat-rail IA dissolved the root page.) A floor rather than an exact
    // count, so adding another grid on the same template is not a failure.
    expect(text.split(AUTO_FILL).length - 1).toBeGreaterThanOrEqual(3)
    // And none is left on (or returned to) the measured column-count form.
    expect(text).not.toMatch(/repeat\(\$\{[^}]+\},\s*minmax\(0,\s*1fr\)\)/)
  })
})
