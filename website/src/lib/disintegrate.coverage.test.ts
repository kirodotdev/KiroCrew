/**
 * `disintegrate`: the erosion and dust timelines it hands to anime, the reduced
 * motion fallback, the palette choice and the cleanup of the particle layer.
 * anime is replaced by a recorder so each timeline callback can be driven in
 * order; the layout is stubbed on the element because happy-dom reports zero size.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { disintegrate } from './disintegrate'

const animeCalls = vi.hoisted(() => [] as Record<string, unknown>[])

vi.mock('./anime.es', () => ({
  default: (opts: Record<string, unknown>) => {
    animeCalls.push(opts)
    return {}
  },
}))

const RECT = { left: 10, top: 20, width: 90, height: 40, right: 100, bottom: 60, x: 10, y: 20, toJSON: () => ({}) }

let el: HTMLElement
let originalMatchMedia: typeof window.matchMedia

function mountElement(rect: Partial<DOMRect> = RECT, reduce = false): HTMLElement {
  const node = document.createElement('div')
  document.body.appendChild(node)
  node.getBoundingClientRect = () => ({ ...RECT, ...rect }) as DOMRect
  window.matchMedia = vi.fn(() => ({ matches: reduce }) as unknown as MediaQueryList)
  return node
}

const erosion = () => animeCalls[0]
const dust = () => animeCalls[1]
const layer = () => document.querySelector<HTMLElement>('div[style*="position: fixed"], div[style*="position:fixed"]')

beforeEach(() => {
  animeCalls.length = 0
  originalMatchMedia = window.matchMedia
  el = mountElement()
})

afterEach(() => {
  window.matchMedia = originalMatchMedia
  vi.restoreAllMocks()
  document.body.replaceChildren()
})

describe('disintegrate', () => {
  it('resolves at once for a missing element and animates nothing', async () => {
    await expect(disintegrate(null)).resolves.toBeUndefined()
    expect(animeCalls).toHaveLength(0)
  })

  it('fades a tiny element out and resolves when the fade completes', async () => {
    const tiny = mountElement({ width: 1, height: 1 })
    const done = disintegrate(tiny)
    expect(animeCalls).toHaveLength(1)
    expect(erosion().targets).toBe(tiny)
    expect(erosion().opacity).toEqual([1, 0])
    expect(erosion().duration).toBe(180)
    ;(erosion().complete as () => void)()
    await expect(done).resolves.toBeUndefined()
  })

  it('takes the plain fade under reduced motion', () => {
    const calm = mountElement(RECT, true)
    void disintegrate(calm)
    expect(animeCalls).toHaveLength(1)
    expect(erosion().targets).toBe(calm)
    expect(erosion().duration).toBe(180)
  })

  it('erodes the element with a left-to-right mask, blur and fade as the front advances', async () => {
    const done = disintegrate(el)
    const state = erosion().targets as { p: number }
    state.p = 0.5
    ;(erosion().update as () => void)()
    expect(el.style.maskImage).toBe('linear-gradient(to right, transparent 50%, #000 62%)')
    expect(el.style.opacity).toBe('0.5')
    expect(el.style.filter).toBe('blur(0.8px)')
    ;(erosion().complete as () => void)()
    await expect(done).resolves.toBeUndefined()
  })

  it('sweeps the mask bottom-to-top when the direction is up', () => {
    void disintegrate(el, { direction: 'up', duration: 300 })
    expect(erosion().duration).toBe(300)
    const state = erosion().targets as { p: number }
    state.p = 1
    ;(erosion().update as () => void)()
    expect(el.style.webkitMaskImage).toContain('to top')
  })

  it('spreads particles across the element and tints them with the given colour', () => {
    vi.spyOn(Math, 'random').mockReturnValue(0.5)
    void disintegrate(el, { particleColor: 'rgb(1, 2, 3)' })
    const particles = dust().targets as HTMLElement[]
    expect(particles).toHaveLength(80)
    expect(particles.every((p) => p.style.background.includes('1, 2, 3'))).toBe(true)
    // 12 columns across the 90px element: column centres run from 13.75px to 96.25px.
    const lefts = particles.map((p) => parseFloat(p.style.left))
    expect(Math.min(...lefts)).toBeCloseTo(RECT.left + (0.5 * RECT.width) / 12)
    expect(Math.max(...lefts)).toBeCloseTo(RECT.left + (11.5 * RECT.width) / 12)
    // Random 0.5 puts every particle at the vertical middle of the element.
    expect(particles.every((p) => parseFloat(p.style.top) === RECT.top + RECT.height / 2)).toBe(true)
  })

  it('falls back to a neutral grey when the given colour is transparent', () => {
    void disintegrate(el, { particleColor: 'rgba(0, 0, 0, 0)' })
    const particles = dust().targets as HTMLElement[]
    expect(particles.every((p) => /200,\s*200,\s*205/.test(p.style.background))).toBe(true)
  })

  it('drives the dust drift, spin and fade values', () => {
    vi.spyOn(Math, 'random').mockReturnValue(0)
    void disintegrate(el)
    const timeline = dust()
    const drift = timeline.translateX as () => number
    const lift = timeline.translateY as () => number
    const spin = timeline.rotate as () => number
    expect(drift()).toBeCloseTo(-11.2)
    expect(lift()).toBe(-22)
    expect(spin()).toBe(-35)
    const opacity = (timeline.opacity as { value: () => number }[])[0]
    expect(opacity.value()).toBeCloseTo(0.6)
  })

  it('staggers the dust by column toward the sweep side', () => {
    vi.spyOn(Math, 'random').mockReturnValue(0)
    void disintegrate(el)
    const delay = dust().delay as (target: HTMLElement, index: number) => number
    const particles = dust().targets as HTMLElement[]
    expect(delay(particles[0], 0)).toBe(0)
    expect(delay(particles[1], 1)).toBeCloseTo(520 * 0.5 / 12)
  })

  it('staggers the dust bottom-first when the sweep is upward', () => {
    vi.spyOn(Math, 'random').mockReturnValue(0)
    void disintegrate(el, { direction: 'up' })
    const delay = dust().delay as (target: HTMLElement, index: number) => number
    const particles = dust().targets as HTMLElement[]
    particles[0].style.top = `${RECT.top}px`
    particles[1].style.top = `${RECT.top + RECT.height}px`
    expect(delay(particles[0], 0)).toBe(260)
    expect(delay(particles[1], 1)).toBe(0)
  })

  it('removes the particle layer once the dust has settled', () => {
    void disintegrate(el)
    expect(layer()).not.toBeNull()
    ;(dust().complete as () => void)()
    expect(layer()).toBeNull()
  })
})
