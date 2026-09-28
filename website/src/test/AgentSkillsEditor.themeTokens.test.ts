/**
 * The chip's colour utilities must name tokens the theme actually declares.
 *
 * A `warning-*` family that does not exist renders an UNSTYLED chip: the class is emitted,
 * tailwind drops it, and the unresolved state loses its only visual signal while every
 * render test still passes.
 *
 * The allow-list is READ OUT of the tailwind theme bridge rather than scanned out of the
 * stylesheet, matching the sibling token tests (`tailwindAlphaTokens.test.ts`) -- one
 * spelling of "where tokens are declared", so a rename cannot leave two scanners
 * disagreeing. Under Tailwind v4 the bridge is the `@theme` block in
 * `src/tailwind-theme.css`, where every colour utility is declared as a
 * `--color-<token>: var(--<token>)` key (the CSS-first replacement for the v3
 * `tailwind.config.js` `theme.extend.colors` map this test used to read).
 */

import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

const here = dirname(fileURLToPath(import.meta.url))

/** The v4 theme bridge that carries the `--color-*` utility ↔ token keys. */
function themeSource(): string {
  return readFileSync(join(here, '..', 'tailwind-theme.css'), 'utf8')
}

/**
 * Colour token names declared in the v4 `@theme` block, from its `--color-<token>`
 * keys. Tailwind spells `--color-warn-subtle` as the utility stem `warn-subtle`, so the
 * key name with its `--color-` prefix stripped IS the class stem, and no shade-flattening
 * is needed -- every shade is already its own `--color-warn-subtle` key.
 */
function declaredTokens(): Set<string> {
  const names = new Set<string>()
  for (const m of themeSource().matchAll(/^\s*--color-([a-z0-9-]+):/gm)) names.add(m[1])
  return names
}

function component(): string {
  return readFileSync(join(here, '..', 'components', 'AgentSkillsEditor.tsx'), 'utf8')
}

/** Colour tokens the component names, from `bg-`/`text-`/`border-` utilities. */
function usedTokens(source: string): string[] {
  const used = new Set<string>()
  for (const m of source.matchAll(/\b(?:bg|text|border)-([a-z][a-z0-9-]*)\b/g)) used.add(m[1])
  return [...used]
}

/** Utilities that set no colour, so name no token. */
const NON_COLOUR = new Set([
  'left', 'right', 'center', 'wrap', 'nowrap', 'ellipsis', 'clip', 'muted-foreground',
  'b', 't', 'l', 'r', 'x', 'y', 'none', 'inherit', 'current', 'transparent',
])

describe('AgentSkillsEditor — theme tokens exist', () => {
  it('names no colour token the tailwind theme leaves undeclared', () => {
    const declared = declaredTokens()
    const missing = usedTokens(component()).filter(
      t => !declared.has(t) && !NON_COLOUR.has(t) && !/^\[/.test(t) && !/^\d/.test(t)
    )
    expect(missing, `not declared in the tailwind theme: ${missing.join(', ')}`).toEqual([])
  })

  it('would catch the near-miss family, so a passing run is not vacuous', () => {
    const declared = declaredTokens()
    // `warn` is the real family; `warning` is the plausible misspelling that renders nothing.
    expect(declared.has('warn')).toBe(true)
    expect(declared.has('warning')).toBe(false)
    const planted = 'className="bg-warning-subtle border border-warning text-warning-fg"'
    const missing = usedTokens(planted).filter(t => !declared.has(t) && !NON_COLOUR.has(t))
    expect(missing).toContain('warning-subtle')
  })
})
