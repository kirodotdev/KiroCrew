// MCP App (SEP-1865) host theme variables — the token map and resolver.
//
// This module reads the dashboard's own design tokens for the currently active
// theme off document.documentElement and returns them keyed by SEP-1865's
// McpUiStyleVariableKey union, for delivery in hostContext.styles.variables. It
// reads computed styles and returns a record; it writes nothing to the DOM (same
// boundary idiom as themeCss.ts). Nothing here flows into mcpAppSrcdoc.ts — the
// srcdoc keeps its three-part assembly.
//
// The dashboard-side contract for the token set this maps FROM (and the rule that
// the four status roles map symmetrically) is website/docs/theming-contract.md;
// the subsystem spec is docs/system-specs/modules/mcp-apps.md.

import { sanitizeCssValue } from './cssSanitize'

/** SEP-1865 `McpUiStyleVariableKey`, revision 2026-01-26 (the revision
 *  `PROTOCOL_VERSION` in McpAppFrame.tsx names). Closed set: a key outside this
 *  union is not part of the protocol and MUST NOT be emitted. */
export const MCP_UI_COLOR_VARIABLE_KEYS = [
  '--color-background-primary', '--color-background-secondary',
  '--color-background-tertiary', '--color-background-inverse',
  '--color-background-ghost', '--color-background-info',
  '--color-background-danger', '--color-background-success',
  '--color-background-warning', '--color-background-disabled',
  '--color-text-primary', '--color-text-secondary', '--color-text-tertiary',
  '--color-text-inverse', '--color-text-info', '--color-text-danger',
  '--color-text-success', '--color-text-warning', '--color-text-disabled',
  '--color-text-ghost',
  '--color-border-primary', '--color-border-secondary', '--color-border-tertiary',
  '--color-border-inverse', '--color-border-ghost', '--color-border-info',
  '--color-border-danger', '--color-border-success', '--color-border-warning',
  '--color-border-disabled',
  '--color-ring-primary', '--color-ring-secondary', '--color-ring-inverse',
  '--color-ring-info', '--color-ring-danger', '--color-ring-success',
  '--color-ring-warning',
] as const

export const MCP_UI_NON_COLOR_VARIABLE_KEYS = [
  '--font-sans', '--font-mono',
  '--font-weight-normal', '--font-weight-medium', '--font-weight-semibold',
  '--font-weight-bold',
  '--font-text-xs-size', '--font-text-sm-size', '--font-text-md-size',
  '--font-text-lg-size',
  '--font-heading-xs-size', '--font-heading-sm-size', '--font-heading-md-size',
  '--font-heading-lg-size', '--font-heading-xl-size', '--font-heading-2xl-size',
  '--font-heading-3xl-size',
  '--font-text-xs-line-height', '--font-text-sm-line-height',
  '--font-text-md-line-height', '--font-text-lg-line-height',
  '--font-heading-xs-line-height', '--font-heading-sm-line-height',
  '--font-heading-md-line-height', '--font-heading-lg-line-height',
  '--font-heading-xl-line-height', '--font-heading-2xl-line-height',
  '--font-heading-3xl-line-height',
  '--border-radius-xs', '--border-radius-sm', '--border-radius-md',
  '--border-radius-lg', '--border-radius-xl', '--border-radius-full',
  '--border-width-regular',
  '--shadow-hairline', '--shadow-sm', '--shadow-md', '--shadow-lg',
] as const

export type McpUiColorVariableKey = (typeof MCP_UI_COLOR_VARIABLE_KEYS)[number]
export type McpUiStyleVariableKey =
  | McpUiColorVariableKey
  | (typeof MCP_UI_NON_COLOR_VARIABLE_KEYS)[number]

// The source descriptor. EVERY key — color and non-color alike — resolves from a
// stored dashboard token; there is no literal kind, because a value with no source
// of truth is exactly the drift this module exists to close (see
// NON_COLOR_TOKEN_MAP). The two kinds differ only in whether the token is emitted
// as read or washed. Modelling this as a tagged value rather than parallel maps is
// what makes "every color key is mapped" a compile-time obligation instead of a
// test-only one. Exported because the token maps below are typed against these
// (COLOR_TOKEN_MAP is Record<McpUiColorVariableKey, ColorSource>).

/** Read the named custom property off document.documentElement. */
export type TokenSource = { from: 'token'; name: string }
/** Read the named custom property and emit `amount` of it over `transparent` —
 *  for a translucent fill the dashboard derives rather than stores. `amount` is a
 *  complete CSS percentage token (`'12%'`), never a bare number: the value is a
 *  stylesheet declaration, and a digit glued to a `%` at the interpolation site
 *  reads to the unit-literal gate as an unlocalized UI measurement. */
export type WashSource = { from: 'wash'; name: string; amount: string }
/** Every key reads a theme token, washed or not. */
export type StyleVariableSource = TokenSource | WashSource
/** Kept as the name the color map is typed against, for readability at its use. */
export type ColorSource = StyleVariableSource

const tok = (name: string): TokenSource => ({ from: 'token', name })
const wash = (name: string, amount: string): WashSource => ({ from: 'wash', name, amount })

/** The wash `bg-info-subtle` already paints with, spelled once and as the whole
 *  CSS token. Pinned equal to `tailwind.config.js`'s `info-subtle` by a test that
 *  parses that config, so the dashboard's own info surfaces and an app's
 *  `--color-background-info` cannot drift into two visibly different washes of one
 *  hue. */
export const INFO_WASH = '12%'

/** `Record`, not `Partial<Record>`: adding a color key to the union without
 *  mapping it fails `tsc` rather than shipping a hole. Every `name` here is in
 *  `ALLOWED_CSS_VARS`, pinned by a test that parses that allowlist.
 *
 *  Three families have no dedicated dashboard token and are derived instead:
 *  `*-inverse` swaps the foreground and background roles (`--text` as a surface,
 *  `--bg` as ink); `*-ghost` / `*-disabled` reuse the recessive tokens the
 *  dashboard's own muted controls already use (`--bg-hover`, `--muted`,
 *  `--border`); and `--color-background-info` is a WASH of `--info`, because
 *  `--info` is the one status hue with no stored `-subtle` companion — see the
 *  status-role note below. All three are marked so a future dedicated token has
 *  one place to land. */
export const COLOR_TOKEN_MAP: Record<McpUiColorVariableKey, ColorSource> = {
  '--color-background-primary':   tok('--bg'),
  '--color-background-secondary': tok('--card'),
  '--color-background-tertiary':  tok('--bg-elevated'),
  '--color-background-inverse':   tok('--text'),          // inverse
  '--color-background-ghost':     tok('--bg-hover'),      // recessive
  '--color-background-info':      wash('--info', INFO_WASH),     // derived
  '--color-background-success':   tok('--ok-subtle'),
  '--color-background-warning':   tok('--warn-subtle'),
  '--color-background-danger':    tok('--danger-subtle'),
  '--color-background-disabled':  tok('--bg-hover'),      // recessive

  '--color-text-primary':   tok('--text'),
  '--color-text-secondary': tok('--muted-strong'),
  '--color-text-tertiary':  tok('--muted'),
  '--color-text-inverse':   tok('--bg'),                  // inverse
  // Status roles are symmetric across all four: `background-*` takes the wash,
  // `text-*`/`border-*`/`ring-*` the strong hue. Never a `-fg` token here — that
  // is ink for a SOLID fill and is `#000` in most themes, so it renders
  // black-on-dark. See theming-contract.md § the four status roles.
  //
  // Where the wash COMES FROM differs for info, and follows the dashboard rather
  // than the pattern: `--ok/--warn/--danger-subtle` are stored per theme, but
  // `--info` has no stored companion and `bg-info-subtle` is derived in
  // tailwind.config.js as `color-mix(in srgb, var(--info) 12%, transparent)`. So
  // the map derives it the same way instead of adding a 57th theme-pack variable
  // — an app's info fill and the dashboard's own `bg-info-subtle` surfaces then
  // resolve from one definition and cannot disagree. A pack that wants a
  // different info wash retunes `--info`, which moves both.
  '--color-text-info':      tok('--info'),
  '--color-text-success':   tok('--ok'),
  '--color-text-warning':   tok('--warn'),
  '--color-text-danger':    tok('--danger'),
  '--color-text-disabled':  tok('--muted'),               // recessive
  '--color-text-ghost':     tok('--muted'),               // recessive

  '--color-border-primary':   tok('--border'),
  '--color-border-secondary': tok('--border-strong'),
  '--color-border-tertiary':  tok('--border-hover'),
  '--color-border-inverse':   tok('--text'),              // inverse
  '--color-border-ghost':     tok('--border'),            // recessive
  '--color-border-info':      tok('--info'),
  '--color-border-success':   tok('--ok'),
  '--color-border-warning':   tok('--warn'),
  '--color-border-danger':    tok('--danger'),
  '--color-border-disabled':  tok('--border'),            // recessive

  '--color-ring-primary':   tok('--ring'),
  '--color-ring-secondary': tok('--border-strong'),
  '--color-ring-inverse':   tok('--text'),                // inverse
  '--color-ring-info':      tok('--info'),
  '--color-ring-success':   tok('--ok'),
  '--color-ring-warning':   tok('--warn'),
  '--color-ring-danger':    tok('--danger'),
}

/**
 * `Partial`: an unmapped non-color key is a legitimate outcome and degrades to
 * the app's own fallback under SEP-1865's graceful-degradation rule.
 *
 * EVERY entry reads a stored dashboard token. Nothing here is a literal, and that
 * is the rule rather than an accident of which keys happened to have one: a
 * literal would be a value with no source of truth, so nothing could tell an app
 * painting `14px` from the dashboard actually rendering something else, and the
 * next change to the real scale would not move it. That is the same drift the
 * radius note below rejects, and the same reason `--color-background-info` is
 * derived from `--info` instead of stored as a 57th variable.
 *
 * So the protocol keys Kiro Crew has no token for are OMITTED, not guessed:
 * `--font-weight-*`, every `--font-*-size` / `--font-*-line-height`,
 * `--border-radius-xs`, `--border-radius-full`, `--border-width-regular` and
 * `--shadow-hairline`. The dashboard stores no typography scale at all — the only
 * `--font-*` properties in `index.css` are the two FAMILIES below — and its text
 * sizes are per-call Tailwind utilities, so there is nothing to inherit. An app
 * keeps its own type scale and pairs it with the host's palette, families, radii
 * and shadows, which is a far thinner seam than the color one (#10352) this
 * module exists to close.
 *
 * Exported so `mcpAppTheme.test.ts` can seed the resolver from the map itself; a
 * seed list spelled separately could drift from what the resolver reads.
 */
export const NON_COLOR_TOKEN_MAP:
  Partial<Record<McpUiStyleVariableKey, TokenSource>> = {
  // These resolve through --theme-font-sans / --theme-font-mono, so an installed
  // pack's faces reach the app. Host-fixed, not in ALLOWED_CSS_VARS.
  '--font-sans': tok('--font-body'),
  '--font-mono': tok('--mono'),

  // Read the dashboard's real radius scale rather than restating its values:
  // duplicating them as literals would let an app drift silently the next time
  // that scale moves, which is the seam this whole module exists to close. These
  // are host-fixed (universal in index.css, re-injected per custom theme by
  // `buildCustomThemeCss`) rather than per-color-mode, so like the font reads
  // above they are deliberately not in ALLOWED_CSS_VARS. `xs` and `full` are
  // omitted: the dashboard has no counterpart for either.
  '--border-radius-sm':   tok('--radius-sm'),
  '--border-radius-md':   tok('--radius-md'),
  '--border-radius-lg':   tok('--radius-lg'),
  '--border-radius-xl':   tok('--radius-xl'),

  '--shadow-sm': tok('--shadow-sm'),
  '--shadow-md': tok('--shadow-md'),
  '--shadow-lg': tok('--shadow-lg'),
  // --shadow-hairline: deliberately unmapped. No counterpart, and a literal
  // shadow would not match whatever palette the app paints on.
}

/**
 * Resolve the SEP-1865 style variables for the CURRENTLY ACTIVE theme.
 *
 * Colors are all-or-nothing: a palette that supplies a host
 * background but not a host text color is worse than supplying neither, because
 * the app then pairs our surface with its own fallback ink. Non-color groups are
 * independent - a missing radius degrades with no contrast hazard.
 *
 * Returns `null` when nothing resolved, so the caller omits `styles` entirely
 * rather than sending `{}`.
 */
export function readMcpAppStyleVariables(): Record<string, string> | null {
  if (typeof window === 'undefined' || typeof document === 'undefined') return null
  let computed: CSSStyleDeclaration
  try {
    computed = getComputedStyle(document.documentElement)
  } catch {
    return null // degrade to "no styles", never throw upward
  }

  // A wash is composed AFTER its base has been sanitized and re-sanitized after,
  // so a rejected hue yields '' (never a color-mix wrapped around a hostile
  // string) and the composed value is a sanitizer fixpoint like every other.
  const read = (src: StyleVariableSource): string => {
    const base = sanitizeCssValue(computed.getPropertyValue(src.name))
    if (!base || src.from === 'token') return base
    return sanitizeCssValue(`color-mix(in srgb, ${base} ${src.amount}, transparent)`)
  }

  // Colors into a staging object first: a single miss discards the whole group.
  const colors: Record<string, string> = {}
  let colorsComplete = true
  for (const key of MCP_UI_COLOR_VARIABLE_KEYS) {
    const value = read(COLOR_TOKEN_MAP[key])
    if (!value) {
      colorsComplete = false
      break
    }
    colors[key] = value
  }

  const out: Record<string, string> = colorsComplete ? colors : {}
  for (const key of MCP_UI_NON_COLOR_VARIABLE_KEYS) {
    const src = NON_COLOR_TOKEN_MAP[key]
    if (!src) continue
    const value = read(src)
    if (value) out[key] = value
  }
  return Object.keys(out).length ? out : null
}

/**
 * Identity of the theme half of a hostContext, for the change-detect gate. Not a
 * hash - the payload is small and an exact string keeps the comparison honest.
 */
export function themeContextKey(
  theme: 'dark' | 'light',
  vars: Record<string, string> | null,
): string {
  return theme + '\u0000' + (vars ? JSON.stringify(vars) : '')
}
