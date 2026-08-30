/**
 * A finger swipe starting on a session or folder row pans the sidebar list;
 * only a deliberate press-and-hold picks the row up.
 *
 * The sidebar's session/folder drag used a single `PointerSensor` with a 5px
 * activation distance. Past that distance dnd-kit's `AbstractPointerSensor`
 * calls `preventDefault()` on every subsequent move event, and dnd-kit installs
 * a non-passive window `touchmove` listener specifically so those calls take
 * effect — its own source says "This is required for iOS Safari"
 * (`TouchSensor.setup`). So on WebKit a swipe that began on a row was swallowed
 * by the sensor, while the same swipe beginning in a GAP between rows — no
 * listener there, so no sensor — panned normally. Chromium ignores
 * `preventDefault()` on `pointermove` for panning, which is why the asymmetry
 * does not reproduce there and cannot be measured with a headless Chromium
 * probe.
 *
 * The split sensors remove the contention instead of tuning it:
 *  - MouseSensor keeps the 5px distance, so mouse drag is unchanged.
 *  - TouchSensor's DELAY constraint means `handleMove` CANCELS the sensor as
 *    soon as the finger travels past the tolerance, handing the gesture back to
 *    the browser; only a stationary hold arms a drag.
 *
 * Same split as the Apps nav rail (App.tsx). These are wiring assertions read
 * from source. jsdom has no compositor, so it cannot demonstrate a pan being
 * swallowed — the mechanism above is what the assertions pin, at the one place
 * where it is decided.
 *
 * That place is now `hooks/useDndSensors.ts`, which owns the split for all
 * three drag surfaces, and `useDndSensors.test.tsx` pins the mechanism itself
 * (separate sensors, delay-not-distance on touch, no PointerSensor anywhere in
 * the tree). What remains this surface's own is which distance it asks for,
 * that it still asks for the keyboard sensor, and where touch-action may be
 * locked; "no second sensor set anywhere" is a tree-wide property and is
 * scanned there, for every file at once.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const SRC = join(__dirname, '..', 'pages', 'ChatSidebar.tsx')
const src = readFileSync(SRC, 'utf8')

describe('chat sidebar drag sensors', () => {
  it('takes its sensors from the shared hook, with this surface 5px distance', () => {
    // 5px is the list's own call: rows are tightly packed and a click only
    // selects, so the threshold can sit lower than the Apps nav rail's 8px.
    expect(src).toMatch(/useDndSensors\(\{\s*distance:\s*5,\s*keyboard:\s*true\s*\}\)/)
    expect(src).toMatch(/from '\.\.\/hooks\/useDndSensors'/)
  })

  it('keeps the keyboard sensor for accessible reordering', () => {
    // This IS a sortable ring, so the sortable coordinate getter has somewhere
    // to move; the hook supplies the getter itself.
    expect(src).toMatch(/useDndSensors\([^)]*keyboard:\s*true/)
  })

  it('locks touch-action only on resize separators, never on drag rows', () => {
    // dnd-kit documents `touch-action: none` as PointerSensor's requirement.
    // The sidebar legitimately locks touch on its two RESIZE separators (the
    // width handle and the history-pane splitter — custom usePointerDrag, not
    // dnd-kit; a resize handle must own the touch). A lockout on a DRAG ROW
    // would re-break panning by the other mechanism, so every occurrence must
    // sit on a role="separator" element.
    expect(src).not.toMatch(/\btouch-none\b/)
    const occurrences = [...src.matchAll(/touchAction:\s*'none'/g)]
    expect(occurrences.length).toBeGreaterThan(0)
    for (const m of occurrences) {
      const context = src.slice(Math.max(0, (m.index ?? 0) - 600), m.index ?? 0)
      expect(context).toContain('role="separator"')
    }
  })
})
