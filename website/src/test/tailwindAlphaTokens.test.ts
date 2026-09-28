import { describe, it, expect, beforeAll } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { compile } from '@tailwindcss/node'

/** Regression: theme colors were once raw `var(--x)` strings that could not
 *  carry an alpha channel, and Tailwind silently DROPPED every opacity-modifier
 *  utility on custom tokens (`border-border/30`, `text-muted/50`,
 *  `bg-accent/10`, … ~90 unique classes) from the build. Bordered elements fell
 *  back to Preflight's default #e5e7eb — glaring white column separators in
 *  dark-mode diff blocks — and translucent fills/text lost their styling
 *  entirely.
 *
 *  Under Tailwind v4 the bridge is `src/tailwind-theme.css`. This compiles that
 *  file exactly as the app does and asserts, from the EMITTED CSS, that a token
 *  utility without a modifier reads the runtime var and that one with a modifier
 *  still reads it inside a translucent `color-mix()`. A token that stopped being
 *  a `--color-*` theme key, or a theme block that lost `inline`, fails here
 *  rather than in a screenshot. */

const WEBSITE = join(__dirname, '..', '..')
const THEME = readFileSync(join(WEBSITE, 'src', 'tailwind-theme.css'), 'utf8')

/** Same shell the phantom-classes gate compiles: default theme + the app's
 *  bridge, no template scan, candidates supplied to `build()`. */
const ORACLE_CSS = [
  '@import "tailwindcss/theme.css" layer(theme);',
  `@import "./src/tailwind-theme.css";`,
  '@tailwind utilities source(none);',
].join('\n')

let css = ''
const rule = (cls: string) => {
  const sel = '.' + cls.replace(/[/.:]/g, (c) => '\\' + c)
  // Every emitted rule for this selector, joined, so a `@supports`-wrapped
  // second declaration (the color-mix half) is visible to the assertions too.
  const found = [...css.matchAll(new RegExp(sel.replace(/[\\^$*+?()[\]{}|]/g, (c) => '\\' + c) + '\\s*\\{([^}]*)\\}', 'g'))]
  return found.map((m) => m[1]).join(' ')
}

beforeAll(async () => {
  const compiler = await compile(ORACLE_CSS, { base: WEBSITE, onDependency() {} })
  css = compiler.build(
    tokens.flatMap(([name]) => [`bg-${name}`, `bg-${name}/30`]),
  )
})

// Spot-check tokens that are used with /NN modifiers across the app.
const tokens: [name: string, cssVar: string][] = [
  ['border', '--border'],
  ['muted', '--muted'],
  ['accent', '--accent'],
  ['danger', '--danger'],
  ['bg-elevated', '--bg-elevated'],
  ['diff-add', '--diff-add'],
]

describe('tailwind theme colors support opacity modifiers', () => {
  it.each(tokens)('%s emits plain var() without alpha', (name, cssVar) => {
    expect(rule(`bg-${name}`)).toContain(`background-color: var(${cssVar})`)
  })

  it.each(tokens)('%s emits a translucent color-mix() with alpha', (name, cssVar) => {
    const body = rule(`bg-${name}/30`)
    // The mix must read the LIVE token (so a theme switch retints it) at the
    // requested alpha over transparent. The colour space is Tailwind's choice
    // and irrelevant for an alpha-only mix — only the var and the % are pinned.
    expect(body).toMatch(new RegExp(`color-mix\\(in \\w+, var\\(${cssVar}\\) 30%, transparent\\)`))
  })

  it('every --color-* theme key reads the runtime token of the same stem', () => {
    // Any future token declared as a literal instead of `var(--x)` would stop
    // following the active theme. `info-subtle` is the one derived wash and is
    // pinned separately by mcpAppTheme.test.ts.
    const keys = [...THEME.matchAll(/^\s*--color-([a-z0-9-]+):\s*([^;]+);/gm)]
    expect(keys.length).toBeGreaterThan(40)
    for (const [, name, value] of keys) {
      if (name === 'info-subtle') continue
      expect(value.trim(), `--color-${name} must read var(--${name})`).toBe(`var(--${name})`)
    }
  })

  it('declares the colour bridge inline so utilities read the token directly', () => {
    // Without `inline` a utility would compile to `var(--color-accent)` plus a
    // `:root` indirection; with `reference` those keys are not emitted into the
    // cascade at all. Both words must survive on the block carrying the colours.
    const block = THEME.match(/@theme([^{]*)\{[^}]*--color-accent:/)
    expect(block, 'the --color-* keys must sit in an @theme block').not.toBeNull()
    expect(block![1]).toMatch(/\binline\b/)
    expect(block![1]).toMatch(/\breference\b/)
  })
})
