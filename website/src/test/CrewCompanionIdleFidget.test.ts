/**
 * useIdleFidget — the built-in ghost's calm "aliveness": a small in-place hop or a
 * brief mood flicker on a gentle timer, never roaming. These pin the parts that
 * decide WHETHER it acts (the `enabled` gate) and WHERE a hop lands (on-screen, clear
 * of the Dock), plus the day/night cadence and the mood pools — all carried verbatim
 * from the desktop app.
 *
 * The hook is timer- and clock-driven, so time is faked and Math.random is stubbed to
 * a constant to pick deterministically from the flat action pool. By day the pool is
 * [mood, hop, ...4 body motions] picked uniformly, so index = floor(r * 6): r=0.25
 * lands on the hop (index 1) and also gives a 40px nudge; r=0.1 lands on the mood
 * (index 0) and picks index 0 of the mood pool. At night the pool is [mood, hop] only.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useIdleFidget } from '../apps/crew-companion/useIdleFidget'
import { PET_W, PET_H } from '../apps/crew-companion/constants'

const HOME = { x: 400, y: 300 }
const DAY = new Date(2026, 0, 1, 12, 0, 0)   // noon → base 150s
const NIGHT = new Date(2026, 0, 1, 2, 0, 0)  // 02:00 → base 600s, an order rarer

const DAY_MAX_DELAY = 300_000    // base 150s + up to 150s jitter
const NIGHT_BASE = 600_000

function mount(enabled: boolean) {
  const walkPath = vi.fn()
  const setMood = vi.fn()
  const playFidget = vi.fn()
  const view = renderHook(() =>
    useIdleFidget({ enabled, getPos: () => HOME, walkPath, setMood, playFidget }),
  )
  return { walkPath, setMood, playFidget, ...view }
}

beforeEach(() => {
  vi.useFakeTimers()
  // Fix the viewport so the hop clamp is deterministic.
  Object.defineProperty(window, 'innerWidth', { value: 1440, configurable: true })
  Object.defineProperty(window, 'innerHeight', { value: 900, configurable: true })
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.useRealTimers()
})

describe('useIdleFidget gating', () => {
  it('does nothing at all while disabled, however long it waits', () => {
    vi.setSystemTime(DAY)
    vi.spyOn(Math, 'random').mockReturnValue(0.25) // would be a hop if enabled
    const { walkPath, setMood } = mount(false)
    act(() => { vi.advanceTimersByTime(DAY_MAX_DELAY * 3) })
    expect(walkPath).not.toHaveBeenCalled()
    expect(setMood).not.toHaveBeenCalled()
  })

  it('when enabled, a tick fires within the day cadence window', () => {
    vi.setSystemTime(DAY)
    vi.spyOn(Math, 'random').mockReturnValue(0.25)
    const { walkPath } = mount(true)
    act(() => { vi.advanceTimersByTime(DAY_MAX_DELAY) })
    expect(walkPath).toHaveBeenCalledTimes(1)
  })
})

describe('useIdleFidget hop geometry', () => {
  it('hops out and comes straight back home, staying on-screen and clear of the Dock', () => {
    vi.setSystemTime(DAY)
    // 0.25 -> index 1 = hop; angle=0.25*2π; dist=30+0.25*40=40.
    vi.spyOn(Math, 'random').mockReturnValue(0.25)
    const { walkPath, setMood } = mount(true)
    act(() => { vi.advanceTimersByTime(DAY_MAX_DELAY) })

    expect(setMood).not.toHaveBeenCalled()
    expect(walkPath).toHaveBeenCalledTimes(1)
    const points = walkPath.mock.calls[0][0] as Array<{ x: number; y: number }>
    expect(points).toHaveLength(2)
    // Second waypoint is always exactly home — it never wanders off.
    expect(points[1]).toEqual(HOME)
    // First waypoint is clamped on-screen and above the Dock margin.
    expect(points[0].x).toBeGreaterThanOrEqual(0)
    expect(points[0].x).toBeLessThanOrEqual(window.innerWidth - PET_W)
    expect(points[0].y).toBeGreaterThanOrEqual(0)
    expect(points[0].y).toBeLessThanOrEqual(window.innerHeight - PET_H - 40)
  })

  it('the hop distance stays inside the 30–70px "nearby" band', () => {
    vi.setSystemTime(DAY)
    vi.spyOn(Math, 'random').mockReturnValue(0.25)
    const { walkPath } = mount(true)
    act(() => { vi.advanceTimersByTime(DAY_MAX_DELAY) })
    const points = walkPath.mock.calls[0][0] as Array<{ x: number; y: number }>
    const dist = Math.hypot(points[0].x - HOME.x, points[0].y - HOME.y)
    // Rounding to whole pixels can shave a fraction, so allow ±1 around the band.
    expect(dist).toBeGreaterThanOrEqual(29)
    expect(dist).toBeLessThanOrEqual(71)
  })
})

describe('useIdleFidget mood flicker', () => {
  it('by day flickers an ambient mood (curious/happy), not a hop', () => {
    vi.setSystemTime(DAY)
    vi.spyOn(Math, 'random').mockReturnValue(0.1) // branch=mood; pool index 0
    const { walkPath, setMood } = mount(true)
    act(() => { vi.advanceTimersByTime(DAY_MAX_DELAY) })
    expect(walkPath).not.toHaveBeenCalled()
    expect(setMood).toHaveBeenCalledTimes(1)
    expect(['curious', 'happy']).toContain(setMood.mock.calls[0][0])
  })

  it('overnight it dozes off (sleepy) and fires an order of magnitude more rarely', () => {
    vi.setSystemTime(NIGHT)
    vi.spyOn(Math, 'random').mockReturnValue(0) // jitter 0 → exactly base; branch=mood
    const { setMood } = mount(true)
    // The whole daytime window passes with nothing: night is much slower.
    act(() => { vi.advanceTimersByTime(DAY_MAX_DELAY) })
    expect(setMood).not.toHaveBeenCalled()
    // At the night base it finally ticks, and the mood is sleepy.
    act(() => { vi.advanceTimersByTime(NIGHT_BASE - DAY_MAX_DELAY) })
    expect(setMood).toHaveBeenCalledTimes(1)
    expect(setMood.mock.calls[0][0]).toBe('sleepy')
  })
})
