import { describe, it, expect } from 'vitest'
import config from '../../tailwind.config.js'

/** Regression: theme colors were raw `var(--x)` strings, which cannot carry
 *  an alpha channel. Tailwind then silently DROPPED every opacity-modifier
 *  utility on custom tokens (`border-border/30`, `text-muted/50`,
 *  `bg-accent/10`, … ~90 unique classes) from the build. Bordered elements
 *  fell back to Preflight's default #e5e7eb — glaring white column
 *  separators in dark-mode diff blocks — and translucent fills/text lost
 *  their styling entirely.
 *
 *  Each token must be a function that emits a plain var() when no alpha is
 *  requested and a color-mix() applying the alpha when one is. */

type ColorFn = (opts: { opacityValue?: string | number }) => string
const colors = (config as { theme: { extend: { colors: Record<string, string | ColorFn> } } })
  .theme.extend.colors

describe('tailwind theme colors support opacity modifiers', () => {
  // Spot-check tokens that are used with /NN modifiers across the app.
  const tokens: [name: string, cssVar: string][] = [
    ['border', '--border'],
    ['muted', '--muted'],
    ['accent', '--accent'],
    ['danger', '--danger'],
    ['bg-elevated', '--bg-elevated'],
    ['diff-add', '--diff-add'],
  ]

  it.each(tokens)('%s emits plain var() without alpha', (name, cssVar) => {
    const fn = colors[name]
    expect(typeof fn, `${name} must be alpha-aware (a function), got ${typeof fn}`).toBe('function')
    expect((fn as ColorFn)({})).toBe(`var(${cssVar})`)
  })

  it.each(tokens)('%s emits color-mix() with alpha', (name, cssVar) => {
    const fn = colors[name] as ColorFn
    expect(fn({ opacityValue: '0.3' }))
      .toBe(`color-mix(in srgb, var(${cssVar}) calc(0.3 * 100%), transparent)`)
  })

  it('every var-backed token is alpha-aware', () => {
    // Any future token added as a bare 'var(--x)' string would silently
    // break /NN modifiers again — catch it here. Static color-mix strings
    // (e.g. info-subtle) are fine: they have no runtime var to alpha.
    for (const [name, value] of Object.entries(colors)) {
      if (typeof value === 'string') {
        expect(value.startsWith('var('), `token "${name}" is a bare var() string — wrap it in withAlpha()`).toBe(false)
      }
    }
  })
})
