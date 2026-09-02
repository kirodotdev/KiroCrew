/**
 * Client-side font detection for the terminal font picker.
 *
 * Both paths run in the browser on purpose. The built-in terminal renders with
 * xterm.js, so glyphs are rasterized by the machine VIEWING the dashboard, not by
 * the gateway host that owns the pty — enumerating fonts server-side would list
 * the wrong machine's font book whenever the dashboard is reached over a tunnel
 * from another computer, and the two cases are indistinguishable from the server
 * (both see a loopback origin).
 *
 * `detectInstalledFonts` needs no permission: it asks the text measurer whether a
 * NAMED family changes the advance width of a probe string, which only happens
 * when the family actually resolved. It can only answer about names it is given,
 * hence the candidate list.
 *
 * `queryLocalMonospaceFonts` is the Local Font Access API: the real font book,
 * but Chromium-only, secure-context-only, and behind a permission prompt — so it
 * is opt-in behind a user gesture rather than something the panel does on mount.
 *
 * Measurement and the window are injected with a default, mirroring
 * `isScreenSnipSupported`: the test environment's canvas stub returns a width
 * derived from the string length alone and never reads `ctx.font`, so a probe
 * hard-wired to the real canvas would be untestable in both directions.
 */

import { compareText } from '../i18n/format'
import { FONT_PROBE_TEXT, MONO_FONT_CANDIDATES } from './monoFontCandidates'

/**
 * Generic families a candidate is compared against.
 *
 * Three, not one: a family that happens to BE the platform's default monospace
 * measures identically to `monospace` while still differing from `serif`, so
 * comparing against a single generic would report the most common terminal fonts
 * as missing. Any single difference proves the name resolved to a real family.
 */
const GENERIC_FAMILIES = ['monospace', 'serif', 'sans-serif'] as const

/** The fallback a terminal font stack must end in — a grid needs fixed width. */
const MONOSPACE_GENERIC = GENERIC_FAMILIES[0]

/** Large enough that per-glyph rounding differences survive as whole pixels. */
const PROBE_SIZE_PX = 72

/** Advance-width difference treated as real rather than float noise. */
const WIDTH_EPSILON = 0.01

/** Narrow and wide glyph runs whose widths agree only in a fixed-width family. */
const NARROW_RUN = 'iiiiiiiiii'
const WIDE_RUN = 'WWWWWWWWWW'

/**
 * Measures a string's advance width in a CSS font stack, or null where text
 * measurement is unavailable.
 */
export type MeasureText = (fontStack: string, text: string) => number | null

let sharedContext: CanvasRenderingContext2D | null | undefined

/**
 * Shared offscreen measuring context, or null where 2D canvas is unavailable (a
 * hardened browser). `undefined` marks "not asked yet" so the lookup happens once
 * per page rather than once per candidate.
 */
function getSharedContext(): CanvasRenderingContext2D | null {
  if (sharedContext !== undefined) return sharedContext
  try {
    sharedContext = document.createElement('canvas').getContext('2d')
  } catch {
    sharedContext = null
  }
  return sharedContext
}

/** Canvas-backed measurement — the production `MeasureText`. */
export const defaultMeasure: MeasureText = (fontStack, text) => {
  const ctx = getSharedContext()
  if (!ctx) return null
  ctx.font = `${PROBE_SIZE_PX}px ${fontStack}`
  return ctx.measureText(text).width
}

/**
 * Quote a family name for a CSS/canvas font shorthand.
 *
 * Always quoted rather than only when spaced: a bare multi-word name parses as
 * several family tokens, and an unquoted name colliding with a CSS keyword
 * (`monospace`, `inherit`) would be read as that keyword instead of a family.
 */
export function cssFontFamilyToken(family: string): string {
  return `'${family.replace(/\\/g, '\\\\').replace(/'/g, "\\'")}'`
}

/**
 * CSS font stack that renders `family` and degrades to the platform monospace.
 *
 * The generic tail is not optional for a terminal: xterm feeds this straight into
 * a canvas `font` shorthand, so a name that does not resolve must land on a
 * fixed-width family rather than a proportional default.
 */
export function monospaceFontStack(family: string): string {
  return `${cssFontFamilyToken(family)}, ${MONOSPACE_GENERIC}`
}

/**
 * Whether a family name resolves to a font installed on this machine.
 *
 * False when measurement is unavailable, which degrades the picker to "type a
 * name" rather than claiming every font is missing — the latter would hide the
 * font the user already selected.
 */
export function isFontInstalled(family: string, measure: MeasureText = defaultMeasure): boolean {
  const name = family.trim()
  if (!name) return false
  const token = cssFontFamilyToken(name)
  for (const generic of GENERIC_FAMILIES) {
    const baseline = measure(generic, FONT_PROBE_TEXT)
    const candidate = measure(`${token}, ${generic}`, FONT_PROBE_TEXT)
    if (baseline === null || candidate === null) return false
    if (Math.abs(candidate - baseline) > WIDTH_EPSILON) return true
  }
  return false
}

/**
 * Whether an INSTALLED family advances every glyph by the same width.
 *
 * Only meaningful for a family that resolved: a missing name falls back to the
 * generic in the stack, and that generic is `monospace`, which would report every
 * uninstalled font as fixed-width. Callers gate on `isFontInstalled` first, or
 * pass names the font book itself supplied.
 */
export function isMonospaceFamily(family: string, measure: MeasureText = defaultMeasure): boolean {
  const name = family.trim()
  if (!name) return false
  const stack = monospaceFontStack(name)
  const narrow = measure(stack, NARROW_RUN)
  const wide = measure(stack, WIDE_RUN)
  if (narrow === null || wide === null) return false
  return Math.abs(narrow - wide) <= WIDTH_EPSILON
}

/**
 * Candidate families that are actually installed, in candidate-list order.
 *
 * Synchronous and cheap — one shared canvas, at most six measurements per name —
 * but still run off the render path by the caller, because the first call also
 * forces the browser to resolve every name.
 */
export function detectInstalledFonts(
  candidates: readonly string[] = MONO_FONT_CANDIDATES,
  measure: MeasureText = defaultMeasure,
): string[] {
  return candidates.filter(name => isFontInstalled(name, measure))
}

interface LocalFontDataLike {
  family?: string
}

type QueryLocalFontsFn = () => Promise<LocalFontDataLike[]>

interface WindowLike {
  queryLocalFonts?: unknown
}

const defaultWindow = (): WindowLike | undefined =>
  typeof window !== 'undefined' ? (window as WindowLike) : undefined

/**
 * Whether this browser exposes the Local Font Access API at all.
 *
 * False on Firefox and Safari, and on any origin that is not a secure context —
 * which is exactly the "dashboard reached over plain HTTP from another machine"
 * case. The picker hides the enumeration affordance rather than offering a button
 * that can only fail.
 */
export function isLocalFontAccessSupported(win: WindowLike | undefined = defaultWindow()): boolean {
  return typeof win?.queryLocalFonts === 'function'
}

export type LocalFontQuery =
  | { ok: true; families: string[] }
  | { ok: false; reason: 'unsupported' | 'denied' }

/**
 * Enumerate the machine's monospace font families via the Local Font Access API.
 *
 * MUST be called from a user gesture: the permission prompt requires transient
 * activation, so calling it on mount fails. A rejection covers both an explicit
 * block and a dismissed prompt — indistinguishable from each other, and neither
 * is fatal, because the probed candidate list remains valid.
 */
export async function queryLocalMonospaceFonts(
  win: WindowLike | undefined = defaultWindow(),
  measure: MeasureText = defaultMeasure,
): Promise<LocalFontQuery> {
  if (!isLocalFontAccessSupported(win)) return { ok: false, reason: 'unsupported' }
  let fonts: LocalFontDataLike[]
  try {
    fonts = await (win!.queryLocalFonts as QueryLocalFontsFn).call(win)
  } catch {
    return { ok: false, reason: 'denied' }
  }
  // One family covers many faces (regular, bold, italic); the picker names
  // families, so the styles collapse into one row.
  const families = new Set<string>()
  for (const font of fonts) {
    const family = typeof font?.family === 'string' ? font.family.trim() : ''
    if (family) families.add(family)
  }
  return {
    ok: true,
    // Ordered for reading, so the collation follows the app's language rather
    // than whatever locale the browser happens to be set to.
    families: Array.from(families)
      .filter(name => isMonospaceFamily(name, measure))
      .sort(compareText),
  }
}

/** Test-only: drop the memoized measuring context. */
export function __resetFontProbe(): void {
  sharedContext = undefined
}
