/**
 * Colour maths for the canvas's custom colours: hex parsing, HSV, the
 * darker shade a border takes from its fill, and the ink that reads on a colour.
 * Pure.
 */

export interface Rgb { r: number; g: number; b: number }
/** Hue 0-360, saturation and value 0-1. */
export interface Hsv { h: number; s: number; v: number }

/** A colour as `#RRGGBB` (upper case) from `#RGB`, `#RRGGBB`, with or without the `#`; anything else is undefined. */
export function toHex(v: unknown): string | undefined {
  if (typeof v !== 'string') return undefined
  let t = v.trim().replace(/^#/, '')
  if (/^[0-9a-f]{3}$/i.test(t)) t = t.split('').map(c => c + c).join('')
  return /^[0-9a-f]{6}$/i.test(t) ? `#${t.toUpperCase()}` : undefined
}

export function hexToRgb(hex: string): Rgb {
  const n = parseInt(hex.replace('#', ''), 16)
  return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 }
}

export function rgbToHex({ r, g, b }: Rgb): string {
  const part = (x: number) => Math.max(0, Math.min(255, Math.round(x))).toString(16).padStart(2, '0')
  return `#${part(r)}${part(g)}${part(b)}`.toUpperCase()
}

export function rgbToHsv({ r, g, b }: Rgb): Hsv {
  const R = r / 255
  const G = g / 255
  const B = b / 255
  const max = Math.max(R, G, B)
  const d = max - Math.min(R, G, B)
  let h = 0
  if (d > 0) {
    if (max === R) h = ((G - B) / d) % 6
    else if (max === G) h = (B - R) / d + 2
    else h = (R - G) / d + 4
    h = (h * 60 + 360) % 360
  }
  return { h, s: max === 0 ? 0 : d / max, v: max }
}

export function hsvToRgb({ h, s, v }: Hsv): Rgb {
  const c = v * s
  const x = c * (1 - Math.abs(((h / 60) % 2) - 1))
  const m = v - c
  const [r, g, b] = h < 60 ? [c, x, 0] : h < 120 ? [x, c, 0] : h < 180 ? [0, c, x] : h < 240 ? [0, x, c] : h < 300 ? [x, 0, c] : [c, 0, x]
  return { r: (r + m) * 255, g: (g + m) * 255, b: (b + m) * 255 }
}

export const hexToHsv = (hex: string): Hsv => rgbToHsv(hexToRgb(hex))
export const hsvToHex = (hsv: Hsv): string => rgbToHex(hsvToRgb(hsv))

/**
 * The border a fill takes when nothing else is picked: the same colour, darker and a
 * touch richer, as FigJam's cards have. Greys and whites only get darker.
 */
export function darker(hex: string): string {
  const { h, s, v } = hexToHsv(hex)
  return hsvToHex({ h, s: Math.min(1, s * 1.08), v: v * 0.78 })
}

/** Relative luminance 0-1 (sRGB, as WCAG defines it). */
export function luminance(hex: string): number {
  const { r, g, b } = hexToRgb(hex)
  const lin = (c: number) => { const x = c / 255; return x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4 }
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)
}

/** Text colour that reads on *hex*: whichever of the canvas's dark ink and white has the better contrast (they cross at luminance 0.215). */
export const inkOn = (hex: string): string => (luminance(hex) > 0.215 ? '#1F2328' : '#FFFFFF')
