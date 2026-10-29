import { describe, expect, it } from 'vitest'

import {
  DEFAULT_POSE,
  DEFAULT_POSE_BG,
  ICON_BG_PALETTE,
  POSE_IDS,
  iconMotionVars,
  poseBodyDataUri,
  poseDataUri,
  poseEyeVars,
  poseEyesDataUri,
  resolvePose,
  seedIconBg,
  seedIconPose,
} from './avatarPoses'
import { BODY_MOTION } from './kiroGhostAvatar'
import motionCss from '../components/crewIconMotion.css?raw'

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

describe('icon working motion', () => {
  it('never animates inside the SVG — the still icon carries no style or motion', () => {
    // An animation inside an <img>'s SVG is re-rastered every frame, and iOS
    // Safari re-rastered it in pieces: head, tail and one eye drew out of step.
    for (const pose of [...POSE_IDS, 'pose-99', 'constructor', '']) {
      for (const uri of [poseDataUri(pose, DEFAULT_POSE_BG), poseBodyDataUri(pose), poseEyesDataUri(pose)]) {
        const svg = svgOf(uri)
        expect(svg).not.toContain('<style')
        expect(svg).not.toMatch(/animation|@keyframes|<animate/)
      }
    }
  })

  it('moves the same face: body and eye layers split the still pose, minus its tile', () => {
    const paths = (svg: string) => svg.match(/<path\b[^>]*\/>/g) ?? []
    for (const pose of POSE_IDS) {
      const still = svgOf(poseDataUri(pose, '#25679d'))
      const body = svgOf(poseBodyDataUri(pose))
      const eyes = svgOf(poseEyesDataUri(pose))
      expect(body).not.toMatch(/<rect\b/)
      expect(eyes).not.toMatch(/<rect\b/)
      // The body is the white silhouette alone; the eyes are the two unfilled paths.
      expect(paths(body)).toHaveLength(1)
      expect(paths(body)[0]).toContain('fill="#fff"')
      expect(paths(eyes)).toHaveLength(2)
      for (const eye of paths(eyes)) expect(eye).not.toContain('fill=')
      // Byte for byte the still frame's paths, on the same canvas: nothing redrawn.
      expect([...paths(body), ...paths(eyes)]).toEqual(paths(still))
      expect(eyes.match(/<svg\b[^>]*>/)![0]).toBe(still.match(/<svg\b[^>]*>/)![0])
    }
  })

  it('blinks each pose toward its own eyes', () => {
    for (const pose of POSE_IDS) {
      const v = poseEyeVars(pose)
      const x = parseFloat(v['--crew-icon-eye-x'])
      const y = parseFloat(v['--crew-icon-eye-y'])
      // Every shipped pose has its eyes in the upper half, away from the tile's centre.
      expect(x).toBeGreaterThan(30)
      expect(x).toBeLessThan(80)
      expect(y).toBeGreaterThan(30)
      expect(y).toBeLessThan(45)
    }
    expect(poseEyeVars('constructor')).toEqual(poseEyeVars(DEFAULT_POSE))
  })

  it('lays the moving root out as a block, the same box as the still icon img', () => {
    // An inline-block sits on the text baseline, so the header stack's wrapper
    // grew past the face and its ring drew a dark band around a raised face.
    const root = motionCss.match(/\.crew-icon-motion\s*\{([^}]*)\}/)![1]
    expect(root).toMatch(/display:\s*block;/)
  })

  it("feeds the ghost's full body-motion numbers to the CSS, scaled to the box", () => {
    const m = BODY_MOTION.full
    const v = iconMotionVars()
    expect(v['--crew-icon-secs']).toBe(`${m.secs}s`)
    expect(v['--crew-icon-fit-scale']).toBe(String(m.fitScale))
    // 1200-unit pixels become a share of the box, so every avatar size moves alike.
    expect(parseFloat(v['--crew-icon-rise'])).toBeCloseTo((-m.rise / 1200) * 100, 3)
    expect(parseFloat(v['--crew-icon-sink'])).toBeCloseTo((m.sink / 1200) * 100, 3)
    expect(v['--crew-icon-sway-to']).toBe(`${m.sway}deg`)
    for (const value of Object.values(v)) expect(value).not.toMatch(/px/)
  })

  it('reads every variable it sets, and keeps a still frame for reduced motion', () => {
    for (const name of Object.keys({ ...iconMotionVars(), ...poseEyeVars(DEFAULT_POSE) })) {
      expect(motionCss).toContain(`var(${name})`)
    }
    const reduced = motionCss.slice(motionCss.indexOf('prefers-reduced-motion'))
    expect(reduced).toMatch(/animation:\s*none\s*!important/)
    expect(reduced).toMatch(/\.crew-icon-fit\s*\{\s*transform:\s*none/)
  })
})
