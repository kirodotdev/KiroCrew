/**
 * Theme shim for the vendored original renderer.
 *
 * The original shipped six switchable themes (`THEMES`, `THEME_LIST`) plus a
 * picker in the settings panel. The team sync decision removed overall themes:
 * Mochi follows the KiroCrew theme instead. This module keeps the original's
 * EXPORT SURFACE so the vendored components compile unchanged, with a single
 * `'kirocrew'` id.
 *
 * LIVE FOLLOW (the mechanism, in three layers):
 *
 * 1. The core stylesheet's THEME VARIABLE blocks are injected into this
 *    window, filtered down to rules that only declare custom properties —
 *    the dashboard's component styles (ambient body::before gradient, button
 *    rules, ...) must not leak into the vendored panel, and the pet overlay
 *    is a transparent window that must not gain a background.
 * 2. `data-theme` / `data-mode` on <html> select the active block. They are
 *    computed the same way the dashboard does (useTheme.applyTheme) from the
 *    same localStorage keys, and recomputed on `storage` events — same-origin
 *    windows receive those whenever the dashboard switches theme, which is
 *    what makes the follow LIVE.
 * 3. An ALIAS layer derives the handful of variable names only the vendored
 *    components use (--text-muted, --bubble-user, ...) from core variables,
 *    so they re-resolve under every theme instead of being frozen literals.
 *
 * Custom/installed themes apply their variables via JS in the dashboard
 * window only; here they fall back to the base kiro palette. FALLBACK_PALETTE
 * remains as the no-stylesheet escape hatch (jsdom, future standalone use).
 */
import coreCss from '../../../../index.css?inline'

export const FALLBACK_PALETTE: Record<string, string> = {
  '--bg': '#19161d',
  '--bg-elevated': '#211d25',
  '--bg-input': '#28242e',
  '--border': '#352f3d',
  '--border-focus': 'rgba(142,72,255,0.4)',
  '--text': '#dcdadf',
  '--text-muted': '#938f9b',
  '--text-faint': '#5e5966',
  '--bubble-user': 'rgba(178,127,255,0.18)',
  '--bubble-assistant': 'rgba(255,255,255,0.04)',
  '--accent': '#8e48ff',
  '--accent-text': '#ffffff',
  '--accent-glow': 'rgba(142,72,255,0.3)',
  '--danger': '#f94359',
  '--success': '#008543',
  '--header-bg': 'rgba(25,22,29,0.95)',
  '--shadow': 'rgba(0,0,0,0.45)',
  '--scrollbar': 'rgba(220,218,223,0.08)',
  '--scrollbar-hover': 'rgba(220,218,223,0.15)',
}

export function applyFallbackTheme(root: HTMLElement = document.documentElement): void {
  for (const [k, v] of Object.entries(FALLBACK_PALETTE)) {
    root.style.setProperty(k, v)
  }
}

/**
 * Only one id ever existed upstream; kept as a type so ported call sites
 * (`applyTheme(m.theme as ThemeId)`) compile unchanged.
 */
export type ThemeId = 'kirocrew'

/**
 * Vendored-vocabulary variables derived FROM core variables. These resolve at
 * use time, so every theme (and every future theme) feeds them for free.
 * color-mix keeps the original's translucent tints working on light themes,
 * where the old white-alpha literals turned invisible.
 */
const ALIAS_VARS: Record<string, string> = {
  '--bg-input': 'var(--bg-hover)',
  '--border-focus': 'color-mix(in srgb, var(--accent) 40%, transparent)',
  '--text-muted': 'var(--muted)',
  '--text-faint': 'var(--muted-strong)',
  '--bubble-user': 'color-mix(in srgb, var(--accent) 16%, transparent)',
  '--bubble-assistant': 'color-mix(in srgb, var(--text) 6%, transparent)',
  '--accent-text': 'var(--accent-fg)',
  '--success': 'var(--ok)',
  '--header-bg': 'color-mix(in srgb, var(--bg) 95%, transparent)',
  '--shadow': 'rgba(0,0,0,0.45)',
  '--scrollbar': 'color-mix(in srgb, var(--text) 8%, transparent)',
  '--scrollbar-hover': 'color-mix(in srgb, var(--text) 15%, transparent)',
}

/** Same computation as the dashboard's useTheme.applyTheme (source of truth
 * there — do not let these drift). Reimplemented because importing the hook
 * would drag the SPA's api client into every Mochi window bundle. */
function computeDatasetTheme(): { theme: string; mode: string } {
  let colorTheme = 'kiro'
  let modePref = 'system'
  try {
    colorTheme = localStorage.getItem('mc-color-theme') || 'kiro'
    modePref = localStorage.getItem('mc-theme') || 'system'
  } catch {
    /* storage unavailable: defaults stand */
  }
  const mode =
    modePref === 'system'
      ? window.matchMedia?.('(prefers-color-scheme: light)').matches
        ? 'light'
        : 'dark'
      : modePref
  const theme = colorTheme === 'emerald' ? mode : `${colorTheme}-${mode}`
  return { theme, mode }
}

function syncDatasetTheme(): void {
  const { theme, mode } = computeDatasetTheme()
  document.documentElement.dataset.theme = theme
  document.documentElement.dataset.mode = mode
}

let installed = false

/**
 * Inject the core stylesheet's variable blocks, filtered: keep only rules
 * that declare at least one custom property and nothing else, so component
 * styles never leak into this window. Returns false when the CSSOM walk is
 * unavailable (jsdom) — the caller then falls back to the static palette.
 *
 * ONE narrow exception: `.hljs*` syntax-token rules are kept even though they
 * declare real properties. Mochi renders code blocks with the core's shared
 * highlighter (one highlighting stack for the whole product, not two), and the
 * token colors live as plain rules in index.css rather than as variables. They
 * are safe to admit because they only ever set `color` / `font-style` on the
 * <code> spans — no background, no layout — so the transparent pet window
 * cannot pick up a surface from them, which is the leak this filter exists to
 * prevent. Keep that property in mind before widening the exception further.
 */
function installCoreThemeVars(): boolean {
  try {
    const styleEl = document.createElement('style')
    styleEl.setAttribute('data-mochi-core-theme', '')
    styleEl.textContent = coreCss
    document.head.appendChild(styleEl)
    const sheet = styleEl.sheet
    if (!sheet) {
      styleEl.remove()
      return false
    }
    for (let i = sheet.cssRules.length - 1; i >= 0; i--) {
      const rule = sheet.cssRules[i]
      let keep = false
      if (rule instanceof CSSStyleRule) {
        if (/(^|[\s,])\.hljs/.test(rule.selectorText)) {
          keep = true
        } else {
          const s = rule.style
          keep = s.length > 0
          for (let j = 0; j < s.length; j++) {
            const prop = s[j]
            // color-scheme rides in the theme blocks and is safe to keep.
            if (!prop.startsWith('--') && prop !== 'color-scheme') {
              keep = false
              break
            }
          }
        }
      }
      if (!keep) sheet.deleteRule(i)
    }
    return true
  } catch {
    return false
  }
}

function installLiveTheme(): void {
  if (installed) return
  installed = true
  if (!installCoreThemeVars()) {
    applyFallbackTheme()
    return
  }
  const root = document.documentElement
  for (const [k, v] of Object.entries(ALIAS_VARS)) {
    root.style.setProperty(k, v)
  }
  syncDatasetTheme()
  // Live follow: the dashboard writes mc-theme / mc-color-theme on switch and
  // same-origin windows get the storage event; system mode flips via the OS.
  window.addEventListener('storage', (e) => {
    if (e.key === 'mc-theme' || e.key === 'mc-color-theme' || e.key === null) syncDatasetTheme()
  })
  window.matchMedia?.('(prefers-color-scheme: light)').addEventListener?.('change', syncDatasetTheme)
}

/**
 * Original signature. Panel-style windows: variables plus an opaque body
 * background (the original set body background from its theme too).
 */
export function applyTheme(_theme?: string | ThemeId): void {
  installLiveTheme()
  document.body.style.background = 'var(--bg)'
}

/**
 * Bubble colors stay STATIC on purpose (upstream note preserved): the pet
 * overlay is a transparent window drawing free-floating shapes, so these few
 * values must be literal rather than resolved from a stylesheet.
 */
export const BUBBLE_COLORS: Record<ThemeId, { bg: string; text: string; shadow: string }> = {
  kirocrew: { bg: 'rgba(33,29,37,0.95)', text: '#f2f1f4', shadow: 'rgba(0,0,0,0.45)' },
}

/**
 * Overlay variant of applyTheme, used by the pet window: variables only, and
 * the body stays transparent (the overlay window would otherwise paint a full
 * screen rectangle).
 */
export function applyThemeVarsOnly(_id?: string | ThemeId): void {
  installLiveTheme()
}
