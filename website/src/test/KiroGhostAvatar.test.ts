/**
 * Guards for the generated Kiro ghost avatars.
 *
 * Three properties here are load-bearing and cannot be checked by eye:
 *  - the silhouette and eye paths are the SHIPPED mark, read back out of
 *    `src/assets/kiro-ghost-mark.svg`, so the generator cannot drift from the art;
 *  - tile colors keep a minimum CIEDE2000 distance, because sampling a hue circle
 *    produced pairs ~13 dE apart that read as a rendering bug rather than as two
 *    identities;
 *  - the prng draw ORDER is frozen, because the stream is positional: inserting a
 *    draw re-rolls every trait after it and silently changes existing faces.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, it, expect } from 'vitest'
import { createAvatar } from '@dicebear/core'
import {
  kiroGhost,
  compose,
  markPaths,
  BODY,
  EYE_A,
  EYE_B,
  TILES,
  BRAND_PURPLE,
  EYES,
  BROWS,
  MOUTHS,
  ACCESSORIES,
  PROPS,
  type KiroGhostTraits,
} from '../lib/kiroGhostAvatar'

const MARK = join(__dirname, '..', 'assets', 'kiro-ghost-mark.svg')

/** All traits off: the neutral reference that must reproduce the mark. */
const BARE: KiroGhostTraits = {
  eyes: 'canon',
  brows: 'none',
  mouth: 'none',
  accessory: 'none',
  prop: 'none',
  blush: false,
  flip: false,
  tile: BRAND_PURPLE,
}

/* ---------- CIEDE2000, for the palette guard only ---------- */

function rgbToLab(hex: string): [number, number, number] {
  const h = hex.replace('#', '')
  const chan = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16) / 255)
  const lin = (v: number) => (v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4)
  const [r, g, b] = chan.map(lin)
  let x = (r * 0.4124564 + g * 0.3575761 + b * 0.1804375) / 0.95047
  const y = r * 0.2126729 + g * 0.7151522 + b * 0.072175
  let z = (r * 0.0193339 + g * 0.119192 + b * 0.9503041) / 1.08883
  const f = (t: number) => (t > 216 / 24389 ? Math.cbrt(t) : (841 / 108) * t + 4 / 29)
  x = f(x)
  z = f(z)
  const fy = f(y)
  return [116 * fy - 16, 500 * (x - fy), 200 * (fy - z)]
}

function deltaE00(c1: string, c2: string): number {
  const [L1, a1, b1] = rgbToLab(c1)
  const [L2, a2, b2] = rgbToLab(c2)
  const rad = Math.PI / 180
  const Cb = (Math.hypot(a1, b1) + Math.hypot(a2, b2)) / 2
  const G = 0.5 * (1 - Math.sqrt(Cb ** 7 / (Cb ** 7 + 25 ** 7)))
  const ap1 = a1 * (1 + G)
  const ap2 = a2 * (1 + G)
  const Cp1 = Math.hypot(ap1, b1)
  const Cp2 = Math.hypot(ap2, b2)
  const hp1 = (Math.atan2(b1, ap1) / rad + 360) % 360
  const hp2 = (Math.atan2(b2, ap2) / rad + 360) % 360
  const dL = L2 - L1
  const dC = Cp2 - Cp1
  let dh = 0
  if (Cp1 * Cp2 !== 0) {
    dh = hp2 - hp1
    if (dh > 180) dh -= 360
    else if (dh < -180) dh += 360
  }
  const dH = 2 * Math.sqrt(Cp1 * Cp2) * Math.sin((dh * rad) / 2)
  const Lb = (L1 + L2) / 2
  const Cpb = (Cp1 + Cp2) / 2
  let hpb = hp1 + hp2
  if (Cp1 * Cp2 !== 0) {
    if (Math.abs(hp1 - hp2) > 180) hpb += hpb < 360 ? 360 : -360
    hpb /= 2
  }
  const T =
    1 -
    0.17 * Math.cos((hpb - 30) * rad) +
    0.24 * Math.cos(2 * hpb * rad) +
    0.32 * Math.cos((3 * hpb + 6) * rad) -
    0.2 * Math.cos((4 * hpb - 63) * rad)
  const dTheta = 30 * Math.exp(-(((hpb - 275) / 25) ** 2))
  const Rc = 2 * Math.sqrt(Cpb ** 7 / (Cpb ** 7 + 25 ** 7))
  const Sl = 1 + (0.015 * (Lb - 50) ** 2) / Math.sqrt(20 + (Lb - 50) ** 2)
  const Sc = 1 + 0.045 * Cpb
  const Sh = 1 + 0.015 * Cpb * T
  const Rt = -Math.sin(2 * dTheta * rad) * Rc
  return Math.sqrt(
    (dL / Sl) ** 2 + (dC / Sc) ** 2 + (dH / Sh) ** 2 + Rt * (dC / Sc) * (dH / Sh),
  )
}

describe('kiroGhost style', () => {
  it('takes its silhouette and eyes from the mark asset, not from copied code', () => {
    // Parsed independently here, so a regression in `markPaths` is caught rather
    // than compared against itself.
    const asset = readFileSync(MARK, 'utf8')
    const ds = [...asset.matchAll(/\sd="([^"]+)"/g)].map((m) => m[1])
    expect(ds).toHaveLength(3)
    expect([BODY, EYE_A, EYE_B]).toEqual(ds)
    for (const d of [BODY, EYE_A, EYE_B]) expect(d.startsWith('M')).toBe(true)
    expect(EYE_A).not.toBe(EYE_B)
  })

  it('refuses an asset whose path count changed instead of rendering blank', () => {
    // A silently empty `d` is invisible until someone looks at a roster, and a
    // fourth path would shift which one is read as an eye.
    expect(() => markPaths('<svg><path d="M0 0Z"/></svg>')).toThrow(/exactly 3 paths/)
    expect(() => markPaths('<svg/>')).toThrow(/found 0/)
    expect(() =>
      markPaths('<svg><path d="M0 0Z"/><path d="M1 1Z"/><path d="M2 2Z"/><path d="M3 3Z"/></svg>'),
    ).toThrow(/found 4/)
  })

  it('reproduces the mark when every trait is off', () => {
    const bare = compose(BARE)
    expect(bare).toContain(`<path d="${BODY}" fill="#ffffff"/>`)
    expect(bare).toContain(EYE_A)
    expect(bare).toContain(EYE_B)
    // Nothing else is drawn: body + two eyes + the tile rect.
    expect(bare.match(/<path|<ellipse|<circle|<rect|<g /g)).toHaveLength(4)
  })

  it('keeps tile colors perceptually apart', () => {
    let min = Infinity
    let closest = ''
    for (let i = 0; i < TILES.length; i++) {
      for (let j = i + 1; j < TILES.length; j++) {
        const d = deltaE00(TILES[i], TILES[j])
        if (d < min) {
          min = d
          closest = `${TILES[i]} vs ${TILES[j]}`
        }
      }
    }
    // 13 dE was the rejected pair; 18 leaves headroom without pinning the exact
    // solver output, so re-running the palette solver does not have to be lockstep.
    expect(min, `closest pair ${closest} at ${min.toFixed(2)} dE`).toBeGreaterThan(18)
  })

  it('renders a local data URI with no remix claim in its metadata', () => {
    const svg = createAvatar(kiroGhost, { seed: 'kirocrew' }).toString()
    // The art is first-party, so DiceBear's "Remix of" rights line must not appear.
    expect(svg).not.toContain('Remix of')
    expect(svg).toContain('Design by')
    expect(createAvatar(kiroGhost, { seed: 'kirocrew' }).toDataUri()).toMatch(
      /^data:image\/svg\+xml/,
    )
  })

  it('is deterministic and distinct across seeds', () => {
    const a = createAvatar(kiroGhost, { seed: 'oncall' }).toString()
    expect(createAvatar(kiroGhost, { seed: 'oncall' }).toString()).toBe(a)
    expect(createAvatar(kiroGhost, { seed: 'kirocrew' }).toString()).not.toBe(a)
  })

  it('pins the draw order', () => {
    // The prng is one positional stream, so these tuples change if a draw is
    // inserted, removed, or reordered in `create` — which would re-roll every
    // existing crew's face. Appending a NEW trait at the end is safe and leaves
    // these untouched; if this test fails, a draw moved. The values themselves
    // carry no meaning beyond being what the frozen order produces.
    const traits = (seed: string) => {
      const e = createAvatar(kiroGhost, { seed }).toJson().extra as Record<string, unknown>
      const { eyes, brows, mouth, accessory, prop, blush, flip, tile } = e
      return { eyes, brows, mouth, accessory, prop, blush, flip, tile }
    }
    expect(traits('oncall')).toEqual({
      eyes: 'cross',
      brows: 'none',
      mouth: 'oh',
      accessory: 'none',
      prop: 'bolt',
      blush: false,
      flip: false,
      tile: '#ee7e4f',
    })
    expect(traits('kirocrew')).toEqual({
      eyes: 'wide',
      brows: 'angry',
      mouth: 'cat',
      accessory: 'cap',
      prop: 'term',
      blush: false,
      flip: true,
      tile: '#eeae4f',
    })
    expect(traits('mochi')).toEqual({
      eyes: 'sparkle',
      brows: 'raised',
      mouth: 'smile',
      accessory: 'none',
      prop: 'glass',
      blush: false,
      flip: false,
      tile: '#21a5de',
    })
  })

  it('names every key its pick lists can produce', () => {
    // A typo in a pick list would silently render a part as an empty string.
    const seen = { eyes: new Set(), brows: new Set(), mouth: new Set(), accessory: new Set(), prop: new Set() }
    for (let i = 0; i < 400; i++) {
      const t = createAvatar(kiroGhost, { seed: `seed-${i}` }).toJson().extra as Record<string, string>
      seen.eyes.add(t.eyes)
      seen.brows.add(t.brows)
      seen.mouth.add(t.mouth)
      seen.accessory.add(t.accessory)
      seen.prop.add(t.prop)
    }
    for (const k of seen.eyes) expect(EYES).toHaveProperty(k as string)
    for (const k of seen.brows) expect(BROWS).toHaveProperty(k as string)
    for (const k of seen.mouth) expect(MOUTHS).toHaveProperty(k as string)
    for (const k of seen.accessory) expect({ ...ACCESSORIES, none: '' }).toHaveProperty(k as string)
    for (const k of seen.prop) expect(PROPS).toHaveProperty(k as string)
  })
})
