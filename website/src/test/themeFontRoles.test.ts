import { describe, it, expect } from 'vitest'

import { buildThemeFontCss, buildCustomThemeCss, scopeOverridesCss } from '../hooks/themeCss'
import type { CustomThemeData, ThemeFontFace } from '../hooks/useTheme'

/**
 * Role-routed pack fonts: a pack's proportional face reaches the Sans option and
 * its monospace face reaches Mono, while System stays on the OS face.
 *
 * The routing is indirect on purpose and that indirection is what these tests
 * pin. The Font Family preference applies `--font-body` as an INLINE style on
 * <html>; an inline declaration outranks every selector, so a pack that declared
 * `--font-body` on its own `[data-theme=…]` block could never win — its font was
 * simply unreachable. Filling `--theme-font-sans` / `--theme-font-mono` instead,
 * and having the preference read THROUGH those tokens, is what makes both
 * directions work at once: the pack's face shows on the default option, and an
 * explicit Mono/System choice still overrides it.
 */

const theme = (fonts: ThemeFontFace[]): CustomThemeData => ({
  name: 'Pack',
  slug: 'pack',
  emoji: '🎨',
  dark: { '--bg': '#000', '--text': '#fff', '--accent': '#f0f' },
  light: { '--bg': '#fff', '--text': '#000', '--accent': '#f0f' },
  level: 1,
  assets: { fonts },
})

const face = (over: Partial<ThemeFontFace> = {}): ThemeFontFace => ({
  family: 'Manrope',
  src: 'styles/fonts/manrope-400.ttf',
  weight: 400,
  style: 'normal',
  ...over,
})

describe('buildThemeFontCss — role routing', () => {
  it('routes a sans face to --theme-font-sans and leaves the mono token unset', () => {
    const css = buildThemeFontCss('pack', theme([face({ role: 'sans' })]))
    expect(css).toContain('--theme-font-sans:')
    expect(css).not.toContain('--theme-font-mono:')
    // An unset token means the var() fallback applies, i.e. Kiro Crew's own
    // monospace stack — the "theme ships only one role" case.
  })

  it('routes a mono face to --theme-font-mono and leaves the sans token unset', () => {
    const css = buildThemeFontCss('pack', theme([
      face({ family: 'Plex Mono', src: 'styles/fonts/plex.ttf', role: 'mono' }),
    ]))
    expect(css).toContain('--theme-font-mono:')
    expect(css).not.toContain('--theme-font-sans:')
  })

  it('fills both tokens when the pack ships both roles', () => {
    const css = buildThemeFontCss('pack', theme([
      face({ role: 'sans' }),
      face({ family: 'Plex Mono', src: 'styles/fonts/plex.ttf', role: 'mono' }),
    ]))
    expect(css).toContain("--theme-font-sans:'Manrope'")
    expect(css).toContain("--theme-font-mono:'Plex Mono'")
  })

  it('treats a face with no role as proportional', () => {
    // Backward compatibility: every pack authored before roles existed shipped a
    // proportional face, so an absent role must not strand it.
    const css = buildThemeFontCss('pack', theme([face()]))
    expect(css).toContain("--theme-font-sans:'Manrope'")
    expect(css).not.toContain('--theme-font-mono:')
  })

  it('keys each token off the first USABLE face of that role, not the first declared', () => {
    // A rejected entry (bad src here) must not name the token, or the stack would
    // reference a family with no @font-face behind it and render nothing.
    const css = buildThemeFontCss('pack', theme([
      face({ family: 'Broken', src: 'evil/../../etc/passwd', role: 'sans' }),
      face({ family: 'Real', role: 'sans' }),
    ]))
    expect(css).toContain("--theme-font-sans:'Real'")
    expect(css).not.toContain('Broken')
  })

  it('emits both weights of one family as separate faces under one token', () => {
    const css = buildThemeFontCss('pack', theme([
      face({ weight: 400, role: 'sans' }),
      face({ src: 'styles/fonts/manrope-600.ttf', weight: 600, role: 'sans' }),
    ]))
    expect(css.match(/@font-face\{/g)).toHaveLength(2)
    expect(css.match(/--theme-font-sans:/g)).toHaveLength(1)
  })

  it('carries the script-fallback aliases in both role tokens', () => {
    // Without the aliases, Han/Devanagari/Bengali fall to per-character browser
    // fallback for every user of the pack — the failure the scriptFonts gate
    // exists to prevent, reproduced here at the pack layer.
    const css = buildThemeFontCss('pack', theme([
      face({ role: 'sans' }),
      face({ family: 'Plex Mono', src: 'styles/fonts/plex.ttf', role: 'mono' }),
    ]))
    expect(css).toContain('--theme-font-sans:\'Manrope\',var(--script-fallbacks),')
    expect(css).toContain('--theme-font-mono:\'Plex Mono\',var(--script-fallbacks-mono),')
  })

  it('returns nothing when no face is usable', () => {
    expect(buildThemeFontCss('pack', theme([face({ src: 'nope.ttf' })]))).toBe('')
    expect(buildThemeFontCss('pack', theme([]))).toBe('')
  })

  it('scopes the tokens to the pack own data-theme selectors', () => {
    const css = buildThemeFontCss('pack', theme([face({ role: 'sans' })]))
    expect(css).toContain('[data-theme="custom-pack-dark"],[data-theme="custom-pack-light"]{')
  })
})

describe('buildCustomThemeCss — the pack variable block reads the role tokens', () => {
  it('defers to the role tokens rather than hardcoding the built-in families', () => {
    // This block sits on the SAME [data-theme] selector the font CSS targets, so a
    // hardcoded stack here would out-specify :root and strand the pack's own faces.
    const css = buildCustomThemeCss('pack', theme([]))
    expect(css).toContain('--font-body:var(--theme-font-sans, var(--script-fallbacks),')
    expect(css).toContain('--mono:var(--theme-font-mono, var(--script-fallbacks-mono),')
  })
})

describe('scopeOverridesCss — font pins are dropped', () => {
  const pins = [
    "body{--font-body:'X',sans-serif}",
    "body{--mono:'X',monospace}",
    "body{--theme-font-sans:'X',sans-serif}",
    "body{--theme-font-mono:'X',monospace}",
    "body{font-family:'X',sans-serif}",
    // The shorthand sets the family too, and index.css styles body with the same
    // shorthand — a later same-specificity rule would win.
    "body{font:400 .875rem/1.55 'X',sans-serif}",
    "body:lang(ja){font-family:'X',sans-serif}",
    '[data-theme="custom-x-dark"] body{font-family:\'X\',sans-serif}',
    // Escaped property names: the browser decodes these while tokenizing, so a
    // raw-text-only test would let them through.
    "body{--font-b\\6f dy:'X',sans-serif}",
    "body{f\\6f nt:400 .875rem/1.55 'X',sans-serif}",
    // Uppercase escape: \\4F decodes to 'O' and standard property names are ASCII
    // case-insensitive, so decoding after a lowercase pass would miss it.
    "body{f\\4F nt:400 .875rem/1.55 'X',sans-serif}",
  ]

  it.each(pins)('drops %s', (css) => {
    // Install rejects such a pack outright; the runtime scoper is the enforced
    // boundary, so it must also drop a pin that never went through install
    // (hand-edited theme store, pre-upgrade pack).
    const r = scopeOverridesCss(css)
    expect(r.kept).toBe(0)
    expect(r.dropped).toBe(1)
  })

  it.each([
    ".topbar{font-family:'X',sans-serif}",
    ".topbar{font:600 12px/1.2 'X',sans-serif}",
    '.code-block{font-family:\'X\',monospace}',
  ])('keeps %s — a face on one surface is theming, not a pin', (css) => {
    const r = scopeOverridesCss(css)
    expect(r.kept).toBe(1)
  })

  it.each([
    'body{font-weight:500}',
    'body{font-size:15px}',
    "body{font-feature-settings:'ss01'}",
  ])('keeps %s — the shorthand match must not swallow other font-* longhands', (css) => {
    const r = scopeOverridesCss(css)
    expect(r.kept).toBe(1)
  })

  it.each([
    'body{--label:" \\66 ont:"}',
    'body{content:"x;font:y";color:#eee}',
  ])('keeps %s — `font:` inside a VALUE is not a declaration', (css) => {
    // Testing the whole block instead of the property names would drop these,
    // which is both a broken pack and a split from the install-time layer.
    const r = scopeOverridesCss(css)
    expect(r.kept).toBe(1)
  })

  it('keeps an unrelated body rule', () => {
    const r = scopeOverridesCss('body{background:#101010;color:#eee}')
    expect(r.kept).toBe(1)
  })
})
