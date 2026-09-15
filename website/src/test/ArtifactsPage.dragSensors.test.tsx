/**
 * A finger swipe starting on an artifact card pans the gallery; only a
 * deliberate press-and-hold picks the card up.
 *
 * The library's drag-to-folder used a single `PointerSensor` with a 6px
 * activation distance. Past that distance dnd-kit's `AbstractPointerSensor`
 * calls `preventDefault()` on every subsequent move event, and dnd-kit installs
 * a non-passive window `touchmove` listener specifically so those calls take
 * effect — its own source says "This is required for iOS Safari"
 * (`TouchSensor.setup`). So on WebKit a swipe that began on a card was
 * swallowed by the sensor, while the same swipe beginning in the GAP between
 * cards — no listener there, so no sensor — panned normally. Chromium ignores
 * `preventDefault()` on `pointermove` for panning, which is why the asymmetry
 * does not reproduce there and cannot be measured with a headless Chromium
 * probe.
 *
 * The split sensors remove the contention instead of tuning it:
 *  - MouseSensor keeps the 6px distance, so mouse drag is unchanged.
 *  - TouchSensor's DELAY constraint means `handleMove` CANCELS the sensor as
 *    soon as the finger travels past the tolerance, handing the gesture back to
 *    the browser; only a stationary hold arms a drag.
 *
 * These are wiring assertions read from source. jsdom has no compositor, so it
 * cannot demonstrate a pan being swallowed — the mechanism above is what the
 * assertions pin, at the one place where it is decided.
 *
 * That place is now `hooks/useDndSensors.ts`, which owns the split for all
 * three drag surfaces, and `useDndSensors.test.tsx` pins the mechanism itself
 * (separate sensors, delay-not-distance on touch, no PointerSensor anywhere in
 * the tree). What remains this surface's own is which distance it asks for and
 * that nothing here locks touch-action; "no second sensor set anywhere" is a
 * tree-wide property and is scanned there, for every file at once.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const SRC = join(__dirname, '..', 'pages', 'ArtifactsPage.tsx')
const src = readFileSync(SRC, 'utf8')

describe('artifact library drag sensors', () => {
  it('takes its sensors from the shared hook, with this surface 6px distance', () => {
    // 6px is the surface's own call: a plain click opens the card, so the
    // threshold has to clear a click without demanding a long drag.
    expect(src).toMatch(/useDndSensors\(\{\s*distance:\s*6\s*\}\)/)
    expect(src).toMatch(/from '\.\.\/hooks\/useDndSensors'/)
  })

  it('asks for no keyboard sensor - this DndContext files, it does not sort', () => {
    // The sortable coordinate getter walks a sortable ring; there is none here
    // (this page does not import @dnd-kit/sortable at all), so requesting it
    // would hand the keyboard a getter with nothing to move between.
    expect(src).not.toMatch(/useDndSensors\([^)]*keyboard/)
    expect(src).not.toContain("@dnd-kit/sortable")
  })

  it('leaves the cards free to pan — no touch-action lockout on a card', () => {
    // dnd-kit documents `touch-action: none` as PointerSensor's requirement.
    // Adding it here would re-break panning by the other mechanism, so the
    // cards must stay at the default `auto`.
    expect(src).not.toMatch(/touchAction:\s*'none'/)
    expect(src).not.toMatch(/\btouch-none\b/)
  })
})
