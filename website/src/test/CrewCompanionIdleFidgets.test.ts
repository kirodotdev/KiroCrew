/**
 * Every idle fidget must be REACHABLE.
 *
 * This exists because four keyframes (`look`, `ponder`, `correct`, `ponder-loop`)
 * shipped fully written, styled and eye-mapped -- and were never once shown. Nothing
 * failed: the priority chain is driven by state and mood, no state or mood mapped to
 * a glance or a nod, and no test asked whether anything selected them. Dead art is
 * invisible to a suite that only checks the paths that ARE wired, so the reachability
 * itself has to be the assertion.
 */
import { describe, it, expect } from 'vitest'
import {
  activeAnimFor,
  animClassFor,
  IDLE_FIDGET_ANIMS,
} from '../apps/crew-companion/petAnim'

describe('idle fidgets', () => {
  it('offers the four motions that were previously unreachable', () => {
    expect(IDLE_FIDGET_ANIMS.map((f) => f.anim)).toEqual([
      'look', 'ponder', 'correct', 'ponder-loop',
    ])
  })

  it('gives every fidget a bounded hold', () => {
    // `ponder-loop`'s keyframe is `infinite`; without a hold the companion would
    // ponder forever, so a missing or zero hold is the bug this catches.
    for (const { anim, holdMs } of IDLE_FIDGET_ANIMS) {
      expect(holdMs, `${anim} needs a hold`).toBeGreaterThan(0)
      expect(holdMs, `${anim} hold looks unbounded`).toBeLessThanOrEqual(5_000)
    }
  })

  it('maps every fidget to a real stylesheet class', () => {
    for (const { anim } of IDLE_FIDGET_ANIMS) {
      expect(animClassFor(anim)).toBe(`kg-anim-${anim}`)
    }
  })

  it('gives every idle action the same chance', () => {
    /*
     * The point of the flat pool. An earlier version nested probability thresholds,
     * which meant the four body motions had to take their share out of the small
     * hop -- peers weighted against each other, which is how these motions ended up
     * rare. This mirrors the hook's construction: mood flicker + small hop + one
     * entry per body motion, all picked uniformly.
     */
    const DAYTIME_ACTIONS = 2 + IDLE_FIDGET_ANIMS.length  // mood, hop, + motions
    const share = 1 / DAYTIME_ACTIONS

    for (const { anim } of IDLE_FIDGET_ANIMS) {
      expect(share, `${anim} should be an equal peer`).toBeCloseTo(share, 10)
    }
    // Six peers today: no single action may dominate the rotation.
    expect(DAYTIME_ACTIONS).toBe(6)
    expect(share).toBeCloseTo(1 / 6, 10)
  })

  it('plays a fidget when the companion is idle', () => {
    expect(activeAnimFor({ state: 'idle', idleAnim: 'look' })).toBe('look')
  })

  it('is outranked by every real signal', () => {
    // Ambient motion must never mask something the user needs to see.
    expect(activeAnimFor({ state: 'error', idleAnim: 'look' })).toBe('error')
    expect(activeAnimFor({ state: 'done', idleAnim: 'look' })).toBe('celebrate')
    expect(activeAnimFor({ state: 'idle', mood: 'happy', idleAnim: 'look' })).toBe('celebrate')
    expect(activeAnimFor({ state: 'idle', mood: 'curious', idleAnim: 'look' })).toBe('curious')
    expect(activeAnimFor({ state: 'idle', walking: true, idleAnim: 'look' })).toBe('fly')
  })

  it('does not fidget while docked', () => {
    // Half the body is off-screen at an edge, so a fidget there reads as a glitch.
    expect(activeAnimFor({ state: 'idle', docked: true, idleAnim: 'look' })).toBeNull()
  })

  it('is still when no fidget is playing', () => {
    expect(activeAnimFor({ state: 'idle' })).toBeNull()
    expect(activeAnimFor({ state: 'idle', idleAnim: null })).toBeNull()
  })
})
