/**
 * Every keyboard focus cue painted from the theme token sheets must clear WCAG
 * 1.4.11 (non-text contrast, 3:1) against the surfaces it is drawn on -- in
 * EVERY theme. See issue #4428.
 *
 * A focus ring below 3:1 leaves a keyboard or screen-reader user unable to see
 * where focus sits (WCAG 2.4.7 is served by the ring EXISTING;
 * focusVisibleRing.test.ts guards that -- 1.4.11 is the separate requirement
 * that it be PERCEIVABLE once it exists). The house ring is the theme `--accent`,
 * and `--accent` is deliberately NOT a token with a contrast contract: it falls
 * below 3:1 against `--bg` in three light palettes (rosepine-light 2.60,
 * everforest-light 2.81, intellij-light 2.92) and lower against the accent-tinted
 * completion-card fill (2.39 / 2.56 / 2.66). So the identity hue cannot carry the
 * minimum, and each focus mechanism pairs the accent with an opaque neutral layer
 * that does -- `--text-strong` for the accent rings, a `--text`-derived mix for
 * the neutral settings-search ring. THIS FILE measures that opaque layer.
 *
 * WHAT IS MEASURED: the opaque contrast-carrying layer of each OPT-IN focus
 * mechanism in index.css --
 *   - the `.focus-ring` primitive's innermost `box-shadow` layer,
 *   - the shared `.focus-ring-accent` / `.focus-ring-accent-inset` / `-gap`
 *     utilities,
 *   - the `.settings-search .focus-ring` neutral ring.
 * The GLOBAL `:focus-visible` rule is outline-only (accent, the identity layer)
 * and carries no hairline, so there is nothing to measure there: a global
 * box-shadow hairline would survive the house `outline-hidden` opt-out and paint
 * a hard box on opted-out elements, so the hairline is opt-in only.
 * The ACCENT band in each is NOT asserted against the floor: it is the identity
 * layer and is allowed to be whatever hue the theme chose. Requiring the opaque
 * layer to clear the floor is what makes the ring perceptible regardless of the
 * accent's own contrast.
 *
 * TWO BACKDROPS: a focused control sits on the page (`--bg`) or inside the
 * accent-tinted completion card (`--accent` at 10% composited over `--bg`), which
 * is the surface that first surfaced this issue. Which one a given call site
 * lands on is a fact this file cannot read, so the floor is required against both.
 *
 * Machinery is shared, not re-derived: the WCAG math comes from `lib/iconContrast`
 * and the palette cascade from `./themePalette`, the single resolver this file
 * shares with scrollbarThumbContrast.test.ts and themeFillForeground.test.ts.
 *
 * SCOPE: built-in palettes only. A theme pack installed from a folder or created
 * in the theme editor supplies its own tokens at runtime and is out of reach of a
 * source-text measurement.
 */
import { describe, it, expect } from 'vitest'
import { readdirSync, readFileSync } from 'node:fs'
import { join, relative, resolve, sep } from 'node:path'
import { relativeLuminance, contrastRatio, parseCssColor } from '../lib/iconContrast'
import { CSS, THEMES, resolveVar } from './themePalette'

/** Sentinel matching no [data-theme=...] selector: resolves the pure `:root`
 *  cascade, the palette a theme-less <html> actually gets. */
const ROOT_ONLY = '\u0000none'

/** WCAG 1.4.11 non-text contrast floor. */
const MIN_NON_TEXT = 3

/** The accent-tint used by the completion-card fill (`bg-accent/10`). */
const CARD_ACCENT_ALPHA = 0.1

const ALL_THEMES = [ROOT_ONLY, ...THEMES]
const label = (t: string) => (t === ROOT_ONLY ? ':root' : t)

interface Rgb {
  r: number
  g: number
  b: number
}

/** The flat colour of a theme token, failing LOUDLY with the theme and token
 *  named -- an unparseable value must become a red test, never a silent skip. */
function tokenRgb(theme: string, prop: string): Rgb {
  const raw = resolveVar(theme, prop)
  if (raw === undefined) throw new Error(`theme ${label(theme)}: ${prop} resolves to nothing`)
  const c = parseCssColor(raw)
  if (!c) throw new Error(`theme ${label(theme)}: ${prop}=${raw} is not a colour this test can measure`)
  return { r: c.r, g: c.g, b: c.b }
}

const lum = ({ r, g, b }: Rgb) => relativeLuminance(r, g, b)

/** `--accent` at 10% composited over `--bg`: the completion-card fill. */
function cardFill(theme: string): Rgb {
  const a = tokenRgb(theme, '--accent')
  const bg = tokenRgb(theme, '--bg')
  return {
    r: a.r * CARD_ACCENT_ALPHA + bg.r * (1 - CARD_ACCENT_ALPHA),
    g: a.g * CARD_ACCENT_ALPHA + bg.g * (1 - CARD_ACCENT_ALPHA),
    b: a.b * CARD_ACCENT_ALPHA + bg.b * (1 - CARD_ACCENT_ALPHA),
  }
}

/** The opaque contrast-carrying layer of each focus mechanism, named for the
 *  error message, and the token it paints. These are the index.css sources of a
 *  keyboard focus cue's hairline; a new one that appears must be added here or
 *  the anchor case below will fail.
 *
 *  The GLOBAL `:focus-visible` rule is deliberately NOT in this list: it is
 *  outline-only (accent, carrying theme identity, not the 3:1 floor), because a
 *  global `box-shadow` hairline survives the house `outline-hidden`/`outline-none`
 *  opt-out and paints a hard box on opted-out elements. Controls that must clear
 *  1.4.11 opt in to one of the utilities below, which do carry the hairline. */
const LAYERS: { where: string; token: string }[] = [
  { where: '.focus-ring primitive hairline', token: '--text-strong' },
  { where: '.focus-ring-accent utility hairline', token: '--text-strong' },
  { where: '.focus-ring-accent-inset utility hairline', token: '--text-strong' },
  { where: '.focus-ring-accent-gap utility hairline', token: '--text-strong' },
]

const stripComments = (css: string) => css.replace(/\/\*[\s\S]*?\*\//g, '')
const ACTIVE = stripComments(CSS)

describe('every keyboard focus cue clears 3:1 non-text contrast in every theme', () => {
  it('anchors the premise: the palettes, the themes, and every focus mechanism are present', () => {
    expect(THEMES.length, 'fewer [data-theme] names than the stylesheet declares').toBeGreaterThanOrEqual(36)

    // The global ring is OUTLINE-ONLY: an accent outline carrying theme
    // identity, with no `box-shadow` hairline. A global hairline would survive
    // the house `outline-hidden`/`outline-none` opt-out (which resets only
    // `outline`) and paint a hard box on opted-out elements, so the hairline
    // lives only in the opt-in utilities below. The accent outline must still be
    // present, or keyboard users lose the baseline cue (WCAG 2.4.7).
    const global = /(?:^|[};])\s*:focus-visible\s*\{([^}]*)\}/m.exec(ACTIVE)
    expect(global, 'no global :focus-visible rule in index.css').not.toBeNull()
    expect(global![1], 'global :focus-visible lost its accent outline').toMatch(/outline:\s*2px solid var\(--accent\)/)
    expect(global![1], 'global :focus-visible must stay outline-only (a box-shadow hairline survives the outline-hidden opt-out)')
      .not.toMatch(/box-shadow/)

    // The shared utilities and the primitive each carry the opaque hairline.
    expect(ACTIVE, '.focus-ring-accent utility missing or lost its hairline')
      .toMatch(/\.focus-ring-accent:focus-visible\{[^}]*var\(--text-strong\)[^}]*var\(--accent\)/)
    expect(ACTIVE, '.focus-ring-accent-inset utility missing or lost its hairline')
      .toMatch(/\.focus-ring-accent-inset:focus-visible\{[^}]*inset[^}]*var\(--text-strong\)[^}]*var\(--accent\)/)
    expect(ACTIVE, '.focus-ring-accent-gap utility missing or lost its hairline')
      .toMatch(/\.focus-ring-accent-gap:focus-visible\{[^}]*var\(--text-strong\)[^}]*var\(--accent\)/)
    expect(ACTIVE, '.focus-ring primitive lost its --text-strong hairline')
      .toMatch(/\.focus-ring:focus-visible\{[^}]*var\(--text-strong\)/)

    // forced-colors / Windows High Contrast: the UA forces every `box-shadow`
    // to `none`, so a utility that drew its ring only with box-shadow would show
    // NO focus ring there (WCAG 2.4.7 regression). Each migrated utility keeps a
    // `transparent` outline — never `outline:none` — which the system repaints
    // in its own highlight colour, matching the `outline-hidden` behaviour the
    // call sites had before migration.
    for (const sel of [
      /\.focus-ring-accent:focus-visible\{([^}]*)\}/,
      /\.focus-ring-accent-inset:focus-visible\{([^}]*)\}/,
      /\.focus-ring-accent-gap:focus-visible\{([^}]*)\}/,
    ]) {
      const body = sel.exec(ACTIVE)
      expect(body, `focus-ring utility ${sel} missing`).not.toBeNull()
      expect(body![1], `${sel} must keep a transparent outline for forced-colors, not outline:none`)
        .toMatch(/outline:\s*2px solid transparent/)
    }

    // Both resolver traps that silently substitute the :root dark palette get an
    // anchor (see themePalette.ts), so a failing resolver cannot make the floor
    // checks below pass against the wrong palette.
    for (const t of ['everforest-light', 'intellij-light', 'rosepine-light']) {
      expect(THEMES).toContain(t)
    }
  })

  it('paints no focus-cue contrast layer below the floor, on page or card fill, in any theme', () => {
    const failures: string[] = []
    for (const { where, token } of LAYERS) {
      for (const theme of ALL_THEMES) {
        const cue = lum(tokenRgb(theme, token))
        const surfaces: { name: string; rgb: Rgb }[] = [
          { name: '--bg', rgb: tokenRgb(theme, '--bg') },
          { name: 'card fill (accent/10 over --bg)', rgb: cardFill(theme) },
        ]
        for (const s of surfaces) {
          const ratio = contrastRatio(cue, lum(s.rgb))
          if (ratio < MIN_NON_TEXT) {
            failures.push(
              `${label(theme)} ${where}: ${token}=${resolveVar(theme, token)} vs ${s.name} is ${ratio.toFixed(2)}:1`,
            )
          }
        }
      }
    }
    expect(
      failures,
      `focus-cue contrast layers below the 3:1 non-text floor:\n${failures.join('\n')}`,
    ).toEqual([])
  })

  it('leaves no accent-only focus ring in the source: every call site uses the shared utility', () => {
    // The regression this issue fixes is a `focus-visible:ring-accent*` utility
    // -- the hue carrying the floor alone. A new one anywhere in non-test source
    // reopens the defect, so it is banned by construction rather than by someone
    // remembering to migrate it. The shared `.focus-ring-accent` class is the
    // sanctioned replacement.
    const root = resolve(__dirname, '..')
    const RING_ACCENT = /focus-visible:ring-accent/
    const offenders: string[] = []
    for (const entry of readdirSync(root, { recursive: true, withFileTypes: true })) {
      if (!entry.isFile()) continue
      if (!/\.(ts|tsx)$/.test(entry.name)) continue
      const rel = relative(root, join(entry.parentPath ?? entry.path, entry.name)).split(sep).join('/')
      if (rel.startsWith('test/')) continue
      if (/\.test\./.test(rel)) continue
      const text = readFileSync(join(root, rel), 'utf8')
      text.split('\n').forEach((line, i) => {
        if (RING_ACCENT.test(line)) offenders.push(`${rel}:${i + 1}`)
      })
    }
    expect(
      offenders,
      'focus-visible:ring-accent is the accent-only ring that fails 1.4.11 in three light themes; ' +
        'use the shared `focus-ring-accent` utility instead (issue #4428):\n' + offenders.join('\n'),
    ).toEqual([])
  })

  it('bans combining a Tailwind shadow-* utility with a .focus-ring-accent* class', () => {
    // The focus ring is painted with `box-shadow`. A Tailwind `shadow-*` utility
    // on the SAME element is also `box-shadow` and, living in @layer utilities,
    // outranks the @layer-components focus-ring class -- so the ring silently
    // reverts to the UA default (or nothing), dropping the --text-strong hairline
    // the whole fix depends on. The per-theme contrast measurement above cannot
    // see this (it reads index.css, not call-site class soup), so it is banned at
    // the call site: a control that needs both a resting shadow and the accent
    // focus ring must express the resting shadow some other way (a wrapper, or a
    // box-shadow in its own rule that composes the ring layers).
    const root = resolve(__dirname, '..')
    // A className fragment that carries BOTH a focus-ring-accent* class and a
    // bare `shadow-<name>` utility (not `shadow-none`, which paints nothing, and
    // not a `*:shadow-*`/`hover:shadow-*` variant that does not apply at focus).
    const FOCUS_RING = /focus-ring-accent(?:-inset|-gap)?\b/
    const SHADOW_UTIL = /(?:^|[\s`'"{])shadow-(?!none\b)[a-z0-9[\]/.-]+/
    const offenders: string[] = []
    for (const entry of readdirSync(root, { recursive: true, withFileTypes: true })) {
      if (!entry.isFile()) continue
      if (!/\.(ts|tsx)$/.test(entry.name)) continue
      const rel = relative(root, join(entry.parentPath ?? entry.path, entry.name)).split(sep).join('/')
      if (rel.startsWith('test/')) continue
      if (/\.test\./.test(rel)) continue
      const text = readFileSync(join(root, rel), 'utf8')
      text.split('\n').forEach((line, i) => {
        if (FOCUS_RING.test(line) && SHADOW_UTIL.test(line)) offenders.push(`${rel}:${i + 1}`)
      })
    }
    expect(
      offenders,
      'a Tailwind shadow-* utility on the same element as a .focus-ring-accent* class ' +
        'overrides the box-shadow focus ring and drops its --text-strong hairline (issue #4428):\n' +
        offenders.join('\n'),
    ).toEqual([])
  })
})

/**
 * forced-colors / Windows High Contrast: the UA forces every `box-shadow` to
 * `none`. The three migrated `.focus-ring-accent*` utilities draw their visible
 * ring with `box-shadow`, so each MUST also carry a `transparent` outline that
 * the system repaints in its own highlight colour — otherwise those controls
 * show no focus ring at all in High Contrast (a WCAG 2.4.7 regression, and the
 * exact defect that blocked the sibling PR #17390).
 *
 * The anchor test above checks the transparent outline is PRESENT. This block is
 * the complement buluoray asked for: it pins the exact outline + offset per
 * utility AND fails if `outline:none` (or `outline:0`) ever comes back in any of
 * them — a plain presence check would still pass if a regression ADDED
 * `outline:none` beside the transparent one, or replaced it. The offset is part
 * of the contract: the `-inset` variant must use `-2px` so its forced-colors
 * ring is drawn inside the clipped box.
 */
describe('forced-colors: the .focus-ring-accent* utilities keep a transparent outline, never outline:none', () => {
  const UTILITIES: { selector: string; className: string; offset: string }[] = [
    { selector: '.focus-ring-accent:focus-visible', className: '.focus-ring-accent', offset: '2px' },
    { selector: '.focus-ring-accent-gap:focus-visible', className: '.focus-ring-accent-gap', offset: '2px' },
    { selector: '.focus-ring-accent-inset:focus-visible', className: '.focus-ring-accent-inset', offset: '-2px' },
  ]

  /** Pull the declaration body of a `selector{...}` rule out of the comment-free
   *  stylesheet. Escapes regex metacharacters in the selector so the dots and
   *  colons are matched literally. */
  function ruleBody(selector: string): string {
    const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    const m = new RegExp(`${escaped}\\{([^}]*)\\}`).exec(ACTIVE)
    if (!m) throw new Error(`no \`${selector}{...}\` rule in index.css`)
    return m[1]
  }

  for (const { selector, className, offset } of UTILITIES) {
    it(`${className} keeps outline:2px solid transparent (offset ${offset}) and no outline:none`, () => {
      const body = ruleBody(selector)

      // The transparent forced-colors outline, with the exact offset this
      // utility is contracted to use.
      expect(
        body,
        `${className} must set \`outline:2px solid transparent\` so forced-colors repaints a ring`,
      ).toMatch(/outline:\s*2px\s+solid\s+transparent/)
      // Offset is matched as a literal substring, not a built-from-string regex:
      // the `px` unit bounds the value so no trailing-digit guard is needed, and
      // a literal check cannot mis-escape the leading `-` of the inset offset.
      const offsetDecl = body.replace(/\s*/g, '').includes(`outline-offset:${offset};`)
      expect(
        offsetDecl,
        `${className} must set \`outline-offset:${offset}\``,
      ).toBe(true)

      // The regression guard: `outline:none` (or `outline:0`) anywhere in the
      // body removes the forced-colors ring. The visible ring is box-shadow,
      // which the UA zeroes in High Contrast, so this would leave no cue.
      expect(
        body,
        `${className} must NOT set \`outline:none\`/\`outline:0\` — forced-colors zeroes the box-shadow, ` +
          'so a non-transparent outline is the only ring left (issue #4428, WCAG 2.4.7)',
      ).not.toMatch(/outline:\s*(?:none|0)\b/)
    })
  }
})
