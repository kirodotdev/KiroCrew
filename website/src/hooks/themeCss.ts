/**
 * Theme CSS text: every builder, sanitizer and filter that produces the
 * stylesheet text `useTheme.tsx` injects.
 *
 * Split out of `useTheme.tsx` so the i18n lint does not read stylesheet text as
 * user-visible copy — the same named-boundary idiom as
 * `apps/md-notebook/styles.ts` and `apps/spec-builder/inlineStyles.ts`, and this
 * module is ignored by path in `website/eslint.i18n.config.js`. The rest of
 * `useTheme.tsx` stays fully gated, which is the point of the split: the theme
 * picker's 19 display names live there and must keep being reported.
 *
 * The boundary is mechanical, not a judgement call: **nothing here touches the
 * DOM.** Every export is `string`/data in, `string` out. The `<style>` tags,
 * `document.head` writes and `style.setProperty` calls stay in `useTheme.tsx`,
 * so this module has no path to the screen at all — a literal added here can
 * only ever reach a CSS parser.
 *
 * Keep it that way. Any copy added to this file will NOT be reported by the
 * i18n gate; copy belongs in the catalog and its render site belongs in a
 * gated module.
 */

import { sanitizeCssValue } from '../lib/cssSanitize'
import type { CustomThemeData } from './useTheme'

// Allowlist of allowed CSS custom property names for themes.
// Only these variables will be injected — unknown keys are silently dropped.
const ALLOWED_CSS_VARS = new Set([
  '--bg', '--bg-accent', '--bg-elevated', '--bg-hover',
  '--card', '--card-fg', '--card-hl',
  '--panel', '--panel-strong', '--chrome',
  '--text', '--text-strong', '--muted', '--muted-strong', '--muted-fg',
  '--border', '--border-strong', '--border-hover',
  '--accent', '--accent-fg', '--accent-hover', '--accent-subtle',
  '--accent-glow', '--ring',
  '--ok', '--ok-fg', '--ok-subtle', '--warn', '--warn-fg', '--warn-subtle',
  '--danger', '--danger-fg', '--danger-subtle', '--info', '--info-fg',
  '--aim', '--aim-fg', '--aim-subtle',
  '--clarify', '--clarify-subtle',
  '--json-key', '--json-str', '--json-num', '--json-bool',
  '--diff-add', '--diff-add-text',
  '--diff-del', '--diff-del-text',
  '--diff-hunk', '--diff-hunk-text', '--diff-meta-text',
  '--shadow-sm', '--shadow-md', '--shadow-lg',
  // Terminal ANSI hues a theme may override; the other fourteen entries the
  // built-in terminal needs are derived from --bg / --text / --danger / --ok /
  // --warn / --info above. Kept in sync with _THEME_CSS_VARS on the backend
  // (TestAllowlistParity asserts the two sets are equal).
  '--term-magenta', '--term-cyan',
])

/**
 * Positive-allowlist CSS value sanitizer lives in src/lib/cssSanitize.ts so
 * WidgetFrame and any other surface that serializes theme vars uses the same
 * filter. See that file for the security rationale.
 */
const escapeCssValue = sanitizeCssValue

/**
 * Build a custom theme's dark + light CSS variable blocks.
 *
 * `slug` must ALREADY be sanitized — `safeSlug` is the one sanitization site, so
 * the `[data-theme]` selector here and the `<style>` element id the caller
 * derives can never disagree. Returns '' for an empty slug (nothing to paint).
 */
export function buildCustomThemeCss(slug: string, theme: CustomThemeData): string {
  if (!slug) return ''

  const buildVars = (vars: Record<string, string>) =>
    Object.entries(vars)
      .filter(([k]) => ALLOWED_CSS_VARS.has(k))
      .map(([k, v]): [string, string] => [k, escapeCssValue(v)])
      .filter(([, v]) => v !== '')  // drop entries with empty/rejected values
      .map(([k, v]) => `${k}:${v}`)
      .join(';')

  // Static defaults (not user-controlled). Both font stacks read the role tokens
  // first: this block sits on the same [data-theme] selector a pack's font CSS
  // targets, so hardcoding the built-in families here would out-specify :root and
  // strand a pack's own faces. An unfilled role falls through to Kiro Crew's stack.
  const darkDefaults =
    '--font-body:var(--theme-font-sans, var(--script-fallbacks),\'Space Grotesk\',-apple-system,BlinkMacSystemFont,sans-serif);' +
    '--mono:var(--theme-font-mono, var(--script-fallbacks-mono),\'JetBrains Mono\',ui-monospace,SFMono-Regular,monospace);' +
    '--radius-sm:6px;--radius-md:8px;--radius-lg:12px;--radius-xl:16px;' +
    'color-scheme:dark;'
  const lightDefaults =
    '--font-body:var(--theme-font-sans, var(--script-fallbacks),\'Space Grotesk\',-apple-system,BlinkMacSystemFont,sans-serif);' +
    '--mono:var(--theme-font-mono, var(--script-fallbacks-mono),\'JetBrains Mono\',ui-monospace,SFMono-Regular,monospace);' +
    '--radius-sm:6px;--radius-md:8px;--radius-lg:12px;--radius-xl:16px;' +
    'color-scheme:light;'

  const darkCss = buildVars(theme.dark)
  const lightCss = buildVars(theme.light)

  return (
    `[data-theme="custom-${slug}-dark"]{${darkCss};${darkDefaults}}\n` +
    `[data-theme="custom-${slug}-light"]{${lightCss};${lightDefaults}}`
  )
}

// ── Level 1 (branded) asset paths ──

/** The backend asset route for one installed theme pack. */
export const assetBase = (slug: string) => `/api/theme/${encodeURIComponent(slug)}/assets`
/** Reduce a theme slug to the chars that are safe in a CSS selector and an element id. */
export const safeSlug = (slug: string) => slug.replace(/[^a-z0-9-]/g, '')
const _safeFamily = (f: string) => f.replace(/[^A-Za-z0-9 _-]/g, '')
// Sanitize a branding asset relative path (e.g. "branding/logo.svg"): allow only
// safe chars, reject traversal/absolute. Defense-in-depth on top of the backend
// asset route's own path-containment; returns '' when unusable.
export const safeAssetPath = (p: string): string => {
  const cleaned = (p || '').replace(/[^a-z0-9./_-]/gi, '')
  if (!cleaned || cleaned.startsWith('/') || cleaned.split('/').includes('..')) return ''
  return cleaned
}

/** A `url()` CSS VALUE addressing one asset in an installed pack, for a custom property. */
export const assetUrlValue = (slug: string, rel: string) => `url('${assetBase(slug)}/${rel}')`

/**
 * Build @font-face rules + the role font tokens for an installed theme, scoped to
 * that theme's data-theme selectors. Returns '' when no declared face is usable,
 * so the caller injects no stylesheet at all.
 *
 * A pack tags each face with a role: `sans` faces fill `--theme-font-sans`,
 * `mono` faces fill `--theme-font-mono`. Those tokens are what the Font Family
 * preference reads through, which is what keeps the routing honest — a Sans
 * selection picks up the pack's proportional face, a Mono selection picks up its
 * monospace face, System stays on the OS face because it reads no token, and an
 * unfilled role falls back to Kiro Crew's own stack. Writing `--font-body`
 * directly here instead would be unreachable: the preference applies it as an
 * inline style on the same <html> element, and inline outranks any selector.
 *
 * `slug` must already be sanitized (see `buildCustomThemeCss`).
 */
export function buildThemeFontCss(slug: string, theme: CustomThemeData): string {
  const fonts = theme.assets?.fonts || []
  const faces: string[] = []
  // Track, per role, the family of the FIRST face that actually made it into the
  // stylesheet — an earlier entry may have been skipped (bad family/src/format),
  // in which case keying a token off it would name a family with no @font-face
  // behind it.
  const firstEmitted: { sans: string; mono: string } = { sans: '', mono: '' }
  for (const f of fonts) {
    const fam = _safeFamily(f.family || '')
    const file = (f.src || '').replace(/[^a-z0-9./_-]/gi, '')
    if (!fam || !file.startsWith('styles/fonts/')) continue
    const fmt = file.endsWith('.woff2') ? 'woff2' : file.endsWith('.ttf') ? 'truetype' : ''
    if (!fmt) continue
    const weight = typeof f.weight === 'number' && f.weight >= 100 && f.weight <= 900 ? f.weight : 400
    const style = f.style === 'italic' ? 'italic' : 'normal'
    const role = f.role === 'mono' ? 'mono' : 'sans'
    faces.push(
      `@font-face{font-family:'${fam}';` +
        `src:url('${assetBase(slug)}/${file}') format('${fmt}');` +
        `font-weight:${weight};font-style:${style};font-display:swap;}`
    )
    if (!firstEmitted[role]) firstEmitted[role] = fam
  }
  if (!faces.length) return ''
  const tokens: string[] = []
  if (firstEmitted.sans) {
    tokens.push(`--theme-font-sans:'${firstEmitted.sans}',var(--script-fallbacks),'Space Grotesk',-apple-system,BlinkMacSystemFont,sans-serif;`)
  }
  if (firstEmitted.mono) {
    tokens.push(`--theme-font-mono:'${firstEmitted.mono}',var(--script-fallbacks-mono),'JetBrains Mono',ui-monospace,SFMono-Regular,monospace;`)
  }
  if (!tokens.length) return faces.join('\n')
  return (
    faces.join('\n') +
    `\n[data-theme="custom-${slug}-dark"],[data-theme="custom-${slug}-light"]{` +
    tokens.join('') +
    '}'
  )
}

// ── §4.2/§5.1 runtime positive-selector scoper ──
// A selector group from overrides.css is kept only if EVERY selector, after
// stripping one optional leading [data-theme="…"] / html[data-theme…] scoping
// prefix, targets one of the 10 allowlisted surfaces (optionally with extra
// chained classes/pseudo-classes on the SAME base — no descendant/child/sibling
// combinators, no ids/attribute selectors, never the forbidden set).
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

// Mirror of the backend font-pin denylist (`_overrides_font_violation`): a pack's
// fonts come from theme.json's role-tagged `fonts` list, never from overrides.css.
// A pin here lands the font on a surface BELOW where the Font Family preference is
// applied, so the user's Mono/System choice would silently stop working. Install
// rejects such a pack outright; dropping the rule at runtime too keeps a pack that
// never went through that check (hand-edited store, pre-upgrade install) from
// taking the preference away.
const _FONT_PIN_PROPS = ['--font-body', '--mono', '--theme-font-sans', '--theme-font-mono']
// Surfaces broad enough that a font-family on them shadows the whole UI. Narrower
// ones (.topbar, .code-block, button.primary) stay free to set their own face.
const _BROAD_FONT_SURFACES = new Set(['body', 'html', '*', ':root'])

// Quoted spans are removed before declarations are parsed, so a `;` or `:` inside
// a string cannot desync the split — the same property the backend's string-aware
// tokenizer has.
const _CSS_STRING_RE = /"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'/g

/**
 * The property names a declaration block declares, normalized the way a browser
 * normalizes them.
 *
 * Property NAMES only, not the raw block: a regex over the whole block also
 * matches text inside a VALUE — a decoded string such as `" \66 ont:"` reads as a
 * declaration — which would drop a legitimate rule and split this layer from the
 * install-time one. Decoding runs BEFORE the lowercase pass because `\4F` decodes
 * to an uppercase letter and property names are ASCII case-insensitive, so
 * lowercasing first leaves `fOnt` unmatched while the browser applies it as `font`.
 *
 * Only a top-level `;` ends a declaration. A `;` nested in a function — the common
 * shape being a data URL, `--label:url(data:text/plain;base64,…)` — belongs to the
 * value, and treating it as a separator fabricates a declaration out of the tail,
 * which drops a rule the backend's own paren-aware tokenizer accepts.
 */
function _declaredProps(block: string): string[] {
  const out: string[] = []
  const src = block.replace(_CSS_STRING_RE, '""')
  let depth = 0
  let buf = ''
  const flush = () => {
    // The FIRST colon separates name from value; a property name cannot contain one.
    const colon = buf.indexOf(':')
    if (colon >= 0) out.push(_decodeCssEscapes(buf.slice(0, colon)).trim().toLowerCase())
    buf = ''
  }
  for (const ch of src) {
    if (ch === '(' || ch === '[' || ch === '{') depth++
    else if (ch === ')' || ch === ']' || ch === '}') {
      if (depth > 0) depth--
    } else if (ch === ';' && depth === 0) {
      flush()
      continue
    }
    buf += ch
  }
  flush()
  return out
}

function _fontPinDenied(prelude: string, block: string): boolean {
  const props = _declaredProps(block)
  if (props.some((p) => _FONT_PIN_PROPS.includes(p))) return true
  // `font` as well as `font-family`: the shorthand sets the family, so matching
  // only the longhand leaves the guarantee one keyword away from a bypass. The
  // other font-* longhands (font-weight, font-size, …) set no family and stay
  // legitimate theming, which exact matching preserves.
  if (!props.some((p) => p === 'font' || p === 'font-family')) return false
  return prelude.split(',').some((sel) => {
    const base = sel
      .trim()
      .toLowerCase()
      .replace(/^(?:html)?\s*\[data-theme[^\]]*\]\s*/i, '')
      .trim()
    // The pseudo strip only applies when something remains in front of it —
    // otherwise it would consume a bare `:root`, itself a broad surface.
    const withoutPseudo = base.replace(/::?[a-z-]+(?:\([^)]*\))?$/, '').trim()
    return _BROAD_FONT_SURFACES.has(withoutPseudo || base)
  })
}

/**
 * A human-readable identifier for one dropped rule, for the Settings notice and
 * the console diagnostic. The selector is what a pack author greps their own
 * overrides.css for, so it is reported verbatim (trimmed to one line and capped —
 * this is untrusted pack text headed for the DOM as a React text node, so length
 * is the only concern). A font pin also names the offending property, because the
 * selector alone (`body`) does not say what was wrong with the rule.
 */
const _DROPPED_ID_MAX = 80

function _droppedRuleId(prelude: string, block: string, fontPin: boolean): string {
  const sel = prelude.replace(/\s+/g, ' ').trim()
  let id = sel
  if (fontPin) {
    const prop = _declaredProps(block).find(
      (p) => _FONT_PIN_PROPS.includes(p) || p === 'font' || p === 'font-family',
    )
    if (prop) id = `${sel} { ${prop} }`
  }
  return id.length > _DROPPED_ID_MAX ? `${id.slice(0, _DROPPED_ID_MAX - 1)}…` : id
}

export function scopeOverridesCss(css: string): {
  css: string
  kept: number
  dropped: number
  /** One entry per dropped rule (selector, plus the offending property for a
   * font pin) so the drop can be REPORTED, not merely counted. Same length as
   * `dropped`. */
  droppedRules: string[]
} {
  const src = css.replace(/\/\*[\s\S]*?\*\//g, '') // strip comments
  let kept = 0
  let dropped = 0
  const droppedRules: string[] = []

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
          const fontPin = _fontPinDenied(prelude, block)
          if (_declDenied(block) || fontPin) {
            dropped++
            droppedRules.push(_droppedRuleId(prelude, block, fontPin))
          } else {
            kept++
            out.push(`${prelude}{${block}}`)
          }
        } else {
          dropped++
          droppedRules.push(_droppedRuleId(prelude, block, false))
        }
      } else {
        buf += ch
        i++
      }
    }
    return out.join('\n')
  }

  return { css: walk(src), kept, dropped, droppedRules }
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
  return safeAssetPath(stack.join('/'))
}

/** Rewrite relative url() refs in scoped overrides.css to absolute asset URLs. */
export function rewriteOverridesUrls(css: string, slug: string): string {
  const base = assetBase(slug)
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
