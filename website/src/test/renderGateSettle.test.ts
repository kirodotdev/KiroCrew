import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { waitForLayoutStable } from '../../scripts/lib/settle.mjs'

// Deterministic fake clock: rAF callbacks fire only when we pump a frame, and `now`
// advances a fixed 16ms per frame. No real timers, so the test is instant and stable --
// mirrors the pure-unit style of src/test/renderVerdict.test.ts (no browser).
function makeClock() {
  let queue = []
  let t = 0
  return {
    now: () => t,
    raf: (cb) => { queue.push(cb) },
    frame: () => { const due = queue; queue = []; t += 16; due.forEach(cb => cb(t)) },
  }
}
const flush = () => Promise.resolve()

describe('render gate settles on GEOMETRY, not on a fixed timeout (#1555)', () => {
  it('stays pending while the measured width changes; resolves only once it holds still', async () => {
    const clock = makeClock()
    // width 0 -> 180 over several frames (a spring), then holds.
    const widths = [0, 40, 95, 140, 168, 179, 180, 180, 180, 180, 180]
    let i = 0
    const sample = () => widths[Math.min(i++, widths.length - 1)]
    let done = false
    const p = waitForLayoutStable(sample, { framesStable: 3, maxFrames: 100, raf: clock.raf, now: clock.now })
      .then(() => { done = true })
    for (let f = 0; f < 6; f++) { clock.frame(); await flush() } // through the moving region
    expect(done).toBe(false)                                      // must NOT sample mid-animation
    for (let f = 0; f < 6; f++) { clock.frame(); await flush() } // through the stable tail
    await p
    expect(done).toBe(true)
  })

  it('never hangs: resolves via the frame cap even if the width never stabilises', async () => {
    const clock = makeClock()
    let w = 0
    const p = waitForLayoutStable(() => (w += 1), { framesStable: 3, maxFrames: 20, raf: clock.raf, now: clock.now })
    for (let f = 0; f < 25; f++) { clock.frame(); await flush() }
    await expect(p).resolves.toMatchObject({ reason: 'cap' })     // capped, no CI deadlock
  })

  it('honours the async-mount floor: does not resolve before minMs even when already stable', async () => {
    const clock = makeClock()
    // Geometry is stable from frame 1, but minMs (200ms) must elapse first, so a page that
    // is static BEFORE its animation starts is not sampled in its not-yet-animated state.
    const p = waitForLayoutStable(() => 42, { framesStable: 3, minMs: 200, maxFrames: 100, raf: clock.raf, now: clock.now })
    let done = false
    p.then(() => { done = true })
    for (let f = 0; f < 5; f++) { clock.frame(); await flush() }  // t=80ms: stable but under the floor
    expect(done).toBe(false)
    for (let f = 0; f < 10; f++) { clock.frame(); await flush() } // past 200ms
    await p
    expect(done).toBe(true)
  })

  it('the gate wires the geometry-settle in and no longer measures right after a fixed timeout', () => {
    const gate = readFileSync(
      resolve(import.meta.dirname, '../../scripts/check-i18n-render.mjs'), 'utf-8')
    expect(gate).toMatch(/waitForLayoutStable/)                   // the stable wait is used
    expect(gate).toMatch(/geometrySignature/)                     // fed the geometry signature
    expect(gate).not.toMatch(/await page\.waitForTimeout\(surface\.settle/) // the #1555 flaky line is gone
  })
})
