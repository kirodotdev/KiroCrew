import { describe, expect, it } from 'vitest'

import { DEFAULT_POSE, POSE_IDS, resolvePose, seedIconPose } from './avatarPoses'

describe('seedIconPose', () => {
  it('always returns a pose this build can draw', () => {
    for (const name of ['Build Box', 'Staging', 'krewworker', 'a', 'ünïçodé 🚀', '123']) {
      const pose = seedIconPose(name)
      expect(POSE_IDS).toContain(pose)
      // The seeded default must itself resolve cleanly (preview === roster).
      expect(resolvePose(pose)).toBe(pose)
    }
  })

  it('is stable for a given name (same crew always opens on the same pose)', () => {
    expect(seedIconPose('Build Box')).toBe(seedIconPose('Build Box'))
    expect(seedIconPose('Staging')).toBe(seedIconPose('Staging'))
  })

  it('falls back to the first pose for an empty or whitespace name', () => {
    expect(seedIconPose('')).toBe(DEFAULT_POSE)
    expect(seedIconPose('   ')).toBe(DEFAULT_POSE)
    expect(seedIconPose(undefined)).toBe(DEFAULT_POSE)
  })

  it('spreads a set of distinct names across more than one pose', () => {
    // Parity with the ghost: a roster of un-customized icon crews should not
    // all read as pose-1. A handful of names must land on >1 distinct pose.
    const names = [
      'Build Box',
      'Staging',
      'Prod Watch',
      'Reviewer',
      'Docs Bot',
      'Release Captain',
      'Nightly',
      'Sandbox',
    ]
    const poses = new Set(names.map(seedIconPose))
    expect(poses.size).toBeGreaterThan(1)
  })
})
