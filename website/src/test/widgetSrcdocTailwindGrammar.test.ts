import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, it, expect } from 'vitest'
import { __lightCanvasRegexesForTests } from '../lib/widgetSrcdoc'

// The light-canvas regexes are now composed from one shared Tailwind grammar
// (#10741). These are the hand-written literals they replaced, copied verbatim
// from the pre-refactor source; each composed regex must match byte for byte,
// flags included, so the refactor cannot change what the detector matches.
const PRE_REFACTOR = {
  LIGHT_TAILWIND_BG_RE:
    /(?:^|[\s"'`])(?:(?=[a-zA-Z0-9@*\[-])[a-zA-Z0-9@*-]{0,64}(?:\[[^\]\s"'`]{1,256}\])?:)*bg-(?:white|(?:slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose)-(?:50|100|200))(?=$|[\s"'`\/])/,
  DARK_TAILWIND_BG_RE:
    /(?:^|[\s"'`])(?:(?=[a-zA-Z0-9@*\[-])[a-zA-Z0-9@*-]{0,64}(?:\[[^\]\s"'`]{1,256}\])?:)*bg-(?:black|(?:slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose)-(?:700|800|900|950))(?=$|[\s"'`\/])/,
  DARK_VARIANT_RE: /(?:^|[\s"'`])(?:(?=[a-zA-Z0-9@*\[-])[a-zA-Z0-9@*-]{0,64}(?:\[[^\]\s"'`]{1,256}\])?:)*dark:/,
  ARBITRARY_BG_CLASS_RE:
    /(?:^|[\s"'`])(?:(?=[a-zA-Z0-9@*\[-])[a-zA-Z0-9@*-]{0,64}(?:\[[^\]\s"'`]{1,256}\])?:)*bg-\[(#[0-9a-f]{3,8}|rgba?\([^\]]*\)|white)\]/gi,
} as const

describe('light-canvas Tailwind grammar (#10741)', () => {
  it.each(Object.keys(PRE_REFACTOR) as (keyof typeof PRE_REFACTOR)[])(
    '%s keeps the pre-refactor source and flags',
    (name) => {
      const composed = __lightCanvasRegexesForTests[name]
      expect(composed.source).toBe(PRE_REFACTOR[name].source)
      expect(composed.flags).toBe(PRE_REFACTOR[name].flags)
    },
  )

  it('holds the hue list and variant grammar once in the module source', () => {
    const here = dirname(fileURLToPath(import.meta.url))
    const src = readFileSync(join(here, '..', 'lib', 'widgetSrcdoc.ts'), 'utf8')
    expect(src.match(/fuchsia/g)).toHaveLength(1)
    expect(src.split('[a-zA-Z0-9@*-]{0,64}')).toHaveLength(2)
  })
})
