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
import { LiquidGlass, resetDisplacementMapCache } from '../components/ui/liquid-glass'

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
const composer = { cornerRadius: 16, frost: 4, lightIntensity: 25 }

function measure(width: number, height: number) {
  act(() => {
    for (const cb of observers) cb([{ contentRect: { width, height } }])
  })
}

/** The effect layers under the root, in document order. Spans, so a `<button>`
 *  host stays valid phrasing content. */
function layersOf(root: HTMLElement) {
  return Array.from(root.querySelectorAll<HTMLElement>(':scope > span[aria-hidden="true"]'))
}

/** The box that carries the frost: the tint layer clips an oversized child so
 *  Chromium's under-blurred far edge lies outside the pane. */
function frostBoxOf(layer: HTMLElement) {
  return layer.firstElementChild as HTMLElement
}

beforeEach(() => {
  vi.useFakeTimers()
  observers.length = 0
  resetDisplacementMapCache()
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
    const frost = frostBoxOf(layers[1])
    expect(frost.style.background).toContain('var(--glass-tint)')
    expect(frost.style.backdropFilter).toBe('blur(4px) saturate(1.55)')
    // Two blur radii of overhang on every side, clipped by the layer.
    expect(frost.style.inset).toBe('-8px')
    expect(layers[1].style.overflow).toBe('hidden')
    // Every layer clips on the same circular radius as the root, and paints
    // under the children inside the host's own stacking context.
    for (const l of layers) expect(l.style.borderRadius).toBe('16px')
    for (const l of layers) expect(l.style.zIndex).toBe('-1')
    expect(root.style.isolation).toBe('isolate')
    // The children are the host's own children: no wrapper box between them
    // and the layers, so a caller's `parentElement` / flex reads still hold.
    expect(root.lastElementChild?.tagName).toBe('SPAN')
    expect(root.lastElementChild?.textContent).toBe('hi')
    expect(root.textContent).toBe('hi')
    // Every layer, and nothing else, carries the attribute index.css hides
    // under the solidifying fallbacks: a decorative `aria-hidden` icon among
    // the directly-rendered children must survive them.
    const { container: c2 } = render(<LiquidGlass {...composer}><svg aria-hidden="true" data-testid="icon" /></LiquidGlass>)
    const root2 = c2.firstElementChild as HTMLElement
    for (const l of layersOf(root2)) expect(l.hasAttribute('data-liquid-glass-layer')).toBe(true)
    expect(root2.querySelector('[data-testid="icon"]')?.hasAttribute('data-liquid-glass-layer')).toBe(false)
    expect(root2.querySelectorAll(':scope > [data-liquid-glass-layer]')).toHaveLength(layersOf(root2).length)
  })

  // The pane IS the control when asked: a follow-up chip is `<LiquidGlass
  // as="button">`, so one element is the flex item, the entrance animation and
  // the click target, and its own attributes ride along.
  it('renders as the element it is asked to be, merging class, style and ref', () => {
    const ref = { current: null as HTMLElement | null }
    const onClick = vi.fn()
    const { container } = render(
      <LiquidGlass {...composer} as="button" ref={ref} type="button" className="chip shrink-0" style={{ animationDelay: '40ms' }} onClick={onClick} aria-busy>
        Go
      </LiquidGlass>,
    )
    const host = container.firstElementChild as HTMLButtonElement
    expect(host.tagName).toBe('BUTTON')
    expect(host.className).toBe('liquid-glass chip shrink-0')
    expect(host.getAttribute('type')).toBe('button')
    expect(host.getAttribute('aria-busy')).toBe('true')
    // The caller's style rides along; the material's own position / radius win.
    expect(host.style.animationDelay).toBe('40ms')
    expect(host.style.position).toBe('relative')
    expect(host.style.borderRadius).toBe('16px')
    expect(ref.current).toBe(host)
    host.click()
    expect(onClick).toHaveBeenCalledTimes(1)
    // A button host holds only phrasing content: every layer is a span.
    expect(host.querySelector('div')).toBeNull()
  })

  // A padded control reports a content box smaller than the box the layers
  // span (`inset: 0` = the padding box), so the measurer reads clientWidth /
  // clientHeight when the host has them and falls back to the content rect.
  it('measures the padding box, not the content box, when the host reports one', () => {
    const { container } = render(<LiquidGlass {...composer} as="button" />)
    const host = container.firstElementChild as HTMLElement
    Object.defineProperty(host, 'clientWidth', { value: 120, configurable: true })
    Object.defineProperty(host, 'clientHeight', { value: 30, configurable: true })
    measure(96, 18)
    const image = host.querySelector('feImage') as SVGElement
    expect(image.getAttribute('width')).toBe('120')
    expect(image.getAttribute('height')).toBe('30')
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
    expect(frostBoxOf(layers[1]).style.backdropFilter).toBe('blur(4px) saturate(1.55)')
    // The side lines lead: 1px of --glass-edge down each flank, nothing on the
    // top and bottom edges. Then the lit edges' crisp core: 1px of --glass-band
    // just inside the top and bottom edges (full strength, so a light page
    // reads #ffffff there). Then the two OUTER half-pixel hairlines just past
    // the top and bottom edges. Then the bevel, lit from straight above (no
    // horizontal offset in either inset).
    expect(layers[2].style.boxShadow).toMatch(/^inset 1px 0(px)? 0(px)? var\(--glass-edge\), inset -1px 0(px)? 0(px)? var\(--glass-edge\), inset 0(px)? 1px 0(px)? var\(--glass-band\), inset 0(px)? -1px 0(px)? var\(--glass-band\), 0(px)? -0\.5px 0(px)? 0(px)? var\(--glass-hairline\), 0(px)? 0\.5px 0(px)? 0(px)? var\(--glass-hairline\), inset 0px 3\.27px/)
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
    // The band gradient lives on a custom property (jsdom drops color-mix() from
    // `background`); the stops are the theme's --glass-band at falling shares.
    expect(specular.style.getPropertyValue('--liquid-glass-bands')).toContain('linear-gradient(to bottom')
    expect(specular.style.getPropertyValue('--liquid-glass-bands')).toContain('color-mix(in srgb, var(--glass-band) 100%, transparent) 0px')
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

  it('rasterises one map for many panes of the same size, and one more per new size', () => {
    // A list of panes (the bell popover's rows, a row of chips) mounts many
    // equal boxes in one commit; the map is a pure function of (size, radius,
    // band), so the second pane reuses the first pane's encode.
    const { container } = render(<><LiquidGlass {...composer} /><LiquidGlass {...composer} /><LiquidGlass {...composer} /></>)
    measure(400, 80)
    expect(fakeContext.putImageData.mock.calls.length).toBe(1)
    const roots = Array.from(container.children) as HTMLElement[]
    for (const root of roots) expect((root.querySelector('feImage') as SVGElement).getAttribute('href')).toBe(MAP_URL)
    // A different size (or radius) is a different map.
    render(<LiquidGlass {...composer} cornerRadius={4} />)
    measure(400, 80)
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
