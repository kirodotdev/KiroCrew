/**
 * Map a crewmate's DASHBOARD PAGE colour tokens onto the CHAT column's CSS
 * variables, so the chat and the Dashboard tab beside it read as one theme.
 *
 * The problem this solves: a crewmate's page can declare its own colours
 * (`theme.tokens`, validated in `artifact_store/dashboard_package.py`), but the
 * chat in the middle stays on the app theme — a strongly coloured page and a
 * default-dark chat look like two different apps. This module takes the two
 * page colours the chat can safely follow and returns the variable overrides
 * the column sets inline.
 *
 * WHY ONLY TWO TOKENS, NOT THE PAGE'S WHOLE PALETTE: a page's tokens are
 * free-form custom-property names a page author picks (up to 64 of them), with
 * no guaranteed meaning. Copying arbitrary names onto the chat's own variables
 * would be guessing. The chat follows exactly the two whose role is
 * unambiguous — the page BACKGROUND (`--bg`) and the page ACCENT (`--accent`),
 * the same names the app's own theme uses — and derives everything else.
 * A page that declares neither keeps today's chat untouched.
 *
 * THE SURFACES COME FROM THE THEME GAP-FILL, NOT A SECOND RAMP: the chat column
 * is painted through many surface variables (`--card`, `--border`, `--muted`,
 * `--bg-hover`, `--bg-elevated`, …), so overriding only `--bg` would leave
 * bubbles and labels on the app theme's values. A custom theme pack that
 * declares only `--bg`/`--text`/`--accent` has the same gap, and
 * `derivedDefaultVars` (`hooks/themeCss.ts`) already fills it. The chat feeds it
 * `{--bg, --text}` and takes its map, so one step table serves both.
 *
 * What this module adds on top is only what a page-driven palette needs and a
 * pack-driven one does not:
 *   - `--text` is DERIVED, not read: black or white, whichever has more contrast
 *     on the page background (`lib/iconContrast`). Against any sRGB colour the
 *     better of the two never drops below ~4.58:1.
 *   - an AA GUARD on the gap-fill's output: a page author did not tune the
 *     background against its text, so on a mid-tone page a surface step can push
 *     `--text` below 4.5:1, or the muted mix can. A surface that fails is pinned
 *     to the background; a muted colour that fails is pinned to `--text`.
 *   - an accent that is always readable: the page accent when it clears AA both
 *     as a fill (with its label) and as text on the chat surfaces, else that
 *     accent pulled toward `--text` until it does. With a recoloured background
 *     the APP accent is never left in place, since it was never measured
 *     against this background.
 *   - the composer's glass tint ladder, re-anchored on the page background.
 *
 * WHY NON-OPAQUE TOKENS ARE REFUSED: a translucent `--bg` (e.g. `rgba(0,0,0,0)`)
 * lets the app background show through, so a contrast measured against the token
 * alone is a fiction. A token the chat cannot paint a solid surface from is
 * treated as absent, and the chat keeps its own theme.
 */
import { contrastRatio, parseCssColor, relativeLuminance } from '../../lib/iconContrast'
import { derivedDefaultVars } from '../../hooks/themeCss'

/**
 * The page colour tokens the chat follows, keyed by CSS custom-property name as
 * the server's `page_theme` emits them. Free-form elsewhere; the chat reads
 * exactly these two.
 */
export interface PageColorTokens {
  /** The page's `--bg`: the surface the chat paints its background from. */
  '--bg'?: string
  /** The page's `--accent`: the chat's accent (caret, links, send). */
  '--accent'?: string
}

/** The chat column's CSS-variable overrides, ready to spread onto an element's style. */
export type ChatThemeVars = Record<string, string>

/** WCAG AA for normal text; stated here so the contract is visible at the call site. */
export const CHAT_AA_TEXT = 4.5

/** The two ends of the tone scale, as readable foregrounds. */
const WHITE = '#ffffff'
const BLACK = '#000000'

/** Surfaces normal `--text` is drawn on, from the gap-fill's output. */
const TEXT_SURFACES = [
  '--card', '--bg-elevated', '--chrome', '--bg-accent', '--panel',
  '--bg-hover', '--card-hl', '--panel-strong',
] as const

/** Secondary foregrounds that must still clear AA on every text surface. */
const SECONDARY_TEXT = ['--muted', '--muted-strong'] as const

/** Base status hues for text on a dark / light page; each is then pulled toward `--text` until it clears AA. */
const STATUS_ON_DARK: Record<string, string> = { ok: '#4ade80', warn: '#fbbf24', danger: '#f87171', info: '#60a5fa' }
const STATUS_ON_LIGHT: Record<string, string> = { ok: '#166534', warn: '#92400e', danger: '#b91c1c', info: '#1d4ed8' }

interface Readable { color: string; ratio: number }
interface Rgb { r: number; g: number; b: number }

/**
 * A channel clamped to the 0–255 the browser actually paints. A page token can
 * be a valid-but-out-of-range CSS colour (`rgb(0,0,500)`), which `parseCssColor`
 * returns verbatim; measuring contrast against the raw channel describes a
 * colour the screen never shows, so every channel is clamped before luminance.
 */
function clampCh(n: number): number {
  return Math.max(0, Math.min(255, n))
}

/** WCAG relative luminance of a triple, clamped to what the browser paints. */
function lumOf(c: Rgb): number {
  return relativeLuminance(clampCh(c.r), clampCh(c.g), clampCh(c.b))
}

/** Luminance of an opaque CSS colour string, or `null`. */
function lumOfStr(s: string | undefined): number | null {
  const c = s ? parseCssColor(s) : null
  return c && c.a >= 0.999 ? lumOf(c) : null
}

/**
 * The readable text colour for an OPAQUE background triple: black or white,
 * whichever has the higher contrast, with the ratio so a caller can refuse a
 * surface whose best text still falls short.
 */
function readableFor(c: Rgb): Readable {
  const bg = lumOf(c)
  const onWhite = contrastRatio(relativeLuminance(255, 255, 255), bg)
  const onBlack = contrastRatio(0, bg)
  return onWhite >= onBlack ? { color: WHITE, ratio: onWhite } : { color: BLACK, ratio: onBlack }
}

/**
 * The readable text colour for a CSS colour string, or `null` when it does not
 * parse or is not opaque — an unknown or see-through colour is unusable, not
 * guessed. Exported for the unit test.
 */
export function readableText(bg: string): Readable | null {
  const c = parseCssColor(bg)
  if (!c || c.a < 0.999) return null
  return readableFor(c)
}

/** `#rrggbb` for *a* mixed toward *b* by *t* (0 = a, 1 = b). */
function mixHex(a: Rgb, b: Rgb, t: number): string {
  const ch = (x: number, y: number) => clampCh(Math.round(clampCh(x) + (clampCh(y) - clampCh(x)) * t)).toString(16).padStart(2, '0')
  return '#' + ch(a.r, b.r) + ch(a.g, b.g) + ch(a.b, b.b)
}

/**
 * The chat column's variable overrides for a crewmate's page colours, or an
 * EMPTY object when the page drives nothing (no background token, a non-opaque
 * or unparseable one, or a background whose best text cannot clear 4.5:1 — the
 * fail-safe is today's look, never unreadable text).
 *
 * An empty result is the default-OFF state expressed as data: the column
 * spreads `{}`, which adds no inline variable, so the chat is byte-for-byte the
 * one on `main`.
 *
 * An accent-only page (no background) is also `{}`: the accent would land as
 * link text on the APP theme's surfaces, which this mapper cannot measure, so
 * links stay on the app accent, which is already readable there.
 */
export function chatThemeVars(tokens: PageColorTokens | null | undefined): ChatThemeVars {
  if (!tokens) return {}
  const bgStr = tokens['--bg']
  const bg = bgStr ? parseCssColor(bgStr) : null
  if (!bgStr || !bg || bg.a < 0.999) return {}
  const text = readableFor(bg)
  if (text.ratio < CHAT_AA_TEXT) return {}

  // The surfaces and secondary text, from the theme gap-fill.
  const derived = derivedDefaultVars({ '--bg': bgStr, '--text': text.color })
  // A background the gap-fill could not mix (its sanitizer or opacity gate
  // refused it) leaves no surfaces: keep the app look rather than half a theme.
  if (!derived['--card']) return {}
  const vars: ChatThemeVars = { '--bg': bgStr, '--text': text.color, ...derived }

  // AA guard on the gap-fill's output (see the module header).
  const bgL = lumOf(bg)
  const textL = text.color === WHITE ? 1 : 0
  for (const k of TEXT_SURFACES) {
    const L = lumOfStr(vars[k])
    if (L !== null && contrastRatio(L, textL) < CHAT_AA_TEXT) vars[k] = bgStr
  }
  const surfaceLums = [bgL, ...TEXT_SURFACES.map((k) => lumOfStr(vars[k])).filter((L): L is number => L !== null)]
  const clearsAll = (L: number) => surfaceLums.every((sL) => contrastRatio(L, sL) >= CHAT_AA_TEXT)
  for (const k of SECONDARY_TEXT) {
    const L = lumOfStr(vars[k])
    if (L === null || !clearsAll(L)) vars[k] = text.color
  }

  // The accent. The page's own when it clears AA as a fill (with its label)
  // AND as text on every chat surface; otherwise it is pulled toward `--text`
  // until it does — the last step IS `--text`, which always clears. With no
  // page accent the pull starts from `--text` directly. The app accent is never
  // kept here: it was measured against the app background, not this one.
  const textRgb: Rgb = text.color === WHITE ? { r: 255, g: 255, b: 255 } : { r: 0, g: 0, b: 0 }
  const accentStr = tokens['--accent']
  const pa = accentStr ? parseCssColor(accentStr) : null
  const pageAccent: Rgb | null = pa && pa.a >= 0.999 ? pa : null
  let accent = text.color
  let label = readableFor(textRgb).color
  for (const t of pageAccent ? [0, 0.25, 0.5, 0.75] : []) {
    const candidate = t === 0 ? accentStr! : mixHex(pageAccent!, textRgb, t)
    const c = parseCssColor(candidate)!
    const lab = readableFor(c)
    if (lab.ratio >= CHAT_AA_TEXT && clearsAll(lumOf(c))) {
      accent = candidate
      label = lab.color
      break
    }
  }
  const ac = parseCssColor(accent)!
  const labelRgb: Rgb = label === WHITE ? { r: 255, g: 255, b: 255 } : { r: 0, g: 0, b: 0 }
  vars['--accent'] = accent
  vars['--accent-fg'] = label
  // The hover and subtle tints follow the chosen accent too, or the main action
  // reverts to the APP accent on hover. A small step toward its own label keeps
  // the hover distinct and `--accent-fg` readable; subtle is the accent at low alpha.
  vars['--accent-hover'] = mixHex(ac, labelRgb, 0.12)
  vars['--accent-subtle'] = `rgba(${clampCh(ac.r)},${clampCh(ac.g)},${clampCh(ac.b)},0.2)`

  // Status colours. Error, warning and success text is drawn from the app's
  // `--danger` / `--warn` / `--ok` / `--info` (and the `-text` twins, which the
  // root rule resolves from the APP values, so they are set here too), measured
  // against the app background and not this page's. Each gets a base hue for the
  // text pole and is pulled toward `--text` until it clears AA on every chat
  // surface, the same walk the accent takes.
  const statusBase = text.color === WHITE ? STATUS_ON_DARK : STATUS_ON_LIGHT
  for (const k of Object.keys(statusBase)) {
    const base = parseCssColor(statusBase[k])!
    let color = text.color
    for (const t of [0, 0.25, 0.5, 0.75]) {
      const candidate = t === 0 ? statusBase[k] : mixHex(base, textRgb, t)
      if (clearsAll(lumOf(parseCssColor(candidate)!))) { color = candidate; break }
    }
    const c = parseCssColor(color)!
    vars['--' + k] = color
    if (k !== 'warn') vars['--' + k + '-text'] = color
    vars['--' + k + '-fg'] = readableFor(c).color
    if (k !== 'info') vars['--' + k + '-subtle'] = `rgba(${clampCh(c.r)},${clampCh(c.g)},${clampCh(c.b)},0.14)`
  }

  // The composer is a Liquid-Glass pane whose tint is a FIXED per-theme rgba
  // (`--glass-tint*`), anchored on the app's own `--bg`. Re-anchor the ladder on
  // the page background at the same alphas the theme uses, so the composer
  // paints from this palette and `--text` stays readable on it.
  const tint = (a: number) => `rgba(${clampCh(bg.r)},${clampCh(bg.g)},${clampCh(bg.b)},${a})`
  vars['--glass-tint-ultrathin'] = tint(0.72)
  vars['--glass-tint-thin'] = tint(0.78)
  vars['--glass-tint-regular'] = tint(0.84)
  vars['--glass-tint-thick'] = tint(0.9)
  vars['--glass-tint-ultrathick'] = tint(0.95)
  vars['--glass-tint-step'] = tint(0.78)
  vars['--glass-tint'] = tint(0.78)
  return vars
}
