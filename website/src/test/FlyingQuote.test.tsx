import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, cleanup } from '@testing-library/react'
import { createRef } from 'react'
import FlyingQuote, { quoteFlight, quoteLandingX } from '../components/FlyingQuote'

/** Captures every framer-motion `animate` call the component makes, while still
 *  running the real animation, so the timing contract between the axes is
 *  assertable without driving rAF. */
const mm = vi.hoisted(() => ({ calls: [] as { keyframes: unknown; opts: Record<string, unknown> }[] }))

vi.mock('framer-motion', async (importOriginal) => {
  const actual = await importOriginal<typeof import('framer-motion')>()
  return {
    ...actual,
    animate: (value: never, keyframes: never, opts: never) => {
      mm.calls.push({ keyframes, opts: (opts ?? {}) as Record<string, unknown> })
      return actual.animate(value, keyframes, opts)
    },
  }
})

/** The quote-to-composer flight is the Safari-download "pluck": it must pop UP
 *  before it drops, and the drop must land promptly — the composer is inert
 *  while the overlay is in flight. The choreography is a pure function so those
 *  properties are assertable without driving rAF. */
describe('quoteFlight', () => {
  const START = 200
  const TARGET = 800

  it('rises above the source before falling to the target', () => {
    const [start, apex, target] = quoteFlight(START, TARGET).y
    expect(start).toBe(START)
    expect(target).toBe(TARGET)
    // Screen Y grows downward: the apex is the SMALLEST of the three.
    expect(apex).toBeLessThan(start)
    expect(apex).toBeLessThan(target)
  })

  it('pops visibly even when the quote sits right on top of the composer', () => {
    const { y: [start, apex] } = quoteFlight(500, 505)
    // A distance-proportional lift would be 1px here — imperceptible.
    expect(start - apex).toBeGreaterThanOrEqual(30)
  })

  it('caps the lift so a quote from the top of a long transcript stays on screen', () => {
    const { y: [start, apex] } = quoteFlight(0, 20000)
    expect(start - apex).toBeLessThanOrEqual(96)
  })

  it('keeps the apex on screen when the quote starts near the top edge', () => {
    // 120px down the viewport: an unclamped 96px lift lands the apex at 24,
    // half the overlay's own height above the top edge — it would vanish and
    // reappear already falling instead of popping.
    const [start, apex] = quoteFlight(120, 900, 38).y
    expect(apex).toBeGreaterThanOrEqual(38)
    expect(apex).toBeLessThan(start)
  })

  it('never sags below the source when the source is already at the ceiling', () => {
    const [start, apex, target] = quoteFlight(30, 900, 38).y
    expect(apex).toBeLessThanOrEqual(start)
    expect(apex).toBeLessThan(target)
  })

  it('spends most of the flight falling, not popping', () => {
    const { times } = quoteFlight(START, TARGET)
    expect(times[0]).toBe(0)
    expect(times[2]).toBe(1)
    expect(times[1]).toBeGreaterThan(0)
    const popShare = times[1]
    expect(popShare).toBeLessThan(0.5)
  })

  it('keeps the whole flight short even across a full-height drop', () => {
    // Sub-linear + capped: 10x the distance must not be 10x the duration.
    const near = quoteFlight(700, 800).duration
    const far = quoteFlight(0, 1400).duration
    expect(far).toBeGreaterThan(near)
    expect(far).toBeLessThanOrEqual(0.7)
  })

  it('grows on the pop, then is swallowed by the input', () => {
    const [from, peak, to] = quoteFlight(START, TARGET).scale
    expect(peak).toBeGreaterThan(from)
    expect(to).toBeLessThan(from)
  })
})

describe('quoteLandingX', () => {
  it('aims at where the quoted text will appear in a wide composer', () => {
    expect(quoteLandingX(25, 1050)).toBe(145)
  })

  it('aims at the centre of a composer too narrow for that inset', () => {
    expect(quoteLandingX(25, 160)).toBe(105)
  })
})

describe('FlyingQuote', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks(); mm.calls.length = 0 })

  const rect = { left: 40, top: 100, width: 200, height: 20 } as DOMRect

  function composer() {
    const host = document.createElement('div')
    host.appendChild(document.createElement('textarea'))
    document.body.appendChild(host)
    return host
  }

  it('completes immediately when the composer is gone, leaving no stuck overlay', () => {
    const onComplete = vi.fn()
    const targetRef = createRef<HTMLElement>()
    render(<FlyingQuote from={rect} targetRef={targetRef} text="hi" onComplete={onComplete} />)
    expect(onComplete).toHaveBeenCalledTimes(1)
  })

  it('lands the horizontal travel exactly when the fall lands', () => {
    // The horizontal axis used to run on a soft spring: on a full-width message
    // it could not catch up inside the flight, so the quote faded out mid-air at
    // a different point for every source column instead of arriving.
    render(<FlyingQuote from={rect} targetRef={{ current: composer() }} text="hi" onComplete={vi.fn()} />)
    const scalar = mm.calls.filter(c => typeof c.keyframes === 'number')
    const vertical = mm.calls.filter(c => Array.isArray(c.keyframes) && c.keyframes.length === 3)
    expect(scalar).toHaveLength(1)
    expect(vertical.length).toBeGreaterThan(0)
    expect(scalar[0].opts.duration).toBe(vertical[0].opts.duration)
    expect(scalar[0].opts.type).toBeUndefined()
  })

  it('skips the flight when the user asked for reduced motion', () => {
    vi.spyOn(window, 'matchMedia').mockReturnValue(
      { matches: true, media: '(prefers-reduced-motion: reduce)' } as MediaQueryList,
    )
    const onComplete = vi.fn()
    render(<FlyingQuote from={rect} targetRef={{ current: composer() }} text="hi" onComplete={onComplete} />)
    // Resolved synchronously: no animation was started to wait on.
    expect(onComplete).toHaveBeenCalledTimes(1)
    expect(mm.calls).toHaveLength(0)
  })
})
