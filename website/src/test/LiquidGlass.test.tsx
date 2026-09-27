/**
 * The Liquid Glass pane is a MATERIAL primitive: it measures its host, builds a
 * displacement map for the bevel, and stacks a refraction layer, a frost+tint
 * layer, a bevel shadow and a specular band, all clipped to one circular
 * outline. None of that renders in a DOM without layout, so these tests drive
 * the two browser seams by hand — a ResizeObserver that reports a size, a 2D
 * canvas that hands back a data URL — and then check what the layers were told
 * to draw.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render } from '@testing-library/react'
import { LiquidGlass } from '../components/ui/liquid-glass'

type ResizeCallback = (entries: Array<{ contentRect: { width: number; height: number } }>) => void

/** Every observer created during a test, so a test can report a size to it. */
const observers: ResizeCallback[] = []

class FakeResizeObserver {
  constructor(cb: ResizeCallback) {
    observers.push(cb)
  }
  observe() {}
  unobserve() {}
  disconnect() {}
}

/** A 2D context that keeps the last raster and encodes to a fixed PNG data URL. */
const fakeContext = {
  createImageData: (w: number, h: number) => ({ data: new Uint8ClampedArray(w * h * 4), width: w, height: h }),
  putImageData: vi.fn(),
}
const MAP_URL = 'data:image/png;base64,map'

/** The composer's settings — the pane has no defaults, every caller sets all three. */
const composer = { cornerRadius: 16, frost: 24, lightIntensity: 24 }

function measure(width: number, height: number) {
  act(() => {
    for (const cb of observers) cb([{ contentRect: { width, height } }])
  })
}

/** The effect layers under the root, in document order. */
function layersOf(root: HTMLElement) {
  return Array.from(root.querySelectorAll<HTMLElement>(':scope > div[aria-hidden="true"]'))
}

beforeEach(() => {
  vi.useFakeTimers()
  observers.length = 0
  fakeContext.putImageData.mockClear()
  vi.stubGlobal('ResizeObserver', FakeResizeObserver)
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(fakeContext as unknown as CanvasRenderingContext2D)
  vi.spyOn(HTMLCanvasElement.prototype, 'toDataURL').mockReturnValue(MAP_URL)
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('LiquidGlass', () => {
  it('renders the frost and the theme tint before it has a size, and no filter yet', () => {
    const { container } = render(<LiquidGlass {...composer}><span>hi</span></LiquidGlass>)
    const root = container.firstElementChild as HTMLElement
    // The root carries exactly the stable hook index.css solidifies; nothing is passed through.
    expect(root.className).toBe('liquid-glass')
    expect(root.style.borderRadius).toBe('16px')
    expect(root.querySelector('svg')).toBeNull()
    const layers = layersOf(root)
    // refraction, frost+tint, bevel — the specular band waits for the map.
    expect(layers).toHaveLength(3)
    expect(layers[0].style.backdropFilter).toBe('')
    // The tint is the polarity-fixed token, not a per-caller colour.
    expect(layers[1].style.background).toContain('var(--glass-tint)')
    expect(layers[1].style.backdropFilter).toBe('blur(24px) saturate(1.55)')
    // Every layer clips on the same circular radius as the root.
    for (const l of layers) expect(l.style.borderRadius).toBe('16px')
    expect(root.textContent).toBe('hi')
  })

  it('builds the displacement map once measured and wires it into one filter', () => {
    const { container } = render(<LiquidGlass {...composer} />)
    const root = container.firstElementChild as HTMLElement
    measure(768, 96)

    const svg = root.querySelector('svg') as SVGElement
    expect(svg).not.toBeNull()
    const filter = svg.querySelector('filter') as SVGFilterElement
    const image = filter.querySelector('feImage') as SVGElement
    expect(image.getAttribute('href')).toBe(MAP_URL)
    expect(image.getAttribute('width')).toBe('768')
    expect(image.getAttribute('preserveAspectRatio')).toBe('none')
    // One bend, not a three-channel dispersion chain: no colour matrix, no blend.
    expect(filter.querySelectorAll('feDisplacementMap')).toHaveLength(1)
    expect(filter.querySelector('feColorMatrix')).toBeNull()
    expect(filter.querySelector('feBlend')).toBeNull()
    expect(filter.querySelector('feGaussianBlur')).not.toBeNull()
    // Half-strength bend of the band on a 96px-tall host: band = .45 * min(96*.18, 26) = 7.776, scale = .5 * band * 2
    const bend = filter.querySelector('feDisplacementMap') as SVGElement
    expect(Number(bend.getAttribute('scale'))).toBeCloseTo(7.776, 3)

    const layers = layersOf(root)
    expect(layers).toHaveLength(4)
    expect(layers[0].style.backdropFilter).toBe(`url(#${filter.id})`)
    expect(layers[1].style.backdropFilter).toBe('blur(24px) saturate(1.55)')
    // The bevel is lit from straight above: no horizontal offset in either inset.
    expect(layers[2].style.boxShadow).toMatch(/^inset 0px 3\.27px/)
    // The map fed the canvas: a 512-capped raster of the host's aspect.
    expect(fakeContext.putImageData).toHaveBeenCalledTimes(1)
    const raster = fakeContext.putImageData.mock.calls[0][0] as { width: number; height: number; data: Uint8ClampedArray }
    expect([raster.width, raster.height]).toEqual([512, 64])
    // Dead centre is flat (128/128); the rim pixel on the left edge pushes outward (red < 128).
    const at = (x: number, y: number) => (y * raster.width + x) * 4
    expect(raster.data[at(256, 32)]).toBe(128)
    expect(raster.data[at(256, 32) + 2]).toBe(128)
    expect(raster.data[at(1, 32)]).toBeLessThan(128)
    expect(raster.data[at(1, 32) + 2]).toBe(128)
    const specular = layers[3]
    expect(specular.style.background).toContain('linear-gradient(to bottom')
    expect(specular.style.maskImage).toContain('data:image/svg+xml')
    expect(specular.style.maskImage).toContain('feGaussianBlur')
    // The rim path is the box's own rounded rectangle: arcs, not a sampled squircle.
    expect(decodeURIComponent(specular.style.maskImage)).toContain('M16.00 0H752.00A16.00 16.00 0 0 1 768.00 16.00V80.00')
  })

  it('rasterises the first size at once, then waits for a resize to settle before rebuilding', () => {
    render(<LiquidGlass {...composer} />)
    measure(400, 80)
    const first = fakeContext.putImageData.mock.calls.length
    expect(first).toBe(1)
    // A sub-pixel wobble is not a new size.
    measure(400.3, 80.2)
    act(() => { vi.advanceTimersByTime(1000) })
    expect(fakeContext.putImageData.mock.calls.length).toBe(first)
    // A drag delivers a stream of sizes: no rebuild while it runs …
    for (const h of [90, 100, 110, 120, 130]) {
      measure(400, h)
      act(() => { vi.advanceTimersByTime(50) })
    }
    expect(fakeContext.putImageData.mock.calls.length).toBe(first)
    // … and exactly one once it has held still.
    act(() => { vi.advanceTimersByTime(200) })
    expect(fakeContext.putImageData.mock.calls.length).toBe(first + 1)
  })

  it('keeps the filter viewport on the live size while the map is still the settled one', () => {
    const { container } = render(<LiquidGlass {...composer} />)
    measure(400, 80)
    measure(400, 140)
    const root = container.firstElementChild as HTMLElement
    // The feImage follows the live size, so the settled map is stretched to it
    // (preserveAspectRatio="none") instead of the bend lagging the box.
    const image = root.querySelector('feImage') as SVGElement
    expect(image.getAttribute('height')).toBe('140')
    expect(fakeContext.putImageData.mock.calls.length).toBe(1)
    act(() => { vi.advanceTimersByTime(200) })
    expect(fakeContext.putImageData.mock.calls.length).toBe(2)
  })

  it('drops a pending rebuild when it unmounts mid-resize', () => {
    const { unmount } = render(<LiquidGlass {...composer} />)
    measure(400, 80)
    measure(400, 120)
    unmount()
    act(() => { vi.advanceTimersByTime(500) })
    expect(fakeContext.putImageData.mock.calls.length).toBe(1)
  })

  it('leaves the filter out when the canvas has no 2D context', () => {
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null)
    const { container } = render(<LiquidGlass {...composer} />)
    measure(400, 80)
    const root = container.firstElementChild as HTMLElement
    expect(root.querySelector('svg')).toBeNull()
    expect(layersOf(root)).toHaveLength(3)
    expect(layersOf(root)[0].style.backdropFilter).toBe('')
  })

  it('draws a square rim when the radius is zero', () => {
    const { container } = render(<LiquidGlass {...composer} cornerRadius={0} />)
    measure(120, 40)
    const root = container.firstElementChild as HTMLElement
    expect(root.style.borderRadius).toBe('0px')
    expect(decodeURIComponent(layersOf(root)[3].style.maskImage)).toContain('d="M0 0H120.00V40.00H0Z"')
  })

  it('caps the radius at half the shorter side so a capsule stays a capsule', () => {
    render(<LiquidGlass {...composer} cornerRadius={24} />)
    measure(384, 40)
    const raster = fakeContext.putImageData.mock.calls[0][0] as { width: number; height: number; data: Uint8ClampedArray }
    expect([raster.width, raster.height]).toEqual([384, 40])
    // With r clamped to 20 the corner is a full semicircle: the pixel just inside
    // the top-left corner sits outside the shape and stays flat.
    expect(raster.data[(0 * 384 + 0) * 4]).toBe(128)
  })
})
