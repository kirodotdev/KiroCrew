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
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const SRC = join(__dirname, '..', 'pages', 'ChatSidebar.tsx')
const src = readFileSync(SRC, 'utf8')

describe('chat sidebar drag sensors', () => {
  it('splits mouse and touch sensors instead of using one PointerSensor', () => {
    expect(src).toMatch(/useSensor\(MouseSensor,\s*\{\s*activationConstraint:\s*\{\s*distance:\s*5\s*\}/)
    expect(src).toMatch(/useSensor\(TouchSensor,\s*\{\s*activationConstraint:\s*\{\s*delay:\s*250,\s*tolerance:\s*5\s*\}/)
  })

  it('keeps the keyboard sensor for accessible reordering', () => {
    expect(src).toMatch(/useSensor\(KeyboardSensor,\s*\{\s*coordinateGetter:\s*sortableKeyboardCoordinates\s*\}/)
  })

  it('does not construct a PointerSensor', () => {
    // Both the import binding and the call site have to go: leaving the import
    // behind is how a later edit quietly reinstates the swallowed-swipe sensor.
    // Asserted on derived values, not on `src`, so a failure prints the
    // offending line rather than the whole file. Prose may still NAME
    // PointerSensor — the comment at the call site explains why it is not used.
    const importLine = src.split('\n').find(l => l.includes("from '@dnd-kit/core'")) ?? ''
    expect(importLine).not.toContain('PointerSensor')
    expect(src.match(/useSensor\(\s*PointerSensor/g)).toBeNull()
  })

  it('gives the touch sensor a delay constraint, never a bare distance', () => {
    // A distance-only touch constraint is the defect: `handleMove` ACTIVATES on
    // distance (and then preventDefaults every move), where a delay constraint
    // CANCELS on distance and lets the browser pan.
    const touch = src.match(/useSensor\(TouchSensor,[^)]*\)/)?.[0] ?? ''
    expect(touch).toContain('delay:')
    expect(touch).not.toMatch(/\bdistance:/)
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
