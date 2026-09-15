/**
 * GhostScene — the Worlds haunt — driven frame by frame.
 *
 * The existing `Scenes.test.tsx` only proves each scene mounts a canvas, so
 * everything this file actually DOES (the twilight sky, the eight outfits, the
 * blink timer, the entrance animation, the speech bubble that follows a
 * session's latest message) was never exercised. happy-dom has no 2D context and
 * no real frame clock, so the harness below supplies both:
 *
 *   - a RECORDING canvas context, so an assertion can ask "was the crown drawn?"
 *     or "which text landed on the overlay?" instead of inspecting pixels;
 *   - a hand-driven `requestAnimationFrame`, so a test advances exactly N frames
 *     with no dependence on elapsed time or a real animation clock;
 *   - a pinned `Math.random` and `Date.now`, so outfit choice, blink timers and
 *     bubble expiry are deterministic.
 *
 * What is asserted is what a viewer would see: how many ghosts are on screen,
 * which one is labelled "haunting", whether the eyes are shut on this frame,
 * whether the bubble is still up.
 */
import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, act } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer from '../store/chatSlice'
import GhostScene from '../pages/scenes/GhostScene'
import type { AgentSource } from '../hooks/useAgentSync'

/* ── Recording 2D context ── */

interface FillRecord { x: number; y: number; w: number; h: number; color: string }
interface TextRecord { text: string; x: number; y: number }

interface Recorder {
  ctx: CanvasRenderingContext2D
  fills: FillRecord[]
  texts: TextRecord[]
  arcs: number[][]
  /** How many radial gradients were requested (moon glow, per-ghost aura). */
  radials: { count: number }
}

/**
 * A 2D context that remembers what it was asked to draw. Unknown members become
 * no-op spies on first access, so a drawing helper reaching for `roundRect` or
 * `ellipse` never has to be enumerated here.
 */
function createRecorder(): Recorder {
  const fills: FillRecord[] = []
  const texts: TextRecord[] = []
  const arcs: number[][] = []
  const radials = { count: 0 }
  const gradient = { addColorStop: vi.fn() }

  const store: Record<string, unknown> = {
    fillStyle: '#000000',
    strokeStyle: '#000000',
    globalAlpha: 1,
    font: '',
    textAlign: 'start',
    textBaseline: 'alphabetic',
    imageSmoothingEnabled: false,
    canvas: { width: 0, height: 0 },
    createLinearGradient: vi.fn(() => gradient),
    createRadialGradient: vi.fn(() => { radials.count++; return gradient }),
    createPattern: vi.fn(() => null),
    // Width proportional to length so the real word-wrapper actually wraps.
    measureText: vi.fn((t: string) => ({ width: t.length * 8 })),
    getImageData: vi.fn(() => ({ data: new Uint8ClampedArray(4) })),
  }
  store.fillRect = vi.fn((x: number, y: number, w: number, h: number) => {
    fills.push({ x, y, w, h, color: String(store.fillStyle) })
  })
  store.fillText = vi.fn((text: string, x: number, y: number) => {
    texts.push({ text, x, y })
  })
  store.arc = vi.fn((x: number, y: number, r: number) => { arcs.push([x, y, r]) })

  const ctx = new Proxy(store, {
    get(target, prop) {
      const key = String(prop)
      if (!(key in target)) target[key] = vi.fn()
      return target[key]
    },
    set(target, prop, value) {
      target[String(prop)] = value
      return true
    },
  }) as unknown as CanvasRenderingContext2D

  return { ctx, fills, texts, arcs, radials }
}

/* ── Harness state ── */

/** Every context handed out this test, in creation order. */
let recorders: Recorder[] = []
/** Frame callbacks queued by a scene loop, keyed by the id rAF handed back. */
let queuedFrames = new Map<number, FrameRequestCallback>()
let frameSeq = 0
/** Value `Math.random()` returns — tests set it before mounting. */
let rand = 0.25
/** Values `Math.random()` hands out first, before falling back to `rand`. */
let randQueue: number[] = []
/** Value `Date.now()` returns — tests advance it to age a speech bubble. */
let nowMs = 1_700_000_000_000

beforeEach(() => {
  recorders = []
  queuedFrames = new Map()
  frameSeq = 0
  rand = 0.25
  randQueue = []
  nowMs = 1_700_000_000_000
  HTMLCanvasElement.prototype.getContext = vi.fn(() => {
    const rec = createRecorder()
    recorders.push(rec)
    return rec.ctx
  }) as unknown as HTMLCanvasElement['getContext']
  vi.spyOn(Math, 'random').mockImplementation(() => randQueue.length ? randQueue.shift()! : rand)
  vi.spyOn(Date, 'now').mockImplementation(() => nowMs)
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
    const id = ++frameSeq
    queuedFrames.set(id, cb)
    return id
  })
  vi.stubGlobal('cancelAnimationFrame', (id: number) => { queuedFrames.delete(id) })
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  localStorage.clear()
})

/** Advance every live scene loop by exactly `n` frames. */
function runFrames(n: number) {
  act(() => {
    for (let i = 0; i < n; i++) {
      const due = [...queuedFrames.values()]
      queuedFrames.clear()
      due.forEach(cb => cb(0))
    }
  })
}

function agent(over: Partial<AgentSource> & { id: string }): AgentSource {
  return {
    name: over.id, label: 'default', kind: 'slot',
    running: false, detail: '', ...over,
  }
}

function mount(agents: AgentSource[], visible = true) {
  const store = configureStore({ reducer: { chat: chatReducer } })
  const base = recorders.length
  const view = render(
    <Provider store={store}>
      <MemoryRouter><GhostScene agents={agents} visible={visible} /></MemoryRouter>
    </Provider>,
  )
  const rerender = (next: AgentSource[], nextVisible = visible) => view.rerender(
    <Provider store={store}>
      <MemoryRouter><GhostScene agents={next} visible={nextVisible} /></MemoryRouter>
    </Provider>,
  )
  // initSceneCanvases takes the pixel context first, then the text overlay.
  return { ...view, rerender, pixel: recorders[base], overlay: recorders[base + 1] }
}

/** Text drawn on the overlay, in draw order. */
const labels = (rec: Recorder) => rec.texts.map(t => t.text)
/** Distinct fill colors used on the pixel canvas. */
const colors = (rec: Recorder) => new Set(rec.fills.map(f => f.color))
const countColor = (rec: Recorder, color: string) => rec.fills.filter(f => f.color === color).length

const EYE = '#14141e'
const BODY = '#e8ecf4'

/** Eight agents, one per anchor spot — enough to reach every outfit. */
const fullHaunt: AgentSource[] = Array.from({ length: 8 }, (_, i) =>
  agent({ id: `slot-full-${i}`, name: `Ghost${i}`, running: i % 2 === 0, detail: `${i} msgs` }),
)

/* ── Tests ── */

describe('GhostScene mounting', () => {
  it('renders the pixel canvas and the text overlay with accessible labels', () => {
    const view = mount([])
    expect(view.getByLabelText('Kiro ghost haunt animation')).toBeInTheDocument()
    expect(view.getByLabelText('Kiro ghost haunt text overlay')).toBeInTheDocument()
  })

  it('sizes the pixel canvas and keeps it unsmoothed so the sprites stay crisp', () => {
    const view = mount([])
    const canvas = view.getByLabelText('Kiro ghost haunt animation') as HTMLCanvasElement
    expect(canvas.style.imageRendering).toBe('pixelated')
    expect(canvas.width).toBeGreaterThan(0)
  })

  it('offers the in-world new-session control because live sources are wired', () => {
    const view = mount([])
    expect(view.getByRole('button', { name: /new session/i })).toBeInTheDocument()
  })
})

describe('GhostScene background', () => {
  it('paints the sky, the moon and the scene titles on the very first frame', () => {
    const { pixel, overlay } = mount([])
    expect(labels(overlay)).toContain('Kiro Haunt')
    expect(labels(overlay)).toContain('friendly hauntings only')
    // Moon disc plus its three craters, and the halo behind it.
    expect(pixel.arcs.length).toBeGreaterThanOrEqual(4)
    expect(pixel.radials.count).toBeGreaterThan(0)
    // Stars and fireflies both light up on frame one.
    expect(colors(pixel)).toContain('#dbe2f7')
    expect(colors(pixel)).toContain('#ffe08a')
  })

  it('reports an empty haunt in the counter', () => {
    const { overlay } = mount([])
    expect(labels(overlay)).toContain('kiro haunt · 0 ghosts')
  })

  it('draws nothing while hidden and starts drawing once it becomes visible', () => {
    const { overlay, rerender } = mount([agent({ id: 'slot-hidden' })], false)
    runFrames(5)
    expect(overlay.texts).toHaveLength(0)

    rerender([agent({ id: 'slot-hidden' })], true)
    runFrames(1)
    expect(labels(overlay)).toContain('Kiro Haunt')
  })

  it('streaks a shooting star in from the left on the 140th frame', () => {
    const view = mount([])
    runFrames(138)   // mount drew tick 1; this covers ticks 2..139
    view.pixel.fills.length = 0
    runFrames(1)     // tick 140 — the spawn tick
    const trail = view.pixel.fills.filter(f => f.color.startsWith('rgba(255,240,200'))
    expect(trail.length).toBeGreaterThan(0)
    // Entered off the left edge and is still travelling in.
    expect(trail.some(f => f.x < 0)).toBe(true)
  })

  it('streaks the star in from the right when the coin flip goes the other way', () => {
    const view = mount([])
    const canvas = view.getByLabelText('Kiro ghost haunt animation') as HTMLCanvasElement
    runFrames(138)
    view.pixel.fills.length = 0
    // First draw decides "spawn?", the second decides "from the left?".
    randQueue = [0.4, 0.9]
    runFrames(1)
    const trail = view.pixel.fills.filter(f => f.color.startsWith('rgba(255,240,200'))
    expect(trail.length).toBeGreaterThan(0)
    expect(trail.some(f => f.x > canvas.width)).toBe(true)
  })
})

describe('GhostScene agent to ghost sync', () => {
  it('draws one ghost per agent and counts them', () => {
    const { pixel, overlay } = mount([
      agent({ id: 'slot-a', name: 'Alpha', running: true, detail: '3 msgs' }),
      agent({ id: 'slot-b', name: 'Beta' }),
      agent({ id: 'cron-c', name: 'Gamma', kind: 'cron', detail: 'every 5m' }),
    ])
    expect(labels(overlay)).toContain('kiro haunt · 3 ghosts')
    expect(labels(overlay)).toEqual(expect.arrayContaining(['Alpha', 'Beta', 'Gamma']))
    expect(colors(pixel)).toContain(BODY)
  })

  it('caps the haunt at the eight anchor spots', () => {
    const many = Array.from({ length: 12 }, (_, i) => agent({ id: `slot-many-${i}` }))
    const { overlay } = mount(many)
    expect(labels(overlay)).toContain('kiro haunt · 8 ghosts')
  })

  it('labels a running agent as haunting and an idle one as idle', () => {
    const { overlay } = mount([
      agent({ id: 'slot-run', name: 'Runner', running: true }),
      agent({ id: 'slot-idle', name: 'Sitter', running: false }),
    ])
    expect(labels(overlay)).toContain('haunting')
    expect(labels(overlay)).toContain('idle')
  })

  it('shows an agent detail line but writes nothing when the detail is blank', () => {
    const withDetail = mount([agent({ id: 'slot-d1', name: 'Withd', detail: '7 msgs' })])
    expect(labels(withDetail.overlay)).toContain('7 msgs')
    withDetail.unmount()

    const bare = mount([agent({ id: 'slot-d2', name: 'Bare' })])
    expect(labels(bare.overlay).filter(t => t === '')).toHaveLength(0)
  })

  it('wraps a long agent name onto several lines', () => {
    const name = 'Extremely Long Ghost Name That Wraps Across Several Lines'
    const { overlay } = mount([agent({ id: 'slot-long', name, detail: 'busy' })])
    const drawn = labels(overlay).filter(t => t !== '' && name.includes(t))
    expect(drawn.length).toBeGreaterThan(1)
    expect(labels(overlay)).not.toContain(name)
  })

  it('reuses a ghost when the agent list reorders, and slides it to the new spot', () => {
    const a = agent({ id: 'slot-move-a', name: 'Mover' })
    const b = agent({ id: 'slot-move-b', name: 'Shifter' })
    const { overlay, rerender } = mount([a, b])
    runFrames(30)

    rerender([b, a])
    overlay.texts.length = 0
    runFrames(30)
    // Both ghosts survived the reorder — no re-entrance, no duplicates.
    expect(labels(overlay)).toContain('kiro haunt · 2 ghosts')
    expect(labels(overlay)).toEqual(expect.arrayContaining(['Mover', 'Shifter']))
  })

  it('picks up an agent rename and running-state change in place', () => {
    const { overlay, rerender } = mount([agent({ id: 'slot-rn', name: 'Before' })])
    expect(labels(overlay)).toContain('idle')

    rerender([agent({ id: 'slot-rn', name: 'After', running: true, detail: 'live' })])
    overlay.texts.length = 0
    runFrames(1)
    expect(labels(overlay)).toContain('After')
    expect(labels(overlay)).toContain('haunting')
    expect(labels(overlay)).not.toContain('Before')
  })

  it('removes ghosts for agents that disappear', () => {
    const keep = agent({ id: 'slot-keep', name: 'Keeper' })
    const { overlay, rerender } = mount([keep, agent({ id: 'slot-gone', name: 'Goner' })])
    rerender([keep])
    overlay.texts.length = 0
    runFrames(1)
    expect(labels(overlay)).toContain('kiro haunt · 1 ghosts')
    expect(labels(overlay)).not.toContain('Goner')
  })

  it('floats a first-time ghost up from below and places a known one at its anchor', () => {
    const nameOf = (rec: Recorder, name: string) => rec.texts.find(t => t.text === name)!

    const first = mount([agent({ id: 'slot-entrance', name: 'Newcomer' })])
    const enteringY = nameOf(first.overlay, 'Newcomer').y
    first.unmount()

    // Second mount: the scene cache now knows this id, so no entrance animation.
    const again = mount([agent({ id: 'slot-entrance', name: 'Newcomer' })])
    const anchoredY = nameOf(again.overlay, 'Newcomer').y
    expect(enteringY).toBeGreaterThan(anchoredY)
    again.unmount()

    // ...and an unknown ghost climbs toward its anchor as frames pass.
    const third = mount([agent({ id: 'slot-entrance-2', name: 'Riser' })])
    const startY = nameOf(third.overlay, 'Riser').y
    third.overlay.texts.length = 0
    runFrames(20)
    expect(nameOf(third.overlay, 'Riser').y).toBeLessThan(startY)
  })
})

describe('GhostScene sprite detail', () => {
  it('gives all eight ghosts a distinct hat, glasses or cape', () => {
    const { pixel } = mount(fullHaunt)
    const used = colors(pixel)
    for (const color of [
      '#3a3a4a',  // round glasses
      '#2d1b4e',  // witch hat
      '#c0392b',  // red cape
      '#181820',  // top hat
      '#111',     // shades
      '#8fa8ff',  // lens glint
      '#27408b',  // blue cape
      '#16a085',  // beanie
      '#f39c12',  // party hat
      '#f1c40f',  // crown
      '#5b2c6f',  // purple cape
    ]) {
      expect(used, `expected ${color} to be drawn`).toContain(color)
    }
  })

  it('flips a caped ghost\u2019s cape to the other side when it turns around', () => {
    const trio = [
      agent({ id: 'slot-cape-a', name: 'CapeA' }),
      agent({ id: 'slot-cape-b', name: 'CapeB' }),
      agent({ id: 'slot-cape-c', name: 'Caped' }),   // third outfit wears the red cape
    ]
    const { pixel, rerender } = mount(trio)
    /**
     * The cape is drawn back panel first, collar third. Which side of the collar
     * the panel lands on IS which way the ghost is facing — and comparing the two
     * needs no knowledge of the scene scale or where the ghost currently is.
     */
    const panelVsCollar = (rec: Recorder) => {
      const cape = rec.fills.filter(f => f.color === '#c0392b')
      return Math.sign(cape[0].x - cape[2].x)
    }
    expect(panelVsCollar(pixel)).toBe(-1)   // cape trails to the left, facing right

    // Reordering sends the caped ghost to the leftmost anchor, so it turns around.
    rerender([trio[2], trio[0], trio[1]])
    pixel.fills.length = 0
    runFrames(3)
    expect(panelVsCollar(pixel)).toBe(1)
  })

  it('blushes only the ghosts that are running', () => {
    const busy = mount([agent({ id: 'slot-blush', name: 'Busy', running: true })])
    expect(colors(busy.pixel)).toContain('#ff8899')
    busy.unmount()

    const resting = mount([agent({ id: 'slot-noblush', name: 'Resting', running: false })])
    expect(colors(resting.pixel)).not.toContain('#ff8899')
  })

  it('shuts a ghost\u2019s eyes when the blink timer runs down, then rearms it', () => {
    rand = 0  // blinkTimer starts at exactly 120 frames
    const { pixel } = mount([agent({ id: 'slot-blink', name: 'Blinker' })])
    runFrames(113)             // through tick 114 — timer at 6, eyes still open
    pixel.fills.length = 0
    runFrames(1)               // tick 115 — timer at 5, eyes shut
    expect(countColor(pixel, EYE)).toBe(2)

    runFrames(5)               // tick 120 — timer hits 0 and rearms to 140
    pixel.fills.length = 0
    runFrames(1)
    expect(countColor(pixel, EYE)).toBe(6)
  })

  it('draws a spectral glow under a running ghost only', () => {
    const busy = mount([agent({ id: 'slot-glow', name: 'Glower', running: true })])
    const radialOnBusy = busy.pixel.radials.count
    busy.unmount()

    const resting = mount([agent({ id: 'slot-noglow', name: 'Dim', running: false })])
    // Both frames draw the moon glow; only the running ghost adds its own aura.
    expect(radialOnBusy).toBeGreaterThan(resting.pixel.radials.count)
  })
})

describe('GhostScene speech bubbles', () => {
  const withMessage = (text: string) =>
    agent({ id: 'slot-msg', name: 'Talker', running: true, lastMessage: text })

  it('stays quiet for a message that was already there when the ghost appeared', () => {
    const { overlay } = mount([withMessage('old news')])
    expect(labels(overlay)).not.toContain('old news')
  })

  it('pops a bubble when the session\u2019s latest message changes', () => {
    const { overlay, rerender } = mount([withMessage('old news')])
    rerender([withMessage('shipping it')])
    overlay.texts.length = 0
    runFrames(1)
    expect(labels(overlay)).toContain('shipping it')
  })

  it('fades the bubble through its final second and drops it after seven', () => {
    const { overlay, rerender } = mount([withMessage('first')])
    rerender([withMessage('fresh')])

    nowMs += 6_500                       // inside the fade-out window
    overlay.texts.length = 0
    runFrames(1)
    expect(labels(overlay)).toContain('fresh')

    nowMs += 1_000                       // past SPEECH_BUBBLE_MS
    overlay.texts.length = 0
    runFrames(1)
    expect(labels(overlay)).not.toContain('fresh')
  })

  it('clears the bubble when the message is emptied', () => {
    const { overlay, rerender } = mount([withMessage('something')])
    rerender([withMessage('')])
    overlay.texts.length = 0
    runFrames(1)
    expect(labels(overlay)).not.toContain('something')
  })
})

describe('GhostScene teardown', () => {
  it('stops its loop on unmount so no further frames are drawn', () => {
    const { overlay, unmount } = mount([agent({ id: 'slot-tear', name: 'Ender' })])
    runFrames(2)
    unmount()
    overlay.texts.length = 0
    runFrames(3)
    expect(overlay.texts).toHaveLength(0)
  })
})
