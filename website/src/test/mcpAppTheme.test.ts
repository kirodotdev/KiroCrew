// Token-map and resolver properties for the MCP App (SEP-1865) host theme
// variables. Properties 1-8, 11, 18 and 19 are the map/resolver half and live
// here; the frame half (Properties 9, 10, 12-17) lives in McpAppFrame.test.tsx
// and mcpAppSrcdoc.test.ts. The numbering is a single enumeration shared across
// those three files. Subsystem spec: docs/system-specs/modules/mcp-apps.md.
//
// happy-dom, like jsdom, resolves NO cascade: a custom property only reads back
// through getComputedStyle(documentElement) when it was set INLINE on that same
// element (the pattern iconContrast.test.ts and WidgetFrame.test.tsx rely on).
// So every property that needs a resolved token seeds documentElement.style
// directly and clears it between tests — the same seeding shape
// PhasedViewTheme.test.tsx uses when the DOM cannot run the real cascade, here
// reading the actual per-theme values out of index.css through
// themePalette.resolveVar for Property 18.
import { describe, it, expect, afterEach, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import {
  MCP_UI_COLOR_VARIABLE_KEYS,
  MCP_UI_NON_COLOR_VARIABLE_KEYS,
  COLOR_TOKEN_MAP,
  NON_COLOR_TOKEN_MAP,
  INFO_WASH,
  readMcpAppStyleVariables,
} from '../lib/mcpAppTheme'
import { sanitizeCssValue } from '../lib/cssSanitize'
import { THEMES, themeDataAttribute } from '../hooks/useTheme'
import { resolveVar } from './themePalette'

// ── Seeding helpers ─────────────────────────────────────────────────────────

/** Every dashboard token name a color key reads. Derived from the map so a seed
 *  set can never drift from what the resolver actually reads. */
const COLOR_TOKEN_NAMES = [
  ...new Set(Object.values(COLOR_TOKEN_MAP).map((s) => s.name)),
]

/** The same, for the non-color keys. Derived from the map for the same reason —
 *  and load-bearing now that NO key resolves from a literal: a non-color key
 *  appears in the payload only if its token is seeded, so any test asserting that
 *  non-color keys survive has to seed these explicitly. */
const NON_COLOR_TOKEN_NAMES = [
  ...new Set(Object.values(NON_COLOR_TOKEN_MAP).map((s) => s!.name)),
]

/** A benign, sanitizer-surviving value keyed to the token name, so a mismatch is
 *  legible in a failure ("--bg had --card's value"). Hex is the simplest domain
 *  member; the exact value never matters to these properties. */
function seedValue(name: string): string {
  // Deterministic 6-hex-digit value per token name.
  let h = 0
  for (const ch of name) h = (h * 31 + ch.charCodeAt(0)) & 0xffffff
  return '#' + h.toString(16).padStart(6, '0')
}

/** Seed a complete, valid color-token set onto documentElement so the resolver's
 *  color group resolves in full. Returns the map of what was seeded. */
function seedAllColorTokens(): Record<string, string> {
  const seeded: Record<string, string> = {}
  for (const name of COLOR_TOKEN_NAMES) {
    const v = seedValue(name)
    document.documentElement.style.setProperty(name, v)
    seeded[name] = v
  }
  return seeded
}

/** The non-color half of the same seeding. */
function seedAllNonColorTokens(): Record<string, string> {
  const seeded: Record<string, string> = {}
  for (const name of NON_COLOR_TOKEN_NAMES) {
    const v = seedValue(name)
    document.documentElement.style.setProperty(name, v)
    seeded[name] = v
  }
  return seeded
}

/**
 * Drive the resolver from a fixed token→value map by spying on getComputedStyle,
 * the WidgetFrame.test.tsx pattern. Used where a value must reach sanitizeCssValue
 * verbatim: happy-dom's CSSOM normalizes a value on `setProperty` (it eats an
 * embedded `;` and won't store a value it cannot parse), so an inline seed cannot
 * deliver a hostile or exotic string to the resolver — a spy can.
 */
function withComputedTokens(vars: Record<string, string>): void {
  vi.spyOn(window, 'getComputedStyle').mockImplementation(((el: Element) => {
    if (el === document.documentElement) {
      return {
        getPropertyValue: (name: string) => vars[name] ?? '',
      } as unknown as CSSStyleDeclaration
    }
    // Non-root elements are irrelevant to the resolver; return an empty reader.
    return { getPropertyValue: () => '' } as unknown as CSSStyleDeclaration
  }) as typeof window.getComputedStyle)
}

/** A complete color-token map with benign values, for the spy path. */
function goodColorTokens(): Record<string, string> {
  const out: Record<string, string> = {}
  for (const name of COLOR_TOKEN_NAMES) out[name] = seedValue(name)
  return out
}

/** Every token, colour and not, with benign values — for the spy path. */
function goodAllTokens(): Record<string, string> {
  const out = goodColorTokens()
  for (const name of NON_COLOR_TOKEN_NAMES) out[name] = seedValue(name)
  return out
}

afterEach(() => {
  vi.restoreAllMocks()
  // A single wipe of the inline style clears every seeded custom property, so no
  // seed leaks into the next test's getComputedStyle read.
  document.documentElement.removeAttribute('style')
})

// ── Properties 1 & 2 (Requirements 1.3, 2.3, 5.6) ────────────────

describe('mcpAppTheme — key subset and map totality (Properties 1, 2)', () => {
  const PROTOCOL_UNION = new Set<string>([
    ...MCP_UI_COLOR_VARIABLE_KEYS,
    ...MCP_UI_NON_COLOR_VARIABLE_KEYS,
  ])
  const UNION_LEN =
    MCP_UI_COLOR_VARIABLE_KEYS.length + MCP_UI_NON_COLOR_VARIABLE_KEYS.length

  // Property 1: Emitted keys are a subset of the protocol union, and the result
  // is at most the combined union length. Validates: Requirements 1.3, 5.6.
  it('emits only protocol-union keys, capped at the union length', () => {
    seedAllColorTokens()
    const result = readMcpAppStyleVariables()
    expect(result).not.toBeNull()
    const keys = Object.keys(result!)
    for (const k of keys) expect(PROTOCOL_UNION.has(k)).toBe(true)
    expect(keys.length).toBeLessThanOrEqual(UNION_LEN)
  })

  // The subset must hold in the degraded (colors missing) shape too, so the cap
  // is not an artifact of a fully-seeded document.
  it('stays a subset when the color group is absent', () => {
    // Only the non-color tokens seeded → color group discarded, non-color kept.
    // Seeded explicitly: with no literal sources left, an unseeded document
    // resolves NOTHING and this would assert over an empty result.
    seedAllNonColorTokens()
    const result = readMcpAppStyleVariables()
    expect(result).not.toBeNull()
    expect(Object.keys(result!).length).toBeGreaterThan(0)
    for (const k of Object.keys(result!)) {
      expect(PROTOCOL_UNION.has(k)).toBe(true)
      expect(k.startsWith('--color-')).toBe(false)
    }
  })

  // Property 2: Color coverage is total in the map — every color key is mapped.
  // The compiler enforces this via Record<McpUiColorVariableKey, ColorSource>;
  // asserted at runtime so a cast cannot erode it. Validates: Requirements 2.3.
  it('maps every color key in COLOR_TOKEN_MAP', () => {
    for (const key of MCP_UI_COLOR_VARIABLE_KEYS) {
      expect(COLOR_TOKEN_MAP[key]).toBeDefined()
      // 'token' or 'wash' — both read a theme token; never 'literal'.
      expect(['token', 'wash']).toContain(COLOR_TOKEN_MAP[key].from)
    }
    // No stray keys beyond the union.
    expect(Object.keys(COLOR_TOKEN_MAP).sort()).toEqual(
      [...MCP_UI_COLOR_VARIABLE_KEYS].sort(),
    )
  })

  // EVERY source, in BOTH maps, reads a stored dashboard token. A literal would be
  // a value with no source of truth — nothing could tell an app painting `14px`
  // from what the dashboard actually renders, and the next change to the real
  // scale would not move it. Kiro Crew stores no typography scale (the only
  // `--font-*` properties in index.css are the two families), so those protocol
  // keys are OMITTED under SEP-1865's graceful-degradation rule rather than
  // guessed. This fails if a literal source kind is reintroduced.
  it('resolves every mapped key from a token, never a hardcoded value', () => {
    const sources = [
      ...Object.entries(COLOR_TOKEN_MAP),
      ...Object.entries(NON_COLOR_TOKEN_MAP),
    ]
    for (const [key, src] of sources) {
      expect(['token', 'wash'], `${key} must read a token`).toContain(src!.from)
      expect(src!.name, `${key} must name a token`).toMatch(/^--[a-z]/)
    }
  })

  // The omissions are deliberate and enumerated, so dropping a key nobody decided
  // to drop is a red test rather than a silent gap in the payload.
  it('omits exactly the protocol keys the dashboard stores no token for', () => {
    const unmapped = MCP_UI_NON_COLOR_VARIABLE_KEYS
      .filter((k) => !NON_COLOR_TOKEN_MAP[k])
    expect(unmapped.sort()).toEqual([
      '--border-radius-full', '--border-radius-xs', '--border-width-regular',
      '--font-heading-2xl-line-height', '--font-heading-2xl-size',
      '--font-heading-3xl-line-height', '--font-heading-3xl-size',
      '--font-heading-lg-line-height', '--font-heading-lg-size',
      '--font-heading-md-line-height', '--font-heading-md-size',
      '--font-heading-sm-line-height', '--font-heading-sm-size',
      '--font-heading-xl-line-height', '--font-heading-xl-size',
      '--font-heading-xs-line-height', '--font-heading-xs-size',
      '--font-text-lg-line-height', '--font-text-lg-size',
      '--font-text-md-line-height', '--font-text-md-size',
      '--font-text-sm-line-height', '--font-text-sm-size',
      '--font-text-xs-line-height', '--font-text-xs-size',
      '--font-weight-bold', '--font-weight-medium', '--font-weight-normal',
      '--font-weight-semibold',
      '--shadow-hairline',
    ].sort())
  })
})

// ── Properties 3 & 4 (Requirements 2.1, 2.2, 2.4) ────────────────

describe('mcpAppTheme — all-or-nothing color delivery (Properties 3, 4)', () => {
  const colorKeys = (r: Record<string, string> | null) =>
    Object.keys(r ?? {}).filter((k) => k.startsWith('--color-'))
  const nonColorKeys = (r: Record<string, string> | null) =>
    Object.keys(r ?? {}).filter((k) => !k.startsWith('--color-'))

  // Property 3: Color delivery is all-or-nothing — every color key or none.
  // Validates: Requirements 2.1, 2.2.
  it('emits every color key when the token set is complete', () => {
    seedAllColorTokens()
    const result = readMcpAppStyleVariables()
    expect(colorKeys(result).sort()).toEqual([...MCP_UI_COLOR_VARIABLE_KEYS].sort())
  })

  it('emits no color key when the token set is empty', () => {
    const result = readMcpAppStyleVariables()
    expect(colorKeys(result)).toEqual([])
  })

  // Property 4: A missing color token discards ONLY the color group — clearing
  // one mapped token drops all --color-*, while non-color keys survive.
  // Validates: Requirements 2.2, 2.4.
  it('discards only the color group when one mapped token is missing', () => {
    seedAllColorTokens()
    seedAllNonColorTokens()
    // Clear exactly one token that a color key reads.
    const cleared = COLOR_TOKEN_MAP['--color-text-primary'].name // '--text'
    document.documentElement.style.removeProperty(cleared)

    const result = readMcpAppStyleVariables()
    // Not null: the non-color tokens still resolve.
    expect(result).not.toBeNull()
    // No color key survives — the whole group is discarded.
    expect(colorKeys(result)).toEqual([])
    // Non-color keys survive, which is what makes the discard SCOPED rather than
    // total: every one of them is a token read, so they are all present here.
    expect(nonColorKeys(result).sort()).toEqual(Object.keys(NON_COLOR_TOKEN_MAP).sort())
  })
})

// ── Property 5 (Requirement 2.5) ─────────────────────────────────

describe('mcpAppTheme — color sources are allowlisted tokens (Property 5)', () => {
  // Parse ALLOWED_CSS_VARS out of themeCss.ts as source text, mirroring
  // TestAllowlistParity in test/test_theme_css_security.py: the two lists cannot
  // diverge with this test still green, so a token rename on either side is a red
  // test rather than a silently unthemed color group.
  const ALLOWED_CSS_VARS = (() => {
    const src = readFileSync(
      join(__dirname, '..', 'hooks', 'themeCss.ts'),
      'utf-8',
    )
    const match = /ALLOWED_CSS_VARS = new Set\(\s*\[([\s\S]*?)\]\s*\)/.exec(src)
    if (!match) throw new Error('ALLOWED_CSS_VARS set literal not found in themeCss.ts')
    const names = [...match[1].matchAll(/['"](--[A-Za-z0-9-]+)['"]/g)].map((m) => m[1])
    if (!names.length) throw new Error('no CSS vars parsed out of ALLOWED_CSS_VARS')
    return new Set(names)
  })()

  // Confidence check on the parser itself: an empty or wrong set would make the
  // membership assertions below pass vacuously.
  it('parses the real allowlist out of themeCss.ts', () => {
    expect(ALLOWED_CSS_VARS.has('--bg')).toBe(true)
    expect(ALLOWED_CSS_VARS.has('--text')).toBe(true)
    expect(ALLOWED_CSS_VARS.size).toBeGreaterThan(20)
  })

  // Property 5: Every COLOR_TOKEN_MAP entry reads an allowlisted token — either
  // directly or as the base of a wash — and never a literal. A wash is still a
  // token read, so it is subject to the same rename guard.
  // Validates: Requirements 2.5.
  it('sources every color key from an allowlisted token, never a literal', () => {
    const violations: string[] = []
    for (const key of MCP_UI_COLOR_VARIABLE_KEYS) {
      const src = COLOR_TOKEN_MAP[key]
      if (src.from !== 'token' && src.from !== 'wash') {
        violations.push(`${key} is a literal, not a token`)
        continue
      }
      if (!ALLOWED_CSS_VARS.has(src.name)) {
        violations.push(`${key} → ${src.name} not in ALLOWED_CSS_VARS`)
      }
    }
    expect(violations).toEqual([])
  })

  // The handoff must not have GROWN the allowlist: every color source is a token
  // that already existed, so `variables.json` still has the same surface and a
  // pack written before this feature themes an app in full. A new token added
  // for the map's benefit alone would fail here.
  it('adds no token to the allowlist for the map\'s benefit', () => {
    expect(ALLOWED_CSS_VARS.has('--info-subtle')).toBe(false)
    for (const key of MCP_UI_COLOR_VARIABLE_KEYS) {
      const src = COLOR_TOKEN_MAP[key]
      if (src.from === 'wash') expect(ALLOWED_CSS_VARS.has(src.name)).toBe(true)
    }
  })
})

// ── Properties 6, 7, 11 (Requirements 1.5, 1.6, 3.2, 5.1) ────────

describe('mcpAppTheme — value domain (Properties 6, 7, 11)', () => {
  // Property 6: No empty value is ever emitted; null (not {}) when nothing
  // resolves. Validates: Requirements 1.5, 1.6.
  it('emits no empty value', () => {
    seedAllColorTokens()
    const result = readMcpAppStyleVariables()
    expect(result).not.toBeNull()
    for (const v of Object.values(result!)) expect(v).not.toBe('')
  })

  it('returns null rather than {} when nothing resolves', () => {
    // Directly reachable now that every source is a token read: an unseeded
    // document resolves nothing at all, so this asserts the real `{}` → null
    // contract rather than approximating it. (Before the literals were dropped no
    // seeding could produce an empty result, because they always resolved.)
    withComputedTokens({})
    expect(readMcpAppStyleVariables()).toBeNull()

    // And the other side of the same predicate: a non-null result always has keys.
    vi.restoreAllMocks()
    seedAllColorTokens()
    const full = readMcpAppStyleVariables()
    expect(full).not.toBeNull()
    expect(Object.keys(full!).length).toBeGreaterThan(0)
  })

  // Property 7: Every emitted value is a sanitizeCssValue fixpoint.
  // Validates: Requirements 5.1, 5.2.
  it('emits only sanitizeCssValue fixpoints', () => {
    seedAllColorTokens()
    const result = readMcpAppStyleVariables()
    expect(result).not.toBeNull()
    for (const v of Object.values(result!)) {
      expect(sanitizeCssValue(v)).toBe(v)
    }
  })

  // Property 11: No emitted value contains 'light-dark'. Asserted, not assumed:
  // '(', ')' and '-' are all allowlist characters, so the sanitizer would pass a
  // light-dark() value — the guarantee is that no real token resolves to one, not
  // that the resolver strips it. So this walks every built-in theme's ACTUAL
  // resolved values (the Property 18 source) and asserts none carries the
  // substring. Validates: Requirements 3.2.
  it('emits no light-dark() value across every built-in theme', () => {
    for (const entry of THEMES) {
      for (const mode of ['dark', 'light'] as const) {
        const attr = themeDataAttribute(entry.value, mode)
        const vars: Record<string, string> = {}
        for (const name of COLOR_TOKEN_NAMES) {
          const v = resolveVar(attr, name)
          if (v) vars[name] = v
        }
        withComputedTokens(vars)
        const result = readMcpAppStyleVariables()
        for (const v of Object.values(result ?? {})) {
          expect(v, `theme ${attr}`).not.toContain('light-dark')
        }
        vi.restoreAllMocks()
      }
    }
  })
})

// ── Property 8 (Requirements 5.1, 5.2) ───────────────────────────

describe('mcpAppTheme — hostile token values are dropped (Property 8)', () => {
  const colorKeys = (r: Record<string, string> | null) =>
    Object.keys(r ?? {}).filter((k) => k.startsWith('--color-'))

  // Property 8: A hostile token value is dropped, not forwarded — and when it
  // feeds a color key, the whole color group is discarded.
  // Validates: Requirements 5.1, 5.2.
  it.each([
    ['a url() call', 'url(https://evil.example/x.png)'],
    ['an embedded semicolon', '#123456; background: red'],
    ['a value over 200 characters', '#' + 'a'.repeat(220)],
  ])('drops the color group when a color token carries %s', (_label, hostile) => {
    // Deliver the raw hostile string through the resolver via a getComputedStyle
    // spy: happy-dom's CSSOM would normalize (and strip the ';' from) an inline
    // setProperty, so the string must be injected at the read boundary to reach
    // sanitizeCssValue verbatim.
    const vars = goodColorTokens()
    // --bg feeds --color-background-primary and --color-text-inverse.
    vars['--bg'] = hostile
    withComputedTokens(vars)

    const result = readMcpAppStyleVariables()
    // sanitizeCssValue rejects each of these outright.
    expect(sanitizeCssValue(hostile)).toBe('')
    // The hostile value itself never appears, whole or in part.
    for (const v of Object.values(result ?? {})) {
      expect(v).not.toContain('url(')
      expect(v).not.toContain(';')
    }
    // Its color key is absent, and (all-or-nothing) so is the whole color group.
    expect(colorKeys(result)).toEqual([])
  })

  // A hostile value on a NON-color token drops only that key, leaving the color
  // group intact — the contrast that shows the discard is scoped to color.
  it('drops only the offending key when a non-color token is hostile', () => {
    // Every token seeded, so the OTHER non-color keys are present and the drop is
    // demonstrably scoped to the one bad key rather than to the whole group.
    const vars = goodAllTokens()
    // --shadow-md feeds only the non-color key --shadow-md.
    vars['--shadow-md'] = 'expression(alert(1))'
    withComputedTokens(vars)
    const result = readMcpAppStyleVariables()
    expect(colorKeys(result).length).toBe(MCP_UI_COLOR_VARIABLE_KEYS.length)
    expect(result!['--shadow-md']).toBeUndefined()
    // Its siblings survive.
    expect(result!['--shadow-sm']).toBeDefined()
    expect(result!['--border-radius-md']).toBeDefined()
  })
})

// ── Property 18 (Requirements 2.2, 2.5) ──────────────────────────

describe('mcpAppTheme — every built-in theme yields a complete color group (Property 18)', () => {
  const MODES = ['dark', 'light'] as const

  // Confidence check: the palette source must actually resolve the tokens a
  // color key reads, or the walk below would pass vacuously against a document
  // seeded with empty strings.
  it('resolves the color tokens for the default theme out of index.css', () => {
    const attr = themeDataAttribute('emerald', 'dark')
    const resolved = COLOR_TOKEN_NAMES.filter((n) => !!resolveVar(attr, n))
    // Not every token need exist for the smoke test, but a healthy majority
    // must, or the seeding cannot produce a full group for ANY theme.
    expect(resolved.length).toBeGreaterThan(COLOR_TOKEN_NAMES.length / 2)
  })

  // Property 18: For every THEMES × {dark, light}, seeding documentElement from
  // the theme's real index.css values yields the full --color-* group. This
  // turns a token-map mistake into a red test rather than one theme's users
  // silently losing theming. Validates: Requirements 2.2, 2.5.
  it.each(
    THEMES.flatMap((t) =>
      MODES.map((mode) => [t.value, mode, themeDataAttribute(t.value, mode)] as const),
    ),
  )('theme %s / %s yields all color keys', (_theme, _mode, attr) => {
    // Seed documentElement from the parsed cascade for this data-theme value,
    // exactly the values the real stylesheet would compute.
    const missingTokens: string[] = []
    for (const name of COLOR_TOKEN_NAMES) {
      const value = resolveVar(attr, name)
      if (value) document.documentElement.style.setProperty(name, value)
      else missingTokens.push(name)
    }

    const result = readMcpAppStyleVariables()
    const colorKeys = Object.keys(result ?? {}).filter((k) => k.startsWith('--color-'))
    // A helpful failure: which theme, and which token the theme failed to define
    // (that is the map/theme mismatch this property exists to catch).
    expect(
      colorKeys.length,
      `theme "${attr}" is missing color keys; unresolved tokens: ${missingTokens.join(', ')}`,
    ).toBe(MCP_UI_COLOR_VARIABLE_KEYS.length)
  })
})

// ── Property 19 (Requirement 2.5) ────────────────────────────────

describe('mcpAppTheme — the info wash matches the dashboard (Property 19)', () => {
  // Property 19: The derived info wash is the SAME wash `bg-info-subtle` paints
  // with. `--info` is the one status hue with no stored `-subtle` companion; the
  // dashboard derives it in tailwind.config.js, and the map derives it from
  // INFO_WASH. Two spellings of one wash inside a single theme is a visible
  // disagreement, so the percentage is pinned against the config that renders the
  // dashboard's own info surfaces. Validates: Requirements 2.5.
  it("derives --color-background-info at tailwind's bg-info-subtle percentage", () => {
    const src = COLOR_TOKEN_MAP['--color-background-info']
    expect(src.from).toBe('wash')
    expect(src.name).toBe('--info')

    const config = readFileSync(
      join(__dirname, '..', '..', 'tailwind.config.js'),
      'utf-8',
    )
    const match =
      /'info-subtle':\s*'color-mix\(in srgb, var\(--info\) (\d+)%, transparent\)'/.exec(
        config,
      )
    expect(
      match,
      'tailwind.config.js no longer spells info-subtle as a color-mix of --info; ' +
        'if it now reads a stored token, COLOR_TOKEN_MAP should read that token too',
    ).not.toBeNull()
    expect(`${match![1]}%`).toBe(INFO_WASH)
  })

  // The resolved value is that derivation with the hue substituted in — and it
  // survives the sanitizer, which is not free: the composed string has to stay
  // inside the character allowlist and under the 200-char cap.
  it('emits the resolved derivation, sanitizer-clean', () => {
    withComputedTokens({ ...goodColorTokens(), '--info': '#0891b2' })
    const value = readMcpAppStyleVariables()!['--color-background-info']
    expect(value).toBe(`color-mix(in srgb, #0891b2 ${INFO_WASH}, transparent)`)
    expect(sanitizeCssValue(value)).toBe(value)
  })

  // A hostile hue must not be wrapped into a color-mix and shipped: the base is
  // sanitized BEFORE composition, so the key drops — and with it, all-or-nothing,
  // the whole color group — rather than the wash becoming a carrier.
  it('drops the wash rather than wrapping a rejected hue', () => {
    withComputedTokens({ ...goodColorTokens(), '--info': 'red; background: url(x)' })
    const result = readMcpAppStyleVariables()
    for (const v of Object.values(result ?? {})) {
      expect(v).not.toContain('url(')
      expect(v).not.toContain(';')
    }
    expect(Object.keys(result ?? {}).filter((k) => k.startsWith('--color-'))).toEqual([])
  })
})
