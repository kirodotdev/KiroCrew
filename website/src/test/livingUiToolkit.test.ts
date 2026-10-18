/**
 * Pins the Living UI toolkit, `toolkit/living-ui.js` in the backend's `living-ui`
 * built-in skill: what each helper does under a fake clock, its minified size
 * budget, and that it picks no colour, font or shape of its own.
 *
 * The file is a plain browser script, so each test runs it in a fresh `node:vm`
 * context whose `requestAnimationFrame`, `IntersectionObserver` and `matchMedia`
 * are fakes the test moves frame by frame. A behaviour test runs ONE marked block
 * alone, the way an author copies it, so a helper that leaned on another block
 * fails here. That SKILL.md carries each block unchanged is pinned on the Python
 * side, by test/test_living_ui_toolkit.py.
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
const CORE_HELPERS = ['loop', 'follow', 'spring', 'settle', 'smooth', 'burst', 'css']

interface Motion { x: number; v: number }
interface LoopHandle { kick(): void; stop(): void; readonly calm: boolean }
interface Toolkit {
  loop(stage: object, tick: (dt: number, calm: boolean) => boolean): LoopHandle
  follow(x: number, target: number, dt: number, tau?: number, calm?: boolean): number
  spring(s: Motion, target: number, dt: number, k?: number, c?: number, calm?: boolean, substeps?: number): boolean
  settle(s: Motion, target: number, dt: number, seconds?: number, calm?: boolean): boolean
  smooth(a: number, b: number, x: number): number
  burst(age: number, rise?: number, fall?: number): number
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
  const observers: FakeObserver[] = []
  const queries: string[] = []
  const listeners = new Set<() => void>()
  const media = {
    matches: reduced,
    addEventListener: (type: string, fn: () => void) => { if (type === 'change') listeners.add(fn) },
    removeEventListener: (type: string, fn: () => void) => { if (type === 'change') listeners.delete(fn) },
  }
  class IntersectionObserver {
    private readonly record: FakeObserver
    constructor(callback: FakeObserver['callback'], options: FakeObserver['options']) {
      this.record = { callback, options, targets: [], disconnected: false }
      observers.push(this.record)
    }
    observe(target: object) { this.record.targets.push(target) }
    disconnect() { this.record.disconnected = true }
  }
  const globals: Record<string, unknown> = {
    requestAnimationFrame: (fn: (time: number) => void) => { frames.set(nextHandle, fn); return nextHandle++ },
    cancelAnimationFrame: (handle: number) => { frames.delete(handle) },
    matchMedia: (query: string) => { queries.push(query); return media },
    IntersectionObserver,
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
  it('marks each core helper as its own block, and each block alone defines just its helper', () => {
    expect([...BLOCKS.keys()]).toEqual(expect.arrayContaining(CORE_HELPERS))
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
