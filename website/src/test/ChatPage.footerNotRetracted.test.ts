import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

/**
 * REGRESSION GUARD — a completed turn's footer survives a LATER, unrelated run.
 *
 * `showFooter` for the last message used to end at a bare `return !slotRunning`,
 * so any run starting in the slot — a cron, a monitor cycle, another tab, a
 * background job with nothing to do with this turn — retracted the footer of a
 * turn that had already finished.
 *
 * Measured frame by frame from a 60fps phone recording (1320x2868): the stats
 * line, the timestamp row and the overflow trigger vanished for 3 frames (50ms),
 * removing ~108px at the transcript's very bottom edge. Content shrinking THERE
 * is the one place it costs the reader their position: a bottom-parked reader is
 * clamped down by the engine, and when the footer returns nothing ever pushes
 * them back up. Each flicker ratcheted them 108px away from the bottom.
 *
 * A message carrying turn stats is proof its turn completed, so that is the
 * condition that makes the footer permanent. Pinned by source scan because the
 * predicate is an inline IIFE inside a 60-prop JSX element with no seam to call.
 */
const SRC = readFileSync(join(__dirname, '..', 'pages', 'ChatPage.tsx'), 'utf8')

describe('completed-turn footer', () => {
  it('is not retracted by a run that started later', () => {
    const i = SRC.indexOf('showFooter={(() => {')
    expect(i).toBeGreaterThan(-1)
    const pred = SRC.slice(i, SRC.indexOf('})()}', i))
    // Turn stats short-circuit BEFORE the running check, or a later run still wins.
    const statsAt = pred.indexOf('turn_stats')
    const runningAt = pred.indexOf('return !slotRunning')
    expect(statsAt).toBeGreaterThan(-1)
    expect(runningAt).toBeGreaterThan(-1)
    expect(statsAt).toBeLessThan(runningAt)
    expect(pred).toMatch(/elapsed_ms[^)]*\)\s*>\s*0\)\s*return true/)
  })

  it('still withholds the footer while the turn that owns it is running', () => {
    // The guard must not become "always show": a streaming turn has no stats yet,
    // and its footer would claim a measurement that does not exist.
    const i = SRC.indexOf('showFooter={(() => {')
    const pred = SRC.slice(i, SRC.indexOf('})()}', i))
    expect(pred).toMatch(/if \(isStreaming\) return false/)
    expect(pred).toContain('return !slotRunning')
  })
})
