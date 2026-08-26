import { describe, it, expect, vi, afterEach } from 'vitest'
import {
  contrastRatio,
  iconNeedsPlate,
  measureIconTone,
  parseCssColor,
  prefersDarkIcon,
  relativeLuminance,
  subscribeThemeSurface,
  surfaceLuminance,
  toneFromPixels,
  MIN_ICON_CONTRAST,
} from '../lib/iconContrast'

/** RGBA bytes for a box of `total` pixels, `painted` of them the given colour. */
function pixels(total: number, painted: number, rgb: [number, number, number], alpha = 255) {
  const out: number[] = []
  for (let i = 0; i < total; i++) {
    if (i < painted) out.push(rgb[0], rgb[1], rgb[2], alpha)
    else out.push(0, 0, 0, 0)
  }
  return out
}

const DARK_SURFACE = relativeLuminance(0x12, 0x14, 0x1a)   // --bg, dark themes
const LIGHT_SURFACE = relativeLuminance(0xfa, 0xfa, 0xfa)  // --bg, light themes

describe('relativeLuminance / contrastRatio', () => {
  it('anchors on the WCAG endpoints', () => {
    expect(relativeLuminance(0, 0, 0)).toBe(0)
    expect(relativeLuminance(255, 255, 255)).toBeCloseTo(1, 5)
    expect(contrastRatio(1, 0)).toBeCloseTo(21, 5)
    expect(contrastRatio(0.5, 0.5)).toBe(1)
  })

  it('is order-independent', () => {
    expect(contrastRatio(0.9, 0.1)).toBeCloseTo(contrastRatio(0.1, 0.9), 10)
  })
})

describe('toneFromPixels', () => {
  it('ignores pixels the icon does not paint', () => {
    // A quarter-covered box of pure black: coverage reflects the painted part
    // only, so a small glyph is not read as a mostly-white icon.
    const tone = toneFromPixels(pixels(16, 4, [0, 0, 0]))!
    expect(tone.coverage).toBeCloseTo(0.25, 5)
    expect(tone.dim).toBe(0)
    expect(tone.bright).toBe(0)
  })

  it('weights coverage by alpha so a faint wash is not full coverage', () => {
    const tone = toneFromPixels(pixels(16, 16, [255, 255, 255], 128))!
    expect(tone.coverage).toBeCloseTo(128 / 255, 3)
  })

  it('reports both ends of the range, not one average', () => {
    // Half black, half white: an average would say "mid grey" and hide the fact
    // that this icon has content at both extremes.
    const half = [...pixels(8, 8, [0, 0, 0]), ...pixels(8, 8, [255, 255, 255])]
    const tone = toneFromPixels(half)!
    expect(tone.dim).toBeCloseTo(0, 5)
    expect(tone.bright).toBeCloseTo(1, 5)
  })

  it('returns null for an entirely transparent or empty box', () => {
    expect(toneFromPixels(pixels(16, 0, [0, 0, 0]))).toBeNull()
    expect(toneFromPixels([])).toBeNull()
    // Alpha below the floor is unpainted, not faint.
    expect(toneFromPixels(pixels(16, 16, [0, 0, 0], 8))).toBeNull()
  })
})

describe('iconNeedsPlate', () => {
  it('plates a tab-coloured dark glyph on a dark surface', () => {
    // The reported bug: a site ships one near-black icon designed for a white
    // browser tab, and on a dark theme it is an invisible shape.
    const tone = toneFromPixels(pixels(16, 8, [24, 23, 27]))
    expect(iconNeedsPlate(tone, DARK_SURFACE)).toBe(true)
  })

  it('plates a white glyph on a light surface — the same bug mirrored', () => {
    const tone = toneFromPixels(pixels(16, 8, [255, 255, 255]))
    expect(iconNeedsPlate(tone, LIGHT_SURFACE)).toBe(true)
  })

  it('leaves the same icons alone on the surface they were designed for', () => {
    const dark = toneFromPixels(pixels(16, 8, [24, 23, 27]))
    const light = toneFromPixels(pixels(16, 8, [255, 255, 255]))
    expect(iconNeedsPlate(dark, LIGHT_SURFACE)).toBe(false)
    expect(iconNeedsPlate(light, DARK_SURFACE)).toBe(false)
  })

  it('leaves a mid-tone brand colour alone in both themes', () => {
    // A blue, an orange or a saturated green reads on either theme. Plating one
    // would make the fix more visible than the bug, which is why the threshold
    // sits far under WCAG's 3:1 for non-text content.
    for (const rgb of [[26, 115, 232], [244, 128, 36], [0, 212, 146]] as [number, number, number][]) {
      const tone = toneFromPixels(pixels(16, 8, rgb))
      expect(iconNeedsPlate(tone, DARK_SURFACE)).toBe(false)
      expect(iconNeedsPlate(tone, LIGHT_SURFACE)).toBe(false)
    }
  })

  it('plates only the band where an icon is absent, not merely washy', () => {
    // The gap that sets the threshold: the reported failures measure 1.0–1.2 in
    // a browser, the mid-tones that must survive start around 1.9. Anything in
    // between is a judgement call this file records rather than rediscovers.
    const octocat = toneFromPixels(pixels(16, 8, [27, 31, 35]))!
    const green = toneFromPixels(pixels(16, 8, [0, 212, 146]))!
    expect(contrastRatio(octocat.bright, DARK_SURFACE)).toBeLessThan(1.4)
    expect(contrastRatio(green.dim, LIGHT_SURFACE)).toBeGreaterThan(1.8)
    expect(MIN_ICON_CONTRAST).toBeGreaterThan(1.4)
    expect(MIN_ICON_CONTRAST).toBeLessThan(1.8)
  })

  it('leaves an icon that carries its own contrast alone', () => {
    // An icon that ships its own backdrop is legible on any surface, because the
    // contrast it needs is inside the image. Requiring BOTH ends of the range to
    // fail is what spares it — an average would call the first icon "dark" and
    // the second "light", and plate each one on the theme that matches.
    const darkTile = [...pixels(12, 12, [10, 10, 12]), ...pixels(4, 4, [250, 250, 250])]
    expect(iconNeedsPlate(toneFromPixels(darkTile), DARK_SURFACE)).toBe(false)
    const lightTile = [...pixels(12, 12, [250, 250, 250]), ...pixels(4, 4, [10, 10, 12])]
    expect(iconNeedsPlate(toneFromPixels(lightTile), LIGHT_SURFACE)).toBe(false)
  })

  it('never plates on a missing measurement', () => {
    // Unknown means untouched: no tone (undecodable icon) and no surface (a
    // colour form we do not parse) must both leave the site's icon as shipped.
    expect(iconNeedsPlate(null, DARK_SURFACE)).toBe(false)
    expect(iconNeedsPlate(toneFromPixels(pixels(16, 8, [0, 0, 0])), null)).toBe(false)
  })

  it('never plates a near-empty icon', () => {
    // Two pixels of a 16x16 box is not a shape a filled square would rescue.
    const tone = toneFromPixels(pixels(1024, 2, [0, 0, 0]))!
    expect(tone.coverage).toBeLessThan(0.02)
    expect(iconNeedsPlate(tone, DARK_SURFACE)).toBe(false)
  })

  it('agrees with the documented threshold at the boundary', () => {
    const tone = { dim: 0.05, bright: 0.05, coverage: 1 }
    const surface = 0.05
    expect(contrastRatio(tone.dim, surface)).toBeLessThan(MIN_ICON_CONTRAST)
    expect(iconNeedsPlate(tone, surface)).toBe(true)
  })
})

describe('prefersDarkIcon', () => {
  it('selects the site\u2019s dark variant only for a dark surface', () => {
    expect(prefersDarkIcon(DARK_SURFACE)).toBe(true)
    expect(prefersDarkIcon(LIGHT_SURFACE)).toBe(false)
  })

  it('treats an unmeasurable surface as not dark', () => {
    // Unknown is not dark: without a measurement the site's default icon is the
    // only defensible choice.
    expect(prefersDarkIcon(null)).toBe(false)
  })

  it('splits at the midpoint, where no real theme sits', () => {
    expect(prefersDarkIcon(0.5)).toBe(true)
    expect(prefersDarkIcon(0.51)).toBe(false)
  })
})

describe('parseCssColor', () => {
  it('parses the functional forms a browser serialises', () => {
    expect(parseCssColor('rgb(18, 20, 26)')).toEqual({ r: 18, g: 20, b: 26, a: 1 })
    expect(parseCssColor('rgba(0, 0, 0, 0.5)')).toEqual({ r: 0, g: 0, b: 0, a: 0.5 })
    expect(parseCssColor('rgb(18 20 26 / 0.25)')).toEqual({ r: 18, g: 20, b: 26, a: 0.25 })
  })

  it('parses color(srgb …), which is how the theme alpha utilities compute', () => {
    // `bg-accent/10` is a color-mix() on a custom property; browsers serialise
    // the computed value in this form rather than as rgba().
    const c = parseCssColor('color(srgb 0 0.831 0.573 / 0.1)')!
    expect(c.r).toBe(0)
    expect(c.g).toBeCloseTo(211.9, 1)
    expect(c.a).toBeCloseTo(0.1, 5)
  })

  it('parses the hex forms the theme variables are written in', () => {
    expect(parseCssColor('#12141a')).toEqual({ r: 18, g: 20, b: 26, a: 1 })
    expect(parseCssColor('#FFF')).toEqual({ r: 255, g: 255, b: 255, a: 1 })
    expect(parseCssColor('#00000080')!.a).toBeCloseTo(0.502, 3)
  })

  it('treats transparent as painted-nothing, and anything unknown as unknown', () => {
    expect(parseCssColor('transparent')).toEqual({ r: 0, g: 0, b: 0, a: 0 })
    expect(parseCssColor('linear-gradient(red, blue)')).toBeNull()
    expect(parseCssColor('oklch(0.7 0.1 200)')).toBeNull()
    expect(parseCssColor('')).toBeNull()
    expect(parseCssColor('rgb(1, 2)')).toBeNull()
  })
})

describe('surfaceLuminance', () => {
  afterEach(() => { document.body.replaceChildren(); document.documentElement.style.cssText = '' })

  it('composites translucent ancestors over the first opaque one', () => {
    // The chip's own tint is translucent, so what the icon really sits on is the
    // tint blended over the opaque surface underneath it.
    const page = document.createElement('div')
    page.style.backgroundColor = 'rgb(0, 0, 0)'
    const chip = document.createElement('span')
    chip.style.backgroundColor = 'rgba(255, 255, 255, 0.5)'
    const iconBox = document.createElement('span')
    chip.appendChild(iconBox)
    page.appendChild(chip)
    document.body.appendChild(page)

    const lum = surfaceLuminance(iconBox)!
    expect(lum).toBeCloseTo(relativeLuminance(127.5, 127.5, 127.5), 5)
    // Not the opaque base alone, and not the tint alone.
    expect(lum).toBeGreaterThan(0)
    expect(lum).toBeLessThan(1)
  })

  it('falls back to the theme background when no ancestor is opaque', () => {
    document.documentElement.style.setProperty('--bg', '#ffffff')
    const el = document.createElement('span')
    document.body.appendChild(el)
    expect(surfaceLuminance(el)).toBeCloseTo(1, 5)
  })

  it('returns null when nothing usable can be read', () => {
    // No opaque ancestor and no parsable --bg: the caller must not guess.
    document.documentElement.style.setProperty('--bg', 'linear-gradient(red, blue)')
    const el = document.createElement('span')
    document.body.appendChild(el)
    expect(surfaceLuminance(el)).toBeNull()
    expect(surfaceLuminance(null)).toBeNull()
  })
})

describe('measureIconTone', () => {
  it('has no opinion about an image that has not decoded', () => {
    const img = document.createElement('img')
    expect(measureIconTone(img)).toBeNull()
  })

  it('has no opinion where there is no 2D canvas', () => {
    // jsdom has no canvas backend, and a browser can refuse a context under
    // memory pressure. Either way the icon must render exactly as shipped.
    const img = document.createElement('img')
    Object.defineProperty(img, 'naturalWidth', { value: 32 })
    Object.defineProperty(img, 'naturalHeight', { value: 32 })
    expect(measureIconTone(img)).toBeNull()
  })

  it('summarises the drawn pixels when a canvas is available', () => {
    const img = document.createElement('img')
    Object.defineProperty(img, 'naturalWidth', { value: 32 })
    Object.defineProperty(img, 'naturalHeight', { value: 32 })
    const data = new Uint8ClampedArray(16 * 16 * 4)
    for (let i = 0; i < data.length; i += 4) {
      data[i] = 20; data[i + 1] = 20; data[i + 2] = 24; data[i + 3] = 255
    }
    const ctx = {
      clearRect: vi.fn(),
      drawImage: vi.fn(),
      getImageData: vi.fn(() => ({ data })),
    }
    const spy = vi
      .spyOn(HTMLCanvasElement.prototype, 'getContext')
      .mockReturnValue(ctx as unknown as CanvasRenderingContext2D)

    const tone = measureIconTone(img)!
    expect(ctx.drawImage).toHaveBeenCalledWith(img, 0, 0, 16, 16)
    expect(tone.coverage).toBeCloseTo(1, 5)
    expect(tone.dim).toBeCloseTo(relativeLuminance(20, 20, 24), 5)
    expect(iconNeedsPlate(tone, DARK_SURFACE)).toBe(true)
    spy.mockRestore()
  })
})

describe('subscribeThemeSurface', () => {
  it('re-notifies on a theme attribute change and stops on unsubscribe', async () => {
    const seen = vi.fn()
    const stop = subscribeThemeSurface(seen)
    document.documentElement.setAttribute('data-theme', 'light')
    await new Promise((r) => setTimeout(r, 0))
    expect(seen).toHaveBeenCalled()

    stop()
    seen.mockClear()
    document.documentElement.setAttribute('data-theme', 'dark')
    await new Promise((r) => setTimeout(r, 0))
    expect(seen).not.toHaveBeenCalled()
    document.documentElement.removeAttribute('data-theme')
  })

  it('shares one observer across subscribers', async () => {
    const a = vi.fn()
    const b = vi.fn()
    const stopA = subscribeThemeSurface(a)
    const stopB = subscribeThemeSurface(b)
    document.documentElement.setAttribute('data-theme', 'nord-dark')
    await new Promise((r) => setTimeout(r, 0))
    expect(a).toHaveBeenCalled()
    expect(b).toHaveBeenCalled()

    // One subscriber leaving must not deafen the other.
    stopA()
    a.mockClear()
    b.mockClear()
    document.documentElement.setAttribute('data-theme', 'dark')
    await new Promise((r) => setTimeout(r, 0))
    expect(a).not.toHaveBeenCalled()
    expect(b).toHaveBeenCalled()
    stopB()
    document.documentElement.removeAttribute('data-theme')
  })
})
