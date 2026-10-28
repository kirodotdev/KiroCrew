/** Session color palettes and utilities. */

import { i18nT } from '../i18n/t'

export type SessionColorMode = 'tint' | 'gradient'
export type PaletteName = 'trailhead' | 'horizon' | 'voyage' | 'odyssey'
export type IntensityName = 'soft' | 'clear' | 'vivid' | 'bold' | 'intense'
export type DefaultColorSetting = number | null | 'auto'

export const PALETTE_SIZE = 7
export const PALETTE_NAMES: PaletteName[] = ['trailhead', 'horizon', 'voyage', 'odyssey']
export const INTENSITY_NAMES: IntensityName[] = ['soft', 'clear', 'vivid', 'bold', 'intense']
export const INTENSITY_THRESHOLDS: Record<IntensityName, number> = { soft: 0.035, clear: 0.07, vivid: 0.12, bold: 0.18, intense: 0.30 }

/** Parse hex (#abc or #aabbcc) or rgba(r,g,b,a) → [r,g,b] or null. */
export function parseColor(str: string): [number, number, number] | null {
  if (!str) return null
  const hex = str.match(/^#([0-9a-f]{3,8})$/i)
  if (hex) {
    const h = hex[1]
    const n = h.length <= 4
      ? parseInt(h[0]+h[0]+h[1]+h[1]+h[2]+h[2], 16)
      : parseInt(h.slice(0, 6), 16)
    return [(n >> 16) & 0xff, (n >> 8) & 0xff, n & 0xff]
  }
  const rgba = str.match(/rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/)
  if (rgba) return [+rgba[1], +rgba[2], +rgba[3]]
  return null
}

export function getContrastingTextColor(bgHex: string): string {
  const rgb = parseColor(bgHex)
  if (!rgb) {
    return '#ffffff'
  }
  const brightness = (rgb[0] * 299 + rgb[1] * 587 + rgb[2] * 114) / 1000
  return brightness > 128 ? '#100f0f' : '#ffffff'
}

/** Human-readable hue name for a swatch (e.g. "Teal", "Light blue"). Used for color-picker tooltips. */
export function colorName(hex: string): string {
  const rgb = parseColor(hex)
  if (!rgb) return hex
  const [r, g, b] = rgb.map(v => v / 255)
  const max = Math.max(r, g, b), min = Math.min(r, g, b), d = max - min
  const l = (max + min) / 2
  // Neutral test on chroma (d) directly, not HSL saturation: s = d/(1-|2l-1|)
  // is singular near l=0/l=1, so a 1-bit channel delta on a near-white/black
  // swatch blows up past any saturation threshold and gets a false hue label.
  if (d < 0.04) return l > 0.8 ? i18nT('utils.sessionColors.white') : l < 0.2 ? i18nT('utils.sessionColors.black') : i18nT('utils.sessionColors.gray')
  let h = 0
  if (d !== 0) {
    if (max === r) h = ((g - b) / d) % 6
    else if (max === g) h = (b - r) / d + 2
    else h = (r - g) / d + 4
    h = (h * 60 + 360) % 360
  }
  // 12-name hue wheel keyed on HSL hue angle (degrees).
  const hues = ['Red', 'Orange', 'Yellow', 'Lime', 'Green', 'Teal', 'Cyan', 'Sky', 'Blue', 'Indigo', 'Purple', 'Pink']
  const base = hues[Math.round(h / 30) % 12]
  if (l > 0.78) return i18nT('utils.sessionColors.light', { name: base.toLowerCase() })
  if (l < 0.32) return i18nT('utils.sessionColors.dark', { name: base.toLowerCase() })
  return base
}

function rgbToOklch(r: number, g: number, b: number): { L: number; C: number; h: number } {
  const [rl, gl, bl] = [r, g, b].map(v => {
    const s = v / 255
    return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4
  })
  const x = 0.4124 * rl + 0.3576 * gl + 0.1805 * bl
  const y = 0.2126 * rl + 0.7152 * gl + 0.0722 * bl
  const z = 0.0193 * rl + 0.1192 * gl + 0.9505 * bl
  const l_ = Math.cbrt(0.8189 * x + 0.3619 * y - 0.1289 * z)
  const m_ = Math.cbrt(0.0330 * x + 0.9293 * y + 0.0361 * z)
  const s_ = Math.cbrt(0.0482 * x + 0.2641 * y + 0.6339 * z)
  const L = 0.2105 * l_ + 0.7937 * m_ - 0.0041 * s_
  const a = 1.9780 * l_ - 2.4286 * m_ + 0.4506 * s_
  const bb = 0.0259 * l_ + 0.7828 * m_ - 0.8087 * s_
  return { L, C: Math.sqrt(a * a + bb * bb), h: (Math.atan2(bb, a) * 180 / Math.PI + 360) % 360 }
}

function oklchToHex(L: number, C: number, h: number): string {
  const hr = h * Math.PI / 180
  const a = C * Math.cos(hr), b = C * Math.sin(hr)
  const l_ = (L + 0.3964 * a + 0.2159 * b) ** 3
  const m_ = (L - 0.1056 * a - 0.0639 * b) ** 3
  const s_ = (L - 0.0895 * a - 1.2914 * b) ** 3
  const x = 1.2271 * l_ - 0.5578 * m_ + 0.2813 * s_
  const y = -0.0406 * l_ + 1.1123 * m_ - 0.0717 * s_
  const z = -0.0764 * l_ - 0.4215 * m_ + 1.5862 * s_
  const f = (v: number) => v <= 0.0031308 ? 12.92 * v : 1.055 * v ** (1 / 2.4) - 0.055
  const clamp = (v: number) => Math.max(0, Math.min(255, Math.round(f(Math.max(0, v)) * 255)))
  return '#' + [3.2406 * x - 1.5372 * y - 0.4986 * z, -0.9689 * x + 1.8758 * y + 0.0415 * z, 0.0557 * x - 0.2040 * y + 1.0570 * z]
    .map(clamp).map(v => v.toString(16).padStart(2, '0')).join('')
}

function toY(rgb: [number, number, number]): number {
  return 0.2126 * (rgb[0] / 255) ** 2.4 + 0.7152 * (rgb[1] / 255) ** 2.4 + 0.0722 * (rgb[2] / 255) ** 2.4
}

function apcaLc(text: [number, number, number], bg: [number, number, number]): number {
  const yt = toY(text), yb = toY(bg)
  if (yb > yt) {
    const s = (yb ** 0.56 - yt ** 0.57) * 1.14
    return s < 0.1 ? 0 : (s - 0.027) * 100
  }
  const s = (yb ** 0.62 - yt ** 0.62) * 1.14
  return s > -0.1 ? 0 : (s + 0.027) * 100
}

function blendRgb(fg: [number, number, number], bg: [number, number, number], pct: number): [number, number, number] {
  return [
    Math.round(fg[0] * pct + bg[0] * (1 - pct)),
    Math.round(fg[1] * pct + bg[1] * (1 - pct)),
    Math.round(fg[2] * pct + bg[2] * (1 - pct)),
  ]
}

function deltaE(rgb1: [number, number, number], rgb2: [number, number, number]): number {
  const a = rgbToOklch(...rgb1), b = rgbToOklch(...rgb2)
  const dL = a.L - b.L, dC = a.C - b.C, dh = (a.h - b.h) * Math.PI / 180
  return Math.sqrt(dL * dL + dC * dC + a.C * b.C * 2 * (1 - Math.cos(dh)))
}

function rgbToHex(rgb: [number, number, number]): string {
  return '#' + rgb.map(v => Math.max(0, Math.min(255, Math.round(v))).toString(16).padStart(2, '0')).join('')
}

function yToOklL(Y: number): number {
  // Achromatic: X = Y = Z = Y/0.9505 (D65), but for gray X≈Y≈Z
  // Use proper LMS matrix for achromatic input
  const l_ = Math.cbrt(0.8189 * Y + 0.3619 * Y + (-0.1289) * Y)
  const m_ = Math.cbrt(0.0330 * Y + 0.9293 * Y + 0.0361 * Y)
  const s_ = Math.cbrt(0.0482 * Y + 0.2641 * Y + 0.6339 * Y)
  return 0.2105 * l_ + 0.7937 * m_ - 0.0041 * s_
}

const LC_DARK = [68, 60, 52, 44, 36, 28, 20]
const LC_LIGHT = [25, 33, 41, 49, 57, 65, 73]

function contrastRampL(bgRgb: [number, number, number], isDark: boolean): number[] {
  const targets = isDark ? LC_DARK : LC_LIGHT
  return targets.map(targetLc => {
    let lo = 0, hi = 1
    for (let j = 0; j < 16; j++) {
      const mid = (lo + hi) / 2
      const gray = Math.round(mid ** (1 / 2.4) * 255)
      const lc = Math.abs(apcaLc([gray, gray, gray], bgRgb))
      if (lc < targetLc) { if (isDark) lo = mid; else hi = mid }
      else { if (isDark) hi = mid; else lo = mid }
    }
    return yToOklL((lo + hi) / 2)
  })
}

function gamutClampChroma(L: number, C: number, h: number): number {
  let lo = 0, hi = C
  for (let j = 0; j < 16; j++) {
    const mid = (lo + hi) / 2
    const hex = oklchToHex(L, mid, h)
    const back = parseColor(hex)
    if (!back) { hi = mid; continue }
    const check = rgbToOklch(...back)
    if (Math.abs(check.L - L) < 0.015 && Math.abs(check.C - mid) < 0.015) lo = mid
    else hi = mid
  }
  return (lo + hi) / 2
}

function parabolicChroma(step: number, C: number): number {
  const t = step / 6
  const minC = C * 0.3, maxC = C * 1.1
  return Math.max(0.03, minC + (maxC - minC) * (-4 * t * t + 4 * t))
}

const HARMONY_OFFSETS = [-144, -96, -48, 0, 48, 96, 144]

export function generateContrastRamp(colorStr: string, bgHex: string, hueSpread: number): string[] {
  const rgb = parseColor(colorStr)
  if (!rgb) return []
  const seed = rgbToOklch(...rgb)
  let C = seed.C
  if (C < 0.02) C = 0.04
  const bgRgb = parseColor(bgHex)
  if (!bgRgb) return []
  const isDark = rgbToOklch(...bgRgb).L < 0.5
  const rampL = contrastRampL(bgRgb, isDark)
  return rampL.map((L, i) => {
    const hue = (seed.h + hueSpread * ((i - 3) / 3) + 360) % 360
    return oklchToHex(L, parabolicChroma(i, C), hue)
  })
}

export function generateHarmony(colorStr: string, bgHex: string, useContrastL: boolean): string[] {
  const rgb = parseColor(colorStr)
  if (!rgb) return []
  const seed = rgbToOklch(...rgb)
  let C = seed.C
  if (C < 0.02) C = 0.04
  const bgRgb = parseColor(bgHex)
  if (!bgRgb) return []
  const isDark = rgbToOklch(...bgRgb).L < 0.5
  const rampL = useContrastL ? contrastRampL(bgRgb, isDark) : null
  return HARMONY_OFFSETS.map((offset, i) => {
    const hue = (seed.h + offset + 360) % 360
    const stepL = rampL ? rampL[i] : seed.L
    return oklchToHex(stepL, Math.max(0.03, gamutClampChroma(stepL, C, hue)), hue)
  })
}

export function generatePalette(colorStr: string, palette: PaletteName, bgHex?: string): string[] {
  const bg = bgHex || '#14161d'
  switch (palette) {
    case 'trailhead': return generateContrastRamp(colorStr, bg, 40)
    case 'horizon': return generateContrastRamp(colorStr, bg, 90)
    case 'voyage': return generateHarmony(colorStr, bg, false)
    case 'odyssey': return generateHarmony(colorStr, bg, true)
    default: return generateContrastRamp(colorStr, bg, 90)
  }
}

export interface PaletteBoost {
  idlePct: number[]
  activePct: number[]
  hoverPct: number[]
  mutedColors: string[]
}

function shiftToward(
  from: [number, number, number], to: [number, number, number],
  bg: [number, number, number], target: number,
): [number, number, number] | null {
  if (Math.abs(apcaLc(to, bg)) < target) return null
  let lo = 0, hi = 1
  for (let i = 0; i < 8; i++) {
    const mid = (lo + hi) / 2
    const b = from.map((v, j) => Math.round(v + mid * (to[j] - v))) as [number, number, number]
    if (Math.abs(apcaLc(b, bg)) >= target) hi = mid; else lo = mid
  }
  const p = (lo + hi) / 2
  return from.map((v, j) => Math.round(v + p * (to[j] - v))) as [number, number, number]
}

function adaptMuted(
  muted: [number, number, number], text: [number, number, number],
  textStrong: [number, number, number],
  tintedBg: [number, number, number], plainBg: [number, number, number],
): string {
  const target = Math.max(45, Math.abs(apcaLc(muted, plainBg)))
  if (Math.abs(apcaLc(muted, tintedBg)) >= target) return rgbToHex(muted)
  const r = shiftToward(muted, text, tintedBg, target)
  if (r) return rgbToHex(r)
  const rs = shiftToward(muted, textStrong, tintedBg, target)
  if (rs) return rgbToHex(rs)
  return rgbToHex(textStrong)
}

export function computePaletteBoost(
  colors: string[], bgAccentHex: string, mutedHex: string, textHex: string,
  isDark: boolean, intensity: IntensityName = 'clear', textStrongHex?: string,
): PaletteBoost {
  const bg = parseColor(bgAccentHex), muted = parseColor(mutedHex), text = parseColor(textHex)
  const textStrong = textStrongHex ? parseColor(textStrongHex) : text
  const baseIdle = isDark ? 15 : 10
  const threshold = INTENSITY_THRESHOLDS[intensity]
  const result: PaletteBoost = { idlePct: [], activePct: [], hoverPct: [], mutedColors: [] }
  for (const col of colors) {
    const fg = parseColor(col)
    if (!fg || !bg) {
      result.idlePct.push(baseIdle); result.activePct.push(35); result.hoverPct.push(isDark ? 22 : 16); result.mutedColors.push(mutedHex); continue
    }
    let idle = baseIdle
    while (idle <= 80) {
      if (deltaE(blendRgb(fg, bg, idle / 100), bg) >= threshold) break
      idle += 2
    }
    idle = Math.min(idle, 80)
    if (!muted || !text) {
      result.idlePct.push(idle); result.activePct.push(idle + 20); result.hoverPct.push(idle + 7); result.mutedColors.push(mutedHex); continue
    }
    result.idlePct.push(idle)
    result.activePct.push(idle + 20)
    result.hoverPct.push(idle + 7)
    result.mutedColors.push(adaptMuted(muted, text, textStrong || text, blendRgb(fg, bg, idle / 100), bg))
  }
  return result
}

export function resolveDefaultColor(setting: DefaultColorSetting, slotCount: number): number | null {
  if (setting === null) return null
  if (setting === 'auto') return slotCount % PALETTE_SIZE
  return setting
}
