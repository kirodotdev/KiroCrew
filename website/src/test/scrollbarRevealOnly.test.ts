/**
 * No scrollbar in the dashboard is visible at rest.
 *
 * The rule, recorded in docs/decisions/2026-10-23-scrollbars-are-never-always-visible.md:
 * a scroll box either hides its scrollbar entirely (`.scrollbar-none`) or shows
 * a thin thumb only while it is being scrolled (`data-scrolling`, set by
 * lib/scrollReveal.ts), in a column it reserves anyway so the reveal moves no
 * content. What is never allowed is a thumb painted at rest. This file holds the
 * stylesheet to that in three ways:
 *
 *   1. the global rule carries both engine halves, rest and reveal;
 *   2. nothing under `src/` paints a thumb outside a `[data-scrolling]` selector
 *      -- not a utility class, not a per-component block, not a JS-embedded
 *      sheet, not a React inline style, not a third-party override token;
 *   3. the revealed thumb clears WCAG 1.4.11 (3:1 non-text contrast) against
 *      every surface it can scroll over, in every theme -- a thumb a user cannot
 *      see while scrolling is the same as no thumb.
 *
 * Asserted against SOURCE TEXT: jsdom never loads index.css and the
 * pseudo-elements have no computed representation to read. The scan is derived
 * from the tree, so a new sheet that paints a thumb fails by construction.
 *
 * SCOPE: the Mochi app's shipped HTML entry points and theme module keep their
 * own `--scrollbar` tokens; they render in their own windows and do not read
 * this stylesheet, so they are named out of scope rather than scanned.
 */
import { describe, it, expect } from 'vitest'
import { readdirSync, readFileSync } from 'node:fs'
import { join, relative, resolve, sep } from 'node:path'
import { relativeLuminance, contrastRatio, parseCssColor } from '../lib/iconContrast'
import { readIndexCss } from './indexCssSource'
import { THEMES, resolveVar } from './themePalette'

const CSS = readIndexCss()
const ROOT = resolve(__dirname, '..')
const PIERRE_CONFIG = readFileSync(join(ROOT, 'pierre/config.ts'), 'utf8')

/** Comments stripped so prose that QUOTES a selector is not scanned as a rule. */
const stripComments = (text: string) =>
  text.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^[ \t]*\/\/.*$/gm, '').replace(/\{\/\*[\s\S]*?\*\/\}/g, '')

/** A rule may paint a thumb only when its selector carries the scrolling state. */
const REVEAL_GATED = /data-scrolling/

/** The shapes that paint a scrollbar, each with what it is allowed to look like. */
const PAINTS: Array<{ name: string; re: RegExp; allowed: (m: RegExpMatchArray) => boolean }> = [
  {
    name: '::-webkit-scrollbar-thumb painted outside [data-scrolling]',
    re: /([^{}\n]*)::-webkit-scrollbar-thumb[^{]*\{[^}]*background(?:-color)?\s*:\s*([^;}]+)/g,
    allowed: (m) => /^(transparent|none)\s*$/.test(m[2]) || REVEAL_GATED.test(m[1]) || /--[\w-]+-current/.test(m[2]),
  },
  {
    name: 'scrollbar-color with a visible thumb outside [data-scrolling]',
    re: /([^{}\n]*)\{[^{}]*?scrollbar-color\s*:\s*([^;}\n'"]+)/g,
    allowed: (m) => /^transparent/.test(m[2].trim()) || REVEAL_GATED.test(m[1]) || /--[\w-]+-current/.test(m[2]),
  },
  {
    name: 'scrollbar-width other than thin or none',
    re: /scrollbar-width\s*:\s*([^;}\n'"]+)/g,
    allowed: (m) => /^(thin|none)\s*$/.test(m[1]),
  },
  // React inline styles spell the same two properties in camelCase.
  {
    name: 'inline scrollbarWidth other than none or thin',
    re: /\bscrollbarWidth\s*:\s*(['"`])([^'"`]*)\1/g,
    allowed: (m) => /^(none|thin)$/.test(m[2]),
  },
  {
    name: 'inline scrollbarColor with a visible thumb',
    re: /\bscrollbarColor\s*:\s*(['"`])([^'"`]*)\1/g,
    allowed: (m) => /^transparent/.test(m[2]),
  },
]

/** Files the dashboard stylesheet does not reach; see the header. */
const OUT_OF_SCOPE = [
  'apps/mochi/avatar.html',
  'apps/mochi/panel.html',
  'apps/mochi/settings.html',
  'apps/mochi/src/shared/themes.ts',
]

function* sourceFiles(): Generator<string> {
  for (const entry of readdirSync(ROOT, { recursive: true, withFileTypes: true })) {
    if (!entry.isFile()) continue
    if (!/\.(css|ts|tsx|html)$/.test(entry.name)) continue
    const rel = relative(ROOT, join(entry.parentPath ?? entry.path, entry.name)).split(sep).join('/')
    // This directory's own specs quote these selectors to assert about them.
    if (rel.startsWith('test/')) continue
    yield rel
  }
}

/** Sentinel that matches no [data-theme=...] selector, so resolution walks the
 *  pure `:root` cascade -- the palette a theme-less <html> actually gets. */
const ROOT_ONLY = '\u0000none'
const MIN_NON_TEXT = 3
/** Every surface a scroll box can sit on. */
const BACKDROPS = ['--bg-elevated', '--bg', '--card'] as const

function tokenLuminance(theme: string, prop: string): number {
  const raw = resolveVar(theme, prop)
  if (raw === undefined) throw new Error(`theme ${theme}: ${prop} resolves to nothing`)
  const c = parseCssColor(raw)
  if (!c) throw new Error(`theme ${theme}: ${prop}=${raw} is not a colour this test can measure`)
  return relativeLuminance(c.r, c.g, c.b)
}

describe('no scrollbar is visible at rest; a thumb shows only while scrolling', () => {
  it('anchors the premise: the global rule carries rest and reveal on both engines', () => {
    const base = stripComments(CSS)
    // WebKit/Blink: transparent thumb at rest, painted under the attribute, in a
    // 6px column that is always reserved.
    expect(base).toMatch(/^\s*::-webkit-scrollbar\{width:6px;height:6px\}/m)
    expect(base).toMatch(/^\s*::-webkit-scrollbar-thumb\{background:transparent/m)
    expect(base).toMatch(/^\s*\[data-scrolling\]::-webkit-scrollbar-thumb\{background:var\(--muted\)\}/m)
    // Firefox only: Blink drops the pseudo-elements once the standard
    // properties are set, so they must stay behind the -moz probe.
    const moz = /@supports \(-moz-appearance:none\)\{([\s\S]*?)\n\s*\}/.exec(base)?.[1] ?? ''
    expect(moz).toMatch(/\*\{scrollbar-width:thin;scrollbar-color:transparent transparent\}/)
    expect(moz).toMatch(/\[data-scrolling\]\{scrollbar-color:var\(--muted\) transparent\}/)
    expect(base.replace(moz, '')).not.toMatch(/^\s*\*\s*\{\s*scrollbar-width/m)
    // The attribute is the one lib/scrollReveal.ts sets.
    expect(readFileSync(join(ROOT, 'lib/scrollReveal.ts'), 'utf8')).toMatch(/SCROLLING_ATTR = 'data-scrolling'/)
    expect(readFileSync(join(ROOT, 'main.tsx'), 'utf8')).toMatch(/^installScrollReveal\(\)$/m)
  })

  it('reaches the Pierre shadow roots through their unsafeCSS, on both factories', () => {
    // The light-DOM rules stop at a shadow boundary. Pierre's own sheet paints
    // the code scroller's thumb on hover and, on Firefox, permanently; the
    // override must ride on BOTH option factories or one family of surfaces
    // keeps a hover-painted bar.
    const css = /export const PIERRE_SCROLL_REVEAL_CSS = `([^`]*)`/.exec(PIERRE_CONFIG)?.[1] ?? ''
    expect(css).toMatch(/\[data-code\]::-webkit-scrollbar-thumb\{background-color:transparent\}/)
    expect(css).toMatch(/\[data-code\]\[data-scrolling\]::-webkit-scrollbar-thumb\{background-color:var\(--diffs-bg-context\)\}/)
    expect(css).toMatch(/\[data-code\]\{scrollbar-color:transparent transparent\}/)
    expect(css).toMatch(/\[data-code\]\[data-scrolling\]\{scrollbar-color:var\(--diffs-bg-context\) transparent\}/)
    for (const factory of ['pierreFileOptions', 'pierreDiffOptions']) {
      const body = new RegExp(`export function ${factory}\\([^)]*\\)[^{]*\\{([^}]*)\\}`).exec(PIERRE_CONFIG)?.[1] ?? ''
      expect(body, `${factory} does not carry PIERRE_SCROLL_REVEAL_CSS`).toContain('unsafeCSS: PIERRE_SCROLL_REVEAL_CSS +')
    }
    // The workspace tree is a second shadow root; its library reveals on hover
    // through a current-thumb variable, which the tree sheet gates the same way.
    expect(PIERRE_CONFIG).toMatch(/\[data-file-tree-virtualized-scroll\]\{--trees-scrollbar-thumb-current:transparent\}/)
    expect(PIERRE_CONFIG).toMatch(/\[data-file-tree-virtualized-scroll\]\[data-scrolling\]\{--trees-scrollbar-thumb-current:var\(--trees-scrollbar-thumb\)\}/)
    // Both hosts are the ones the reveal module attaches to.
    expect(readFileSync(join(ROOT, 'lib/scrollReveal.ts'), 'utf8')).toMatch(/SHADOW_SCROLL_HOST_TAGS = \['diffs-container', 'file-tree-container'\]/)
  })

  it('finds the sources it scans, so an empty walk cannot pass vacuously', () => {
    const files = [...sourceFiles()]
    expect(files).toContain('index.css')
    expect(files).toContain('styles/chat-transcript.css')
    expect(files).toContain('pierre/config.ts')
    for (const known of OUT_OF_SCOPE) expect(files, `${known} moved: update the out-of-scope list`).toContain(known)
  })

  it('lets no file in src/ paint a thumb at rest', () => {
    const offenders: string[] = []
    for (const rel of sourceFiles()) {
      if (OUT_OF_SCOPE.includes(rel)) continue
      const raw = readFileSync(join(ROOT, rel), 'utf8')
      // Cheap pre-filter: the shapes below all spell "scrollbar" somewhere.
      if (!/scrollbar/i.test(raw)) continue
      const text = stripComments(raw)
      for (const { name, re, allowed } of PAINTS) {
        for (const m of text.matchAll(re)) {
          if (!allowed(m)) offenders.push(`${rel}: ${name}: ${m[0].replace(/\s+/g, ' ').trim().slice(0, 140)}`)
        }
      }
    }
    expect(offenders, `rules that paint a scrollbar at rest:\n${offenders.join('\n')}`).toEqual([])
  })

  it('names the out-of-scope files for what they are: each still paints one', () => {
    // The list is only worth as much as this case: a Mochi sheet that stopped
    // painting a thumb should leave the list, not sit on it as cover.
    for (const rel of OUT_OF_SCOPE) {
      const text = stripComments(readFileSync(join(ROOT, rel), 'utf8'))
      expect(
        /--scrollbar['"]?\s*:|::-webkit-scrollbar-thumb/.test(text),
        `${rel} no longer paints a thumb: drop it from the out-of-scope list`,
      ).toBe(true)
    }
  })

  it('shows a thumb that clears 3:1 against every surface, in every theme', () => {
    // --muted is the one token the reveal paints (both engines, anchored above).
    // The resolver is shared with themeFillForeground.test.ts, which anchors
    // its two silent-fallback traps.
    expect(THEMES.length).toBeGreaterThanOrEqual(36)
    const failures: string[] = []
    for (const theme of [ROOT_ONLY, ...THEMES]) {
      const thumb = tokenLuminance(theme, '--muted')
      for (const backdrop of BACKDROPS) {
        const ratio = contrastRatio(thumb, tokenLuminance(theme, backdrop))
        if (ratio < MIN_NON_TEXT) {
          failures.push(`${theme === ROOT_ONLY ? ':root' : theme}: --muted vs ${backdrop} is ${ratio.toFixed(2)}:1`)
        }
      }
    }
    expect(failures, `revealed thumb below the 3:1 non-text-contrast floor:\n${failures.join('\n')}`).toEqual([])
  })
})
