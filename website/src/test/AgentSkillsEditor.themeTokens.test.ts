/**
 * The chip's colour utilities must name tokens the theme actually declares.
 *
 * A `warning-*` family that does not exist renders an UNSTYLED chip: the class is emitted,
 * tailwind drops it, and the unresolved state loses its only visual signal while every
 * render test still passes.
 *
 * The allow-list is READ OUT of the tailwind theme config rather than scanned out of the
 * stylesheet, matching the sibling token tests -- one spelling of "where tokens are
 * declared", so a rename cannot leave two scanners disagreeing.
 */

import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

import tailwindConfig from '../../tailwind.config.js'

const here = dirname(fileURLToPath(import.meta.url))

/** Colour token names declared in the tailwind theme's `extend.colors`. */
function declaredTokens(): Set<string> {
  const colors = (tailwindConfig as { theme: { extend: { colors: Record<string, unknown> } } })
    .theme.extend.colors
  const names = new Set<string>()
  for (const [family, value] of Object.entries(colors)) {
    names.add(family)
    // A family can be an object of shades (`warn: { subtle, fg }`), which tailwind spells
    // as `warn-subtle`; flattening here is what lets a shade be checked by its class name.
    if (value && typeof value === 'object') {
      for (const shade of Object.keys(value as Record<string, unknown>)) {
        names.add(shade === 'DEFAULT' ? family : `${family}-${shade}`)
      }
    }
  }
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
  it('names no colour token tailwind.config.js leaves undeclared', () => {
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
