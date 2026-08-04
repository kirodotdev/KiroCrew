import {
  useState,
  useEffect,
  useRef,
  useCallback,
  createContext,
  useContext,
  type ReactNode,
} from 'react'
import { useQuery, useMutation } from '@tanstack/react-query'
import { api } from '../api/client'
import { reportSeamCollision } from '../apps/seamCollision'
import { sanitizeCssValue } from '../lib/cssSanitize'
import { safeSetItem } from '../utils/safeStorage'

import { i18nT } from '../i18n/t'

export type ModePreference = 'dark' | 'light' | 'system'
export type ResolvedMode = 'dark' | 'light'
export type ColorTheme = string  // built-in slug or 'custom-{slug}'

export interface ThemeEntry {
  value: string
  label: string
  custom?: boolean
  /** True for themes installed from a folder/GitHub (read-only), vs editor-created customs. */
  installed?: boolean
}

export interface ThemeFontFace {
  family: string
  src: string
  weight?: number
  style?: string
}

export interface ThemeBranding {
  botName?: string
  logo?: string
  favicon?: string
  wordmark?: string
}

/** Closed enum of overlay placements (mirrors backend `_THEME_OVERLAY_POSITIONS`). */
export type ThemeOverlayPosition =
  | 'top'
  | 'bottom'
  | 'left'
  | 'right'
  | 'top-left'
  | 'top-right'
  | 'bottom-left'
  | 'bottom-right'
  | 'center'
  | 'fullscreen'

/** Overlay animation hint (mirrors backend `_THEME_OVERLAY_ANIMATIONS`). */
export type ThemeOverlayAnimation = 'continuous' | 'once' | 'none'

/**
 * Manifest-declared overlay placement/behaviour (§3.1). The backend surfaces
 * `assets.overlays` as objects; the loader also tolerates a bare `string`
 * shape for a stale descriptor — see `ThemeExperienceLayer`.
 * `trigger` is `continuous | activate | idle-<N>s`.
 */
export interface ThemeOverlayDecl {
  id: string
  position: ThemeOverlayPosition
  zIndex: number
  pointerEvents: boolean
  animation: ThemeOverlayAnimation
  trigger: string
}

/** Topbar declaration (§3.1): file presence + optional height/hideOnMobile. */
export interface ThemeTopbar {
  dark?: boolean
  light?: boolean
  /** CSS length, e.g. `28px`; defaults to `28px` when absent. */
  height?: string
  hideOnMobile?: boolean
}

/** One audio trigger entry (§3.3). Consumed by the audio engine. */
export interface ThemeAudioTrigger {
  src: string
  volume: number
  /** Seconds; 0 == unlimited (ambient trigger). */
  maxDuration: number
}

/** Ambient (looping) audio bed (§3.3). */
export interface ThemeAudioAmbient {
  src: string
  volume: number
  loop: boolean
  fadeIn: number
}

/** Parsed `audio/manifest.json` map (§3.3). */
export interface ThemeAudioManifest {
  triggers: Record<string, ThemeAudioTrigger>
  ambient: ThemeAudioAmbient | null
}

export interface ThemeAssets {
  branding?: ThemeBranding
  fonts?: ThemeFontFace[]
  hasOverrides?: boolean
  // L2 assets: overlays, topbar, audio, persona.
  overlays?: ThemeOverlayDecl[]
  topbar?: ThemeTopbar
  hasAudio?: boolean
  /** Parsed audio manifest. */
  audio?: ThemeAudioManifest
  hasPersona?: boolean
  /**
   * Persona descriptor for installed L2 packs (backend-provided). Lets the
   * consent layer bind a grant to the exact persona text: `sha256` fingerprints
   * the persona.md content so a re-install with changed text re-prompts, and
   * `text` is shown verbatim in the consent modal so the user sees exactly what
   * will be injected into the system prompt. Absent on legacy/pre-upgrade descriptors.
   */
  personaInfo?: { sha256: string; chars: number; text: string }
}

export interface CustomThemeData {
  name: string
  slug: string
  emoji: string
  dark: Record<string, string>
  light: Record<string, string>
  /** Capability tier: 0 color · 1 branded · 2 experience. Absent ⇒ 0. */
  level?: number
  /** L1/L2 asset descriptor from the backend (installed themes only). */
  assets?: ThemeAssets
}

function getSystemMode(): ResolvedMode {
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

function resolveMode(pref: ModePreference): ResolvedMode {
  return pref === 'system' ? getSystemMode() : pref
}

function applyTheme(colorTheme: ColorTheme, mode: ResolvedMode, pref: ModePreference) {
  const el = document.documentElement
  if (colorTheme.startsWith('custom-')) {
    el.dataset.theme = `${colorTheme}-${mode}`
  } else {
    el.dataset.theme = colorTheme === 'emerald' ? mode : `${colorTheme}-${mode}`
  }
  el.dataset.mode = mode
  // The PREFERENCE, exposed separately from the resolved mode because the two
  // mean different things to the Electron shell. `data-mode` is what to paint;
  // `data-mode-pref` is whether the user asked to follow the OS. main.js maps
  // this onto `nativeTheme.themeSource`, which must stay `system` under Auto —
  // pinning it to dark/light also pins `prefers-color-scheme` in every renderer,
  // which is the media query Auto resolves through. See syncNativeTheme.
  el.dataset.modePref = pref
}

// Allowlist of allowed CSS custom property names for themes.
// Only these variables will be injected — unknown keys are silently dropped.
const ALLOWED_CSS_VARS = new Set([
  '--bg', '--bg-accent', '--bg-elevated', '--bg-hover',
  '--card', '--card-fg', '--card-hl',
  '--panel', '--panel-strong', '--chrome',
  '--text', '--text-strong', '--muted', '--muted-strong',
  '--border', '--border-strong', '--border-hover',
  '--accent', '--accent-hover', '--accent-subtle',
  '--accent-glow', '--ring',
  '--ok', '--ok-subtle', '--warn', '--warn-subtle',
  '--danger', '--danger-subtle', '--info',
  '--aim', '--aim-subtle',
  '--clarify', '--clarify-subtle',
  '--diff-add', '--diff-add-text',
  '--diff-del', '--diff-del-text',
  '--diff-hunk', '--diff-hunk-text', '--diff-meta-text',
  '--shadow-sm', '--shadow-md', '--shadow-lg',
])

/**
 * Positive-allowlist CSS value sanitizer lives in src/lib/cssSanitize.ts so
 * WidgetFrame and any other surface that serializes theme vars uses the same
 * filter. See that file for the security rationale.
 */
const escapeCssValue = sanitizeCssValue

/** Inject a custom theme's CSS variables as a <style> tag in the document head. */
function injectCustomThemeCSS(theme: CustomThemeData) {
  // Validate slug is safe for use in CSS selector
  const slug = theme.slug.replace(/[^a-z0-9-]/g, '')
  if (!slug) return

  const id = `mc-custom-theme-${slug}`
  document.getElementById(id)?.remove()

  const style = document.createElement('style')
  style.id = id

  const buildVars = (vars: Record<string, string>) =>
    Object.entries(vars)
      .filter(([k]) => ALLOWED_CSS_VARS.has(k))
      .map(([k, v]): [string, string] => [k, escapeCssValue(v)])
      .filter(([, v]) => v !== '')  // drop entries with empty/rejected values
      .map(([k, v]) => `${k}:${v}`)
      .join(';')

  // Static defaults (not user-controlled)
  const darkDefaults =
    '--font-body:var(--script-fallbacks),\'Space Grotesk\',-apple-system,BlinkMacSystemFont,sans-serif;' +
    '--mono:var(--script-fallbacks-mono),\'JetBrains Mono\',ui-monospace,SFMono-Regular,monospace;' +
    '--radius-sm:6px;--radius-md:8px;--radius-lg:12px;--radius-xl:16px;' +
    'color-scheme:dark;'
  const lightDefaults =
    '--font-body:var(--script-fallbacks),\'Space Grotesk\',-apple-system,BlinkMacSystemFont,sans-serif;' +
    '--mono:var(--script-fallbacks-mono),\'JetBrains Mono\',ui-monospace,SFMono-Regular,monospace;' +
    '--radius-sm:6px;--radius-md:8px;--radius-lg:12px;--radius-xl:16px;' +
    'color-scheme:light;'

  const darkCss = buildVars(theme.dark)
  const lightCss = buildVars(theme.light)

  style.textContent =
    `[data-theme="custom-${slug}-dark"]{${darkCss};${darkDefaults}}\n` +
    `[data-theme="custom-${slug}-light"]{${lightCss};${lightDefaults}}`

  document.head.appendChild(style)
}

/** Remove injected CSS for a custom theme. */
function removeCustomThemeCSS(slug: string) {
  const safe = _safeSlug(slug)
  document.getElementById(`mc-custom-theme-${safe}`)?.remove()
  document.getElementById(`mc-theme-fonts-${safe}`)?.remove()
}

// ── Level 1 (branded) asset application ──
//
// Fonts are injected once per installed theme, scoped to that theme's
// data-theme selectors, so they only take effect when the theme is active
// (the browser lazy-loads a @font-face only when a matching element uses it).
// Branding (favicon / logo / bot-name) and overrides.css are *lifecycle*
// scoped: applied only while the theme is the active selection and reverted on
// switch-away. On top of the install-time denylist, overrides.css is also
// fetched, parsed, and run through the §4.2/§5.1 positive-selector allowlist at
// runtime (see _scopeOverridesCss) before injection — defense-in-depth so a
// rule can only reach one of the 10 allowlisted surfaces.

const _assetBase = (slug: string) => `/api/theme/${encodeURIComponent(slug)}/assets`
const _safeSlug = (slug: string) => slug.replace(/[^a-z0-9-]/g, '')
const _safeFamily = (f: string) => f.replace(/[^A-Za-z0-9 _-]/g, '')
// Sanitize a branding asset relative path (e.g. "branding/logo.svg"): allow only
// safe chars, reject traversal/absolute. Defense-in-depth on top of the backend
// asset route's own path-containment; returns '' when unusable.
const _safeAssetPath = (p: string): string => {
  const cleaned = (p || '').replace(/[^a-z0-9./_-]/gi, '')
  if (!cleaned || cleaned.startsWith('/') || cleaned.split('/').includes('..')) return ''
  return cleaned
}

/** Inject @font-face rules + a --font-body override for an installed theme. */
function injectThemeFonts(theme: CustomThemeData) {
  const slug = _safeSlug(theme.slug)
  if (!slug) return
  const id = `mc-theme-fonts-${slug}`
  document.getElementById(id)?.remove()
  const fonts = theme.assets?.fonts || []
  const faces: string[] = []
  // Track the family of the FIRST face that actually made it into the stylesheet
  // — fonts[0] may have been skipped (bad family/src/format), in which case
  // keying --font-body off it would name a family with no @font-face behind it.
  let firstEmittedFamily = ''
  for (const f of fonts) {
    const fam = _safeFamily(f.family || '')
    const file = (f.src || '').replace(/[^a-z0-9./_-]/gi, '')
    if (!fam || !file.startsWith('styles/fonts/')) continue
    const fmt = file.endsWith('.woff2') ? 'woff2' : file.endsWith('.ttf') ? 'truetype' : ''
    if (!fmt) continue
    const weight = typeof f.weight === 'number' && f.weight >= 100 && f.weight <= 900 ? f.weight : 400
    const style = f.style === 'italic' ? 'italic' : 'normal'
    faces.push(
      `@font-face{font-family:'${fam}';` +
        `src:url('${_assetBase(slug)}/${file}') format('${fmt}');` +
        `font-weight:${weight};font-style:${style};font-display:swap;}`
    )
    if (!firstEmittedFamily) firstEmittedFamily = fam
  }
  if (!faces.length) return
  const primary = firstEmittedFamily
  const style = document.createElement('style')
  style.id = id
  style.textContent =
    faces.join('\n') +
    `\n[data-theme="custom-${slug}-dark"],[data-theme="custom-${slug}-light"]{` +
    `--font-body:'${primary}',var(--script-fallbacks),'Space Grotesk',-apple-system,BlinkMacSystemFont,sans-serif;}`
  document.head.appendChild(style)
}

// ── §4.2/§5.1 runtime positive-selector scoper ──
// A selector group from overrides.css is kept only if EVERY selector, after
// stripping one optional leading [data-theme="…"] / html[data-theme…] scoping
// prefix, targets one of the 10 allowlisted surfaces (optionally with extra
// chained classes/pseudo-classes on the SAME base — no descendant/child/sibling
// combinators, no ids/attribute selectors, never the forbidden set).
const _OVERRIDES_ID = 'mc-theme-overrides'
const _ALLOWED_ELEMENTS = new Set(['', 'body', 'button']) // blocks iframe/script/div/…
const _ALLOWED_CLASSES = new Set([
  'topbar',
  'chat-container',
  'sidebar',
  'message-bubble',
  'input-area',
  'code-block',
])
const _FORBIDDEN_CLASSES = new Set(['token', 'credential'])

/** True if a single selector targets an allowlisted surface. */
function _selectorAllowed(sel: string): boolean {
  let s = sel.trim()
  if (!s) return false
  // Strip one optional leading scoping prefix: [data-theme…] or html[data-theme…].
  s = s.replace(/^(?:html)?\s*\[data-theme[^\]]*\]\s*/i, '').trim()
  if (!s) return false
  // No descendant/child/sibling combinators may remain in the compound.
  if (/[\s>+~]/.test(s)) return false
  // No ids or (remaining) attribute selectors — blocks #app-root and [data-auth].
  if (s.includes('#') || s.includes('[')) return false
  // Leading element (optional) followed by chained .class / :pseudo / ::pseudo.
  const m = /^([a-zA-Z][\w-]*)?((?:\.[\w-]+|::?[\w-]+(?:\([^)]*\))?)*)$/.exec(s)
  if (!m) return false
  const element = (m[1] || '').toLowerCase()
  if (!_ALLOWED_ELEMENTS.has(element)) return false
  const classes = new Set<string>()
  const pseudoEls = new Set<string>()
  const tokRe = /(\.[\w-]+)|(::?[\w-]+(?:\([^)]*\))?)/g
  let t: RegExpExecArray | null
  while ((t = tokRe.exec(m[2] || '')) !== null) {
    if (t[1]) {
      classes.add(t[1].slice(1).toLowerCase())
    } else if (t[2]) {
      const dbl = t[2].startsWith('::')
      const name = t[2].replace(/^::?/, '').replace(/\(.*$/, '').toLowerCase()
      if (dbl || name === 'before' || name === 'after') pseudoEls.add(name)
      // single-colon pseudo-classes (:hover, :focus, …) are tolerated / ignored
    }
  }
  for (const c of classes) if (_FORBIDDEN_CLASSES.has(c)) return false
  if (element === 'body') {
    // Only bare body / body::before / body::after (single-colon tolerated).
    if (classes.size) return false
    for (const p of pseudoEls) if (p !== 'before' && p !== 'after') return false
    return true
  }
  if (element === 'button') return classes.has('primary')
  // Class-based surfaces (element === ''): allowed if any base class is present.
  for (const c of classes) if (_ALLOWED_CLASSES.has(c)) return true
  return false
}

/** True if a comma selector group is kept (every selector must pass). */
function _groupAllowed(group: string): boolean {
  const parts = group.split(',')
  return parts.length > 0 && parts.every((p) => _selectorAllowed(p))
}

/**
 * Filter an overrides.css string to allowlisted rules only. Top-level rules
 * whose selector group passes are emitted verbatim; @media blocks are recursed
 * into (wrapper preserved, inner filtered); every other at-rule is dropped.
 */
// Declaration-body denylist — mirrors the backend `_THEME_CSS_DENY_RE`
// (theme_validate.py). Non-global (no /g) so repeated `.test()` is stateless.
// Applied to KEPT rules' bodies so the runtime boundary is fail-closed for
// declarations, not just selectors.
const _DECL_DENY_RE =
  /@import|expression\s*\(|javascript:|-moz-binding|url\s*\(\s*['"]?\s*(?:https?:)?\/\//i

// Evasion normalization: a browser decodes CSS
// `\`-escapes during tokenization, so `\75 rl(` becomes `url(`. Comments are
// already stripped globally below. Decode escapes and run the denylist on the
// decoded text too, so an escaped forbidden token can't hide from the scoper.
// Mirrors backend `_decode_css_escapes` / `_css_denylist_normalize`. Not a full
// CSS parse (that is #316) — just the minimal decode the known evasions exploit.
const _CSS_ESCAPE_RE = /\\(?:([0-9a-fA-F]{1,6})\s?|([\s\S]))/g
function _decodeCssEscapes(s: string): string {
  return s.replace(_CSS_ESCAPE_RE, (_m, hex: string | undefined, ch: string | undefined) => {
    if (hex !== undefined) {
      try {
        return String.fromCodePoint(parseInt(hex, 16))
      } catch {
        return ''
      }
    }
    return ch ?? ''
  })
}
/** True if a declaration body hits the denylist raw OR after escape-decoding. */
function _declDenied(block: string): boolean {
  return _DECL_DENY_RE.test(block) || _DECL_DENY_RE.test(_decodeCssEscapes(block))
}

function _scopeOverridesCss(css: string): { css: string; kept: number; dropped: number } {
  const src = css.replace(/\/\*[\s\S]*?\*\//g, '') // strip comments
  let kept = 0
  let dropped = 0

  const walk = (input: string): string => {
    const out: string[] = []
    let i = 0
    let buf = ''
    while (i < input.length) {
      const ch = input[i]
      // Skip quoted strings verbatim so braces/quotes inside them (e.g.
      // `content:"}"`) can't desync the brace walker. Mirrors the backend
      // `_css_skip_string`. (Comments are already stripped above.)
      if (ch === '"' || ch === "'") {
        buf += ch
        i++
        while (i < input.length) {
          const c = input[i]
          buf += c
          i++
          if (c === '\\') {
            if (i < input.length) {
              buf += input[i]
              i++
            }
            continue
          }
          if (c === ch) break
        }
        continue
      }
      if (ch === '{') {
        const prelude = buf.trim()
        buf = ''
        let depth = 1
        i++
        const start = i
        while (i < input.length && depth > 0) {
          const c = input[i]
          if (c === '"' || c === "'") {
            // Skip string literal so braces inside it don't affect depth.
            i++
            while (i < input.length) {
              const cc = input[i]
              i++
              if (cc === '\\') {
                if (i < input.length) i++
                continue
              }
              if (cc === c) break
            }
            continue
          }
          if (c === '{') depth++
          else if (c === '}') depth--
          if (depth > 0) i++
        }
        const block = input.slice(start, i)
        i++ // skip closing }
        if (prelude.startsWith('@')) {
          if (/^@media\b/i.test(prelude)) {
            const inner = walk(block)
            if (inner.trim()) out.push(`${prelude}{${inner}}`)
          }
          // all other at-rules (@import, @font-face, @supports, …) are dropped
        } else if (_groupAllowed(prelude)) {
          // Fail-closed on declaration BODIES too, not just selectors:
          // mirror the backend install denylist at runtime so a declaration that
          // EVADES install-time validation (encoding drift, future CSS features)
          // is still dropped before it reaches the main document. Legit packs are
          // unaffected — their declarations already passed the identical check at
          // install. Closes the selector-allowlist / declaration-fail-open
          // asymmetry pending the #316 CSSOM consolidation.
          if (_declDenied(block)) {
            dropped++
          } else {
            kept++
            out.push(`${prelude}{${block}}`)
          }
        } else {
          dropped++
        }
      } else {
        buf += ch
        i++
      }
    }
    return out.join('\n')
  }

  return { css: walk(src), kept, dropped }
}

// ── §4.2 pack-relative url() rewriting ──
// overrides.css is injected as an inline <style>, so a relative url() would
// resolve against the *document* base (→ 404), not the pack. Rewrite relative
// refs in KEPT rules to absolute asset-route URLs, resolving against the
// stylesheet's virtual location `styles/overrides.css` (so `../branding/x.png`
// → `<assetBase>/branding/x.png`, `fonts/x.ttf` → `<assetBase>/styles/fonts/x.ttf`).
// data:, absolute (`/…`, incl. already-rewritten `/api/theme/…`) and schemed
// (http:, blob:, …) urls are left untouched — external refs are install-blocked;
// this is defense-in-depth. Traversal escaping the pack root is neutralized.

/** Resolve a relative overrides.css url() ref to a sanitized pack-relative path
 *  (rooted at the pack), or '' if it escapes the pack root / is otherwise unsafe. */
function _resolveOverrideAsset(rel: string): string {
  const stack = ['styles'] // stylesheet dir = styles/ (styles/overrides.css)
  for (const seg of rel.split('/')) {
    if (seg === '' || seg === '.') continue
    if (seg === '..') {
      if (!stack.length) return '' // escaped the pack root
      stack.pop()
      continue
    }
    stack.push(seg)
  }
  return _safeAssetPath(stack.join('/'))
}

/** Rewrite relative url() refs in scoped overrides.css to absolute asset URLs. */
function _rewriteOverridesUrls(css: string, slug: string): string {
  const base = _assetBase(slug)
  return css.replace(/url\(\s*(['"]?)([^'")]*)\1\s*\)/gi, (whole, _q, raw) => {
    const u = (raw || '').trim()
    if (!u) return whole
    if (/^data:/i.test(u)) return whole // inline data URI — leave as-is
    if (u.startsWith('/')) return whole // absolute same-origin (incl. /api/theme/…)
    if (/^[a-z][a-z0-9+.-]*:/i.test(u)) return whole // schemed (http:, blob:, …) — install-blocked
    const safe = _resolveOverrideAsset(u)
    if (!safe) return "url('')" // traversal / unsafe → neutralized, no raw ../ leaks
    return `url('${base}/${safe}')`
  })
}

// Monotonic token so an in-flight fetch can't re-inject after a switch-away.
let _overridesToken = 0

/**
 * Apply/remove the active installed theme's runtime-scoped overrides.css.
 * Returns a promise that settles once the fetch+inject completes (or
 * immediately when there is nothing to fetch) so the theme-switch status
 * indicator has a natural "applied" point to clear on.
 */
function applyThemeOverrides(theme: CustomThemeData | undefined): Promise<void> {
  const myToken = ++_overridesToken
  document.getElementById(_OVERRIDES_ID)?.remove()
  const slug = theme ? _safeSlug(theme.slug) : ''
  if (!theme?.assets?.hasOverrides || !slug) return Promise.resolve()
  if (typeof fetch !== 'function') return Promise.resolve()
  return fetch(`${_assetBase(slug)}/styles/overrides.css`)
    .then((r) => (r.ok ? r.text() : ''))
    .then((raw) => {
      if (myToken !== _overridesToken || !raw) return // superseded or empty
      const { css, dropped } = _scopeOverridesCss(raw)
      if (dropped) {
        // eslint-disable-next-line no-console -- intentional theme-scoper diagnostic
        console.debug(`[theme] overrides.css: dropped ${dropped} disallowed rule(s)`)
      }
      if (!css.trim()) return
      // Rewrite pack-relative url() refs → absolute asset-route URLs (the inline
      // <style> injection point means relative refs would 404 against the doc base).
      const scoped = _rewriteOverridesUrls(css, slug)
      document.getElementById(_OVERRIDES_ID)?.remove()
      const style = document.createElement('style')
      style.id = _OVERRIDES_ID
      style.textContent = scoped
      document.head.appendChild(style)
    })
    .catch(() => {
      /* fetch failure → no overrides (silent) */
    })
}

/** Apply/revert the active installed theme's branding (favicon + logo var). */
function applyThemeBranding(theme: CustomThemeData | undefined) {
  const root = document.documentElement
  document.getElementById('mc-theme-favicon')?.remove()
  root.style.removeProperty('--theme-logo')
  const b = theme?.assets?.branding
  const slug = theme ? _safeSlug(theme.slug) : ''
  if (!b || !slug) return
  const fav = b.favicon ? _safeAssetPath(b.favicon) : ''
  if (fav) {
    const link = document.createElement('link')
    link.id = 'mc-theme-favicon'
    link.rel = 'icon'
    link.href = `${_assetBase(slug)}/${fav}`
    document.head.appendChild(link)
  }
  const logo = b.logo ? _safeAssetPath(b.logo) : ''
  if (logo) {
    root.style.setProperty('--theme-logo', `url('${_assetBase(slug)}/${logo}')`)
  }
}

/**
 * Catalog KEY for each built-in theme whose display name is descriptive COPY
 * rather than a proper noun.
 *
 * Nearly every built-in is named after an upstream palette project (Monokai,
 * Solarized, Dracula, Nord, Rosé Pine, Catppuccin, Tokyo Night, Gruvbox,
 * Everforest), a product (Kiro, IntelliJ), or the display technology it is tuned
 * for (AMOLED). Those are proper nouns — translating one would sever the name
 * from the upstream project a user is looking for — so they stay verbatim as
 * `label` in `THEMES` below, exactly like the theme names an installed pack or a
 * `registerTheme()` caller supplies. `High Contrast` is the one that names a
 * rendering PROPERTY (the accessibility palette) rather than a palette identity,
 * so it is copy in the same sense as every other settings label.
 *
 * Keys, not strings: `THEMES` is evaluated at module load, so an `i18nT()` call
 * there would freeze whatever language was active at boot. The lookup happens in
 * `builtinThemes()`, which runs during render. Shaped as a flat `Record` of full
 * literal keys and indexed inline at the `i18nT()` call, because that is the form
 * `scripts/check-i18n-keys.mjs` can resolve statically.
 */
export const THEME_LABEL_KEY: Record<string, string> = {
  highcontrast: 'hooks.theme.high_contrast',
}

export const THEMES: ThemeEntry[] = [
  { value: 'emerald', label: '🌿 Emerald' },
  { value: 'monokai', label: '🎨 Monokai' },
  { value: 'solarized', label: '☀️ Solarized' },
  { value: 'amber', label: '🔥 Amber' },
  { value: 'dracula', label: '🔮 Dracula' },
  { value: 'nord', label: '🌊 Nord' },
  { value: 'rosepine', label: '🌹 Rosé Pine' },
  { value: 'catppuccin', label: '🐱 Catppuccin' },
  { value: 'tokyonight', label: '🌃 Tokyo Night' },
  { value: 'gruvbox', label: '🍦 Gruvbox' },
  { value: 'ice', label: '🧊 Ice' },
  { value: 'amoled', label: '🖤 AMOLED' },
  { value: 'kiro', label: '👻 Kiro' },
  { value: 'intellij', label: '😶‍🌫️ IntelliJ' },
  // The only descriptive name here, so the only one with a catalog key: `label`
  // holds the glyph alone as the pre-resolution fallback and the full display
  // string lives in `hooks.theme.high_contrast` (emoji included, as
  // `components.themeEditor.color_picker` already does). Nothing renders this
  // entry's `label` directly — `allThemes` goes through `builtinThemes()`.
  { value: 'highcontrast', label: '🔆' },
  { value: 'everforest', label: '🌲 Everforest' },
  { value: 'amoled-midnight', label: '🌌 AMOLED Midnight' },
  { value: 'amoled-grey-calm', label: '🌑 AMOLED Grey Calm' },
]

/**
 * `THEMES` with every descriptive name resolved for the CURRENT language.
 *
 * Called from `useThemeState()` on each render (through `allThemes`), which is
 * what lets the picker follow a language switch — the module-level `THEMES` array
 * cannot. An entry with no `THEME_LABEL_KEY` entry is passed through untouched:
 * its name is a proper noun and has no catalog key by design.
 */
function builtinThemes(): ThemeEntry[] {
  return THEMES.map((t) =>
    // `hasOwnProperty`, not `in`: theme values also arrive from persisted config
    // and from `registerTheme()`, so a value named `toString` or `constructor`
    // would otherwise resolve to an inherited Object.prototype member and hand a
    // function to i18next.
    Object.prototype.hasOwnProperty.call(THEME_LABEL_KEY, t.value)
      ? { ...t, label: i18nT(THEME_LABEL_KEY[t.value]) }
      : t,
  )
}

/** Default color theme applied on first run when no preference is persisted. */
export const DEFAULT_COLOR_THEME: ColorTheme = 'kiro'

/**
 * Downstream-registered built-in themes.
 *
 * Extension seam: a downstream edition (or plugin bundle) adds its own theme
 * options to the theme picker via `registerTheme()` from its entry module,
 * instead of editing the `THEMES` array on every upstream sync. These are
 * built-in (non-`custom`) themes — the theme's CSS block ships with the
 * edition's overlay; this registry only contributes the picker entry (value +
 * label). The core registers none, so the stock picker shows only `THEMES`.
 *
 * Read via `allThemes` (`[...THEMES, ...registered, ...customThemes]`), so a
 * registered theme appears in the picker without touching this file. Registration
 * is expected at module-load time (edition composition), before the picker
 * renders — this registry is not reactive.
 */
const REGISTERED_THEMES: ThemeEntry[] = []

/**
 * Register additional built-in theme picker entries at runtime. A duplicate
 * `value` (already in `THEMES` or previously registered) is ignored and logs a
 * warning, so re-entrant registration (e.g. HMR) stays idempotent.
 */
export function registerTheme(entries: ThemeEntry[]): void {
  for (const entry of entries) {
    if (
      THEMES.some((t) => t.value === entry.value) ||
      REGISTERED_THEMES.some((t) => t.value === entry.value)
    ) {
      reportSeamCollision('theme', `theme ${entry.value} already registered; ignoring duplicate`)
      continue
    }
    REGISTERED_THEMES.push(entry)
  }
}

/** All registered downstream themes, in insertion order. */
export function getRegisteredThemes(): readonly ThemeEntry[] {
  return REGISTERED_THEMES
}

const SYNC_EVENT = 'mc-theme-sync'
export const CUSTOM_THEMES_CHANGED_EVENT = 'mc-custom-themes-changed'

function broadcast(mode: ModePreference, colorTheme: ColorTheme) {
  window.dispatchEvent(new CustomEvent(SYNC_EVENT, { detail: { mode, colorTheme } }))
}

/** Notify all useTheme instances that custom themes have changed. */
function broadcastCustomThemesChanged() {
  window.dispatchEvent(new Event(CUSTOM_THEMES_CHANGED_EVENT))
}

export interface ThemeContextValue {
  theme: ResolvedMode
  preference: ModePreference
  cycle: () => void
  setTheme: (pref: ModePreference) => void
  colorTheme: ColorTheme
  setColorTheme: (t: ColorTheme) => void
  /**
   * True while an *installed* theme's async assets (branding + runtime-scoped
   * overrides.css) are being applied after a switch. Built-in / editor-custom
   * (L0) themes apply synchronously and do not flip this. Drives a lightweight
   * "Applying…" indicator; guarded by a ~150ms minimum-visible window so it
   * never flickers.
   */
  themeSwitching: boolean
  allThemes: ThemeEntry[]
  /** Active installed theme's branding bot-name, or null for built-ins / L0. */
  brandName: string | null
  customThemes: ThemeEntry[]
  customThemeDataMap: Map<string, CustomThemeData>
  themeVersion: number
  onboarded: boolean
  importOnboarded: boolean
  /**
   * Has the user seen the mandatory first-run Privacy chapter?
   *
   * Browser-local on purpose, unlike `onboarded` / `importOnboarded`: it only
   * ever gates a screen INSIDE first run, and a finished first run (`onboarded`,
   * which IS server-backed) implies it — so a second machine never re-shows the
   * chapter to someone who already completed onboarding, and no config field is
   * needed to say so.
   */
  privacyAcked: boolean
  themeBootReady: boolean
  markOnboarded: () => void
  markImportOnboarded: () => void
  markPrivacyAcked: () => void
  addCustomTheme: (data: Omit<CustomThemeData, 'slug'> & { slug?: string }) => Promise<CustomThemeData>
  deleteCustomTheme: (slug: string) => Promise<void>
  loadCustomThemes: () => Promise<void>
}

const ThemeContext = createContext<ThemeContextValue | null>(null)

/**
 * Mount once near the app root. All theme state lives in this provider's
 * single useThemeState() instance; every useTheme() consumer downstream
 * reads from the shared context rather than spinning up its own
 * localStorage / matchMedia / API subscriptions.
 */
export function ThemeProvider({ children }: { children: ReactNode }) {
  const value = useThemeState()
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext)
  if (!ctx) {
    throw new Error('useTheme must be used within <ThemeProvider>')
  }
  return ctx
}

/**
 * Internal state hook — ONLY called once, by ThemeProvider. All theme state,
 * effects, listeners, and API calls live here. Consumers reach this via
 * useTheme() → useContext(ThemeContext), so there's exactly one subscription
 * regardless of how many components render it.
 */
function useThemeState(): ThemeContextValue {
  const [mode, setMode] = useState<ModePreference>(
    () => (localStorage.getItem('mc-theme') as ModePreference) || 'system'
  )
  const [colorTheme, setColorThemeState] = useState<ColorTheme>(
    () => (localStorage.getItem('mc-color-theme') as ColorTheme) || DEFAULT_COLOR_THEME
  )
  const [resolved, setResolved] = useState<ResolvedMode>(() => resolveMode(mode))
  const [customThemes, setCustomThemes] = useState<ThemeEntry[]>([])
  const [customThemeDataMap, setCustomThemeDataMap] = useState<Map<string, CustomThemeData>>(new Map())
  // Monotonic counter bumped on any change that affects computed CSS vars on
  // documentElement: mode change, color-theme change, and in-place edits to
  // the active custom theme (same slug, new values). Consumers that read the
  // DOM's computed styles (e.g. WidgetFrame serializing vars into an iframe)
  // include this in their memo deps so they re-read after the edit flow in
  // themeEditor dispatches CUSTOM_THEMES_CHANGED_EVENT.
  const [themeVersion, setThemeVersion] = useState(0)
  const bumpThemeVersion = useCallback(() => setThemeVersion(v => v + 1), [])
  const [onboarded, setOnboarded] = useState(() => !!localStorage.getItem('mc-onboarded'))
  // Active installed theme's branding bot-name (null for built-ins / L0 themes).
  const [brandName, setBrandName] = useState<string | null>(null)
  // Lightweight "Applying…" indicator: true only while an INSTALLED theme's
  // async assets settle after a switch (built-ins/editor-customs are instant).
  const [themeSwitching, setThemeSwitching] = useState(false)
  // Slugs of installed (folder/GitHub) themes — read synchronously in
  // setColorTheme (via a ref so its identity stays stable) to decide whether a
  // selection is one whose async assets warrant the indicator.
  const installedSlugsRef = useRef<Set<string>>(new Set())
  // Timestamp of the current switch, for the ~150ms minimum-visible guard.
  const switchStartRef = useRef(0)
  // Current colorTheme, read synchronously in setColorTheme (via a ref so its
  // identity stays stable) to no-op a same-value re-select.
  const colorThemeRef = useRef(colorTheme)
  useEffect(() => {
    colorThemeRef.current = colorTheme
  }, [colorTheme])
  // Gate self-repair on the first custom-theme load so a persisted custom-<slug>
  // selection isn't reset to the default before the theme list has arrived.
  const [customThemesLoaded, setCustomThemesLoaded] = useState(false)
  const [importOnboarded, setImportOnboarded] = useState(
    () => !!localStorage.getItem('mc-import-onboarded') || !!localStorage.getItem('mc-onboarded'),
  )
  // Seeded from `mc-onboarded` as well as its own flag: a user who finished
  // first run before this chapter existed (or on another machine) has no reason
  // to be shown it now.
  const [privacyAcked, setPrivacyAcked] = useState(
    () => !!localStorage.getItem('mc-privacy-acked') || !!localStorage.getItem('mc-onboarded'),
  )
  const legacyOnboardedRef = useRef(
    !!localStorage.getItem('mc-onboarded') && !localStorage.getItem('mc-import-onboarded'),
  )
  const legacyMigrationStartedRef = useRef(false)
  const [themeBootReady, setThemeBootReady] = useState(false)

  const loadCustomThemes = useCallback(async () => {
    try {
      const res = await api.themes()
      const themes: ThemeEntry[] = (res.themes || []).map(
        (t: { slug: string; name: string; emoji: string; source?: string }) => ({
          value: `custom-${t.slug}`,
          label: `${t.emoji} ${t.name}`,
          custom: true,
          installed: t.source === 'installed',
        })
      )
      setCustomThemes(themes)

      // Fetch all theme details in parallel to avoid serial waterfall
      const dataMap = new Map<string, CustomThemeData>()
      const results = await Promise.allSettled(
        (res.themes || []).map((t: { slug: string }) => api.themeDetail(t.slug))
      )
      for (const r of results) {
        if (r.status === 'fulfilled') {
          dataMap.set(r.value.slug, r.value)
          injectCustomThemeCSS(r.value)
          injectThemeFonts(r.value)
        }
      }
      setCustomThemeDataMap(dataMap)
      setCustomThemesLoaded(true)
      bumpThemeVersion()
    } catch {
      // API not available yet — ignore
    }
  }, [bumpThemeVersion])

  // Load custom themes from API on mount + listen for cross-instance changes
  useEffect(() => {
    loadCustomThemes()
    const handler = () => loadCustomThemes()
    window.addEventListener(CUSTOM_THEMES_CHANGED_EVENT, handler)
    return () => window.removeEventListener(CUSTOM_THEMES_CHANGED_EVENT, handler)
  }, [loadCustomThemes])

  const { mutate: persistTheme } = useMutation({
    mutationFn: (body: {
      mode?: string
      color?: string
      onboarded?: boolean
      import_onboarded?: boolean
    }) => api.updateThemeConfig(body),
  })

  // Fetch workspace theme config from server on boot.
  // Server is the source of truth; localStorage is a render cache.
  const { data: bootData, isFetched: themeBootFetched } = useQuery({
    queryKey: ['theme-boot'],
    queryFn: () => api.themeBoot(),
    staleTime: Infinity,  // only need it once on mount
    retry: false,         // if server unavailable, fall back to localStorage silently
  })

  useEffect(() => {
    if (!themeBootFetched) return
    if (bootData) {
      if (bootData.mode && bootData.mode !== mode) {
        safeSetItem('mc-theme', bootData.mode)
        setMode(bootData.mode as ModePreference)
        setResolved(resolveMode(bootData.mode as ModePreference))
      }
      if (bootData.color && bootData.color !== colorTheme) {
        safeSetItem('mc-color-theme', bootData.color)
        setColorThemeState(bootData.color)
      }
      const hasOnboardingState =
        typeof bootData.onboarded === 'boolean'
        && typeof bootData.import_onboarded === 'boolean'
      const needsLegacyMigration =
        hasOnboardingState
        && legacyOnboardedRef.current
        && (!bootData.onboarded || !bootData.import_onboarded)
      if (needsLegacyMigration && !legacyMigrationStartedRef.current) {
        legacyMigrationStartedRef.current = true
        setOnboarded(true)
        setImportOnboarded(true)
        setPrivacyAcked(true)
        safeSetItem('mc-privacy-acked', '1')
        persistTheme(
          { onboarded: true, import_onboarded: true },
          {
            onSuccess: () => {
              safeSetItem('mc-import-onboarded', '1')
              legacyOnboardedRef.current = false
            },
          },
        )
      } else if (!legacyMigrationStartedRef.current) {
        if (typeof bootData.onboarded === 'boolean') {
          setOnboarded(bootData.onboarded)
          if (bootData.onboarded) {
            safeSetItem('mc-onboarded', '1')
            // A completed first run passed through the Privacy chapter (or
            // predates it) — the server flag is the durable record of both.
            safeSetItem('mc-privacy-acked', '1')
            setPrivacyAcked(true)
          } else {
            localStorage.removeItem('mc-onboarded')
          }
        }
        if (typeof bootData.import_onboarded === 'boolean') {
          setImportOnboarded(bootData.import_onboarded)
          if (bootData.import_onboarded) {
            safeSetItem('mc-import-onboarded', '1')
          } else {
            localStorage.removeItem('mc-import-onboarded')
          }
        }
      }
    }
    setThemeBootReady(true)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bootData, themeBootFetched])

  useEffect(() => {
    applyTheme(colorTheme, resolved, mode)
    bumpThemeVersion()
  }, [resolved, colorTheme, mode, bumpThemeVersion])

  // Tell the Electron shell which mode PREFERENCE is active, so it can set
  // `nativeTheme.themeSource` to match ('system' under Auto). Pushed on change
  // rather than only pulled on window focus so switching Dark → Auto un-pins
  // `prefers-color-scheme` immediately; Chromium then fires a change event on
  // the media query below if the effective value moved. No-op in a browser.
  useEffect(() => {
    const bridge = (window as unknown as {
      electronAPI?: { setThemeMode?: (pref: string) => void }
    }).electronAPI
    bridge?.setThemeMode?.(mode)
  }, [mode])

  // Report the resolved accent to the Electron shell (if present) so the NEXT
  // launch's boot splash (loading.html) paints in the user's chosen colour.
  // Reads the computed --accent after paint; a no-op in a plain browser.
  useEffect(() => {
    const bridge = (window as unknown as {
      electronAPI?: { setThemeAccent?: (hex: string) => void }
    }).electronAPI
    if (!bridge?.setThemeAccent) return
    const id = requestAnimationFrame(() => {
      const hex = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()
      if (hex) bridge.setThemeAccent!(hex)
    })
    return () => cancelAnimationFrame(id)
  }, [resolved, colorTheme, themeVersion])

  useEffect(() => {
    const handler = (e: Event) => {
      const { mode: m, colorTheme: ct } = (e as CustomEvent).detail
      setMode(m)
      setResolved(resolveMode(m))
      setColorThemeState(ct)
    }
    window.addEventListener(SYNC_EVENT, handler)
    return () => window.removeEventListener(SYNC_EVENT, handler)
  }, [])

  useEffect(() => {
    if (mode !== 'system') return
    const mql = window.matchMedia('(prefers-color-scheme: dark)')
    const handler = () => setResolved(getSystemMode())
    mql.addEventListener('change', handler)
    return () => mql.removeEventListener('change', handler)
  }, [mode])

  const setMode_ = useCallback((pref: ModePreference) => {
    safeSetItem('mc-theme', pref)
    setMode(pref)
    setResolved(resolveMode(pref))
    const ct = (localStorage.getItem('mc-color-theme') as ColorTheme) || DEFAULT_COLOR_THEME
    broadcast(pref, ct)
    persistTheme({ mode: pref })
  }, [persistTheme])

  const cycleMode = useCallback(() => {
    const next: ModePreference = mode === 'system' ? 'light' : mode === 'light' ? 'dark' : 'system'
    setMode_(next)
  }, [mode, setMode_])

  const setColorTheme = useCallback((t: ColorTheme) => {
    // No-op a same-value re-select: nothing changes, and flipping themeSwitching
    // on for an installed slug here would never clear (the apply effect only runs
    // when colorTheme actually changes), wedging the "Applying…" indicator.
    if (t === colorThemeRef.current) return
    safeSetItem('mc-color-theme', t)
    // Show the "Applying…" indicator only for installed themes, whose branding
    // + overrides.css apply asynchronously. Built-ins / editor customs (L0) are
    // instant, so we don't flash for them. A ~150ms minimum-visible guard in
    // the apply effect below prevents flicker.
    const slug = t.startsWith('custom-') ? t.slice('custom-'.length) : ''
    if (slug && installedSlugsRef.current.has(slug)) {
      switchStartRef.current = Date.now()
      setThemeSwitching(true)
    }
    setColorThemeState(t)
    const m = (localStorage.getItem('mc-theme') as ModePreference) || 'system'
    broadcast(m, t)
    persistTheme({ color: t })
  }, [persistTheme])

  // Keep the installed-slug lookup (read in setColorTheme) in sync with the
  // loaded theme list, without changing setColorTheme's identity.
  useEffect(() => {
    installedSlugsRef.current = new Set(
      customThemes
        .filter(t => t.installed)
        .map(t => t.value.slice('custom-'.length))
    )
  }, [customThemes])

  // Apply the active installed theme's branding + scoped overrides.css; revert
  // for built-ins / L0. (Fonts are injected at load, scoped by data-theme.)
  // When a switch flipped `themeSwitching` on, clear it once the async
  // overrides fetch settles — held for a ~150ms minimum so it doesn't flicker.
  useEffect(() => {
    const active = colorTheme.startsWith('custom-')
      ? customThemeDataMap.get(colorTheme.slice('custom-'.length))
      : undefined
    try {
      applyThemeBranding(active)
      setBrandName(active?.assets?.branding?.botName ?? null)
    } catch {
      // A branding-application throw must not skip the overrides settle below,
      // which is what clears the "Applying…" indicator — otherwise it wedges.
      setBrandName(null)
    }
    let cancelled = false
    applyThemeOverrides(active).finally(() => {
      if (cancelled) return
      const remaining = Math.max(0, 150 - (Date.now() - switchStartRef.current))
      window.setTimeout(() => {
        if (!cancelled) setThemeSwitching(false)
      }, remaining)
    })
    return () => {
      cancelled = true
    }
  }, [colorTheme, customThemeDataMap])

  // Self-repair: a persisted selection that is no longer valid falls back to
  // the default built-in — a quick pre-apply validity check at boot (and after
  // any theme-list refresh). Two dangling cases:
  //   1. An unknown *built-in* value (e.g. a theme removed in a newer build,
  //      like the retired 'lumon') — can never become valid, so repair at once.
  //      A downstream-registered theme (registerTheme → REGISTERED_THEMES) is a
  //      valid built-in picker entry too, so it must count as known here — else
  //      selecting an edition theme (LCARS, Lumon, …) would bounce to the
  //      default, since it isn't in the core THEMES array.
  //   2. A dangling custom-<slug> whose pack is uninstalled — repair once the
  //      theme list has loaded (gated so a valid install isn't reset early).
  useEffect(() => {
    if (
      !colorTheme.startsWith('custom-') &&
      !THEMES.some(t => t.value === colorTheme) &&
      !REGISTERED_THEMES.some(t => t.value === colorTheme)
    ) {
      setColorTheme(DEFAULT_COLOR_THEME)
      return
    }
    if (!customThemesLoaded) return
    //   3. A custom-<slug> that a REGISTERED theme shadows. The pack is still
    //      installed, so case 2 does not fire, but the pack row is filtered out of
    //      `allThemes` — leaving the user on the near-unstyled pack rendering with
    //      no selected row in the picker and no way back. Migrate to the registered
    //      slug, which is the same theme properly styled, rather than resetting to
    //      the default and losing their choice entirely.
    //      Scoped to INSTALLED packs, matching the `allThemes` filter: an
    //      editor-created theme is never filtered out, so it is still selectable
    //      and must not be migrated away from the user.
    if (colorTheme.startsWith('custom-')) {
      const slug = colorTheme.slice('custom-'.length)
      const shadowed =
        REGISTERED_THEMES.some(t => t.value === slug) &&
        customThemes.some(t => t.value === colorTheme && t.installed)
      if (shadowed) {
        setColorTheme(slug)
        return
      }
      if (!customThemeDataMap.has(slug)) {
        setColorTheme(DEFAULT_COLOR_THEME)
      }
    }
  }, [customThemesLoaded, colorTheme, customThemeDataMap, customThemes, setColorTheme])

  /** Add a new custom theme via API, inject CSS, and select it. */
  const addCustomTheme = useCallback(async (data: Omit<CustomThemeData, 'slug'> & { slug?: string }) => {
    const res = await api.createTheme(data)
    if (!res.ok) throw new Error(res.error || 'Failed to create theme')
    const theme: CustomThemeData = res.theme
    injectCustomThemeCSS(theme)
    await loadCustomThemes()
    setColorTheme(`custom-${theme.slug}`)
    broadcastCustomThemesChanged()
    return theme
  }, [loadCustomThemes, setColorTheme])

  /** Delete a custom theme via API. */
  const deleteCustomTheme = useCallback(async (slug: string) => {
    await api.deleteTheme(slug)
    removeCustomThemeCSS(slug)
    if (colorTheme === `custom-${slug}`) {
      setColorTheme(DEFAULT_COLOR_THEME)
    }
    await loadCustomThemes()
    broadcastCustomThemesChanged()
  }, [colorTheme, setColorTheme, loadCustomThemes])

  // Combined themes list: built-in + registered + custom. `builtinThemes()` (not
  // `THEMES`) so a descriptive built-in name is resolved for the current language
  // on every render; every consumer reads `allThemes`, so none of them needs a
  // resolver.
  //
  // An installed pack whose slug matches a REGISTERED theme is dropped, because
  // the two would render as two picker rows for one theme and the pack row is the
  // broken one. `registerTheme()` de-duplicates against `THEMES` and
  // `REGISTERED_THEMES`, but an installed pack arrives asynchronously from
  // `GET /api/themes` — long after registration — so it cannot be caught there.
  // The values differ (`lcars` vs `custom-lcars`), which is exactly why nothing
  // flagged it: a registered theme's CSS is keyed to `[data-theme="lcars-dark"]`
  // while a pack renders under `[data-theme="custom-lcars-dark"]`, so the pack
  // copy shows only the flat variables in its `variables.json` and loses every
  // structural rule the registered theme ships. Registration wins: it is the
  // build-time contribution that carries the real stylesheet.
  // Scoped to INSTALLED packs. An editor-created custom theme that happens to
  // share a slug is the user's own object with edit/delete affordances keyed off
  // this list, so filtering it would hide it from the editor with no way to reach
  // or remove it — a worse failure than the duplicate row.
  const registeredSlugs = new Set(REGISTERED_THEMES.map(t => t.value))
  const allThemes: ThemeEntry[] = [
    ...builtinThemes(),
    ...REGISTERED_THEMES,
    ...customThemes.filter(
      t => !(t.installed && registeredSlugs.has(t.value.replace(/^custom-/, ''))),
    ),
  ]

  const markOnboarded = useCallback(() => {
    safeSetItem('mc-onboarded', '1')
    setOnboarded(true)
    persistTheme({ onboarded: true })
  }, [persistTheme])

  const markImportOnboarded = useCallback(() => {
    safeSetItem('mc-import-onboarded', '1')
    setImportOnboarded(true)
  }, [])

  const markPrivacyAcked = useCallback(() => {
    safeSetItem('mc-privacy-acked', '1')
    setPrivacyAcked(true)
  }, [])

  return {
    theme: resolved,
    preference: mode,
    cycle: cycleMode,
    setTheme: setMode_,
    colorTheme,
    setColorTheme,
    themeSwitching,
    allThemes,
    brandName,
    customThemes,
    customThemeDataMap,
    themeVersion,
    onboarded,
    importOnboarded,
    privacyAcked,
    themeBootReady,
    markOnboarded,
    markImportOnboarded,
    markPrivacyAcked,
    addCustomTheme,
    deleteCustomTheme,
    loadCustomThemes,
  }
}
