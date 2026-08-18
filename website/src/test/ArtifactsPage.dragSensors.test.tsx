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
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const SRC = join(__dirname, '..', 'pages', 'ArtifactsPage.tsx')
const src = readFileSync(SRC, 'utf8')

describe('artifact library drag sensors', () => {
  it('splits mouse and touch sensors instead of using one PointerSensor', () => {
    expect(src).toMatch(/useSensor\(MouseSensor,\s*\{\s*activationConstraint:\s*\{\s*distance:\s*6\s*\}/)
    expect(src).toMatch(/useSensor\(TouchSensor,\s*\{\s*activationConstraint:\s*\{\s*delay:\s*250,\s*tolerance:\s*5\s*\}/)
  })

  it('does not construct a PointerSensor', () => {
    // Both the import binding and the call site have to go: leaving the import
    // behind is how a later edit quietly reinstates the swallowed-swipe sensor.
    // Asserted on derived values, not on `src`, so a failure prints the offending
    // line rather than the whole file. Prose may still NAME PointerSensor — the
    // comment at the call site explains why it is not used.
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

  it('leaves the cards free to pan — no touch-action lockout on a card', () => {
    // dnd-kit documents `touch-action: none` as PointerSensor's requirement.
    // Adding it here would re-break panning by the other mechanism, so the
    // cards must stay at the default `auto`.
    expect(src).not.toMatch(/touchAction:\s*'none'/)
    expect(src).not.toMatch(/\btouch-none\b/)
  })
})
