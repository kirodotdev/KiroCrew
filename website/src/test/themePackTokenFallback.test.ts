import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, it, expect } from 'vitest'
import { DESIGNED_TOKEN_FALLBACKS, buildCustomThemeCss } from '../hooks/themeCss'
import type { CustomThemeData } from '../hooks/useTheme'

/**
 * `variables.json` requires only `--bg`, `--text` and `--accent`, so a valid pack
 * can declare three of the 56 allowlisted tokens. The rest inherit from
 * `index.css`'s bare `:root`, which carries the DARK palette and has no light
 * counterpart — so a pack with a light `--bg` used to render dark-mode secondary
 * text on its own light surfaces. These tests pin the gap-fill that replaces that
 * inherit.
 */

/** The documented minimum: a light palette declaring only the three required vars. */
const minimalLightPack: CustomThemeData = {
  slug: 'pack',
  name: 'Pack',
  dark: { '--bg': '#060e2a', '--text': '#d4e6ff', '--accent': '#f4d03f' },
  light: { '--bg': '#e8f8f5', '--text': '#1a3a4a', '--accent': '#e67e22' },
} as unknown as CustomThemeData

/**
 * Declarations of the `index.css` rule whose selector list starts with `selector`.
 *
 * The parity test needs the built-in palette's own spelling of the eleven pairs
 * `DESIGNED_TOKEN_FALLBACKS` copies. `themeCss.ts` cannot read a stylesheet — the
 * module's boundary is that nothing in it touches the DOM — so the copy has to
 * exist; reading the sheet HERE is a test-time file read that boundary does not
 * forbid, and it is what stops the two spellings drifting apart unnoticed.
 */
function declarations(css: string, selector: string): Record<string, string> {
  const start = css.indexOf(selector)
  if (start === -1) throw new Error(`no rule matching ${selector} in index.css`)
  const open = css.indexOf('{', start)
  const close = css.indexOf('}', open)
  if (open === -1 || close === -1) throw new Error(`unterminated rule for ${selector}`)
  const out: Record<string, string> = {}
  for (const decl of css.slice(open + 1, close).split(';')) {
    const i = decl.indexOf(':')
    if (i === -1) continue
    out[decl.slice(0, i).trim()] = decl.slice(i + 1).trim()
  }
  return out
}

/** Split the emitted stylesheet into its two blocks so a test can target one. */
function block(css: string, mode: 'dark' | 'light'): string {
  const m = new RegExp(`\\[data-theme="custom-pack-${mode}"\\]\\{([^}]*)\\}`).exec(css)
  if (!m) throw new Error(`no ${mode} block in: ${css}`)
  return m[1]
}

describe('buildCustomThemeCss — tokens a pack omits are derived, not inherited', () => {
  it('gives --muted-fg the pack background instead of letting it inherit #fff', () => {
    // The measured failure: inherited #fff on a white card is 1.0:1.
    const light = block(buildCustomThemeCss('pack', minimalLightPack), 'light')
    expect(light).toContain('--muted-fg:#e8f8f5')
    expect(light).not.toContain('--muted-fg:#fff')
  })

  it('derives the muted ramp from the pack palette so it tracks polarity', () => {
    const light = block(buildCustomThemeCss('pack', minimalLightPack), 'light')
    // 75% of #1a3a4a into #e8f8f5, mixed in sRGB.
    expect(light).toContain('--muted:#4e6a75')
    expect(light).toContain('--muted-strong:#395764')
    expect(light).toContain('--text-strong:#1a3a4a')
  })

  it('derives the surfaces too, so a light pack does not get the dark --card', () => {
    // Measured before this: the pack omits --card, inherited #181b22 from the
    // dark :root, and its own dark --text then sat on it at 1.22:1.
    const light = block(buildCustomThemeCss('pack', minimalLightPack), 'light')
    expect(light).toContain('--card:#e0f0ee')
    expect(light).toContain('--panel:#d8e9e7')
    expect(light).toContain('--border:#cbdddd')
    expect(light).not.toContain('#181b22')
  })

  it('emits concrete hex, never a color-mix Tailwind would nest and drop', () => {
    // Tailwind builds `/40`-style alpha utilities by wrapping the token in its
    // own color-mix(in oklab, …); a nested color-mix argument does not survive
    // that, and the utility falls back to an inherited colour.
    const css = buildCustomThemeCss('pack', minimalLightPack)
    expect(css).not.toContain('color-mix')
  })

  it('derives from an rgb() palette too, so it is not left on the dark inherit', () => {
    // variables.json accepts rgb(); skipping it would leave that pack showing the
    // very defect this module fixes.
    const fnBg = {
      ...minimalLightPack,
      light: { '--bg': 'rgb(232, 248, 245)', '--text': '#1a3a4a', '--accent': '#e67e22' },
    } as unknown as CustomThemeData
    const light = block(buildCustomThemeCss('pack', fnBg), 'light')
    expect(light).toContain('--card:#e0f0ee')
    expect(light).toContain('--muted:#4e6a75')
    expect(light).toContain('--accent-fg:#000')
  })

  it('skips the ramp for a colour form it cannot mix, rather than guessing', () => {
    const named = {
      ...minimalLightPack,
      light: { '--bg': 'hsl(168 46% 93%)', '--text': '#1a3a4a', '--accent': '#e67e22' },
    } as unknown as CustomThemeData
    const light = block(buildCustomThemeCss('pack', named), 'light')
    expect(light).not.toContain('--card:')
    expect(light).not.toContain('--muted:')
    // The luminance pick needs only the fill, so it still applies.
    expect(light).toContain('--accent-fg:#000')
  })

  it('fills the gap in the dark block too, not only the light one', () => {
    const dark = block(buildCustomThemeCss('pack', minimalLightPack), 'dark')
    expect(dark).toContain('--muted-fg:#060e2a')
    expect(dark).toContain('--text-strong:#d4e6ff')
  })

  it('picks the accent foreground from the accent luminance', () => {
    const css = buildCustomThemeCss('pack', minimalLightPack)
    // #e67e22 is light enough to carry black; #f4d03f more so.
    expect(block(css, 'light')).toContain('--accent-fg:#000')
    expect(block(css, 'dark')).toContain('--accent-fg:#000')

    const darkAccent = {
      ...minimalLightPack,
      light: { '--bg': '#e8f8f5', '--text': '#1a3a4a', '--accent': '#2c3e50' },
    } as unknown as CustomThemeData
    expect(block(buildCustomThemeCss('pack', darkAccent), 'light')).toContain('--accent-fg:#fff')
  })

  it('derives a status foreground only when the pack declared its fill', () => {
    const withOk = {
      ...minimalLightPack,
      light: { ...minimalLightPack.light, '--ok': '#27ae60' },
    } as unknown as CustomThemeData
    expect(block(buildCustomThemeCss('pack', withOk), 'light')).toContain('--ok-fg:#000')
    // No --ok declared: guessing the fill would be inventing a palette.
    expect(block(buildCustomThemeCss('pack', minimalLightPack), 'light')).not.toContain('--ok-fg')
  })

  it('leaves a declared token alone rather than emitting a derived duplicate', () => {
    const declared = {
      ...minimalLightPack,
      light: {
        ...minimalLightPack.light,
        '--muted': '#3f7668',
        '--muted-fg': '#ffffff',
        '--accent-fg': '#123456',
      },
    } as unknown as CustomThemeData
    const light = block(buildCustomThemeCss('pack', declared), 'light')
    expect(light).toContain('--muted:#3f7668')
    expect(light).toContain('--muted-fg:#ffffff')
    expect(light).toContain('--accent-fg:#123456')
    expect(light).not.toContain('--muted:#4e6a75')
    expect(light).not.toContain('--muted-fg:#e8f8f5')
    expect(light).not.toContain('--accent-fg:#000')
  })

  it('picks a foreground from an rgb() fill as well as a hex one', () => {
    const fnAccent = {
      ...minimalLightPack,
      light: { '--bg': '#e8f8f5', '--text': '#1a3a4a', '--accent': 'rgb(230, 126, 34)' },
    } as unknown as CustomThemeData
    const light = block(buildCustomThemeCss('pack', fnAccent), 'light')
    expect(light).toContain('--accent-fg:#000')
    expect(light).toContain('--muted-fg:#e8f8f5')
  })

  // ── CSS injection ──
  //
  // A `themes/<slug>.json` can be written to disk directly, bypassing install
  // validation, and the theme-detail route hands the raw file back. Every custom
  // theme's CSS is injected into document.head on boot regardless of which theme
  // is selected, so a value that closes the block escapes into page-wide CSS.
  // `buildVars` sanitizes every value it emits; the derived copies must too.
  describe('a hostile pack value cannot escape the selector block', () => {
    const hostile = (light: Record<string, string>) =>
      ({ ...minimalLightPack, light } as unknown as CustomThemeData)

    it.each([
      ['brace break-out', '#000;}html{filter:invert(1)'],
      ['declaration break-out', '#000;position:fixed'],
      ['url() exfiltration', 'url(https://example.invalid/x)'],
      ['comment break-out', '#000*/;}html{opacity:0'],
    ])('drops a --text carrying a %s', (_label, payload) => {
      const css = buildCustomThemeCss('pack', hostile({
        '--bg': '#e8f8f5', '--text': payload, '--accent': '#e67e22',
      }))
      // Nothing may leave the two blocks, so the only braces are their own.
      expect(css.match(/\{/g)?.length).toBe(2)
      expect(css).not.toContain('filter:invert')
      expect(css).not.toContain('position:fixed')
      expect(css).not.toContain('url(')
      expect(css).not.toContain('opacity:0')
      // Fail closed: the ramp is dropped rather than derived from a bad value.
      expect(block(css, 'light')).not.toContain('--text-strong:')
      expect(block(css, 'light')).not.toContain('--card:')
    })

    it('drops a hostile --bg instead of copying it into --muted-fg', () => {
      const css = buildCustomThemeCss('pack', hostile({
        '--bg': '#fff;}html{display:none', '--text': '#1a3a4a', '--accent': '#e67e22',
      }))
      expect(css.match(/\{/g)?.length).toBe(2)
      expect(css).not.toContain('display:none')
      expect(block(css, 'light')).not.toContain('--muted-fg:')
    })

    it('drops a hostile fill instead of deriving a foreground from it', () => {
      const css = buildCustomThemeCss('pack', hostile({
        '--bg': '#e8f8f5', '--text': '#1a3a4a', '--accent': '#000;}html{filter:blur(4px)',
      }))
      expect(css.match(/\{/g)?.length).toBe(2)
      expect(css).not.toContain('filter:blur')
      expect(block(css, 'light')).not.toContain('--accent-fg:')
    })
  })

  // -- Translucent input --
  //
  // A translucent colour's rendered lightness depends on what is behind it, which
  // this module cannot know. Reading `rgba(0,0,0,.15)` as opaque black would pick
  // a WHITE foreground for a fill that actually renders pale: invisible text.
  describe('a non-opaque colour is refused, not read as opaque', () => {
    const withLight = (light: Record<string, string>) =>
      ({ ...minimalLightPack, light } as unknown as CustomThemeData)

    it.each([
      ['rgba()', 'rgba(0, 0, 0, 0.15)'],
      ['8-digit hex', '#00000026'],
      ['4-digit hex', '#0003'],
      ['slash alpha', 'rgb(0 0 0 / 0.15)'],
    ])('does not derive --accent-fg from a %s fill', (_label, accent) => {
      const light = block(buildCustomThemeCss('pack', withLight({
        '--bg': '#e8f8f5', '--text': '#1a3a4a', '--accent': accent,
      })), 'light')
      // White on a fill that renders pale is the exact failure being avoided.
      expect(light).not.toContain('--accent-fg')
      // The rest of the ramp does not depend on --accent, so it still applies.
      expect(light).toContain('--card:#e0f0ee')
    })

    it('skips the surface ramp when --bg is translucent', () => {
      const light = block(buildCustomThemeCss('pack', withLight({
        '--bg': 'rgba(232, 248, 245, 0.6)', '--text': '#1a3a4a', '--accent': '#e67e22',
      })), 'light')
      expect(light).not.toContain('--card:')
      expect(light).not.toContain('--muted:')
      // --muted-fg copies --bg, so it must be withheld for the same reason.
      expect(light).not.toContain('--muted-fg')
      // --accent is opaque and its pick needs only itself.
      expect(light).toContain('--accent-fg:#000')
    })

    it('withholds --text-strong and --card-fg when --text is translucent', () => {
      const light = block(buildCustomThemeCss('pack', withLight({
        '--bg': '#e8f8f5', '--text': '#1a3a4acc', '--accent': '#e67e22',
      })), 'light')
      expect(light).not.toContain('--text-strong')
      expect(light).not.toContain('--card-fg')
      expect(light).not.toContain('--card:')
    })

    it('still derives from a fully opaque 8-digit hex', () => {
      // A trailing ff is opaque, so the alpha check must not reject it.
      const light = block(buildCustomThemeCss('pack', withLight({
        '--bg': '#e8f8f5ff', '--text': '#1a3a4aff', '--accent': '#e67e22ff',
      })), 'light')
      expect(light).toContain('--card:#e0f0ee')
      expect(light).toContain('--muted:#4e6a75')
      expect(light).toContain('--accent-fg:#000')
    })
  })

  // -- Out-of-gamut channels --
  //
  // A browser clamps `rgb(300 300 300)` to white; the parser hands back the raw
  // 300, and the value sanitizer (char allowlist + function denylist) passes it.
  // Unclamped, `(300).toString(16)` is the three-digit '12c', so a derived token
  // would be `#12c12c12c` — a parseable hash-token that is not a colour, which
  // every consumer silently drops back to its inherited (dark) value.
  describe('an out-of-range channel is clamped, not emitted as a malformed hex', () => {
    const withLight = (light: Record<string, string>) =>
      ({ ...minimalLightPack, light } as unknown as CustomThemeData)

    const derivedHexes = (css: string) =>
      (block(css, 'light').match(/#[0-9a-fA-F]+/g) ?? [])

    it.each([
      ['above the gamut', 'rgb(300 300 300)'],
      ['below the gamut', 'rgb(-40 -40 -40)'],
    ])('keeps every derived value a well-formed hex for a %s --bg', (_l, bg) => {
      const css = buildCustomThemeCss('pack', withLight({
        '--bg': bg, '--text': '#1a3a4a', '--accent': '#e67e22',
      }))
      const light = block(css, 'light')
      // The ramp still runs — clamping is a repair, not a refusal.
      expect(light).toContain('--card:')
      for (const hex of derivedHexes(css)) {
        expect(hex.length === 4 || hex.length === 7 || hex.length === 9).toBe(true)
      }
      expect(light).not.toMatch(/#[0-9a-fA-F]{10,}/)
      expect(light).not.toMatch(/--[a-z-]+:#[0-9a-fA-F]*-/)
    })

    it('clamps to the same value the browser would render', () => {
      // rgb(300 300 300) clamps to #ffffff, so every MIXED token must match the
      // ones an explicitly-white pack gets. The copied tokens (--muted-fg copies
      // --bg, --card-fg copies --text) are excluded: they are passed through
      // verbatim, and the browser clamps a valid rgb() itself.
      const MIXED = new Set([
        '--card', '--bg-elevated', '--chrome', '--bg-accent', '--panel',
        '--bg-hover', '--card-hl', '--panel-strong', '--border',
        '--border-strong', '--border-hover', '--muted', '--muted-strong',
      ])
      const mixed = (bg: string) => {
        const light = block(buildCustomThemeCss('pack', withLight({
          '--bg': bg, '--text': '#000000', '--accent': '#e67e22',
        })), 'light')
        return light
          .split(';')
          .filter((d) => MIXED.has(d.split(':')[0]))
          .join(';')
      }
      expect(mixed('rgb(300 300 300)')).toBe(mixed('#ffffff'))
      expect(mixed('rgb(300 300 300)')).toContain('--card:#f5f5f5')
    })
  })

  // -- All-or-nothing --
  //
  // With an unparseable `--text` the surface ramp is skipped and `--card` stays
  // the inherited DARK value, so emitting the LIGHT designed set on top of it
  // would be worse than the pre-derivation inherit, which at least matched.
  describe('a palette is uniformly derived or uniformly inherited, never mixed', () => {
    const unmixableText = {
      ...minimalLightPack,
      light: { '--bg': '#e8f8f5', '--text': 'hsl(200 50% 20%)', '--accent': '#e67e22' },
    } as unknown as CustomThemeData

    it('withholds the light designed set when the surface ramp was skipped', () => {
      const light = block(buildCustomThemeCss('pack', unmixableText), 'light')
      // Precondition: the ramp really is skipped for this palette.
      expect(light).not.toContain('--card:')
      // So the light syntax/diff set must not be emitted onto a dark card.
      expect(light).not.toContain('--json-key:#001080')
      expect(light).not.toContain('--diff-add-text:#1a7f37')
      // Nor the dark one — the inherit already supplies it.
      expect(light).not.toContain('--json-key:')
    })

    it('withholds --muted-fg too, since --muted is still the inherited fill', () => {
      const light = block(buildCustomThemeCss('pack', unmixableText), 'light')
      expect(light).not.toContain('--muted-fg')
      // The --accent pick needs only --accent, so it still applies.
      expect(light).toContain('--accent-fg:#000')
    })
  })

  // -- Designed sets: syntax, diff --
  //
  // These cannot be mixed from the palette, but leaving them to inherit would
  // re-create the bug one layer up: they are DARK values, and the surfaces are
  // now derived light, so JSON and diff text would land light-on-light.
  describe('syntax and diff colours fall back to the matching polarity', () => {
    it('uses the light set for a light pack', () => {
      const light = block(buildCustomThemeCss('pack', minimalLightPack), 'light')
      expect(light).toContain('--json-key:#001080')
      expect(light).toContain('--json-str:#A31515')
      expect(light).toContain('--diff-add-text:#1a7f37')
      expect(light).toContain('--diff-meta-text:#1f2328')
      // The dark set must not leak in.
      expect(light).not.toContain('#9CDCFE')
      expect(light).not.toContain('#7ee787')
    })

    it('uses the dark set for the dark block', () => {
      const dark = block(buildCustomThemeCss('pack', minimalLightPack), 'dark')
      expect(dark).toContain('--json-key:#9CDCFE')
      expect(dark).toContain('--diff-add-text:#7ee787')
      expect(dark).not.toContain('#001080')
    })

    it('follows the pack --bg, not which block is being built', () => {
      // A pack may legitimately ship a DARK `light` block (an always-dark theme).
      const darkLightBlock = {
        ...minimalLightPack,
        light: { '--bg': '#000000', '--text': '#FFC97A', '--accent': '#E51E1E' },
      } as unknown as CustomThemeData
      const light = block(buildCustomThemeCss('pack', darkLightBlock), 'light')
      expect(light).toContain('--json-key:#9CDCFE')
      expect(light).not.toContain('--json-key:#001080')
    })

    it('leaves a declared syntax colour alone', () => {
      const declared = {
        ...minimalLightPack,
        light: { ...minimalLightPack.light, '--json-key': '#123456' },
      } as unknown as CustomThemeData
      const light = block(buildCustomThemeCss('pack', declared), 'light')
      expect(light).toContain('--json-key:#123456')
      expect(light).not.toContain('--json-key:#001080')
    })
  })

  it('writes only custom-* selectors, so no built-in palette can be affected', () => {
    const css = buildCustomThemeCss('pack', minimalLightPack)
    const selectors = css.match(/\[data-theme="[^"]+"\]/g) ?? []
    expect(selectors.length).toBeGreaterThan(0)
    for (const s of selectors) expect(s).toMatch(/^\[data-theme="custom-pack-(dark|light)"\]$/)
  })

  describe('the designed fallback set matches index.css', () => {
    const sheet = readFileSync(resolve(__dirname, '../index.css'), 'utf-8')
    // The FULL selector list, not a prefix: `:root,[data-theme="dark"]` and
    // `[data-theme="light"]` each also open an earlier shadows-only rule, and
    // matching that one would read eleven `undefined`s as agreement.
    const dark = declarations(sheet, ':root,[data-theme="dark"],[data-theme="amber-dark"]{')
    const light = declarations(sheet, '[data-theme="light"],[data-theme="amber-light"]{')

    it.each(Object.entries(DESIGNED_TOKEN_FALLBACKS))(
      '%s is spelled the same in both places',
      (token, [onLight, onDark]) => {
        expect(light[token]).toBe(onLight)
        expect(dark[token]).toBe(onDark)
      },
    )
  })
})



