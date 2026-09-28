/**
 * Body-motion selection, tested at the pure-function layer (like the walk geometry)
 * so the React shell around it does not have to be simulated.
 *
 * These pin the precedence the desktop app shipped in PetWidget's `activeAnim` chain
 * — error > celebrate > curious > fly — plus the measured eye offsets each posed
 * reaction holds. The port had none of this: the keyframes existed for ponder,
 * celebrate and error only, so a travelling companion never floated and a curious one
 * only moved its eyes, which is what made it look inert.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { activeAnimFor, animClassFor } from '../apps/crew-companion/petAnim'
import { ghostEyeOffsetFor, POSED_ANIMS, EYE_BEATS } from '../apps/crew-companion/ghostEyes'

const CSS = readFileSync(
  resolve(__dirname, '../apps/crew-companion/petMotion.css'),
  'utf8',
)

describe('activeAnimFor — precedence', () => {
  it('holds still when idle and settled', () => {
    expect(activeAnimFor({ state: 'idle' })).toBeNull()
  })

  it('floats while travelling — the motion an idle hop needs', () => {
    expect(activeAnimFor({ state: 'idle', walking: true })).toBe('fly')
  })

  it('cocks its head when curious, even mid-travel', () => {
    // The expression is the more informative of the two, so it outranks the float.
    expect(activeAnimFor({ state: 'idle', mood: 'curious', walking: true })).toBe('curious')
  })

  it('celebrates a finished job over being curious', () => {
    expect(activeAnimFor({ state: 'done', mood: 'curious' })).toBe('celebrate')
  })

  it('shakes on error above everything else', () => {
    expect(activeAnimFor({ state: 'error', mood: 'curious', walking: true })).toBe('error')
  })

  it('ponders while busy, but never over a ported reaction', () => {
    expect(activeAnimFor({ state: 'loading' })).toBe('ponder-loop')
    expect(activeAnimFor({ state: 'loading', mood: 'curious' })).toBe('curious')
  })

  it('goes quiet when docked — except for a celebration', () => {
    // A half-off-screen body must not be shaken or cocked around; finishing a job is
    // still worth a hop.
    expect(activeAnimFor({ state: 'error', docked: true })).toBeNull()
    expect(activeAnimFor({ state: 'idle', mood: 'curious', docked: true })).toBeNull()
    expect(activeAnimFor({ state: 'idle', walking: true, docked: true })).toBeNull()
    expect(activeAnimFor({ state: 'loading', docked: true })).toBeNull()
    expect(activeAnimFor({ state: 'done', docked: true })).toBe('celebrate')
  })

  it('stays out of the way during breathing', () => {
    // The breathing overlay drives the scale; a second animation would fight it.
    for (const state of ['inhale', 'hold', 'exhale']) {
      expect(activeAnimFor({ state })).toBeNull()
    }
  })
})

describe('animClassFor', () => {
  it('maps a motion to its stylesheet class, and stillness to nothing', () => {
    expect(animClassFor('fly')).toBe('kg-anim-fly')
    expect(animClassFor('ponder-loop')).toBe('kg-anim-ponder-loop')
    expect(animClassFor(null)).toBeUndefined()
  })

  it('every motion it can name has real keyframes to play', () => {
    // A class with no keyframes is a silent no-op — exactly the failure that left the
    // companion inert, so it is pinned here rather than discovered on screen.
    for (const anim of ['error', 'celebrate', 'curious', 'fly', 'look', 'ponder-loop'] as const) {
      expect(CSS, anim).toContain(`.${animClassFor(anim)} `)
    }
    for (const frames of ['kg-error', 'kg-celebrate', 'kg-curious', 'kg-fly', 'kg-look', 'kg-ponder']) {
      expect(CSS, frames).toContain(`@keyframes ${frames} `)
    }
  })

  it('honours reduced motion for the newly ported reactions', () => {
    const reduced = CSS.slice(CSS.indexOf('.kg-anim-look'))
    expect(reduced).toMatch(/prefers-reduced-motion/)
  })
})

describe('posed eye handling', () => {
  it('holds the measured offset for each posed reaction', () => {
    // Measured from the mascot footage, mirrored for this art's facing — not invented.
    expect(ghostEyeOffsetFor('look')).toEqual({ dx: -0.95, dy: 0 })
    expect(ghostEyeOffsetFor('curious')).toEqual({ dx: -0.85, dy: -0.55 })
    expect(ghostEyeOffsetFor('ponder')).toEqual({ dx: -0.42, dy: -0.79 })
    expect(ghostEyeOffsetFor('ponder-loop')).toEqual({ dx: -0.42, dy: -0.79 })
  })

  it('adds nothing while flying — the walking pose already looks ahead', () => {
    expect(ghostEyeOffsetFor('fly')).toEqual({ dx: 0, dy: 0 })
  })

  it('leaves the resting reactions to the live eyes', () => {
    // idle, celebrate and error keep cursor tracking; only the posed set freezes it.
    expect(ghostEyeOffsetFor(null)).toEqual({ dx: 0, dy: 0 })
    expect(POSED_ANIMS.has('celebrate')).toBe(false)
    expect(POSED_ANIMS.has('error')).toBe(false)
    for (const anim of ['look', 'curious', 'ponder', 'ponder-loop', 'fly']) {
      expect(POSED_ANIMS.has(anim), anim).toBe(true)
    }
  })

  it('squashes the eyes only where the footage does', () => {
    // Error opens on the wince dash; curious foreshortens on the snap. Ponder keeps
    // open eyes on purpose — thinning at this size read as a rendering glitch.
    expect(EYE_BEATS.error).toEqual({ at: 0, ms: 220, sx: 1.2, sy: 0.22 })
    expect(EYE_BEATS.curious).toEqual({ at: 90, ms: 170, sx: 0.45, sy: 1.04 })
    expect(EYE_BEATS.ponder).toBeUndefined()
  })
})
