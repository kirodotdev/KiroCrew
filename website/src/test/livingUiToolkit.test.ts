/**
 * Pins the Living UI toolkit, `toolkit/living-ui.js` in the backend's `living-ui`
 * built-in skill: what each helper does under a fake clock, its minified size
 * budget, and that it picks no colour, font or shape of its own.
 *
 * The file is a plain browser script, so each test runs it in a fresh `node:vm`
 * context whose `requestAnimationFrame`, `setTimeout`, `IntersectionObserver`,
 * `matchMedia` and tab visibility are fakes the test moves frame by frame. The elements a helper
 * writes come from this file's happy-dom document, and a MutationObserver counts
 * each write. A behaviour test runs ONE marked block alone, the way an author
 * copies it, so a helper that leaned on another block fails here. That SKILL.md
 * carries each block unchanged is pinned on the Python side, by
 * test/test_living_ui_toolkit.py.
 */
import { existsSync, readdirSync, readFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { runInNewContext } from 'node:vm'
import { transform } from 'esbuild'
import { describe, expect, it } from 'vitest'
import { THEME_VAR_NAMES } from '../lib/widgetSrcdoc'

const SKILL_DIR = resolve(__dirname, '../../../src/kiro_crew/builtin_skills/living-ui')
const SOURCE = readFileSync(join(SKILL_DIR, 'toolkit', 'living-ui.js'), 'utf8')
const LOOKS_DIR = join(SKILL_DIR, 'looks')
const BUDGET_BYTES = 5120
const HELPERS = ['loop', 'follow', 'spring', 'settle', 'smooth', 'burst', 'tour', 'readout', 'focusOne', 'explode', 'css']

interface Motion { x: number; v: number }
interface LoopHandle { kick(): void; stop(): void; readonly calm: boolean }
interface TourOptions { start?: number; resume?: number; period?: number; loops?: number }
interface TourHandle { readonly active: boolean; play(): void; pause(): void; takeOver(): void }
interface Words { say(text: string): void; describe(text: string): void; phase(text: string): void }
interface Part { el: Element & ElementCSSInlineStyle; move: [number, number] }
interface ExplodeOptions { stagger?: number; k?: number; c?: number }
interface Exploded { open(apart: boolean): void; toggle(): void; step(dt: number, calm: boolean): boolean }
interface Toolkit {
  loop(stage: object, tick: (dt: number, calm: boolean) => boolean): LoopHandle
  follow(x: number, target: number, dt: number, tau?: number, calm?: boolean): number
  spring(s: Motion, target: number, dt: number, k?: number, c?: number, calm?: boolean, substeps?: number): boolean
  settle(s: Motion, target: number, dt: number, seconds?: number, calm?: boolean): boolean
  smooth(a: number, b: number, x: number): number
  burst(age: number, rise?: number, fall?: number): number
  tour(stage: Element, at: (t: number) => void, options?: TourOptions): TourHandle
  readout(el: Element, options?: { target?: Element; live?: Element }): Words
  focusOne(root: Element, chosen: Element | null): void
  explode(items: Part[], options?: ExplodeOptions): Exploded
  css: string
}

/** The marked helpers by name, read from the same marker lines the block-copy script reads. */
function markedBlocks(source: string): Map<string, string> {
  const blocks = new Map<string, string>()
  const marked = /^\/\/ living-ui:begin ([A-Za-z][A-Za-z0-9]*)\n([\s\S]*?)^\/\/ living-ui:end \1$/gm
  for (const match of source.matchAll(marked)) blocks.set(match[1], match[2])
  return blocks
}

const BLOCKS = markedBlocks(SOURCE)

function block(name: string): string {
  const code = BLOCKS.get(name)
  if (code === undefined) throw new Error(`toolkit/living-ui.js marks no block named ${name}`)
  return code
}

interface FakeObserver {
  callback: (entries: Array<{ isIntersecting: boolean }>) => void
  options: { rootMargin?: string }
  targets: object[]
  disconnected: boolean
}

/** Runs `code` as a page script whose clock, viewport and motion setting only the test moves. */
function page(code: string, { reduced = false } = {}) {
  let now = 1000
  let nextHandle = 1
  const frames = new Map<number, (time: number) => void>()
  const timers = new Map<number, { due: number; fn: () => void }>()
  const observers: FakeObserver[] = []
  const queries: string[] = []
  const listeners = new Set<() => void>()
  const media = {
    matches: reduced,
    addEventListener: (type: string, fn: () => void) => { if (type === 'change') listeners.add(fn) },
    removeEventListener: (type: string, fn: () => void) => { if (type === 'change') listeners.delete(fn) },
  }
  const shown = new Set<() => void>()
  const tab = {
    hidden: false,
    addEventListener: (type: string, fn: () => void) => { if (type === 'visibilitychange') shown.add(fn) },
    removeEventListener: (type: string, fn: () => void) => { if (type === 'visibilitychange') shown.delete(fn) },
  }
  class IntersectionObserver {
    private readonly record: FakeObserver
    constructor(callback: FakeObserver['callback'], options: FakeObserver['options'] = {}) {
      this.record = { callback, options, targets: [], disconnected: false }
      observers.push(this.record)
    }
    observe(target: object) { this.record.targets.push(target) }
    disconnect() { this.record.disconnected = true }
  }
  const globals: Record<string, unknown> = {
    requestAnimationFrame: (fn: (time: number) => void) => { frames.set(nextHandle, fn); return nextHandle++ },
    cancelAnimationFrame: (handle: number) => { frames.delete(handle) },
    setTimeout: (fn: () => void, ms = 0) => { timers.set(nextHandle, { due: now + ms, fn }); return nextHandle++ },
    clearTimeout: (handle: number) => { timers.delete(handle) },
    matchMedia: (query: string) => { queries.push(query); return media },
    IntersectionObserver,
    document: tab,
  }
  runInNewContext(code, globals)
  return {
    lui: globals.LivingUI as Toolkit,
    /** Moves the clock on by `ms`, then runs every frame that was waiting for it. */
    frame(ms = 1000 / 60) {
      now += ms
      const due = [...frames.values()]
      frames.clear()
      for (const fn of due) fn(now)
    },
    waiting: () => frames.size,
    /** Moves the clock on by `ms`, running each timer that comes due on the way, earliest first. */
    wait(ms: number) {
      const until = now + ms
      for (;;) {
        let next: [number, { due: number; fn: () => void }] | undefined
        for (const entry of timers) if (entry[1].due <= until && (!next || entry[1].due < next[1].due)) next = entry
        if (!next) break
        timers.delete(next[0])
        now = next[1].due
        next[1].fn()
      }
      now = until
    },
    pending: () => timers.size,
    /** Runs `fn` on the page's clock `ms` from now. Of timers due at one instant, the one set first runs first. */
    later(ms: number, fn: () => void) {
      timers.set(nextHandle++, { due: now + ms, fn })
    },
    /** Hides or shows the tab, as switching to another tab and back does. */
    setHidden(on: boolean) {
      tab.hidden = on
      for (const fn of [...shown]) fn()
    },
    /** Delivers one observer callback carrying these entries, oldest first. */
    view(...inView: boolean[]) {
      for (const observer of observers) {
        if (!observer.disconnected) observer.callback(inView.map((isIntersecting) => ({ isIntersecting })))
      }
    },
    setReduced(on: boolean) {
      media.matches = on
      for (const fn of [...listeners]) fn()
    },
    observers,
    queries,
    listeners,
  }
}

/** A tick that records every call and answers whether it is still moving. */
function recorder(moving: (call: number) => boolean = () => true) {
  const calls: Array<{ dt: number; calm: boolean }> = []
  const tick = (dt: number, calm: boolean) => {
    calls.push({ dt, calm })
    return moving(calls.length)
  }
  return { calls, tick }
}

/**
 * Takes the writes `node` itself saw since the last call: `text <new text>` for a
 * replaced text, `<attribute> <value now>` for an attribute set or removed.
 */
function writes(node: Node): () => string[] {
  const seen: MutationRecord[] = []
  const observer = new MutationObserver((records) => { seen.push(...records) })
  observer.observe(node, { childList: true, attributes: true })
  return () => {
    const records = seen.splice(0).concat(observer.takeRecords())
    // happy-dom reports one text replacement as a removal, then an addition: count it once.
    const halfOfReplace = (i: number) =>
      records[i].type === 'childList' && !records[i].addedNodes.length && (records[i + 1]?.addedNodes.length ?? 0) > 0
    return records.flatMap((record, i) => {
      if (halfOfReplace(i)) return []
      if (record.type === 'childList') return [`text ${[...record.addedNodes].map((added) => added.textContent).join('')}`]
      return [`${record.attributeName} ${(record.target as Element).getAttribute(record.attributeName ?? '')}`]
    })
  }
}

describe('loop', () => {
  it('waits until the stage is in view, watched with a 120px margin', () => {
    const p = page(block('loop'))
    const stage = {}
    const { calls, tick } = recorder()
    p.lui.loop(stage, tick)
    expect(p.observers).toHaveLength(1)
    expect(p.observers[0].targets[0]).toBe(stage)
    expect(p.observers[0].options.rootMargin).toBe('120px 0px')
    p.frame()
    expect(calls).toHaveLength(0)
    p.view(true)
    p.frame()
    expect(calls).toHaveLength(1)
  })

  it('gives the first frame after a wake 1/60 s, then the real gap, never more than 1/30 s', () => {
    const p = page(block('loop'))
    const { calls, tick } = recorder()
    p.lui.loop({}, tick)
    p.view(true)
    p.frame(16)
    p.frame(20)
    p.frame(250)
    expect(calls.map((call) => call.dt)).toEqual([1 / 60, 20 / 1000, 1 / 30])
  })

  it('sleeps once tick returns false, and kick() wakes it with a fresh first frame', () => {
    const p = page(block('loop'))
    const { calls, tick } = recorder((call) => call !== 2)
    const run = p.lui.loop({}, tick)
    p.view(true)
    p.frame()
    p.frame()
    expect(p.waiting()).toBe(0)
    p.frame()
    expect(calls).toHaveLength(2)
    run.kick()
    run.kick()
    expect(p.waiting()).toBe(1)
    p.frame(5000)
    expect(calls).toHaveLength(3)
    expect(calls[2].dt).toBe(1 / 60)
  })

  it('reads the newest observer entry when one callback carries several', () => {
    const p = page(block('loop'))
    const { calls, tick } = recorder()
    const run = p.lui.loop({}, tick)
    p.view(false, true)
    p.frame()
    expect(calls).toHaveLength(1)
    p.view(true, false)
    expect(p.waiting()).toBe(0)
    p.frame()
    run.kick()
    expect(p.waiting()).toBe(0)
    expect(calls).toHaveLength(1)
  })

  it('passes calm from prefers-reduced-motion and wakes when that setting changes', () => {
    const p = page(block('loop'))
    const { calls, tick } = recorder(() => false)
    const run = p.lui.loop({}, tick)
    expect(p.queries).toEqual(['(prefers-reduced-motion: reduce)'])
    p.view(true)
    p.frame()
    expect(calls.map((call) => call.calm)).toEqual([false])
    expect(run.calm).toBe(false)
    p.setReduced(true)
    expect(run.calm).toBe(true)
    expect(p.waiting()).toBe(1)
    p.frame()
    expect(calls.map((call) => call.calm)).toEqual([false, true])
  })

  it('stop() cancels the waiting frame and lets go of the observer and the media listener', () => {
    const p = page(block('loop'))
    const { calls, tick } = recorder()
    const run = p.lui.loop({}, tick)
    p.view(true)
    expect(p.waiting()).toBe(1)
    expect(p.listeners.size).toBe(1)
    run.stop()
    expect(p.waiting()).toBe(0)
    expect(p.observers[0].disconnected).toBe(true)
    expect(p.listeners.size).toBe(0)
    run.kick()
    p.frame()
    expect(calls).toHaveLength(0)
  })

  it('stays stopped when stop() comes while it sleeps or from inside tick', () => {
    const asleep = page(block('loop'))
    const run = asleep.lui.loop({}, () => false)
    asleep.view(true)
    asleep.frame()
    run.stop()
    run.kick()
    expect(asleep.waiting()).toBe(0)

    const inside = page(block('loop'))
    const handle: { run?: LoopHandle } = {}
    handle.run = inside.lui.loop({}, () => {
      handle.run?.stop()
      return true
    })
    inside.view(true)
    inside.frame()
    expect(inside.waiting()).toBe(0)
  })

  it('can wake again after a tick throws', () => {
    const p = page(block('loop'))
    let broken = true
    const run = p.lui.loop({}, () => {
      if (broken) throw new Error('a bad copy')
      return false
    })
    p.view(true)
    expect(() => p.frame()).toThrow('a bad copy')
    broken = false
    run.kick()
    expect(p.waiting()).toBe(1)
  })

  it('keeps one frame waiting when tick itself calls kick()', () => {
    const p = page(block('loop'))
    const handle: { run?: LoopHandle } = {}
    handle.run = p.lui.loop({}, () => {
      handle.run?.kick()
      return true
    })
    p.view(true)
    p.frame()
    expect(p.waiting()).toBe(1)
  })
})

describe('follow', () => {
  const { lui } = page(block('follow'))

  it('covers 1 - e^(-t / tau) of the gap after t seconds, at any frame rate', () => {
    const after = (frames: number, fps: number) => {
      let x = 0
      for (let i = 0; i < frames; i++) x = lui.follow(x, 1, 1 / fps)
      return x
    }
    const expected = 1 - Math.exp(-0.3 / 0.2)
    expect(after(18, 60)).toBeCloseTo(expected, 9)
    expect(after(9, 30)).toBeCloseTo(expected, 9)
  })

  it('lands exactly on its target and stays there', () => {
    let x = 0
    let frames = 0
    while (x !== 1 && frames < 600) {
      x = lui.follow(x, 1, 1 / 60)
      frames++
    }
    expect(x).toBe(1)
    expect(frames).toBeLessThan(120)
    expect(lui.follow(x, 1, 1 / 60)).toBe(1)
  })

  it('jumps straight to its target under calm, and stays put when no time passes', () => {
    expect(lui.follow(0, 1, 1 / 60, 0.2, true)).toBe(1)
    expect(lui.follow(0.25, 1, 0)).toBe(0.25)
  })
})

describe('spring', () => {
  const { lui } = page(block('spring'))

  it('overshoots once, by about 7%, then lands exactly on its target', () => {
    const s = { x: 0, v: 0 }
    const xs: number[] = []
    const moving: boolean[] = []
    while (xs.length < 600) {
      moving.push(lui.spring(s, 1, 1 / 60))
      xs.push(s.x)
      if (!moving[moving.length - 1]) break
    }
    const over = xs.findIndex((x) => x > 1)
    const back = xs.findIndex((x, i) => i > over && x <= 1)
    expect(xs.slice(0, over).every((x, i) => i === 0 || x >= xs[i - 1])).toBe(true)
    expect(Math.max(...xs.slice(over, back))).toBeGreaterThan(1.05)
    expect(Math.max(...xs.slice(over, back))).toBeLessThan(1.09)
    expect(xs.slice(back).every((x) => Math.abs(x - 1) < 0.01)).toBe(true)
    expect(moving.slice(0, -1).every(Boolean)).toBe(true)
    expect(moving[moving.length - 1]).toBe(false)
    expect(xs.length).toBeLessThan(60)
    expect(s).toEqual({ x: 1, v: 0 })
  })

  it('jumps to its target and stops under calm', () => {
    const s = { x: 0, v: 3 }
    expect(lui.spring(s, 1, 1 / 60, 420, 26, true)).toBe(false)
    expect(s).toEqual({ x: 1, v: 0 })
  })
})

describe('settle', () => {
  const { lui } = page(block('settle'))

  it('covers about 90% of the way in `seconds`, rising without ever passing its target', () => {
    const s = { x: 0, v: 0 }
    const xs: number[] = []
    let moving = true
    while (moving && xs.length < 600) {
      moving = lui.settle(s, 1, 1 / 60)
      xs.push(s.x)
    }
    expect(xs[23]).toBeGreaterThan(0.88)
    expect(xs[23]).toBeLessThan(0.93)
    expect(xs.every((x, i) => x <= 1 && (i === 0 || x >= xs[i - 1]))).toBe(true)
    expect(s).toEqual({ x: 1, v: 0 })
  })

  it('never passes its target, even when it starts moving fast toward it', () => {
    const s = { x: 0, v: 40 }
    const xs: number[] = []
    let moving = true
    while (moving && xs.length < 600) {
      moving = lui.settle(s, 1, 1 / 60)
      xs.push(s.x)
    }
    expect(Math.max(...xs)).toBeLessThanOrEqual(1)
    expect(s).toEqual({ x: 1, v: 0 })
  })

  it('solves each frame exactly, so two half frames equal one whole frame', () => {
    const whole = { x: 0, v: 0 }
    const halves = { x: 0, v: 0 }
    lui.settle(whole, 1, 1 / 30)
    lui.settle(halves, 1, 1 / 60)
    lui.settle(halves, 1, 1 / 60)
    expect(halves.x).toBeCloseTo(whole.x, 12)
    expect(halves.v).toBeCloseTo(whole.v, 10)
  })

  it('jumps to its target and stops under calm', () => {
    const s = { x: 0, v: 0 }
    expect(lui.settle(s, 3, 1 / 60, 0.4, true)).toBe(false)
    expect(s).toEqual({ x: 3, v: 0 })
  })

  it('lands at once, instead of on NaN, when `seconds` is not above 0', () => {
    for (const seconds of [0, -1]) {
      const s = { x: 0, v: 0 }
      expect(lui.settle(s, 2, 1 / 60, seconds)).toBe(false)
      expect(s).toEqual({ x: 2, v: 0 })
    }
  })
})

describe('smooth', () => {
  const { lui } = page(block('smooth'))
  const slope = (x: number) => (lui.smooth(2, 4, x + 1e-6) - lui.smooth(2, 4, x - 1e-6)) / 2e-6

  it('is 0 up to a and 1 from b, with an S-curve between that starts and ends flat', () => {
    expect([1, 2, 3, 4, 9].map((x) => lui.smooth(2, 4, x))).toEqual([0, 0, 0.5, 1, 1])
    expect(Math.abs(slope(2))).toBeLessThan(1e-3)
    expect(Math.abs(slope(4))).toBeLessThan(1e-3)
    expect(slope(3)).toBeCloseTo(0.75, 6)
  })
})

describe('burst', () => {
  const { lui } = page(block('burst'))

  it('is 0 before the event, then rises from a flat start to 1 at `rise`', () => {
    expect(lui.burst(-0.5)).toBe(0)
    expect(lui.burst(0)).toBe(0)
    expect(lui.burst(0.001)).toBeLessThan(0.001)
    expect(lui.burst(0.04)).toBeCloseTo(0.5, 12)
    expect(lui.burst(0.08)).toBe(1)
  })

  it('fades more slowly than it rose, with `fall` as its time constant', () => {
    expect(lui.burst(0.08 + 0.13)).toBeCloseTo(Math.exp(-1), 12)
    expect(lui.burst(0.08 + 0.13 * Math.LN2)).toBeCloseTo(0.5, 12)
  })

  it('stays above 0 until it has faded, then returns exactly 0', () => {
    const ages = Array.from({ length: 960 }, (_, i) => (i + 1) / 1000)
    expect(ages.filter((age) => !(lui.burst(age) > 0))).toEqual([])
    expect(lui.burst(0.08 + 0.13 * 7)).toBe(0)
  })
})

describe('tour', () => {
  /** The inputs a tour pauses for, each built the way it reaches the stage from a part inside it. */
  const INPUTS: Record<string, () => Event> = {
    pointerdown: () => new PointerEvent('pointerdown', { bubbles: true }),
    pointermove: () => new PointerEvent('pointermove', { bubbles: true }),
    focusin: () => new FocusEvent('focusin', { bubbles: true }),
    keydown: () => new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }),
  }

  /**
   * A tour over a stage holding one part, with a control beside the stage. Frames
   * run 16 ms apart, so the tour's t steps by 16 while it drives.
   */
  function touring(options: TourOptions = { period: 1000 }, settings: { reduced?: boolean } = {}) {
    const p = page(block('tour'), settings)
    const figure = document.createElement('div')
    const stage = figure.appendChild(document.createElement('div'))
    const part = stage.appendChild(document.createElement('button'))
    const control = figure.appendChild(document.createElement('button'))
    const calls: number[] = []
    const tour = p.lui.tour(stage, (t) => { calls.push(t) }, options)
    return {
      p, stage, part, control, tour,
      /** The t of each at call since the last take. */
      take: () => calls.splice(0),
      /** No frame and no timer is waiting to run. */
      idle: () => p.waiting() === 0 && p.pending() === 0,
      /** Brings the stage into view and waits out `start`, so the next frame drives. */
      begin(start = 1400) {
        p.view(true)
        p.wait(start)
      },
      frames(count: number) {
        for (let i = 0; i < count; i++) p.frame(16)
      },
      input: (type: string, at: Element = part) => at.dispatchEvent(INPUTS[type]()),
    }
  }

  it('first calls at once the stage has been in view for `start` ms, counted from then, not from load', () => {
    const { p, tour, take, idle } = touring()
    p.wait(10_000)
    expect(take()).toEqual([])
    expect(idle()).toBe(true)
    p.view(true)
    p.wait(1399)
    expect(p.waiting()).toBe(0)
    p.wait(1)
    expect(tour.active).toBe(true)
    p.frame(16)
    expect(take()).toEqual([0])

    const early = touring({ period: 1000, start: 500 })
    early.begin(499)
    expect(early.p.waiting()).toBe(0)
    early.p.wait(1)
    early.frames(1)
    expect(early.take()).toEqual([0])
  })

  it('never tours a stage that never comes into view, and reads the newest observer entry with no margin', () => {
    const { p, take, idle } = touring()
    expect(p.observers[0].options.rootMargin).toBeUndefined()
    p.view(false)
    p.view(true, false)
    p.wait(20_000)
    p.frame(16)
    expect(take()).toEqual([])
    expect(idle()).toBe(true)
    p.view(false, true)
    p.wait(1400)
    p.frame(16)
    expect(take()).toEqual([0])
  })

  it('passes t in ms since its loop began: 0 on the first frame, then the real gap, never more than 1/30 s', () => {
    const { p, take, begin, frames } = touring({ period: 1000 })
    begin()
    p.frame(16)
    p.frame(20)
    p.frame(250)
    expect(take()).toEqual([0, 20, 20 + 100 / 3])
    frames(60)
    const times = take()
    expect(times.every((t) => t >= 0 && t < 1000)).toBe(true)
    expect(times[58]).toBeCloseTo(997 + 1 / 3, 9)
    expect(times[59]).toBeCloseTo(13 + 1 / 3, 9)

    const short = touring({ period: 500, loops: 1 })
    short.begin()
    short.frames(40)
    expect(short.take()).toEqual([...Array.from({ length: 32 }, (_, i) => i * 16), 500])
  })

  it.each(Object.keys(INPUTS))('pauses at once on %s inside the stage, and goes on from there `resume` ms later', (type) => {
    const { p, tour, take, begin, frames, input } = touring()
    begin()
    frames(10)
    expect(take().at(-1)).toBe(144)
    input(type)
    expect(tour.active).toBe(false)
    expect(p.waiting()).toBe(0)
    p.wait(3599)
    expect(p.waiting()).toBe(0)
    p.wait(1)
    frames(2)
    expect(take()).toEqual([144, 160])
  })

  it('stays paused through a steady stream of input, and resumes `resume` ms after the last one', () => {
    const { p, take, begin, frames, input } = touring()
    begin()
    frames(5)
    take()
    const waiting: number[] = []
    for (let i = 0; i < 40; i++) {
      input(i % 2 ? 'pointermove' : 'keydown')
      p.wait(500)
      waiting.push(p.waiting())
    }
    expect(waiting.every((count) => count === 0)).toBe(true)
    p.wait(3099)
    expect(p.waiting()).toBe(0)
    p.wait(1)
    p.frame(16)
    expect(take()).toEqual([64])
  })

  it('ignores input outside the stage, and takeOver() pauses it the same way for a control out there', () => {
    const { p, tour, take, control, begin, frames, input } = touring()
    begin()
    frames(3)
    input('pointerdown', control)
    input('keydown', control)
    frames(1)
    expect(take()).toEqual([0, 16, 32, 48])
    tour.takeOver()
    expect(tour.active).toBe(false)
    expect(p.waiting()).toBe(0)
    p.wait(3599)
    expect(p.waiting()).toBe(0)
    p.wait(1)
    p.frame(16)
    expect(take()).toEqual([48])
  })

  it('rests after `loops` loops on a last at(period), with nothing left running, and play() starts over', () => {
    const { p, tour, take, begin, frames, input, idle } = touring({ period: 1000, loops: 3 })
    begin()
    frames(400)
    const times = take()
    expect(times).toHaveLength(189)
    expect(times.filter((t, i) => i > 0 && t < times[i - 1])).toHaveLength(2)
    expect(times.slice(-2)).toEqual([992, 1000])
    expect(tour.active).toBe(false)
    expect(idle()).toBe(true)
    input('pointermove')
    p.view(false)
    p.view(true)
    p.wait(10_000)
    frames(10)
    expect(take()).toEqual([])
    expect(idle()).toBe(true)
    tour.play()
    frames(400)
    const again = take()
    expect(again).toHaveLength(189)
    expect([again[0], again.at(-1)]).toEqual([0, 1000])
    expect(idle()).toBe(true)
  })

  it('never calls at under reduced motion, and stops at once when that turns on mid-tour', () => {
    const still = touring({ period: 1000 }, { reduced: true })
    still.begin()
    still.tour.play()
    still.frames(10)
    expect(still.take()).toEqual([])
    expect(still.idle()).toBe(true)
    expect(still.p.queries).toEqual(['(prefers-reduced-motion: reduce)'])

    const { p, tour, take, begin, frames, idle } = touring()
    begin()
    frames(5)
    expect(take().at(-1)).toBe(64)
    p.setReduced(true)
    expect(tour.active).toBe(false)
    expect(idle()).toBe(true)
    p.wait(10_000)
    tour.play()
    frames(10)
    expect(take()).toEqual([])
    p.setReduced(false)
    p.wait(1399)
    expect(p.waiting()).toBe(0)
    p.wait(1)
    p.frame(16)
    expect(take()).toEqual([64])
  })

  it('calls no at offscreen, in a hidden tab or paused, and its time stands still meanwhile', () => {
    const { p, tour, take, begin, frames, idle } = touring()
    begin()
    frames(4)
    p.view(false)
    expect([tour.active, idle()]).toEqual([false, true])
    p.wait(10_000)
    begin()
    frames(1)
    p.setHidden(true)
    expect([tour.active, idle()]).toEqual([false, true])
    p.wait(10_000)
    p.setHidden(false)
    p.wait(1400)
    frames(1)
    tour.pause()
    expect([tour.active, idle()]).toEqual([false, true])
    p.wait(10_000)
    tour.play()
    frames(2)
    expect(take()).toEqual([0, 16, 32, 48, 48, 48, 48, 64])
  })

  it('never moves keyboard focus and writes nothing to the page, the live region included', () => {
    const p = page(block('tour'))
    const figure = document.body.appendChild(document.createElement('div'))
    const stage = figure.appendChild(document.createElement('div'))
    stage.tabIndex = 0
    const part = stage.appendChild(document.createElement('button'))
    const live = figure.appendChild(document.createElement('p'))
    live.setAttribute('aria-live', 'polite')
    const control = figure.appendChild(document.createElement('button'))
    try {
      control.focus()
      const seen = new MutationObserver(() => {})
      seen.observe(document.body, { subtree: true, childList: true, attributes: true, characterData: true })
      const times: number[] = []
      const tour = p.lui.tour(stage, (t) => { times.push(t) }, { period: 1000 })
      p.view(true)
      p.wait(1400)
      for (let i = 0; i < 100; i++) p.frame(16)
      expect(document.activeElement).toBe(control)
      part.focus()
      expect(tour.active).toBe(false)
      p.wait(3600)
      for (let i = 0; i < 100; i++) p.frame(16)
      expect(times).toHaveLength(190)
      expect(tour.active).toBe(false)
      expect(document.activeElement).toBe(part)
      expect(seen.takeRecords()).toEqual([])
      seen.disconnect()
    } finally {
      figure.remove()
    }
  })

  it('holds off an input that lands at the same instant as `start`, before or after its timer', () => {
    for (const first of ['input', 'timer']) {
      const { p, take, input } = touring()
      if (first === 'input') p.later(1400, () => input('pointerdown'))
      p.view(true)
      if (first === 'timer') p.later(1400, () => input('pointerdown'))
      p.wait(1400)
      expect(p.waiting(), first).toBe(0)
      p.wait(3599)
      expect(p.waiting(), first).toBe(0)
      p.wait(1)
      p.frame(16)
      expect(take(), first).toEqual([0])
    }
  })

  it('pauses for an input in its last loop, then finishes that loop and rests', () => {
    const { p, tour, take, begin, frames, input, idle } = touring({ period: 1000, loops: 3 })
    begin()
    frames(150)
    expect(take().at(-1)).toBe(384)
    input('keydown')
    p.wait(3600)
    frames(100)
    const rest = take()
    expect(rest).toHaveLength(40)
    expect([rest[0], ...rest.slice(-2)]).toEqual([384, 992, 1000])
    expect([tour.active, idle()]).toEqual([false, true])
  })

  it('stays paused after pause() through any input or takeOver(), with nothing running, until play()', () => {
    const { p, tour, take, begin, frames, input, idle } = touring()
    begin()
    frames(3)
    tour.play()
    frames(1)
    tour.pause()
    for (const type of Object.keys(INPUTS)) input(type)
    tour.takeOver()
    expect(idle()).toBe(true)
    p.wait(10_000)
    expect(take()).toEqual([0, 16, 32, 48])
    tour.play()
    expect(tour.active).toBe(true)
    frames(2)
    expect(take()).toEqual([48, 64])
  })

  it('waits `resume` ms, not `start`, when the stage comes back into view soon after an input', () => {
    const { p, take, begin, frames, input } = touring()
    begin()
    frames(2)
    input('pointerdown')
    p.wait(1000)
    p.view(false)
    p.wait(200)
    p.view(true)
    p.wait(1400)
    expect(p.waiting()).toBe(0)
    p.wait(2200)
    p.frame(16)
    expect(take()).toEqual([0, 16, 16])
  })

  it('restarts the `resume` wait when takeOver() comes while an input already paused it', () => {
    const { p, tour, take, begin, frames, input } = touring()
    begin()
    frames(2)
    input('pointerdown')
    p.wait(2000)
    tour.takeOver()
    p.wait(3599)
    expect(p.waiting()).toBe(0)
    p.wait(1)
    p.frame(16)
    expect(take()).toEqual([0, 16, 16])
  })

  const NO_PERIOD: Array<[string, TourOptions]> = [
    ['missing', {}],
    ['0', { period: 0 }],
    ['below 0', { period: -500 }],
    ['not a number', { period: Number.NaN }],
  ]

  it.each(NO_PERIOD)('never calls at with its period %s, and leaves nothing running', (_, options) => {
    const { p, tour, take, begin, frames, idle } = touring(options)
    begin()
    tour.play()
    frames(10)
    p.wait(10_000)
    expect(take()).toEqual([])
    expect([tour.active, idle()]).toEqual([false, true])
  })

  it('takes no options at all, and then never runs', () => {
    const p = page(block('tour'))
    const calls: number[] = []
    const tour = p.lui.tour(document.createElement('div'), (t) => { calls.push(t) })
    p.view(true)
    p.wait(1400)
    tour.play()
    p.frame(16)
    expect([calls, tour.active, p.waiting(), p.pending()]).toEqual([[], false, 0, 0])
  })

  it('is active only while it drives, inside its at calls too, but not in the last one as it rests', () => {
    const p = page(block('tour'))
    const stage = document.createElement('div')
    const inside: boolean[] = []
    const handle: { tour?: TourHandle } = {}
    handle.tour = p.lui.tour(stage, () => { inside.push(handle.tour?.active === true) }, { period: 1000, loops: 1 })
    const tour = handle.tour
    const states = [tour.active]
    p.view(true)
    states.push(tour.active)
    p.wait(1400)
    states.push(tour.active)
    p.frame(16)
    states.push(tour.active)
    stage.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
    states.push(tour.active)
    p.wait(3600)
    for (let i = 0; i < 70; i++) p.frame(16)
    states.push(tour.active)
    expect(states).toEqual([false, false, true, true, false, false])
    expect(inside).toHaveLength(65)
    expect(inside.slice(0, -1).every(Boolean)).toBe(true)
    expect(inside.at(-1)).toBe(false)
  })
})

describe('readout', () => {
  it('say writes the readout only when its text changes', () => {
    const { lui } = page(block('readout'))
    const el = document.createElement('output')
    const taken = writes(el)
    const words = lui.readout(el)
    for (const text of ['50%', '50%', '51%', '51%', '51%', '50%']) words.say(text)
    expect(taken()).toEqual(['text 50%', 'text 51%', 'text 50%'])
    expect(el.textContent).toBe('50%')
  })

  it('describe writes aria-valuetext on target at once, only when its text changes', () => {
    const { lui } = page(block('readout'))
    const el = document.createElement('output')
    const stage = document.createElement('div')
    const [onStage, onReadout] = [writes(stage), writes(el)]
    const words = lui.readout(el, { target: stage })
    words.describe('Half open')
    expect(onStage()).toEqual(['aria-valuetext Half open'])
    words.describe('Half open')
    expect(onStage()).toEqual([])
    words.describe('Open')
    expect(onStage()).toEqual(['aria-valuetext Open'])
    expect(onReadout()).toEqual([])
  })

  it('describe never runs from loop: a tick that says each frame writes no value text and no phase', () => {
    const p = page(block('loop') + block('readout'))
    const [stage, el, live] = [document.createElement('div'), document.createElement('output'), document.createElement('p')]
    const [onStage, onReadout, onLive] = [writes(stage), writes(el), writes(live)]
    const words = p.lui.readout(el, { target: stage, live })
    const state = { x: 0, target: 0 }
    const run = p.lui.loop(stage, (dt) => {
      state.x = Math.min(state.target, state.x + dt)
      words.say(`${Math.round(state.x * 100)}%`)
      return state.x !== state.target
    })
    p.view(true)
    state.target = 1 // one input: the state changes once
    words.describe('Full')
    run.kick()
    for (let i = 0; i < 90; i++) p.frame()
    p.wait(1000)
    expect(state.x).toBe(1)
    expect(onReadout().length).toBeGreaterThan(30)
    expect(onStage()).toEqual(['aria-valuetext Full'])
    expect(onLive()).toEqual([])
  })

  it('phase speaks the newest phase once 700 ms pass with no newer one, and only when it changed', () => {
    const p = page(block('readout'))
    const live = document.createElement('p')
    const taken = writes(live)
    const words = p.lui.readout(document.createElement('output'), { live })
    words.phase('opening')
    p.wait(400)
    words.phase('open')
    p.wait(699)
    expect(taken()).toEqual([])
    p.wait(1)
    expect(taken()).toEqual(['text open'])
    words.phase('open')
    p.wait(700)
    expect(taken()).toEqual([])
    for (let i = 0; i < 20; i++) {
      words.phase('moving')
      p.wait(100)
    }
    expect(taken()).toEqual([])
    words.phase('closed')
    p.wait(700)
    expect(taken()).toEqual(['text closed'])
    expect(p.pending()).toBe(0)
  })

  it('describe and phase do nothing with no target or live region', () => {
    const p = page(block('readout'))
    const el = document.createElement('output')
    const taken = writes(el)
    const words = p.lui.readout(el)
    words.describe('Open')
    words.phase('open')
    expect(p.pending()).toBe(0)
    p.wait(1000)
    expect(taken()).toEqual([])
  })
})

describe('focusOne', () => {
  const { lui } = page(block('focusOne'))
  // The LivingUI.css selectors that light one element and dim the rest.
  const LIT = '[data-lui-on]'
  const DIMMED = '[data-lui-focus] [data-lui-dim]:not([data-lui-on])'

  /** Three parts whose still markup already lights b, its settled state. */
  function figure() {
    const root = document.createElement('div')
    const parts = ['a', 'b', 'c'].map((id) => Object.assign(document.createElement('button'), { id }))
    for (const part of parts) part.setAttribute('data-lui-dim', '')
    parts[1].setAttribute('data-lui-on', '')
    root.append(...parts)
    return { root, parts }
  }
  const lit = (root: Element) => [...root.querySelectorAll(LIT)].map((el) => el.id)
  const dimmed = (parts: Element[]) => parts.filter((el) => el.matches(DIMMED)).map((el) => el.id)

  it('marks what the LivingUI.css rules select', () => {
    const { css } = page(block('css')).lui
    expect(css).toContain(`\n${LIT} {`)
    expect(css).toContain(`\n${DIMMED} {`)
  })

  it('lights the chosen element, takes the light from every other one, and dims the rest', () => {
    const { root, parts } = figure()
    lui.focusOne(root, parts[0])
    expect(root.hasAttribute('data-lui-focus')).toBe(true)
    expect(lit(root)).toEqual(['a'])
    expect(dimmed(parts)).toEqual(['b', 'c'])
    lui.focusOne(root, parts[2])
    expect(lit(root)).toEqual(['c'])
    expect(dimmed(parts)).toEqual(['a', 'b'])
  })

  it('takes both attributes away for null, so nothing is lit or dimmed', () => {
    const { root, parts } = figure()
    lui.focusOne(root, parts[0])
    lui.focusOne(root, null)
    expect(root.hasAttribute('data-lui-focus')).toBe(false)
    expect(lit(root)).toEqual([])
    expect(dimmed(parts)).toEqual([])
  })

  it('writes nothing when the lit element is chosen again, and never moves keyboard focus', () => {
    const { root, parts } = figure()
    root.tabIndex = 0
    document.body.append(root)
    try {
      root.focus()
      lui.focusOne(root, parts[0])
      const taken = [root, ...parts].map(writes)
      lui.focusOne(root, parts[0])
      expect(taken.flatMap((take) => take())).toEqual([])
      expect(document.activeElement).toBe(root)
    } finally {
      root.remove()
    }
  })
})

describe('explode', () => {
  const SVG = 'http://www.w3.org/2000/svg'
  const FRAME = 1 / 60

  /** `count` parts, each an SVG <g> of its own, part i moving by [10(i + 1), -5(i + 1)] when apart. */
  function figure(count: number, options?: ExplodeOptions, code = block('explode'), settings = {}) {
    const p = page(code, settings)
    const stage = document.createElementNS(SVG, 'svg')
    const parts: Part[] = Array.from({ length: count }, (_, i) => ({
      el: stage.appendChild(document.createElementNS(SVG, 'g')),
      move: [10 * (i + 1), -5 * (i + 1)],
    }))
    return { p, stage, parts, view: p.lui.explode(parts, options) }
  }

  /** How far a part is along its move now, read from its translate: 0 when it has none. */
  function share(part: Part): number {
    const transform = part.el.style.transform
    if (!transform) return 0
    const found = /^translate\((\S+)px, (\S+)px\)$/.exec(transform)
    if (!found) throw new Error(`not a translate in px: ${transform}`)
    return Number(found[1]) / part.move[0]
  }

  const transforms = (parts: Part[]) => parts.map((part) => part.el.style.transform)

  /** Steps the view a frame at a time until every part has landed; the number of frames it took. */
  function settle(view: Exploded): number {
    let frames = 1
    while (view.step(FRAME, false)) {
      if (++frames > 1000) throw new Error('the view never settled')
    }
    return frames
  }

  /** Steps the view 1 ms at a time for `ms` ms: the first ms at which each part's translate changed. */
  function firstMoves(view: Exploded, parts: Part[], ms: number): Array<number | null> {
    const before = transforms(parts)
    const first: Array<number | null> = parts.map(() => null)
    for (let t = 1; t <= ms; t++) {
      view.step(0.001, false)
      parts.forEach((part, i) => {
        if (first[i] === null && part.el.style.transform !== before[i]) first[i] = t
      })
    }
    return first
  }

  /**
   * Steps two views 1 ms at a time for `ms` ms after `turn(t)` acts on the first
   * one at each step: the first ms at which each part of the first is drawn
   * anywhere other than the same part of the second.
   */
  function parting(one: ReturnType<typeof figure>, two: ReturnType<typeof figure>, ms: number, turn: (t: number) => void) {
    const first: Array<number | null> = one.parts.map(() => null)
    for (let t = 1; t <= ms; t++) {
      turn(t)
      one.view.step(0.001, false)
      two.view.step(0.001, false)
      one.parts.forEach((part, i) => {
        if (first[i] === null && part.el.style.transform !== two.parts[i].el.style.transform) first[i] = t
      })
    }
    return first
  }

  it('opens the parts in order, each `stagger` ms after the one before, 35 by default', () => {
    const { view, parts } = figure(4)
    view.open(true)
    expect(firstMoves(view, parts, 300)).toEqual([1, 35, 70, 105])
    for (const [stagger, starts] of [[100, [1, 100, 200]], [0, [1, 1, 1]]] as const) {
      const custom = figure(3, { stagger })
      custom.view.open(true)
      expect(firstMoves(custom.view, custom.parts, 300), `stagger ${stagger}`).toEqual(starts)
    }
  })

  it('closes in reverse order, so the last part out is the first to leave for home', () => {
    const { view, parts } = figure(4)
    view.open(true)
    settle(view)
    expect(parts.map(share)).toEqual([1, 1, 1, 1])
    view.open(false)
    expect(firstMoves(view, parts, 300)).toEqual([105, 70, 35, 1])
    settle(view)
    expect(parts.map(share)).toEqual([0, 0, 0, 0])
  })

  it('re-staggers only the parts whose target changes when it closes part-way through opening', () => {
    // 50 ms in, parts 0 and 1 are on their way out and parts 2 and 3 still wait.
    const [closed, opened] = [figure(4), figure(4)]
    closed.view.open(true)
    opened.view.open(true)
    const taken = closed.parts.map((part) => writes(part.el))
    const parted = parting(closed, opened, 400, (t) => { if (t === 51) closed.view.open(false) })
    // Part 1, the last one out, turns back at once, part 0 turns 35 ms later,
    // and parts 2 and 3 never leave home.
    expect(parted.slice(0, 2)).toEqual([85, 51])
    expect(taken.map((take) => take().length > 0)).toEqual([true, true, false, false])
    settle(closed.view)
    expect(closed.parts.map(share)).toEqual([0, 0, 0, 0])
  })

  it('re-staggers only the parts whose target changes when it opens part-way through closing', () => {
    // 50 ms into closing, parts 3 and 2 are on their way home and parts 1 and 0 still wait.
    const [opened, closed] = [figure(4), figure(4)]
    for (const { view } of [opened, closed]) {
      view.open(true)
      settle(view)
      view.open(false)
    }
    const taken = opened.parts.map((part) => writes(part.el))
    const parted = parting(opened, closed, 400, (t) => { if (t === 51) opened.view.open(true) })
    // Part 2, the last one in, goes back out at once, part 3 goes 35 ms later,
    // and parts 0 and 1 never leave where they rest apart.
    expect(parted.slice(2)).toEqual([51, 85])
    expect(taken.map((take) => take().length > 0)).toEqual([false, false, true, true])
    settle(opened.view)
    expect(opened.parts.map(share)).toEqual([1, 1, 1, 1])
  })

  it('changes nothing when told to open while it opens, or to close while it is closed', () => {
    const [again, once] = [figure(4), figure(4)]
    again.view.open(true)
    once.view.open(true)
    expect(parting(again, once, 400, (t) => { if (t === 51) again.view.open(true) })).toEqual([null, null, null, null])

    const shut = figure(2)
    const taken = shut.parts.map((part) => writes(part.el))
    shut.view.open(false)
    expect([shut.view.step(FRAME, false), shut.view.step(FRAME, false)]).toEqual([false, false])
    expect(taken.flatMap((take) => take())).toEqual([])
  })

  it('toggle() opens a closed view and closes an open one, part-way too', () => {
    const [toggled, told] = [figure(3), figure(3)]
    toggled.view.toggle()
    told.view.open(true)
    const turns: Record<number, boolean> = { 41: false, 300: true, 1600: false }
    const parted = parting(toggled, told, 3600, (t) => {
      if (!(t in turns)) return
      toggled.view.toggle()
      told.view.open(turns[t])
    })
    expect(parted).toEqual([null, null, null])
    expect(toggled.parts.map(share)).toEqual([0, 0, 0])
  })

  it('answers true while a part moves or waits to start, and false once every part has landed', () => {
    // With 2 s between starts, part 0 lands long before part 1 leaves.
    const { view, parts } = figure(2, { stagger: 2000 })
    view.open(true)
    const answers: boolean[] = []
    for (let frame = 1; frame <= 400 && answers.at(-1) !== false; frame++) {
      answers.push(view.step(FRAME, false))
      if (frame === 100) expect(parts.map(share)).toEqual([1, 0])
    }
    expect(answers.slice(0, -1).every(Boolean)).toBe(true)
    expect(answers.at(-1)).toBe(false)
    expect(answers.length).toBeGreaterThan(190)
    expect(parts.map(share)).toEqual([1, 1])
    const taken = parts.map((part) => writes(part.el))
    expect([view.step(FRAME, false), view.step(FRAME, true)]).toEqual([false, false])
    expect(taken.flatMap((take) => take())).toEqual([])
  })

  it('drops the wait of a part a reversal keeps home, so it answers false once the moved parts land', () => {
    // 100 ms in, part 0 is on its way out and part 1 waits for its start at 2 s.
    const { view, parts } = figure(2, { stagger: 2000 })
    view.open(true)
    for (let frame = 0; frame < 6; frame++) view.step(FRAME, false)
    view.open(false)
    expect(settle(view) * FRAME).toBeLessThan(1.6)
    expect(parts.map(share)).toEqual([0, 0])
  })

  it('lands every part on its target in one step under calm, the waiting ones too', () => {
    const { view, parts } = figure(4)
    view.open(true)
    view.step(FRAME, false)
    expect(parts.map((part) => share(part) > 0)).toEqual([true, false, false, false])
    expect(view.step(FRAME, true)).toBe(false)
    expect(transforms(parts)).toEqual([
      'translate(10px, -5px)', 'translate(20px, -10px)', 'translate(30px, -15px)', 'translate(40px, -20px)',
    ])
    view.open(false)
    expect(view.step(FRAME, true)).toBe(false)
    expect(transforms(parts)).toEqual(['', '', '', ''])
    expect(view.step(FRAME, false)).toBe(false)
  })

  it('moves each el by its share of move in px, and takes the translate away once it is home', () => {
    const { view, parts } = figure(2)
    const taken = parts.map((part) => writes(part.el))
    for (let frame = 0; frame < 5; frame++) view.step(FRAME, false)
    expect(taken.flatMap((take) => take())).toEqual([])
    view.open(true)
    let checked = 0
    for (let frame = 0; frame < 20; frame++) {
      view.step(FRAME, false)
      for (const part of parts.filter((one) => one.el.style.transform)) {
        const [x, y] = (/^translate\((\S+)px, (\S+)px\)$/.exec(part.el.style.transform) ?? []).slice(1).map(Number)
        expect(y / part.move[1]).toBeCloseTo(x / part.move[0], 12)
        checked++
      }
    }
    expect(checked).toBe(20 + 18)
    settle(view)
    expect(transforms(parts)).toEqual(['translate(10px, -5px)', 'translate(20px, -10px)'])
    view.open(false)
    settle(view)
    expect(transforms(parts)).toEqual(['', ''])
  })

  it('springs each part with k and c: the defaults pass the target by under 1% and land about 1.4 s after it starts', () => {
    const path = (options?: ExplodeOptions) => {
      const { view, parts } = figure(1, options)
      view.open(true)
      const shares: number[] = []
      while (view.step(FRAME, false) && shares.length < 1000) shares.push(share(parts[0]))
      return shares
    }
    const defaults = path()
    expect(Math.max(...defaults)).toBeGreaterThan(1)
    expect(Math.max(...defaults)).toBeLessThan(1.01)
    expect(defaults.length * FRAME).toBeGreaterThan(1.2)
    expect(defaults.length * FRAME).toBeLessThan(1.6)
    const firm = path({ k: 400, c: 40 })
    expect(firm.length).toBeLessThan(defaults.length * 0.6)
    expect(Math.max(...firm)).toBeLessThanOrEqual(1)
    expect(Math.max(...path({ c: 4 }))).toBeGreaterThan(1.3)
  })

  it('moves each part exactly as the spring block moves its { x, v }, frame for frame', () => {
    // explode carries its own copy of spring's step so it can be copied alone;
    // this keeps the two copies the same, so a fix to one shows up in the other.
    const p = page(block('spring') + block('explode'))
    for (const [k, c] of [[64, 14.2], [400, 40], [64, 4]]) {
      const part: Part = { el: document.createElementNS(SVG, 'g'), move: [1, 1] }
      const view = p.lui.explode([part], { k, c })
      const s = { x: 0, v: 0 }
      view.open(true)
      let frames = 0
      for (let moving = true; moving && frames < 1000; frames++) {
        moving = view.step(FRAME, false)
        expect(p.lui.spring(s, 1, FRAME, k, c), `k ${k}, c ${c}, frame ${frames}`).toBe(moving)
        expect(share(part), `k ${k}, c ${c}, frame ${frames}`).toBe(s.x)
      }
      expect([frames > 20, s]).toEqual([true, { x: 1, v: 0 }])
    }
  })

  it('lands a part only within 0.0005 of its target, never at the top of a swing where it stands still', () => {
    // A loose spring swings past its target many times; at the top of each swing
    // its speed passes through 0, which 0.1 ms steps are sure to catch.
    const { view, parts } = figure(1, { c: 4 })
    view.open(true)
    let before = 0
    for (let t = 0; t < 100_000 && view.step(0.0001, false); t++) before = share(parts[0])
    expect(share(parts[0])).toBe(1)
    expect(Math.abs(before - 1)).toBeLessThan(6e-4)
  })

  it('runs from loop, which sleeps once every part has landed, after one frame under reduced motion', () => {
    for (const reduced of [false, true]) {
      const { p, stage, parts, view } = figure(3, undefined, block('loop') + block('explode'), { reduced })
      let ticks = 0
      const run = p.lui.loop(stage, (dt, calm) => {
        ticks++
        return view.step(dt, calm)
      })
      view.open(true)
      p.view(true)
      for (let frame = 0; frame < 400 && p.waiting(); frame++) p.frame()
      expect(p.waiting(), `reduced ${reduced}`).toBe(0)
      expect(parts.map(share), `reduced ${reduced}`).toEqual([1, 1, 1])
      if (reduced) expect(ticks).toBe(1)
      else expect(ticks).toBeGreaterThan(80)
      run.stop()
    }
  })
})

describe('css', () => {
  it('carries the rules the helpers rely on', () => {
    const { css } = page(block('css')).lui
    expect(css).toContain('[data-lui-focus] [data-lui-dim]:not([data-lui-on]) { opacity: .45 }')
    expect(css).toContain('[data-lui-on] { color: var(--accent); stroke: var(--accent) }')
    expect(css).toContain('[data-lui-stage]:focus-visible { outline: 1px solid var(--muted); outline-offset: 6px }')
    expect(css).toContain(
      '@media (prefers-reduced-motion: reduce) { [data-lui-stage] * { transition: none !important } }',
    )
  })
})

describe('the toolkit file', () => {
  it('marks each helper as its own block, and each block alone defines just its helper', () => {
    expect([...BLOCKS.keys()]).toEqual(expect.arrayContaining(HELPERS))
    for (const [name, code] of BLOCKS) expect(Object.keys(page(code).lui)).toEqual([name])
  })

  it('defines every helper once when all the blocks share one script', () => {
    expect(Object.keys(page(SOURCE).lui).sort()).toEqual([...BLOCKS.keys()].sort())
  })

  it(`minifies to at most ${BUDGET_BYTES} bytes`, async () => {
    const { code } = await transform(SOURCE, { loader: 'js', minify: true })
    expect(new TextEncoder().encode(code).length).toBeLessThanOrEqual(BUDGET_BYTES)
  })
})

// CSS colour keywords. `transparent` and `currentcolor` are left out: neither
// brings a colour of its own.
const NAMED_COLOURS = new Set(
  (
    'aliceblue antiquewhite aqua aquamarine azure beige bisque black blanchedalmond blue blueviolet ' +
    'brown burlywood cadetblue chartreuse chocolate coral cornflowerblue cornsilk crimson cyan ' +
    'darkblue darkcyan darkgoldenrod darkgray darkgreen darkgrey darkkhaki darkmagenta ' +
    'darkolivegreen darkorange darkorchid darkred darksalmon darkseagreen darkslateblue ' +
    'darkslategray darkslategrey darkturquoise darkviolet deeppink deepskyblue dimgray dimgrey ' +
    'dodgerblue firebrick floralwhite forestgreen fuchsia gainsboro ghostwhite gold goldenrod gray ' +
    'green greenyellow grey honeydew hotpink indianred indigo ivory khaki lavender lavenderblush ' +
    'lawngreen lemonchiffon lightblue lightcoral lightcyan lightgoldenrodyellow lightgray ' +
    'lightgreen lightgrey lightpink lightsalmon lightseagreen lightskyblue lightslategray ' +
    'lightslategrey lightsteelblue lightyellow lime limegreen linen magenta maroon ' +
    'mediumaquamarine mediumblue mediumorchid mediumpurple mediumseagreen mediumslateblue ' +
    'mediumspringgreen mediumturquoise mediumvioletred midnightblue mintcream mistyrose moccasin ' +
    'navajowhite navy oldlace olive olivedrab orange orangered orchid palegoldenrod palegreen ' +
    'paleturquoise palevioletred papayawhip peachpuff peru pink plum powderblue purple ' +
    'rebeccapurple red rosybrown royalblue saddlebrown salmon sandybrown seagreen seashell sienna ' +
    'silver skyblue slateblue slategray slategrey snow springgreen steelblue tan teal thistle ' +
    'tomato turquoise violet wheat white whitesmoke yellow yellowgreen'
  ).split(' '),
)

/** Every place `text` picks a colour, font or shape of its own instead of reading the theme. */
function styleFindings(text: string): string[] {
  const allowed = new Set<string>(THEME_VAR_NAMES)
  const found: string[] = []
  for (const match of text.matchAll(/#[0-9a-f]{3,8}\b|\b(?:rgba?|hsla?)\(/gi)) found.push(`colour ${match[0]}`)
  for (const match of text.matchAll(/\bfont-family\b|\bfont-size\s*:\s*[\d.]+px\b|\bborder-radius\b|\bbox-shadow\b/gi)) {
    found.push(`shape or font ${match[0]}`)
  }
  for (const match of text.matchAll(/var\(\s*(--[A-Za-z0-9_-]+)/g)) {
    if (!allowed.has(match[1])) found.push(`not a theme variable ${match[1]}`)
  }
  // A value after `name:` (CSS) or `name="` (an SVG attribute) that is a named colour.
  for (const match of text.matchAll(/[A-Za-z-]+\s*(?::|=\s*["'])\s*([^;{}"'\n]*)/g)) {
    for (const word of match[1].toLowerCase().match(/[a-z]+/g) ?? []) {
      if (NAMED_COLOURS.has(word)) found.push(`named colour ${word} in ${match[0].trim()}`)
    }
  }
  return found
}

describe('style neutrality', () => {
  it('flags each kind of style the toolkit must not pick, and passes theme variables', () => {
    const picks = [
      'color: red', 'stroke="white"', 'background: #fff', 'fill: rgba(0, 0, 0, .5)', 'hsl(200 50% 50%)',
      'font-family: serif', 'font-size: 12px', 'border-radius: 4px', 'box-shadow: none', 'color: var(--brand)',
    ]
    for (const pick of picks) expect(styleFindings(pick), pick).not.toEqual([])
    const neutral = ['color: var(--accent)', 'outline: 1px solid var(--muted)', 'opacity: .45', 'stroke: currentColor']
    for (const ok of neutral) expect(styleFindings(ok), ok).toEqual([])
  })

  it('finds none in the toolkit, in LivingUI.css, or in any file under looks/', () => {
    const texts: Array<[string, string]> = [
      ['toolkit/living-ui.js', SOURCE],
      ['LivingUI.css', page(block('css')).lui.css],
    ]
    if (existsSync(LOOKS_DIR)) {
      for (const entry of readdirSync(LOOKS_DIR, { recursive: true, withFileTypes: true })) {
        if (entry.isFile()) {
          const path = join(entry.parentPath, entry.name)
          texts.push([path, readFileSync(path, 'utf8')])
        }
      }
    }
    for (const [name, text] of texts) expect(styleFindings(text), name).toEqual([])
    expect(SOURCE).toContain('var(--accent)')
  })
})
