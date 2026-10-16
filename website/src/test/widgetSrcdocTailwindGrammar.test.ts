import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, it, expect } from 'vitest'
import { resolveWidgetTheme } from '../lib/widgetSrcdoc'

// The light-canvas regexes are composed from one shared Tailwind grammar
// (#10741). These cases sweep what that grammar emits -- every default hue at
// every stop -- through the detector's only seam, so a hue or stop dropped from
// the shared pieces shows up as a behaviour change, not a source diff.
const DARK_VARS = { '--bg': '#1a1a2e', '--text': '#e5e7eb', '--card': '#23233a' }
const isLightCanvasAuthored = (html: string): boolean =>
  resolveWidgetTheme(html, DARK_VARS, 'dark').mode === 'light'

const HUES = [
  'slate', 'gray', 'zinc', 'neutral', 'stone', 'red', 'orange', 'amber', 'yellow', 'lime', 'green',
  'emerald', 'teal', 'cyan', 'sky', 'blue', 'indigo', 'violet', 'purple', 'fuchsia', 'pink', 'rose',
]

describe('light-canvas Tailwind grammar (#10741)', () => {
  it.each(HUES)('flags bg-%s at the light stops, behind a variant too', (hue) => {
    for (const stop of ['50', '100', '200']) {
      expect(isLightCanvasAuthored(`<div class="bg-${hue}-${stop}">x</div>`)).toBe(true)
      expect(isLightCanvasAuthored(`<div class="md:hover:bg-${hue}-${stop}/80">x</div>`)).toBe(true)
    }
  })

  it.each(HUES)('treats bg-%s at the dark stops as a mixed palette beside bg-white', (hue) => {
    for (const stop of ['700', '800', '900', '950']) {
      expect(isLightCanvasAuthored(`<div class="bg-white"><b class="bg-${hue}-${stop}">x</b></div>`)).toBe(false)
      expect(isLightCanvasAuthored(`<div class="bg-white"><b class="lg:bg-${hue}-${stop}">x</b></div>`)).toBe(false)
    }
  })

  it.each(HUES)('ignores bg-%s at the middle stops', (hue) => {
    for (const stop of ['300', '400', '500', '600']) {
      expect(isLightCanvasAuthored(`<div class="bg-${hue}-${stop}">x</div>`)).toBe(false)
      expect(isLightCanvasAuthored(`<div class="bg-white"><b class="bg-${hue}-${stop}">x</b></div>`)).toBe(true)
    }
  })

  it('holds the hue list and the variant grammar once in the module source', () => {
    const here = dirname(fileURLToPath(import.meta.url))
    const src = readFileSync(join(here, '..', 'lib', 'widgetSrcdoc.ts'), 'utf8')
    // Anchored on regex syntax, not a bare word, so a comment cannot trip it.
    expect(src.split('|purple|fuchsia|pink|rose').length - 1).toBe(1)
    expect(src.split('[a-zA-Z0-9@*-]{0,64}').length - 1).toBe(1)
  })
})
