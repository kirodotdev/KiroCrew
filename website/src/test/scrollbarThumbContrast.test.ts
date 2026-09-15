/**
 * Every scrollbar thumb painted from the THEME TOKEN sheets must clear WCAG
 * 1.4.11 (non-text contrast, 3:1) against the surfaces it scrolls over -- in
 * EVERY theme.
 *
 * A thumb below 3:1 leaves a user unable to perceive that a box scrolls or where
 * they are in it. That is true of the coarse-pointer thumb, where there is no
 * hover state to fall back on, and it is equally true of the hover-revealed
 * thumb and of the global base-layer thumb: 1.4.11 grants hover no exemption, so
 * "hover implies a pointer already engaged with the box" is a product judgement
 * about affordance, not a conformance argument.
 *
 * The rungs are DERIVED from the sheets, not listed here: every rule that paints
 * `::-webkit-scrollbar-thumb`, every `scrollbar-color` thumb colour, and the
 * custom property the embedded file-tree component reads. A new hover-only
 * utility, or a token swap on an existing one, is therefore measured here by
 * construction rather than by someone remembering to add a case. Two sources are
 * scanned -- `index.css` and the file explorer's JS-embedded sheet -- because a
 * thumb is no more visible for living in a template literal.
 *
 * Three backdrops, not one. A scroll box sits on the page (`--bg`), on an
 * elevated panel (`--bg-elevated`, the surface the floor was originally
 * established against) or inside a card (`--card`), and which one a given
 * utility lands on is a call-site fact this file cannot read. Requiring the
 * floor against all three is what makes the result independent of that.
 *
 * Machinery is shared, not re-derived: the WCAG math comes from
 * `lib/iconContrast` and the palette cascade comes from `./themePalette`, the
 * single resolver this file shares with themeFillForeground.test.ts. That
 * module's header documents this stylesheet's three wrong-measurement traps
 * (partial per-component override blocks, compound selector lists, and section
 * banner comments swept into a selector) and why exactly one copy of the parser
 * exists.
 *
 * SCOPE, and what is deliberately NOT here:
 *
 * 1. Built-in palettes only. A theme pack installed from a folder or created in
 *    the theme editor supplies its own tokens at runtime.
 * 2. The Mochi app's three shipped HTML entry points (`apps/mochi/avatar.html`,
 *    `panel.html`, `settings.html`) paint six more thumb rungs, and they are NOT
 *    scanned. They do not read this stylesheet's palette: they read Mochi's own
 *    `--scrollbar` / `--scrollbar-hover`, declared in
 *    `apps/mochi/src/shared/themes.ts` as 8% and 15% alpha over the text colour.
 *    Both are far below the floor, so this is a real defect and not an argument
 *    that it is fine -- but an alpha thumb has no flat colour to measure. It
 *    must be COMPOSITED over whatever it happens to sit on first, which is a
 *    capability this file's resolver does not have and a remedy (pick an opaque
 *    rung, or raise the alpha until the composite clears 3:1) that belongs to
 *    Mochi's own token system rather than to a token swap in this one. Extending
 *    the scan without that capability would report a number for a colour nobody
 *    paints, which is the exact failure mode the resolver anchors below exist to
 *    prevent.
 */
import { describe, it, expect } from 'vitest'
import { readdirSync, readFileSync } from 'node:fs'
import { join, relative, resolve, sep } from 'node:path'
import { relativeLuminance, contrastRatio, parseCssColor } from '../lib/iconContrast'
import { CSS, THEMES, resolveVar } from './themePalette'
import { FE_CSS } from '../apps/file-explorer/styles'

/** Sentinel that matches no [data-theme=...] selector, so resolution walks the
 *  pure `:root` cascade -- the palette a theme-less <html> actually gets. */
const ROOT_ONLY = '\u0000none'

/** WCAG 1.4.11 non-text contrast floor. */
const MIN_NON_TEXT = 3

/** Every surface a scroll box can sit on. See the header. */
const BACKDROPS = ['--bg-elevated', '--bg', '--card'] as const

/** Comments stripped for the same reason themePalette strips them: prose in a
 *  banner or rationale block must not be scanned as though it were a rule. */
const stripComments = (css: string) => css.replace(/\/\*[\s\S]*?\*\//g, '')

const SOURCES = [
  { name: 'index.css', text: stripComments(CSS) },
  { name: 'apps/file-explorer/styles.ts', text: stripComments(FE_CSS) },
]

/** Luminance of a theme's token, failing LOUDLY with the theme and token named:
 *  an unparseable value (a future `var()` indirection, `color-mix(...)`, a missing
 *  declaration) must become a red test naming the culprit, never a silent skip
 *  that leaves that theme unmeasured. */
function tokenLuminance(theme: string, prop: string): number {
  const raw = resolveVar(theme, prop)
  if (raw === undefined) throw new Error(`theme ${theme}: ${prop} resolves to nothing`)
  const c = parseCssColor(raw)
  if (!c) throw new Error(`theme ${theme}: ${prop}=${raw} is not a colour this test can measure`)
  return relativeLuminance(c.r, c.g, c.b)
}

/** One painted thumb: where it is painted, and the token it names. */
interface Rung {
  source: string
  selector: string
  token: string
}

const RUNGS: Rung[] = []

for (const { name, text } of SOURCES) {
  // WebKit/Blink: any selector ending in ::-webkit-scrollbar-thumb, with or
  // without a trailing :hover grab state. `transparent` base rules are skipped --
  // they are the hidden half of the hover-reveal pattern, not a painted
  // affordance.
  for (const m of text.matchAll(/^[ \t]*([^{}\n]*::-webkit-scrollbar-thumb(?::hover)?)\s*\{([^{}]*)\}/gm)) {
    const bg = /(?:^|;)\s*background\s*:\s*var\((--[\w-]+)\)/.exec(m[2])
    if (bg) RUNGS.push({ source: name, selector: m[1].trim(), token: bg[1] })
  }

  // Firefox: `scrollbar-color: <thumb> <track>` -- the FIRST colour is the thumb.
  for (const m of text.matchAll(/^[ \t]*([^{}\n]+?)\s*\{[^{}]*?scrollbar-color\s*:\s*var\((--[\w-]+)\)/gm)) {
    RUNGS.push({ source: name, selector: `${m[1].trim()} (scrollbar-color)`, token: m[2] })
  }

  // The embedded file-tree component takes its thumb through a custom property,
  // so it is invisible to both patterns above.
  for (const m of text.matchAll(/--trees-scrollbar-thumb-override\s*:\s*var\((--[\w-]+)\)/g)) {
    RUNGS.push({ source: name, selector: '--trees-scrollbar-thumb-override', token: m[1] })
  }
}

/**
 * Rungs that intensify another rung: identical selector plus a trailing `:hover`.
 * A grab state must not merely differ from the rung it replaces, it must read as
 * STRONGER -- otherwise a "stronger" token silently makes the thumb harder to
 * see. This is the assertion that disqualifies --muted-strong, which sits below
 * --muted against an elevated backdrop in most dark themes.
 */
const PAIRS = RUNGS.flatMap((strong) => {
  if (!strong.selector.endsWith(':hover')) return []
  const baseSelector = strong.selector.slice(0, -':hover'.length)
  const base = RUNGS.find((r) => r.source === strong.source && r.selector === baseSelector)
  return base ? [{ base, strong }] : []
})

const ALL_THEMES = [ROOT_ONLY, ...THEMES]
const label = (theme: string) => (theme === ROOT_ONLY ? ':root' : theme)
const at = (r: Rung) => `${r.source} ${r.selector}`

describe('every theme-token scrollbar thumb clears 3:1 non-text contrast in every theme', () => {
  it('anchors the premise: the parser found the palettes, the themes and every rung', () => {
    // A regex that silently stopped matching would make the loops below pass on
    // an empty haystack. The stylesheet declares 36 theme palettes (18 families
    // x 2 polarities, matching THEMES in hooks/useTheme.tsx), so the floor sits
    // at the real count rather than comfortably below it.
    expect(THEMES.length, 'fewer [data-theme] names than the stylesheet declares').toBeGreaterThanOrEqual(36)
    for (const backdrop of BACKDROPS) {
      expect(resolveVar(ROOT_ONLY, backdrop), `:root ${backdrop} not found`).toBeTruthy()
    }

    // Each shape the sources paint must be represented, so a regex that stopped
    // matching one of them is caught here instead of silently dropping that rung
    // from coverage.
    const found = RUNGS.map(at)
    expect(found, 'base-layer thumb rung not found').toContain('index.css ::-webkit-scrollbar-thumb')
    expect(found, 'base-layer grab rung not found').toContain('index.css ::-webkit-scrollbar-thumb:hover')
    expect(found, 'hover-revealed thumb rung not found')
      .toContain('index.css .scroll-fade:hover::-webkit-scrollbar-thumb')
    expect(found, 'Firefox thumb rung not found').toContain('index.css .scroll-fade:hover (scrollbar-color)')
    expect(found, 'file-tree thumb rung not found').toContain('index.css --trees-scrollbar-thumb-override')
    expect(found, 'file-explorer tab-strip thumb rung not found')
      .toContain('apps/file-explorer/styles.ts .mc-fe-tabs::-webkit-scrollbar-thumb')
    expect(RUNGS.length, 'fewer painted thumb rungs than the sources declare').toBeGreaterThanOrEqual(12)
    expect(PAIRS.length, 'no grab-state pair found to check for intensification').toBeGreaterThanOrEqual(2)

    // Both resolver traps that silently substitute the :root dark palette get an
    // anchor, because either one would make most of the assertions below measure
    // a theme that is not the theme they name.
    expect(THEMES).toContain('kiro-dark')
    // Compound selector list: `[data-theme="light"],[data-theme="amber-light"]{...}`.
    for (const light of ['light', 'amber-light']) {
      expect(THEMES).toContain(light)
      expect(resolveVar(light, '--bg-elevated'), `${light} lost its compound-block palette`)
        .not.toBe(resolveVar(ROOT_ONLY, '--bg-elevated'))
    }
    // Section banner comment on the line above the block. monokai-dark declares
    // its own --bg-elevated (#3e3d32); resolving it to the :root dark default
    // means the banner was swept into the selector list again.
    expect(resolveVar('monokai-dark', '--bg-elevated'), 'monokai-dark lost its banner-prefixed palette')
      .not.toBe(resolveVar(ROOT_ONLY, '--bg-elevated'))
  })

  it('scans every source that paints a thumb, or names it as out of scope', () => {
    // The header's scope claim is only worth as much as this case. A new sheet
    // that paints a thumb -- another app entry point, another JS-embedded style
    // block -- would otherwise sit unmeasured while the header still read as
    // though coverage were complete, which is how the Mochi rungs went unnoticed
    // in the first place. Finding one here forces a decision: scan it, or add it
    // to the out-of-scope list with the reason.
    const root = resolve(__dirname, '..')
    // The quote is optional because Mochi declares its tokens as JS object keys
    // (`'--scrollbar': 'rgba(...)'`) rather than as CSS declarations.
    const PAINTS =
      /::-webkit-scrollbar-thumb|scrollbar-color\s*:|--trees-scrollbar-thumb-override|--scrollbar['"]?\s*:/
    const SCANNED = ['index.css', 'apps/file-explorer/styles.ts']
    const OUT_OF_SCOPE = [
      // Alpha thumbs on Mochi's own token system -- see the header's SCOPE note.
      'apps/mochi/avatar.html',
      'apps/mochi/panel.html',
      'apps/mochi/settings.html',
      'apps/mochi/src/shared/themes.ts',
    ]

    const painters: string[] = []
    for (const entry of readdirSync(root, { recursive: true, withFileTypes: true })) {
      if (!entry.isFile()) continue
      if (!/\.(css|ts|tsx|html)$/.test(entry.name)) continue
      const rel = relative(root, join(entry.parentPath ?? entry.path, entry.name)).split(sep).join('/')
      // This directory's own specs quote these selectors to assert about them.
      if (rel.startsWith('test/')) continue
      if (PAINTS.test(readFileSync(join(root, rel), 'utf8'))) painters.push(rel)
    }

    // A walk that silently returned nothing would make the comparison vacuous.
    expect(painters.length, 'no thumb-painting source found at all').toBeGreaterThanOrEqual(
      SCANNED.length + OUT_OF_SCOPE.length,
    )
    for (const known of [...SCANNED, ...OUT_OF_SCOPE]) {
      expect(painters, `${known} no longer paints a thumb: drop it from this list`).toContain(known)
    }
    expect(
      painters.filter((p) => !SCANNED.includes(p) && !OUT_OF_SCOPE.includes(p)),
      'source(s) paint a scrollbar thumb but are neither scanned nor named out of scope',
    ).toEqual([])
  })

  it('paints no thumb below the floor, on any surface, in any theme', () => {
    const failures: string[] = []
    for (const rung of RUNGS) {
      for (const theme of ALL_THEMES) {
        const thumb = tokenLuminance(theme, rung.token)
        for (const backdrop of BACKDROPS) {
          const ratio = contrastRatio(thumb, tokenLuminance(theme, backdrop))
          if (ratio < MIN_NON_TEXT) {
            failures.push(
              `${label(theme)} ${at(rung)}: ${rung.token}=${resolveVar(theme, rung.token)} vs ` +
                `${backdrop}=${resolveVar(theme, backdrop)} is ${ratio.toFixed(2)}:1`,
            )
          }
        }
      }
    }
    expect(failures, `thumb rungs below the 3:1 non-text-contrast floor:\n${failures.join('\n')}`).toEqual([])
  })

  it('makes every grab state stronger than the rung it replaces, in any theme', () => {
    const failures: string[] = []
    for (const { base, strong } of PAIRS) {
      for (const theme of ALL_THEMES) {
        for (const backdrop of BACKDROPS) {
          const surface = tokenLuminance(theme, backdrop)
          const from = contrastRatio(tokenLuminance(theme, base.token), surface)
          const to = contrastRatio(tokenLuminance(theme, strong.token), surface)
          if (to < from) {
            failures.push(
              `${label(theme)} ${at(strong)} on ${backdrop}: ${strong.token} is ${to.toFixed(2)}:1, ` +
                `weaker than ${base.token} at ${from.toFixed(2)}:1`,
            )
          }
        }
      }
    }
    expect(failures, `grab states weaker than the rung they replace:\n${failures.join('\n')}`).toEqual([])
  })
})
