/**
 * Shared measure-and-scan helpers for status-hued TEXT on its own tinted fill:
 * `--warn-text` (glassWarnTextContrast.test.ts) and `--ok-text` /
 * `--danger-text` / `--info-text` (statusTextContrast.test.ts). One copy, so the scanner and
 * the arithmetic cannot drift between the hues.
 *
 * Each `--<hue>-text` token is the plain hue under `:root` and, under
 * `[data-mode="light"]`, `color-mix(in srgb, var(--<hue>) N%, #000)`. The share
 * is read from index.css, so a change to the rule is measured rather than a
 * copy of it.
 */
import { expect } from 'vitest'
import { readdirSync, readFileSync } from 'node:fs'
import { join, relative, resolve, sep } from 'node:path'
import { relativeLuminance, parseCssColor } from '../lib/iconContrast'
import { CSS, THEMES, resolveVar } from './themePalette'

export type StatusHue = 'warn' | 'ok' | 'danger' | 'info'
export interface Rgb { r: number; g: number; b: number }

export const MIN_TEXT = 4.5
/** The surfaces a translucent tint is laid over when measured. */
export const SURFACES = ['--bg', '--bg-elevated', '--card'] as const

/** index.css with comments stripped, so a rule quoted in prose is not read. */
export const ACTIVE = CSS.replace(/\/\*[\s\S]*?\*\//g, '')
/** tailwind-theme.css with comments stripped: the `--color-*` bridge. */
export const BRIDGE = readFileSync(resolve(__dirname, '../tailwind-theme.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '')
export const LIGHT_THEMES = THEMES.filter((t) => t === 'light' || t.endsWith('-light'))
export const DARK_THEMES = THEMES.filter((t) => !LIGHT_THEMES.includes(t))

export function tokenRgb(theme: string, prop: string): Rgb {
  const raw = resolveVar(theme, prop)
  if (raw === undefined) throw new Error(`theme ${theme}: ${prop} resolves to nothing`)
  const c = parseCssColor(raw)
  if (!c) throw new Error(`theme ${theme}: ${prop}=${raw} is not a colour this test can measure`)
  return { r: c.r, g: c.g, b: c.b }
}

/** The alpha of a theme's `--<hue>-subtle` (`rgba(r,g,b,A)`): the share of the
 *  hue a `bg-<hue>-subtle` fill lays over its surface. */
export function subtleShare(theme: string, hue: StatusHue): number {
  const raw = resolveVar(theme, `--${hue}-subtle`)
  // A hue with no `--<hue>-subtle` token (info) gets its fill from the
  // tailwind bridge as a fixed mix of the hue toward transparent.
  if (raw === undefined) {
    const m = new RegExp(`--color-${hue}-subtle:\\s*color-mix\\(in srgb,\\s*var\\(--${hue}\\)\\s*(\\d+)%,\\s*transparent\\)`).exec(BRIDGE)
    if (!m) throw new Error(`theme ${theme}: --${hue}-subtle resolves to nothing and the bridge has no mix for it`)
    return Number(m[1]) / 100
  }
  const c = raw === undefined ? null : parseCssColor(raw)
  if (!c) throw new Error(`theme ${theme}: --${hue}-subtle=${raw} is not a colour this test can measure`)
  return c.a
}

/** `color-mix(in srgb, a w, b)`: a straight per-channel mix in sRGB. */
export const mix = (a: Rgb, b: Rgb, w: number): Rgb => ({
  r: a.r * w + b.r * (1 - w),
  g: a.g * w + b.g * (1 - w),
  b: a.b * w + b.b * (1 - w),
})
export const lum = ({ r, g, b }: Rgb) => relativeLuminance(r, g, b)
export const BLACK: Rgb = { r: 0, g: 0, b: 0 }

/** The light-mode token's hue share, read from the `[data-mode="light"]` rule. */
export function lightShare(hue: StatusHue): number {
  const re = new RegExp(`\\[data-mode="light"\\]\\s*\\{[^{}]*--${hue}-text\\s*:\\s*color-mix\\(in srgb,\\s*var\\(--${hue}\\)\\s*(\\d+)%,\\s*#000\\)\\s*;?[^{}]*\\}`)
  const m = re.exec(ACTIVE)
  expect(m, `index.css lost the [data-mode="light"] --${hue}-text mix toward black`).not.toBeNull()
  return Number(m![1]) / 100
}

/** Light-mode `--<hue>-text` for a theme at a given share. */
export const lightText = (theme: string, hue: StatusHue, share: number): Rgb =>
  mix(tokenRgb(theme, `--${hue}`), BLACK, share)

// ── Source scan ──────────────────────────────────────────────────────────────
// A class token is a run of [\w-/] bounded by non-class characters; a variant
// prefix (`hover:`) is kept with it. `text-<hue>` must end the token exactly:
// `text-<hue>-text` and `text-<hue>-fg` are other utilities, `text-<hue>/80` an
// opacity modifier these tests do not govern.
export function patterns(hue: StatusHue) {
  return {
    plain: new RegExp(`(?<![\\w-])(?:[\\w-]+:)*text-${hue}(?![\\w/-])`),
    safe: new RegExp(`(?<![\\w-])(?:[\\w-]+:)*text-${hue}-text(?![\\w/-])`),
    tint: new RegExp(`(?<![\\w-])(?:[\\w-]+:)*bg-${hue}(?:-subtle|\\/(\\d+))(?![\\w-])`, 'g'),
    plainPfx: new RegExp(`(?<![\\w-])((?:[\\w-]+:)*)text-${hue}(?![\\w/-])`, 'g'),
    safePfx: new RegExp(`(?<![\\w-])((?:[\\w-]+:)*)text-${hue}-text(?![\\w/-])`, 'g'),
    tintPfx: new RegExp(`(?<![\\w-])((?:[\\w-]+:)*)bg-${hue}(?:-subtle|\\/\\d+)(?![\\w-])`, 'g'),
  }
}

/** True when some state of the line lays a `hue` tint under plain
 *  `text-<hue>`. A tint that only exists under `hover:` is covered by
 *  `hover:text-<hue>-text` (or by an at-rest `text-<hue>-text`), which keeps
 *  the control's plain hue at rest where no tint sits under it. */
export function plainOnTint(text: string, hue: StatusHue): boolean {
  const p = patterns(hue)
  const prefixes = (re: RegExp) => new Set([...text.matchAll(re)].map((m) => m[1]))
  const plain = prefixes(p.plainPfx)
  const safe = prefixes(p.safePfx)
  for (const t of prefixes(p.tintPfx)) {
    const covered = safe.has(t) || (t !== '' && safe.has(''))
    if (!covered && (plain.has(t) || plain.has(''))) return true
  }
  return false
}

export interface SourceLine { file: string; line: number; text: string }

function sourceLines(): SourceLine[] {
  const root = resolve(__dirname, '..')
  const out: SourceLine[] = []
  for (const entry of readdirSync(root, { recursive: true, withFileTypes: true })) {
    if (!entry.isFile() || !/\.tsx?$/.test(entry.name) || /\.test\.tsx?$/.test(entry.name)) continue
    const full = join(entry.parentPath, entry.name)
    if (full.split(sep).includes('test')) continue
    const file = relative(root, full)
    readFileSync(full, 'utf8').split('\n').forEach((text, i) => out.push({ file, line: i + 1, text }))
  }
  return out
}
/** Every non-test source line under website/src. */
export const LINES = sourceLines()
export const FILES = new Set(LINES.map((l) => l.file))

/** Every `hue` tint paired with `text-<hue>-text` anywhere in the source: the
 *  set the token is measured against. */
export function pairedTints(hue: StatusHue): { tints: Map<string, number | 'subtle'>; sites: string[] } {
  const p = patterns(hue)
  const tints = new Map<string, number | 'subtle'>()
  const sites: string[] = []
  for (const l of LINES) {
    if (!p.safe.test(l.text)) continue
    const found = [...l.text.matchAll(p.tint)]
    if (found.length === 0) continue
    sites.push(`${l.file}:${l.line}`)
    for (const m of found) tints.set(m[1] === undefined ? 'subtle' : `/${m[1]}`, m[1] === undefined ? 'subtle' : Number(m[1]) / 100)
  }
  return { tints, sites }
}
