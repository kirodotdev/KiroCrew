import { describe, expect, it } from 'vitest'

import {
  DEFAULT_POSE,
  DEFAULT_POSE_BG,
  ICON_BG_PALETTE,
  POSE_IDS,
  poseDataUri,
  resolvePose,
  seedIconBg,
  seedIconPose,
} from './avatarPoses'
import { workingStyle } from './kiroGhostAvatar'

/** The SVG text behind a pose data URI. */
const svgOf = (uri: string) => decodeURIComponent(uri.replace(/^data:image\/svg\+xml;utf8,/, ''))

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

describe('poseDataUri working variant', () => {
  it('is the still frame unless working is asked for', () => {
    const still = poseDataUri('pose-3', '#25679d')
    expect(poseDataUri('pose-3', '#25679d', false)).toBe(still)
    expect(poseDataUri('pose-3', '#25679d', undefined)).toBe(still)
    expect(svgOf(still)).not.toContain('<style>')
    expect(svgOf(still)).not.toContain('kg-bob')
  })

  it("wraps the silhouette in the ghost's body-motion groups at the ghost's full amplitude", () => {
    const svg = svgOf(poseDataUri('pose-3', '#25679d', true))
    // The ghost's OWN style block, verbatim — one source of tempo. Full, never
    // subtle: a pose has no moving eyes, so the body is the whole cue.
    expect(svg).toContain(workingStyle('full'))
    expect(svg).not.toContain(workingStyle('subtle'))
    expect(svg).toContain('class="kg-fit"')
    expect(svg).toContain('class="kg-sway"')
    expect(svg).toContain('class="kg-bob"')
    expect(svg).toContain('class="kg-sq"')
  })

  it('moves the drawing without changing it: the tile stays put and the art is byte-identical', () => {
    const still = svgOf(poseDataUri('pose-3', '#25679d'))
    const working = svgOf(poseDataUri('pose-3', '#25679d', true))
    const rect = still.match(/<rect\b[^>]*\/>/)![0]
    const body = still.slice(still.indexOf(rect) + rect.length, still.lastIndexOf('</svg>'))
    // The recoloured tile is outside the motion groups: it is the first thing
    // after the opening tag in both frames, before any `<g`.
    expect(working.indexOf(rect)).toBe(still.indexOf(rect))
    expect(working.indexOf(rect)).toBeLessThan(working.indexOf('<g'))
    // Everything the still frame draws over the tile is inside the groups,
    // unchanged — no path, colour or eye was rewritten to make it move.
    expect(working).toContain(`>${body}</g>`)
    expect(working).toContain('fill="#25679d"')
  })

  it('animates every shipped pose and the unknown-pose fallback alike', () => {
    for (const pose of [...POSE_IDS, 'pose-99', 'constructor', '']) {
      const svg = svgOf(poseDataUri(pose, DEFAULT_POSE_BG, true))
      expect(svg).toContain('class="kg-bob"')
      expect(svg.match(/<rect\b/g)).toHaveLength(1)
    }
  })
})
