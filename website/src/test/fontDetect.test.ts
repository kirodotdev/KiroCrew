import { describe, it, expect, vi, afterEach } from 'vitest'
import {
  cssFontFamilyToken,
  defaultMeasure,
  detectInstalledFonts,
  isFontInstalled,
  isLocalFontAccessSupported,
  isMonospaceFamily,
  queryLocalMonospaceFonts,
  __resetFontProbe,
  type MeasureText,
} from '../utils/fontDetect'

/**
 * Measurement is injected rather than driven through a real canvas: the test
 * environment's `getContext('2d')` stub answers `measureText` from the string's
 * LENGTH and never reads `ctx.font`, so every font stack measures identically
 * there and a canvas-backed probe could not be observed in either direction.
 */

/** Widths keyed by the family token, falling back to a per-generic baseline. */
function fakeMeasure(widths: Record<string, number>, base = 100): MeasureText {
  return (fontStack, text) => {
    for (const [needle, width] of Object.entries(widths)) {
      if (fontStack.includes(needle)) return width * text.length
    }
    return base * text.length
  }
}

afterEach(() => { __resetFontProbe() })

describe('cssFontFamilyToken', () => {
  it('quotes a family name so a multi-word name is one token', () => {
    expect(cssFontFamilyToken('JetBrainsMono Nerd Font')).toBe("'JetBrainsMono Nerd Font'")
  })

  it('quotes a single-word name too, so it cannot be read as a CSS keyword', () => {
    expect(cssFontFamilyToken('monospace')).toBe("'monospace'")
  })

  it('escapes a quote and a backslash rather than breaking out of the token', () => {
    expect(cssFontFamilyToken("Ke'ith\\Mono")).toBe("'Ke\\'ith\\\\Mono'")
  })
})

describe('isFontInstalled', () => {
  it('is true when the named family changes the advance width', () => {
    expect(isFontInstalled('Fira Code', fakeMeasure({ "'Fira Code'": 120 }))).toBe(true)
  })

  it('is false when the name resolves to the generic in every comparison', () => {
    expect(isFontInstalled('Nope Mono', fakeMeasure({}))).toBe(false)
  })

  it('is true for a family that IS the platform monospace, via another generic', () => {
    // Matches `monospace` exactly, so a single-generic probe would miss it.
    const measure: MeasureText = (stack, text) => {
      if (stack.includes('serif') || stack.includes('sans-serif')) {
        return (stack.includes("'DejaVu Sans Mono'") ? 100 : 90) * text.length
      }
      return 100 * text.length
    }
    expect(isFontInstalled('DejaVu Sans Mono', measure)).toBe(true)
  })

  it('is false for an empty or whitespace-only name', () => {
    expect(isFontInstalled('   ', fakeMeasure({}))).toBe(false)
  })

  it('is false when measurement is unavailable, rather than claiming installed', () => {
    expect(isFontInstalled('Fira Code', () => null)).toBe(false)
  })
})

describe('isMonospaceFamily', () => {
  it('is true when narrow and wide runs advance the same', () => {
    expect(isMonospaceFamily('Hack', fakeMeasure({ "'Hack'": 7 }))).toBe(true)
  })

  it('is false when a wide glyph advances further', () => {
    const measure: MeasureText = (_stack, text) => (text.startsWith('W') ? 200 : 50) * text.length
    expect(isMonospaceFamily('Georgia', measure)).toBe(false)
  })

  it('is false for an empty name and when measurement is unavailable', () => {
    expect(isMonospaceFamily('', fakeMeasure({}))).toBe(false)
    expect(isMonospaceFamily('Hack', () => null)).toBe(false)
  })
})

describe('detectInstalledFonts', () => {
  it('keeps candidate order and drops the families that do not resolve', () => {
    const measure = fakeMeasure({ "'Menlo'": 120, "'Hack'": 130 })
    expect(detectInstalledFonts(['Consolas', 'Hack', 'Menlo'], measure)).toEqual(['Hack', 'Menlo'])
  })

  it('returns nothing when no candidate resolves', () => {
    expect(detectInstalledFonts(['Consolas', 'Hack'], fakeMeasure({}))).toEqual([])
  })
})

describe('defaultMeasure', () => {
  it('measures through the environment canvas', () => {
    expect(defaultMeasure('monospace', 'abc')).toBeTypeOf('number')
  })

  it('returns null where the environment offers no 2D context', () => {
    const real = HTMLCanvasElement.prototype.getContext
    HTMLCanvasElement.prototype.getContext =
      vi.fn(() => null) as unknown as HTMLCanvasElement['getContext']
    try {
      __resetFontProbe()
      expect(defaultMeasure('monospace', 'abc')).toBeNull()
    } finally {
      HTMLCanvasElement.prototype.getContext = real
    }
  })
})

describe('isLocalFontAccessSupported', () => {
  it('is false when the API is absent (Firefox, Safari, insecure context)', () => {
    expect(isLocalFontAccessSupported({})).toBe(false)
  })

  it('is false when the property is present but not callable', () => {
    expect(isLocalFontAccessSupported({ queryLocalFonts: 'yes' })).toBe(false)
  })

  it('is true when the API is a function', () => {
    expect(isLocalFontAccessSupported({ queryLocalFonts: () => Promise.resolve([]) })).toBe(true)
  })

  it('reads the real window by default', () => {
    // happy-dom exposes no Local Font Access API, so the default is the
    // unsupported branch — the same answer Firefox gives.
    expect(isLocalFontAccessSupported()).toBe(false)
  })
})

describe('queryLocalMonospaceFonts', () => {
  const mono = fakeMeasure({}, 7)

  it('reports unsupported without calling anything', async () => {
    await expect(queryLocalMonospaceFonts({}, mono)).resolves.toEqual({
      ok: false, reason: 'unsupported',
    })
  })

  it('reports denied when the permission prompt rejects', async () => {
    const win = { queryLocalFonts: vi.fn(() => Promise.reject(new Error('blocked'))) }
    await expect(queryLocalMonospaceFonts(win, mono)).resolves.toEqual({
      ok: false, reason: 'denied',
    })
  })

  it('collapses faces to families, sorts them, and drops blanks', async () => {
    const win = {
      queryLocalFonts: () => Promise.resolve([
        { family: 'Menlo' }, { family: 'Menlo' }, { family: ' Hack ' },
        { family: '   ' }, {}, { family: 42 as unknown as string },
      ]),
    }
    await expect(queryLocalMonospaceFonts(win, mono)).resolves.toEqual({
      ok: true, families: ['Hack', 'Menlo'],
    })
  })

  it('keeps only the fixed-width families', async () => {
    const proportional: MeasureText = (stack, text) =>
      (stack.includes("'Georgia'") && text.startsWith('W') ? 200 : 50) * text.length
    const win = { queryLocalFonts: () => Promise.resolve([{ family: 'Georgia' }, { family: 'Hack' }]) }
    await expect(queryLocalMonospaceFonts(win, proportional)).resolves.toEqual({
      ok: true, families: ['Hack'],
    })
  })
})
