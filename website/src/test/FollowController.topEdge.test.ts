// FollowController with the live edge at the TOP (newest-first transcript).
//
// The decisions are the bottom edge's mirrored onto the constant target 0:
// leaving the edge is a DOWNWARD move, returning is an UPWARD arrival inside
// FOLLOW_REENGAGE_PX of 0, and the automatic pin writes 0. The bottom-only
// geometry (clamps, viewport growth, shrink allowance) must not leak in.

import { describe, it, expect } from 'vitest'
import {
  FOLLOW_REENGAGE_PX,
  computeAtEdge,
  distanceFromEdge,
  evaluateAutoPin,
  followTarget,
  resolveUserScrollStick,
  type ScrollGeom,
} from '../hooks/virtualizer/FollowController'

const geomAt = (scrollTop: number): ScrollGeom => ({ scrollTop, scrollHeight: 5000, clientHeight: 600 })

describe('edge geometry', () => {
  it('the top edge targets 0 and measures distance as scrollTop', () => {
    const g = geomAt(250)
    expect(followTarget(g, 'top')).toBe(0)
    expect(distanceFromEdge(g, 'top')).toBe(250)
    expect(computeAtEdge(g, 100, 'top')).toBe(false)
    expect(computeAtEdge(geomAt(80), 100, 'top')).toBe(true)
  })

  it('the bottom edge keeps the bottom geometry', () => {
    const g = geomAt(4400)
    expect(followTarget(g, 'bottom')).toBe(4400)
    expect(distanceFromEdge(g, 'bottom')).toBe(0)
    expect(computeAtEdge(geomAt(0), 100, 'bottom')).toBe(false)
  })
})

describe('resolveUserScrollStick, top edge', () => {
  const decide = (stick: boolean, prev: number, now: number) =>
    resolveUserScrollStick({ stick, followOutput: true, scrollTop: now, prevScrollTop: prev, geom: geomAt(now), edge: 'top' })

  it('a downward move releases, even a small one inside the band', () => {
    expect(decide(true, 0, 300)).toBe(false)
    expect(decide(true, 0, 8)).toBe(false)
  })

  it('an upward arrival inside the band re-engages; one short of it does not', () => {
    expect(decide(false, 300, FOLLOW_REENGAGE_PX)).toBe(true)
    expect(decide(false, 300, 0)).toBe(true)
    expect(decide(false, 300, FOLLOW_REENGAGE_PX + 1)).toBe(false)
  })

  it('a neutral event keeps the previous state', () => {
    expect(decide(false, 5, 5)).toBe(false)
    expect(decide(true, 5, 5)).toBe(true)
  })

  it('with no prior observation the decision is position-only', () => {
    expect(decide(false, -1, 10)).toBe(true)
    expect(decide(true, -1, 400)).toBe(false)
  })

  it('a position flush with the BOTTOM is not read as the live edge', () => {
    // The bottom edge keeps follow for a reader the engine clamps flush at the
    // end; at the top edge the end is history, so an arrival there releases.
    const g: ScrollGeom = { scrollTop: 4400, scrollHeight: 5000, clientHeight: 600 }
    expect(resolveUserScrollStick({ stick: true, followOutput: true, scrollTop: 4400, prevScrollTop: 4000, geom: g, edge: 'top' })).toBe(false)
  })

  it('followOutput off never follows', () => {
    expect(resolveUserScrollStick({ stick: true, followOutput: false, scrollTop: 0, prevScrollTop: 300, geom: geomAt(0), edge: 'top' })).toBe(false)
  })
})

describe('evaluateAutoPin, top edge', () => {
  it('pins a follower in the re-engage band to 0', () => {
    expect(evaluateAutoPin({ stick: true, geom: geomAt(10), lastWriteTop: 10, edge: 'top' })).toEqual({ pin: true, stick: true, target: 0 })
  })

  it('a follower already at 0 needs no write', () => {
    expect(evaluateAutoPin({ stick: true, geom: geomAt(0), lastWriteTop: 0, edge: 'top' })).toEqual({ pin: false, stick: true, target: 0 })
  })

  it('a reader who scrolled down past our write before the scroll event landed is released, not yanked', () => {
    expect(evaluateAutoPin({ stick: true, geom: geomAt(400), lastWriteTop: 0, edge: 'top' })).toEqual({ pin: false, stick: false, target: 0 })
  })

  it('is indifferent to the bottom geometry and the run state', () => {
    // An idle run and a large viewport shrink are bottom-only reasons to hold
    // or release; neither moves a target that is always 0.
    const r = evaluateAutoPin({ stick: true, geom: geomAt(12), lastWriteTop: 12, edge: 'top', runActive: false, viewportShrink: 300 })
    expect(r).toEqual({ pin: true, stick: true, target: 0 })
  })

  it('a restore gate releases and a released reader is never pinned', () => {
    expect(evaluateAutoPin({ stick: true, geom: geomAt(10), lastWriteTop: 10, edge: 'top', restoreGate: true }).stick).toBe(false)
    expect(evaluateAutoPin({ stick: false, geom: geomAt(10), lastWriteTop: 10, edge: 'top' }).pin).toBe(false)
  })
})
