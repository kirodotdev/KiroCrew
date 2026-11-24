import { describe, expect, it } from 'vitest'
import { INK_CLEAR, INK_DARK, paleInkInto } from './botShader'
import { INK_LIGHT, clothTones, toOklch, wornInk } from './botSkin'

const TINTS = { lilac: 0x8f7bff, green: 0x2fd66b, blue: 0x35b6f2, orange: 0xf2a23a, pink: 0xf2668b, grey: 0xa9b1c0 }
const lin2srgb = (c: number) => (c <= 0.0031308 ? c * 12.92 : 1.055 * Math.pow(c, 1 / 2.4) - 0.055)
/** How far two hues are apart, in radians, round the circle. */
const hueGap = (a: number, b: number) => Math.abs(Math.atan2(Math.sin(a - b), Math.cos(a - b)))

describe('the ink of what a Bot wears, in XR', () => {
  it.each(Object.entries(TINTS))('is drawn pale for a %s Bot: near white, never a dark pixel', (_name, tint) => {
    const ink = wornInk(tint)
    const [L] = toOklch(ink)
    expect(L).toBeCloseTo(INK_LIGHT, 2)
    // On a screen every part of it is brighter than 215 of 255: glasses that add light show it as light.
    for (const part of ink) expect(lin2srgb(part) * 255).toBeGreaterThan(215)
  })

  it.each(Object.entries(TINTS).filter(([name]) => name !== 'grey'))('keeps a breath of the %s Bot\'s own hue', (_name, tint) => {
    const [, C, h] = toOklch(wornInk(tint))
    const [, , hue] = toOklch([((tint >> 16) & 255) / 255, ((tint >> 8) & 255) / 255, (tint & 255) / 255].map(c => (c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4))) as [number, number, number])
    expect(C).toBeGreaterThan(0.01)
    expect(C).toBeLessThan(0.05)
    expect(hueGap(h, hue)).toBeLessThan(0.2)
  })

  it('is paler than the body it lies on, so a band of it still reads on the skin', () => {
    for (const tint of Object.values(TINTS)) {
      const [inkL] = toOklch(wornInk(tint))
      const [bodyL] = toOklch(clothTones(tint).mid)
      expect(inkL - bodyL).toBeGreaterThan(0.05)
    }
  })
})

describe('the ink in the worn layer\'s shader', () => {
  const FRAGMENT = '#include <common>\nvoid main() {\n\tvec4 diffuseColor = vec4( 1.0 );\n\t#include <map_fragment>\n}'

  it('goes in once, right after the art\'s picture is read, with the pale tone as a uniform', () => {
    const shader = { fragmentShader: FRAGMENT }
    expect(paleInkInto(shader)).toBe(true)
    expect(shader.fragmentShader.match(/uniform vec3 wearInk;/g)).toHaveLength(1)
    const lines = shader.fragmentShader.split('\n')
    const read = lines.findIndex(l => l.includes('#include <map_fragment>'))
    expect(lines[read + 1]).toContain('mix( wearInk, diffuseColor.rgb')
  })

  it('takes ink whole and leaves a colour of the art alone', () => {
    // The shader's own rule, worked by hand: how much of its own colour a pixel keeps, by its brightest part.
    const keeps = (brightest: number) => { const t = Math.min(1, Math.max(0, (brightest - INK_DARK) / (INK_CLEAR - INK_DARK))); return t * t * (3 - 2 * t) }
    expect(keeps(0)).toBe(0)
    // The darkest colour the dashboard's art uses beside ink is far brighter than this in one of its parts.
    expect(keeps(0.2)).toBe(1)
    expect(keeps(1)).toBe(1)
    expect(INK_DARK).toBeLessThan(INK_CLEAR)
  })

  it('leaves a shader alone that reads no picture', () => {
    const shader = { fragmentShader: 'void main() {}' }
    expect(paleInkInto(shader)).toBe(false)
    expect(shader.fragmentShader).toBe('void main() {}')
  })
})
