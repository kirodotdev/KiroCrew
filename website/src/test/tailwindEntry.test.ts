import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

/**
 * The shape of the Tailwind entry in `src/index.css` is load-bearing, and the
 * obvious "simplification" breaks it silently.
 *
 * Every Tailwind v4 tutorial opens with `@import "tailwindcss";`. That one line
 * puts the generated utilities inside `@layer utilities`, and in a cascade layer
 * a rule loses to ANY unlayered rule whatever its specificity — so `text-muted`
 * on an element would lose to a bare `a { color: … }` further down index.css,
 * and `hover:text-accent` would lose to `.foo { color: … }`. The 2,900 lines of
 * component CSS below the entry were written under v3, where utilities were
 * emitted unlayered and competed on plain specificity and order; index.css keeps
 * that by emitting `@tailwind utilities` unlayered itself. Nothing else would
 * notice a rewrite to the stock import: the build is green, the tests are green,
 * and a few hundred utilities quietly stop applying.
 *
 * The other two pins guard the content scan: `source(none)` plus explicit
 * `@source` lines is what keeps `scripts/`, `docs/` and the capture fixtures out
 * of the stylesheet, and the edition marker is what `editionExtensionPlugin`
 * swaps for a downstream edition's `@source` (see vite.config.ts).
 */
const css = readFileSync(join(__dirname, '..', 'index.css'), 'utf8')
/** Directives only: comments are stripped so prose about the stock import does
 *  not read as the import itself. (Not for the `@source` globs: `**\/*` inside
 *  their quoted strings looks like a comment opener to this regex.) */
const directives = css.replace(/\/\*[\s\S]*?\*\//g, '')

describe('Tailwind entry (src/index.css)', () => {
  it('emits the utilities unlayered, never through the stock `@import "tailwindcss"`', () => {
    expect(directives).not.toMatch(/@import\s+["']tailwindcss["']/)
    expect(directives).not.toMatch(/@import\s+["']tailwindcss\/utilities(?:\.css)?["']/)
    expect(directives).toMatch(/^@tailwind utilities source\(none\);$/m)
  })

  it('imports theme and preflight into their cascade layers', () => {
    expect(directives).toMatch(/@import\s+"tailwindcss\/theme\.css"\s+layer\(theme\);/)
    expect(directives).toMatch(/@import\s+"tailwindcss\/preflight\.css"\s+layer\(base\);/)
    expect(directives).toMatch(/@import\s+"\.\/tailwind-theme\.css";/)
  })

  it('scans only index.html and src/**, plus the edition marker the seam replaces', () => {
    expect(css).toContain('@source "../index.html";')
    expect(css).toContain('@source "../src/**/*.{ts,tsx}";')
    // The marker is a comment, so it too is read from the raw file.
    expect(css).toContain('/* @kirocrew-edition-source */')
  })
})
