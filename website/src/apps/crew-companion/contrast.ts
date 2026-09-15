/**
 * Colour contrast maths — pure, so the rules can be unit-tested without a DOM.
 *
 * Needed because the panel follows the user's Kiro Crew theme rather than a palette
 * we control. That is the right behaviour, but it means readability can no longer be
 * guaranteed at build time: measured against the app's own `kiro-dark` fallback, the
 * accent purple as TEXT sits at 3.57:1 on the card and 2.58:1 on a tinted accent
 * pill — both below the 4.5:1 WCAG AA threshold for normal text.
 *
 * So instead of choosing between "matches the theme" and "is readable", the panel
 * measures the theme's own colours at runtime and only uses the accent for text
 * where it actually passes, falling back to the primary text colour where it does
 * not. Every theme keeps its character; no theme gets unreadable labels.
 */

export interface Rgb { r: number; g: number; b: number; a: number }

/**
 * Parse the colour formats `getComputedStyle` actually returns, plus hex for
 * literals used in code. Returns null for anything unrecognised (e.g. a named
 * colour or a colour function), so callers fall back rather than guessing.
 */
export function parseCssColor(input: string): Rgb | null {
  const s = String(input || '').trim()
  if (!s) return null

  const hex = s.match(/^#([0-9a-f]{3}|[0-9a-f]{6})$/i)
  if (hex) {
    const h = hex[1]
    const full = h.length === 3 ? h.split('').map(c => c + c).join('') : h
    return {
      r: parseInt(full.slice(0, 2), 16),
      g: parseInt(full.slice(2, 4), 16),
      b: parseInt(full.slice(4, 6), 16),
      a: 1,
    }
  }

  const fn = s.match(/^rgba?\(\s*([^)]+)\)$/i)
  if (fn) {
    // Accepts both comma and space separated forms.
    const parts = fn[1].split(/[,\s/]+/).filter(Boolean).map(Number)
    if (parts.length < 3 || parts.slice(0, 3).some(n => !Number.isFinite(n))) return null
    const a = parts.length >= 4 && Number.isFinite(parts[3]) ? parts[3] : 1
    return { r: parts[0], g: parts[1], b: parts[2], a }
  }

  return null
}

/**
 * Flatten a translucent colour onto an opaque background.
 *
 * Theme accents are often given as rgba tints (the fallback's accent pill is
 * `rgba(142,72,255,0.3)`), and a contrast ratio computed from the raw rgba would
 * describe a colour nobody can see.
 */
export function compositeOver(fg: Rgb, bg: Rgb): Rgb {
  const a = Math.max(0, Math.min(1, fg.a))
  return {
    r: a * fg.r + (1 - a) * bg.r,
    g: a * fg.g + (1 - a) * bg.g,
    b: a * fg.b + (1 - a) * bg.b,
    a: 1,
  }
}

function channel(v: number): number {
  const c = Math.max(0, Math.min(255, v)) / 255
  return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4)
}

/** WCAG relative luminance. */
export function luminance(c: Rgb): number {
  return 0.2126 * channel(c.r) + 0.7152 * channel(c.g) + 0.0722 * channel(c.b)
}

/** WCAG contrast ratio, 1..21. Translucent inputs are flattened onto each other. */
export function contrastRatio(fg: Rgb, bg: Rgb): number {
  const base = bg.a < 1 ? compositeOver(bg, { r: 255, g: 255, b: 255, a: 1 }) : bg
  const top = fg.a < 1 ? compositeOver(fg, base) : fg
  const l1 = luminance(top)
  const l2 = luminance(base)
  const hi = Math.max(l1, l2)
  const lo = Math.min(l1, l2)
  return (hi + 0.05) / (lo + 0.05)
}

/** WCAG AA for normal-size text. */
export const AA_TEXT = 4.5

/**
 * Choose between a preferred colour and a safe one, by measured contrast.
 *
 * Returns `preferred` when it clears `target` against `bg`, otherwise `fallback`.
 * Unparseable input yields `fallback`: an unknown colour is treated as unsafe
 * rather than assumed fine.
 */
export function pickReadable(
  preferred: string, bg: string, fallback: string, target = AA_TEXT,
): string {
  const fg = parseCssColor(preferred)
  const back = parseCssColor(bg)
  if (!fg || !back) return fallback
  return contrastRatio(fg, back) >= target ? preferred : fallback
}
