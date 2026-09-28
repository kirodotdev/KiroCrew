/**
 * The 4-7-8 timeline, ported case-for-case from the desktop app's own suite
 * (`crew-companion/src/test/breathing.test.ts`, 11 cases).
 *
 * The TypeScript is the specification. Several of these pin numbers that are easy
 * to break while tuning the animation — the 4/7/8 counts themselves, the 1:2
 * inhale:exhale ratio that is the active part of the technique, and the rule that
 * the count never reads 0 mid-phase.
 */
import { describe, it, expect } from 'vitest'
import {
  breathStateAt,
  BREATH_PHASES,
  BREATH_CYCLES,
  CYCLE_MS,
  TOTAL_MS,
  READY_MS,
} from '../apps/crew-companion/breathing'

describe('4-7-8 breathing timeline', () => {
  it('is 4 cycles of 19s plus a lead-in', () => {
    expect(CYCLE_MS).toBe(19_000)
    expect(BREATH_CYCLES).toBe(4)
    expect(TOTAL_MS).toBe(READY_MS + 19_000 * 4)
  })

  // 4-7-8 is defined by those exact counts; easy to break while tuning.
  it('uses the 4-7-8 counts', () => {
    expect(BREATH_PHASES.map((p) => p.ms)).toEqual([4000, 7000, 8000])
    expect(BREATH_PHASES.map((p) => p.labelKey)).toEqual(['apps.crewCompanion.breathe.inhale', 'apps.crewCompanion.breathe.hold', 'apps.crewCompanion.breathe.exhale'])
  })

  it('exhales for twice the inhale', () => {
    expect(BREATH_PHASES[2].ms / BREATH_PHASES[0].ms).toBe(2)
  })

  describe('lead-in', () => {
    it('starts in the ready state, counting down', () => {
      const s = breathStateAt(0)
      expect(s.ready).toBe(true)
      expect(s.phase.labelKey).toBe('apps.crewCompanion.breathe.ready')
      expect(s.cycle).toBe(0)
      expect(s.secondsLeft).toBe(3)
    })

    it('counts the lead-in down to 1', () => {
      expect(breathStateAt(READY_MS - 1).secondsLeft).toBe(1)
    })

    // The whole point of the lead-in is that breath 1 does not start mid-thought.
    it('begins the first inhale only after the lead-in', () => {
      expect(breathStateAt(READY_MS - 1).ready).toBe(true)
      const first = breathStateAt(READY_MS)
      expect(first.ready).toBe(false)
      expect(first.phase.labelKey).toBe('apps.crewCompanion.breathe.inhale')
      expect(first.cycle).toBe(1)
    })
  })

  it('walks the phases in order within a cycle', () => {
    expect(breathStateAt(READY_MS + 1_000).phase.labelKey).toBe('apps.crewCompanion.breathe.inhale') // 0-4s
    expect(breathStateAt(READY_MS + 6_000).phase.labelKey).toBe('apps.crewCompanion.breathe.hold') // 4-11s
    expect(breathStateAt(READY_MS + 15_000).phase.labelKey).toBe('apps.crewCompanion.breathe.exhale') // 11-19s
  })

  it('crosses into the next cycle cleanly', () => {
    const s = breathStateAt(READY_MS + CYCLE_MS + 200)
    expect(s.cycle).toBe(2)
    expect(s.phase.labelKey).toBe('apps.crewCompanion.breathe.inhale')
  })

  // The count is the focal point, so it must never read 0 mid-phase.
  it('counts each phase down to 1, never 0', () => {
    expect(breathStateAt(READY_MS).secondsLeft).toBe(4) // 4s inhale
    expect(breathStateAt(READY_MS + 4_000).secondsLeft).toBe(7) // 7s hold
    expect(breathStateAt(READY_MS + 11_000).secondsLeft).toBe(8) // 8s exhale
    for (let t = 0; t < TOTAL_MS; t += 50) {
      expect(breathStateAt(t).secondsLeft).toBeGreaterThanOrEqual(1)
    }
  })

  it('reports done only after the final cycle', () => {
    expect(breathStateAt(TOTAL_MS - 1).done).toBe(false)
    expect(breathStateAt(TOTAL_MS).done).toBe(true)
  })

  it('resolves a phase at every 100ms step', () => {
    for (let t = 0; t < TOTAL_MS; t += 100) {
      const s = breathStateAt(t)
      expect(s.phase.labelKey).toBeTruthy()
      expect(s.cycle).toBeGreaterThanOrEqual(0)
      expect(s.cycle).toBeLessThanOrEqual(BREATH_CYCLES)
    }
  })

  it('is robust to a negative elapsed time', () => {
    // requestAnimationFrame timestamps are monotonic, but a caller subtracting a
    // later start from an earlier frame would otherwise index out of range.
    expect(breathStateAt(-500).ready).toBe(true)
  })
})
