/**
 * Pasted-stylesheet import for the custom-theme editor: turns a block of CSS
 * custom-property declarations into the `dark` / `light` variable maps a custom
 * theme stores, and back again.
 *
 * Only data in, data out. The parse result carries property names, refused
 * values and counts, never sentences, so every message the user reads is
 * resolved from the catalog at the render site in `components/themeEditor.tsx`.
 *
 * Which mode a declaration lands in is read from the selectors (and at-rule
 * preludes) enclosing it: a context naming `dark` sets dark mode, one naming
 * `light` sets light mode, and anything else — `:root`, `html`, a bare
 * declaration list, a context naming both — is SHARED and applies to both. So
 * `.dark {}`, `[data-theme="dark"] {}` and
 * `@media (prefers-color-scheme: dark) { :root {} }` all mean the same thing.
 * A mode named only inside `:not(…)` selects nothing: `:root:not(.dark)` reads
 * as `:root`.
 *
 * Security: this is NOT the boundary. A theme's values are re-filtered by
 * `sanitizeCssValue` when the stylesheet is built and by the backend's own
 * allowlist on save. The checks here exist so the user sees which declarations
 * will be dropped BEFORE saving, instead of a server refusal naming one key.
 */

import { sanitizeCssValue } from './cssSanitize'
import { ALLOWED_CSS_VARS } from '../hooks/themeCss'

export type ThemeVars = Record<string, string>

export interface ThemeCssImport {
  /** Declarations whose context names dark mode. */
  dark: ThemeVars
  /** Declarations whose context names light mode. */
  light: ThemeVars
  /** Declarations applying to both modes (`:root`, bare lists, …). */
  shared: ThemeVars
  /** Property names that are not theme variables, in first-seen order. */
  unknown: string[]
  /** Theme variables whose value the sanitizer refuses, with that value, in first-seen order. */
  invalid: RefusedDeclaration[]
  /** The first unknown name that matches a theme variable except for letter case. */
  caseMismatch?: { wrong: string; right: string }
  /** Number of distinct variables that will be applied, across all three maps. */
  recognized: number
  /** Theme name from a leading `/* name: … *\/` comment, capped to the backend's limit. */
  name?: string
  /** Emoji from a leading `/* emoji: … *\/` comment. */
  emoji?: string
}

/** A theme variable the sanitizer refused, and the value it refused. */
export interface RefusedDeclaration {
  name: string
  value: string
}

/** Mirrors `_THEME_NAME_MAX_LEN` in `dashboard/theme_validate.py`. */
const THEME_NAME_MAX_LEN = 60

/** Longest refused value `refusedLabel` shows before cutting it short. */
const REFUSED_VALUE_MAX_LEN = 40

/** Each theme variable keyed by its lower-cased name, for the case-mismatch hint. */
const VAR_BY_LOWER = new Map([...ALLOWED_CSS_VARS].map(v => [v.toLowerCase(), v]))

type Mode = 'dark' | 'light' | 'shared'

/** `text` with every `:not(…)` group removed, nested parentheses included. */
function withoutNegations(text: string): string {
  let out = ''
  let i = 0
  while (i < text.length) {
    if (text.startsWith(':not(', i)) {
      let depth = 0
      for (i += 4; i < text.length; i++) {
        if (text[i] === '(') depth++
        else if (text[i] === ')' && --depth === 0) { i++; break }
      }
    } else {
      out += text[i++]
    }
  }
  return out
}

/** The mode an enclosing context selects; see the module header. */
function modeOf(context: string[]): Mode {
  // `:not(.dark)` excludes dark mode rather than selecting either one, so it
  // counts as shared: paired with a `.dark` block, as it usually is, the shared
  // value is exactly what light mode ends up with.
  const text = withoutNegations(context.join(' ').toLowerCase())
  const dark = /(^|[^a-z0-9])dark([^a-z0-9]|$)/.test(text)
  const light = /(^|[^a-z0-9])light([^a-z0-9]|$)/.test(text)
  if (dark === light) return 'shared'
  return dark ? 'dark' : 'light'
}

/** Parse pasted CSS into per-mode theme variables. Never throws. */
export function parseThemeCss(input: string): ThemeCssImport {
  const out: ThemeCssImport = { dark: {}, light: {}, shared: {}, unknown: [], invalid: [], recognized: 0 }
  // A whole chat reply pasted at once: read only its fenced code block. Prose
  // around it would otherwise become part of the first selector, and a sentence
  // such as "a dark and light theme" makes that block read as both modes.
  const body = /```[a-z]*[ \t]*\n([\s\S]*?)```/i.exec(input)?.[1] ?? input
  // The design prompt asks for the name and emoji as leading comments, so a
  // reply pasted whole fills the editor's fields too.
  const name = /\/\*\s*name\s*:\s*([^\n]*?)\s*\*\//i.exec(body)?.[1]?.trim()
  if (name) out.name = name.slice(0, THEME_NAME_MAX_LEN)
  const emoji = /\/\*\s*emoji\s*:\s*([^\n]*?)\s*\*\//i.exec(body)?.[1]?.trim()
  if (emoji) out.emoji = [...emoji].slice(0, 8).join('')
  const css = body.replace(/\/\*[\s\S]*?(\*\/|$)/g, ' ')
  const context: string[] = []
  const unknown = new Set<string>()
  // Keyed by mode + name: a refused value is only moot when a later declaration
  // of the same variable IN THE SAME MODE replaces it, as the cascade would.
  const invalid = new Map<string, RefusedDeclaration>()

  const declare = (raw: string) => {
    const colon = raw.indexOf(':')
    if (colon < 0) return
    // Custom-property names are case-sensitive: `--BG` is not `--bg`.
    const name = raw.slice(0, colon).trim()
    if (!name) return
    // Collapse whitespace so a value wrapped across lines (a long shadow) is the
    // single-line value the sanitizer's character allowlist accepts.
    const value = raw.slice(colon + 1).replace(/!\s*important\s*$/i, '').replace(/\s+/g, ' ').trim()
    if (!ALLOWED_CSS_VARS.has(name)) { unknown.add(name); return }
    const mode = modeOf(context)
    const clean = sanitizeCssValue(value)
    if (!clean) { invalid.set(`${mode} ${name}`, { name, value }); return }
    invalid.delete(`${mode} ${name}`)
    out[mode][name] = clean
  }

  let buf = ''
  for (const ch of css) {
    if (ch === '{') {
      context.push(buf.trim())
      buf = ''
    } else if (ch === '}') {
      declare(buf)
      buf = ''
      context.pop()
    } else if (ch === ';') {
      declare(buf)
      buf = ''
    } else {
      buf += ch
    }
  }
  declare(buf)

  out.unknown = [...unknown]
  // The same refused value in two modes is one thing to fix, so it is listed once.
  const refused = new Map([...invalid.values()].map(d => [`${d.name}:${d.value}`, d]))
  out.invalid = [...refused.values()]
  for (const wrong of out.unknown) {
    const right = VAR_BY_LOWER.get(wrong.toLowerCase())
    if (right) { out.caseMismatch = { wrong, right }; break }
  }
  out.recognized = new Set([
    ...Object.keys(out.dark), ...Object.keys(out.light), ...Object.keys(out.shared),
  ]).size
  return out
}

/** A refused declaration as `name: value`, its value cut to a readable length. */
export function refusedLabel({ name, value }: RefusedDeclaration): string {
  const chars = [...value]
  const shown = chars.length > REFUSED_VALUE_MAX_LEN ? `${chars.slice(0, REFUSED_VALUE_MAX_LEN).join('')}…` : value
  return `${name}: ${shown}`
}

/**
 * Lay an import over existing per-mode variables: shared first, then the
 * mode-specific block, so `.dark { --bg }` beats `:root { --bg }` in dark mode
 * exactly as the cascade would. Variables the paste does not mention keep their
 * current value, which is what lets a short snippet customise a full theme.
 */
export function applyThemeCss(
  imported: ThemeCssImport, dark: ThemeVars, light: ThemeVars,
): { dark: ThemeVars; light: ThemeVars } {
  return {
    dark: { ...dark, ...imported.shared, ...imported.dark },
    light: { ...light, ...imported.shared, ...imported.light },
  }
}

/** Serialise per-mode variables as the `.dark` / `.light` blocks the parser reads. */
export function themeVarsToCss(dark: ThemeVars, light: ThemeVars): string {
  const block = (selector: string, vars: ThemeVars) => {
    const lines = Object.entries(vars)
      .filter(([, v]) => typeof v === 'string' && v.trim() !== '')
      .map(([k, v]) => `  ${k}: ${v};`)
    return `${selector} {\n${lines.join('\n')}\n}`
  }
  if (!Object.keys(dark).length && !Object.keys(light).length) return ''
  return `${block('.dark', dark)}\n\n${block('.light', light)}\n`
}

/**
 * The Paste CSS tab's placeholder: a sample stylesheet, not prose, so it is built
 * from data by the same serialiser rather than kept as a catalog string a
 * translator would be asked to localise.
 */
export function exampleThemeCss(): string {
  return themeVarsToCss(
    { '--bg': '#12141a', '--text': '#e6e8ee', '--accent': '#8b5cf6' },
    { '--bg': '#fafafa', '--text': '#1f2328', '--accent': '#7c3aed' },
  )
}
