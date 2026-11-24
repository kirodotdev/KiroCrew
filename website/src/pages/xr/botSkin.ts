/**
 * A Bot's skin: matte, as cloth is. No highlight spot and no white edge.
 *
 * The body is drawn on glasses that add light to a room, where a dark pixel is a hole in the body. So:
 *   1. the body is the Bot's colour, lifted until it gives enough light to stand in front of a room;
 *   2. the form is told by soft changes of colour, not by darkness: a little paler toward the light, a little
 *      deeper in its own hue away from it;
 *   3. the edge is a wide soft band in the Bot's own colour, paler than the body: the light that the fibres of
 *      cloth catch where the surface turns away from the eye. It is what holds the outline on a room.
 * All three are one small picture of a lit ball (a matcap) for each colour: no light in the scene, one texture read.
 */
import { CanvasTexture, MeshMatcapMaterial, SRGBColorSpace } from 'three'
import { CLOTH_KEY, foldInto } from './botShader'

type Rgb = [number, number, number]

const srgb2lin = (c: number) => (c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4))
const lin2srgb = (c: number) => (c <= 0.0031308 ? c * 12.92 : 1.055 * Math.pow(c, 1 / 2.4) - 0.055)
const clamp01 = (x: number) => Math.min(1, Math.max(0, x))
const smooth = (a: number, b: number, x: number) => { const t = clamp01((x - a) / (b - a)); return t * t * (3 - 2 * t) }
const rgbOf = (n: number): Rgb => [srgb2lin(((n >> 16) & 255) / 255), srgb2lin(((n >> 8) & 255) / 255), srgb2lin((n & 255) / 255)]

/** Lightness, chroma and hue in OKLCH, so that every tint is treated alike: a yellow and a blue get the same lift. */
export function toOklch([r, g, b]: Rgb): Rgb {
  const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b)
  const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b)
  const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b)
  const L = 0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s
  const A = 1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s
  const B = 0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s
  return [L, Math.hypot(A, B), Math.atan2(B, A)]
}
function fromOklch([L, C, h]: Rgb): Rgb {
  const A = C * Math.cos(h), B = C * Math.sin(h)
  const l = (L + 0.3963377774 * A + 0.2158037573 * B) ** 3
  const m = (L - 0.1055613458 * A - 0.0638541728 * B) ** 3
  const s = (L - 0.0894841775 * A - 1.291485548 * B) ** 3
  return [4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s, -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s, -0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s]
}
/** A colour of this hue at a chosen lightness, with as much of the wanted chroma as a screen can show. Linear RGB. */
function shade(h: number, L: number, C: number): Rgb {
  for (let i = 0; i < 40; i += 1) {
    const rgb = fromOklch([L, C, h])
    if (rgb.every(v => v >= -1e-4 && v <= 1 + 1e-4)) return rgb.map(clamp01) as Rgb
    C *= 0.94
  }
  return fromOklch([L, 0, 0]).map(clamp01) as Rgb
}

/** The matte cloth. */
export const CLOTH = {
  size: 128,
  // The body's middle tone, from the tile's own lightness (OKLCH): the body must give light, and a dark tile
  // still gives a deeper body than a light one.
  midBase: 0.50, midPerL: 0.36,
  // The form is told softly.
  litUp: 0.05, shadeDown: 0.06,
  midChroma: 1.15, litChroma: 1.0, shadeChroma: 1.2,
  litHue: 0.06, shadeHue: -0.06,
  light: [-0.25, 0.80, 0.55],   // soft light from above, a little from the left
  wrap: 0.8,                    // the light goes nearly all the way round: no terminator
  // The edge: a band of the fuzz colour, from `fuzzFrom` of the ball's radius out to its rim. The fuzz is a
  // little lighter than the middle tone and as full of colour as a screen can show; much lighter than that a
  // screen has little colour left, and the body looks frosted.
  fuzzFrom: 0.62, fuzzPower: 1.2, fuzzGain: 0.9, fuzzUp: 0.12, fuzzMax: 0.93, fuzzChroma: 1.0,
  // The body's points carry a shade for folds. Cloth keeps this share of it: a deep fold is a hole on glasses.
  fold: 0.6,
} as const

/** The colours a tint gives the cloth, in linear RGB. */
export function clothTones(tint: number, k: typeof CLOTH = CLOTH): { mid: Rgb; lit: Rgb; shade: Rgb; fuzz: Rgb } {
  const [L, C, h] = toOklch(rgbOf(tint))
  const mid = Math.min(0.9, k.midBase + k.midPerL * L)
  return {
    mid: shade(h, mid, C * k.midChroma),
    lit: shade(h + k.litHue, Math.min(0.96, mid + k.litUp), C * k.litChroma),
    shade: shade(h + k.shadeHue, mid - k.shadeDown, C * k.shadeChroma),
    fuzz: shade(h, Math.min(k.fuzzMax, mid + k.fuzzUp), C * k.fuzzChroma),
  }
}

/** The picture of a ball of this cloth: soft light, no highlight, and the fuzz at its edge. */
export function clothCanvas(tint: number, k: typeof CLOTH = CLOTH): HTMLCanvasElement {
  const n = k.size, c = document.createElement('canvas')
  c.width = c.height = n
  const ctx = c.getContext('2d')
  // A browser with no working 2D canvas leaves the picture blank; the body is then flat, not broken.
  if (!ctx) return c
  const tone = clothTones(tint, k), img = ctx.createImageData(n, n)
  const ll = Math.hypot(...k.light), L = k.light.map(v => v / ll)
  for (let py = 0; py < n; py += 1) for (let px = 0; px < n; px += 1) {
    let x = (px + 0.5) / n * 2 - 1, y = 1 - (py + 0.5) / n * 2, r = Math.hypot(x, y)
    if (r > 0.999) { x *= 0.999 / r; y *= 0.999 / r; r = 0.999 }
    const z = Math.sqrt(1 - r * r)
    // From -1 (away from the light) through 0 to 1 (toward it).
    const t = smooth(0, 1, (x * L[0] + y * L[1] + z * L[2] + k.wrap) / (1 + k.wrap)) * 2 - 1
    const e = Math.pow(smooth(k.fuzzFrom, 1, r), k.fuzzPower) * k.fuzzGain
    const i = (py * n + px) * 4
    for (let ch = 0; ch < 3; ch += 1) {
      let v = t >= 0 ? tone.mid[ch] + (tone.lit[ch] - tone.mid[ch]) * t : tone.mid[ch] + (tone.shade[ch] - tone.mid[ch]) * -t
      v += (tone.fuzz[ch] - v) * e
      img.data[i + ch] = Math.round(lin2srgb(clamp01(v)) * 255)
    }
    img.data[i + 3] = 255
  }
  ctx.putImageData(img, 0, 0)
  return c
}

/**
 * The tone that the ink of a Bot's flat art is drawn in on the body: near white, with a breath of the Bot's own
 * hue. The art's ink is black, which on glasses that add light is a hole in the body. Linear RGB.
 */
export function wornInk(tint: number): Rgb {
  const [, C, h] = toOklch(rgbOf(tint))
  return shade(h, INK_LIGHT, Math.min(C, INK_CHROMA))
}
/** How light the ink is drawn (OKLCH), and the most colour it takes from the Bot. */
export const INK_LIGHT = 0.96
export const INK_CHROMA = 0.035

/** The skin for one colour: three's matcap material, so it draws with no light and in one draw call. */
export function cloth(tint: number): MeshMatcapMaterial {
  const matcap = new CanvasTexture(clothCanvas(tint))
  matcap.colorSpace = SRGBColorSpace
  const m = new MeshMatcapMaterial({ matcap, vertexColors: true })
  m.onBeforeCompile = shader => foldInto(shader, CLOTH.fold)
  m.customProgramCacheKey = () => CLOTH_KEY
  return m
}
