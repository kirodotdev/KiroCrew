import { describe, expect, it } from 'vitest'

import { DEFAULT_POSE, DEFAULT_POSE_BG, ICON_BG_PALETTE, POSE_IDS, resolvePose, seedIconBg, seedIconPose } from './avatarPoses'

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

describe('seedIconBg', () => {
  it('always returns a colour from the icon palette', () => {
    for (const name of ['Build Box', 'Staging', 'krewworker', 'a', 'ünïçodé 🚀', '123']) {
      expect(ICON_BG_PALETTE).toContain(seedIconBg(name))
    }
  })

  it('is stable for a given name (the colour follows the name, not chance)', () => {
    expect(seedIconBg('Build Box')).toBe(seedIconBg('Build Box'))
    expect(seedIconBg('Staging')).toBe(seedIconBg('Staging'))
  })

  it('falls back to the brand default for an empty or whitespace name', () => {
    expect(seedIconBg('')).toBe(DEFAULT_POSE_BG)
    expect(seedIconBg('   ')).toBe(DEFAULT_POSE_BG)
    expect(seedIconBg(undefined)).toBe(DEFAULT_POSE_BG)
  })

  it('spreads a set of distinct names across more than one colour', () => {
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
    const colours = new Set(names.map(seedIconBg))
    expect(colours.size).toBeGreaterThan(1)
  })

  it('does not move in lockstep with the pose (different hash salt)', () => {
    // A roster where pose and colour varied identically would read as a single
    // 2-axis gradient; the ':bg' salt decorrelates them.
    const names = ['Build Box', 'Staging', 'Prod Watch', 'Reviewer', 'Docs Bot', 'Nightly']
    const pairs = names.map((n) => `${seedIconPose(n)}|${seedIconBg(n)}`)
    expect(new Set(pairs).size).toBe(names.length)
  })
})
