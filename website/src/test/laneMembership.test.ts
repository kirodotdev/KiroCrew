// The ONE rule for "did the lane this install follows publish the bytes it is
// running", shared by the header chip and Settings › About. It had already
// existed as three hand-rolled copies that drifted on what licenses the
// exemption, which is what this module (and this file) exist to stop.
import { describe, it, expect } from 'vitest'
import { bytesAreTheStableRelease } from '../utils/laneMembership'

describe('bytesAreTheStableRelease', () => {
  it('exempts a promoted stable release: follows stable, and the lane is not ahead', () => {
    // `0.4.1rc1` IS the 0.4.1 release — the population every stamp-based rule
    // mislabelled as insider.
    expect(bytesAreTheStableRelease({
      followedChannel: 'stable', laneAnswered: true, runningAheadOfLane: false,
    })).toBe(true)
  })

  it('does NOT exempt bytes the stable lane never published', () => {
    // An insider build whose switcher was flipped to stable is still prerelease.
    expect(bytesAreTheStableRelease({
      followedChannel: 'stable', laneAnswered: true, runningAheadOfLane: true,
    })).toBe(false)
  })

  it('does NOT exempt without a completed comparison', () => {
    // Unknown stays unknown: never claim these bytes are the stable release on
    // the strength of a check that did not finish. This is the deliberate cost —
    // a promoted-stable install on a feed-unreachable host keeps its prerelease
    // affordances rather than being handed an unproven exemption.
    expect(bytesAreTheStableRelease({
      followedChannel: 'stable', laneAnswered: false, runningAheadOfLane: false,
    })).toBe(false)
  })

  it.each(['insider', 'nightly', '', undefined, null])(
    'never exempts an install following %s',
    channel => {
      expect(bytesAreTheStableRelease({
        followedChannel: channel, laneAnswered: true, runningAheadOfLane: false,
      })).toBe(false)
    },
  )
})
