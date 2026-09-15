/* ── Terminal ANSI palette ──
 * xterm.js renders ANSI-coloured output (prompt themes, `ls`, `git diff`, test
 * runners) with its own built-in palette unless the 16 entries are supplied.
 * They are derived here from the app theme's CSS custom properties so the
 * terminal tracks the active theme, including a user-installed theme pack, with
 * no per-theme code.
 *
 * Five of the eight base colours already carry the right meaning in every
 * shipped theme and are read directly: --danger is red, --ok is green, --warn is
 * yellow, --info is blue, and --bg / --text are the two extremes. Magenta and
 * cyan have no semantic equivalent, so they read --term-magenta / --term-cyan,
 * which the base stylesheet defines and any theme may override.
 *
 * --accent is deliberately NOT used as a hue source: it equals --ok on
 * monokai-dark and everforest, which would render two ANSI colours identically.
 */

/** Reads one CSS custom property; returns '' when the theme does not define it. */
export type VarReader = (name: string) => string

/** Fallbacks for the two hues no semantic variable carries. */
const TERM_MAGENTA_FALLBACK = '#c678dd'
const TERM_CYAN_FALLBACK = '#56b6c2'

/** How far a `bright*` entry moves from its base colour, as a 0..1 ratio. */
const BRIGHT_MIX = 0.3

/** Parsed sRGB channels in 0..255, or null when the value is not a hex colour. */
function parseHex(value: string): [number, number, number] | null {
  const hex = value.trim()
  const short = /^#([0-9a-f])([0-9a-f])([0-9a-f])$/i.exec(hex)
  if (short) {
    return [short[1], short[2], short[3]].map((c) => parseInt(c + c, 16)) as [number, number, number]
  }
  const long = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex)
  if (long) {
    return [long[1], long[2], long[3]].map((c) => parseInt(c, 16)) as [number, number, number]
  }
  return null
}

function toHex(channels: [number, number, number]): string {
  return '#' + channels.map((c) => Math.round(c).toString(16).padStart(2, '0')).join('')
}

/** Perceived luminance in 0..255; null when the value is not a hex colour. */
function luminance(value: string): number | null {
  const rgb = parseHex(value)
  if (!rgb) return null
  return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]
}

/**
 * Moves a colour towards white on a light-on-dark theme and towards black on a
 * dark-on-light one, so a `bright*` entry stays distinguishable from its base in
 * both polarities. Non-hex values (a theme using rgb() or a colour function) are
 * returned unchanged rather than mangled: a duplicate colour degrades better
 * than a wrong one.
 */
export function brighten(value: string, towardsWhite: boolean): string {
  const rgb = parseHex(value)
  if (!rgb) return value
  const target = towardsWhite ? 255 : 0
  return toHex(rgb.map((c) => c + (target - c) * BRIGHT_MIX) as [number, number, number])
}

/** The 16 ANSI entries of xterm's ITheme. */
export interface AnsiPalette {
  black: string; red: string; green: string; yellow: string
  blue: string; magenta: string; cyan: string; white: string
  brightBlack: string; brightRed: string; brightGreen: string; brightYellow: string
  brightBlue: string; brightMagenta: string; brightCyan: string; brightWhite: string
}

/**
 * Builds the ANSI palette for the active theme.
 *
 * `read` is expected to return '' for a variable the theme does not define (an
 * incomplete custom theme, or a pack written before a variable existed), in
 * which case the fallback applies — an empty string handed to xterm renders as a
 * broken colour rather than falling back on its own.
 */
export function ansiPaletteFromVars(read: VarReader): AnsiPalette {
  const bg = read('--bg').trim() || '#1e1e2e'
  const text = read('--text').trim() || '#cdd6f4'
  const red = read('--danger').trim() || '#ef4444'
  const green = read('--ok').trim() || '#22c55e'
  const yellow = read('--warn').trim() || '#eab308'
  const blue = read('--info').trim() || '#0891b2'
  const magenta = read('--term-magenta').trim() || TERM_MAGENTA_FALLBACK
  const cyan = read('--term-cyan').trim() || TERM_CYAN_FALLBACK

  // ANSI 0 and 7 mean dark and light, NOT background and foreground: mapping
  // black to --bg would make `\e[30m` output invisible on a light theme, where
  // the background IS the light end. Order the two extremes by luminance
  // instead, so both polarities keep readable output.
  const bgLum = luminance(bg)
  const textLum = luminance(text)
  const textIsLighter = bgLum === null || textLum === null ? true : textLum >= bgLum
  const black = textIsLighter ? bg : text
  const white = textIsLighter ? text : bg

  // On a dark theme the bright ramp lifts towards white; on a light one it
  // deepens towards black, which is what keeps it visible against the surface.
  const up = textIsLighter

  return {
    black, red, green, yellow, blue, magenta, cyan, white,
    brightBlack: brighten(black, up),
    brightRed: brighten(red, up),
    brightGreen: brighten(green, up),
    brightYellow: brighten(yellow, up),
    brightBlue: brighten(blue, up),
    brightMagenta: brighten(magenta, up),
    brightCyan: brighten(cyan, up),
    brightWhite: brighten(white, up),
  }
}
