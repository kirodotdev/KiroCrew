/**
 * OfficeScene — the Worlds open-plan office — driven frame by frame.
 *
 * `Scenes.test.tsx` only proves the scene mounts a canvas, so everything the
 * office actually DOES after mount (the doorway glow while someone walks in,
 * the blinking monitors, the steaming mug, the coffee break, the whiteboard
 * visit, the pair-programming huddle and its chat bubbles) never ran. jsdom has
 * no 2D context and no frame clock, so the harness supplies both:
 *
 *   - a RECORDING canvas context, so a test can ask "was the doorway glow
 *     painted?" or "which labels landed on the overlay?" instead of reading
 *     pixels;
 *   - a hand-driven `requestAnimationFrame`, so a test advances exactly N
 *     frames with no dependence on a real animation clock;
 *   - a pinned `Math.random` and `Date.now`, so the collaboration, coffee and
 *     whiteboard dice rolls and the speech-bubble expiry are deterministic.
 *
 * Recording is switched off while fast-forwarding thousands of frames (the
 * office routine only starts at tick 600) and switched back on for the single
 * frame under assertion, so memory stays flat.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, act } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer from '../store/chatSlice'
import OfficeScene from '../pages/scenes/OfficeScene'
import { markAgentsKnown } from '../hooks/sceneStateCache'
import { SCENE_SCALE } from '../pages/scenes/config'
import type { AgentSource } from '../hooks/useAgentSync'

/* ── Scene geometry mirrored from OfficeScene (it exports none of it) ── */
const S = SCENE_SCALE
/** Desk anchors; an agent sits at (x + 10, y + 20). */
const DESKS = [
  { x: 40, y: 120 }, { x: 120, y: 120 }, { x: 200, y: 120 }, { x: 280, y: 120 },
  { x: 40, y: 200 }, { x: 120, y: 200 }, { x: 200, y: 200 }, { x: 280, y: 200 },
]
const COFFEE = { x: 390, y: 240 }
const CLOCK = { x: 170, y: 22 }

/* ── Recording 2D context ── */

interface FillRecord { x: number; y: number; w: number; h: number; color: string }
interface TextRecord { text: string; x: number; y: number }

interface Recorder {
  ctx: CanvasRenderingContext2D
  fills: FillRecord[]
  texts: TextRecord[]
  /** How many dashed-line runs the collaboration link asked for. */
  dashes: { count: number }
}

/**
 * A 2D context that remembers what it was told to draw. Unknown members become
 * no-op spies on first access, so a helper reaching for `ellipse` or
 * `setLineDash` never has to be enumerated here.
 */
function createRecorder(): Recorder {
  const fills: FillRecord[] = []
  const texts: TextRecord[] = []
  const dashes = { count: 0 }
  const gradient = { addColorStop: vi.fn() }

  const store: Record<string, unknown> = {
    fillStyle: '#000000',
    strokeStyle: '#000000',
    globalAlpha: 1,
    lineWidth: 1,
    font: '',
    textAlign: 'start',
    textBaseline: 'alphabetic',
    imageSmoothingEnabled: false,
    canvas: { width: 0, height: 0 },
    createLinearGradient: vi.fn(() => gradient),
    createRadialGradient: vi.fn(() => gradient),
    createPattern: vi.fn(() => null),
    // Width proportional to length so the real word-wrapper actually wraps.
    measureText: vi.fn((t: string) => ({ width: t.length * 8 })),
    getImageData: vi.fn(() => ({ data: new Uint8ClampedArray(4) })),
  }
  store.fillRect = (x: number, y: number, w: number, h: number) => {
    if (capturing) fills.push({ x, y, w, h, color: String(store.fillStyle) })
  }
  store.fillText = (text: string, x: number, y: number) => {
    if (capturing) texts.push({ text, x, y })
  }
  store.setLineDash = (pattern: number[]) => {
    if (capturing && pattern.length) dashes.count++
  }

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

  return { ctx, fills, texts, dashes }
}

/* ── Harness state ── */

/** Every context handed out this test, in creation order. */
let recorders: Recorder[] = []
/** Frame callbacks queued by the scene loop, keyed by the id rAF handed back. */
let queuedFrames = new Map<number, FrameRequestCallback>()
let frameSeq = 0
/** Value `Math.random()` returns — low enough to pass every routine dice roll. */
let rand = 0.05
/** Value `Date.now()` returns — tests advance it to age a speech bubble. */
let nowMs = 1_700_000_000_000
/** While false, the recording context drops everything (fast-forward mode). */
let capturing = true

beforeEach(() => {
  recorders = []
  queuedFrames = new Map()
  frameSeq = 0
  rand = 0.05
  nowMs = 1_700_000_000_000
  capturing = true
  HTMLCanvasElement.prototype.getContext = vi.fn(() => {
    const rec = createRecorder()
    recorders.push(rec)
    return rec.ctx
  }) as unknown as HTMLCanvasElement['getContext']
  vi.spyOn(Math, 'random').mockImplementation(() => rand)
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

/** Advance every live scene loop by exactly `n` frames, recording each one. */
function runFrames(n: number) {
  act(() => {
    for (let i = 0; i < n; i++) {
      const due = [...queuedFrames.values()]
      queuedFrames.clear()
      due.forEach(cb => cb(0))
    }
  })
}

/** Fast-forward `n` frames without recording anything they paint. */
function fastForward(n: number) {
  capturing = false
  runFrames(n)
  capturing = true
}

/** Drop everything recorded so far, so the next frame stands alone. */
function clearRecords() {
  recorders.forEach(r => { r.fills.length = 0; r.texts.length = 0; r.dashes.count = 0 })
}

/**
 * Step the loop in bursts, recording only the last frame of each burst, until
 * `pred` holds. Frame-exact expectations would be brittle — an agent's walk
 * length depends on which desk it started from — so poll instead of guessing.
 */
function stepUntil(pred: () => boolean, maxFrames: number, burst = 10): boolean {
  for (let elapsed = 0; elapsed < maxFrames; elapsed += burst) {
    fastForward(burst - 1)
    clearRecords()
    runFrames(1)
    if (pred()) return true
  }
  return false
}

function agent(over: Partial<AgentSource> & { id: string }): AgentSource {
  return { name: over.id, label: 'default', kind: 'slot', running: false, detail: '', ...over }
}

function mount(agents: AgentSource[], visible = true) {
  const store = configureStore({ reducer: { chat: chatReducer } })
  const base = recorders.length
  const view = render(
    <Provider store={store}>
      <MemoryRouter><OfficeScene agents={agents} visible={visible} /></MemoryRouter>
    </Provider>,
  )
  const rerender = (next: AgentSource[], nextVisible = visible) => view.rerender(
    <Provider store={store}>
      <MemoryRouter><OfficeScene agents={next} visible={nextVisible} /></MemoryRouter>
    </Provider>,
  )
  // initSceneCanvases takes the pixel context first, then the text overlay.
  return { ...view, rerender, pixel: recorders[base], overlay: recorders[base + 1] }
}

/** Text drawn on the overlay, in draw order. */
const labels = (rec: Recorder) => rec.texts.map(t => t.text)
const hasColor = (rec: Recorder, color: string) => rec.fills.some(f => f.color === color)
/** Where a given label landed, or undefined when it was not drawn. */
const textAt = (rec: Recorder, text: string) => rec.texts.find(t => t.text === text)

/**
 * Whether the cubicle nameplate for `name` is hanging on desk `deskIdx`.
 * Matched by position, because the whiteboard prints the same 8-character
 * clipping of a running agent's name on its kanban cards.
 */
function hasNameplate(rec: Recorder, deskIdx: number, name: string): boolean {
  const desk = DESKS[deskIdx]
  return rec.texts.some(t => t.text === name.slice(0, 8)
    && t.x === (desk.x + 15) * S && t.y === (desk.y + 1) * S)
}

const DOOR_GLOW = 'rgba(255,200,50,0.08)'
const MUG_STEAM = 'rgba(255,255,255,0.1)'
const SCREEN_GREEN = '#33ff33'
const CLOCK_HAND_DOT = '#f00'
const COLLAB_MARK = '#f90'

/** Names are 9 characters so the full label never collides with the
 *  8-character cubicle nameplate the scene derives from it. */
const ROSALINDA = 'Rosalinda'
const FERDINAND = 'Ferdinand'
const BERNADETT = 'Bernadett'
const CONSTANZA = 'Constanza'

/** Four agents already seen by the office, so they start seated. */
function seatedCrew(prefix: string, running = true): AgentSource[] {
  const names = [ROSALINDA, FERDINAND, BERNADETT, CONSTANZA]
  const sources = names.map((name, i) => agent({ id: `slot-${prefix}-${i}`, name, running }))
  markAgentsKnown('office', sources.map(s => s.id))
  return sources
}

/* ── Tests ── */

describe('OfficeScene furniture', () => {
  it('renders the pixel canvas and the text overlay with accessible labels', () => {
    const view = mount([])
    expect(view.getByLabelText('Office scene')).toBeInTheDocument()
    expect(view.getByLabelText('Office scene labels')).toBeInTheDocument()
  })

  it('keeps the pixel canvas unsmoothed so the sprites stay crisp', () => {
    const view = mount([])
    const canvas = view.getByLabelText('Office scene') as HTMLCanvasElement
    expect(canvas.style.imageRendering).toBe('pixelated')
    expect(canvas.width).toBeGreaterThan(0)
  })

  it('paints the signage and the kanban columns on the very first frame', () => {
    const { overlay } = mount([])
    expect(labels(overlay)).toContain('Agent Office')
    expect(labels(overlay)).toContain('headquarters')
    expect(labels(overlay)).toEqual(expect.arrayContaining(['To Do', 'Active', 'Done']))
    // Nothing is running, so the board shows the placeholder card.
    expect(labels(overlay)).toContain('No tasks')
  })

  it('counts an empty office and marks all eight cubicles vacant', () => {
    const { overlay } = mount([])
    expect(labels(overlay)).toContain('0/8 agents')
    expect(labels(overlay).filter(t => t === 'empty')).toHaveLength(8)
  })

  it('draws nothing while hidden and starts drawing once it becomes visible', () => {
    const { overlay, rerender } = mount([agent({ id: 'slot-hidden' })], false)
    runFrames(5)
    expect(overlay.texts).toHaveLength(0)

    rerender([agent({ id: 'slot-hidden' })], true)
    runFrames(1)
    expect(labels(overlay)).toContain('Agent Office')
  })

  it('lists the running agents on the whiteboard instead of the placeholder', () => {
    const { overlay } = mount(seatedCrew('board'))
    expect(labels(overlay)).not.toContain('No tasks')
    // Cards are clipped to eight characters.
    expect(labels(overlay)).toContain(ROSALINDA.slice(0, 8))
  })
})

describe('OfficeScene animated fittings', () => {
  it('flashes the clock second dot on the alternating 16-frame phase', () => {
    mount([])
    clearRecords()
    runFrames(1)               // tick 2 — dot off
    expect(hasColor(recorders[0], CLOCK_HAND_DOT)).toBe(false)

    fastForward(15)
    clearRecords()
    runFrames(1)               // tick 18 — dot on
    const dot = recorders[0].fills.find(f => f.color === CLOCK_HAND_DOT)
    expect(dot).toBeDefined()
    expect(dot!.x).toBeCloseTo((CLOCK.x - 0.5) * S)
  })

  it('blinks a cursor on an occupied monitor', () => {
    mount(seatedCrew('cursor'))
    fastForward(7)
    clearRecords()
    runFrames(1)               // tick 9 — cursor phase on
    // The typed lines are 0.8 tall; the cursor is the square one.
    const cursor = recorders[0].fills.filter(f => f.color === SCREEN_GREEN && f.h === 1 * S)
    expect(cursor.length).toBeGreaterThan(0)
  })

  it('shows a standby dot on a vacant monitor and drips the coffee machine', () => {
    mount([agent({ id: 'slot-standby' })])
    fastForward(31)
    clearRecords()
    runFrames(1)               // tick 33 — both 32-frame phases on
    const rec = recorders[0]
    const vacant = DESKS[7]
    expect(rec.fills.some(f => f.color === '#333'
      && f.x === (vacant.x + 14) * S && f.y === (vacant.y + 9) * S)).toBe(true)
    expect(rec.fills.some(f => f.x === (COFFEE.x + 5) * S && f.y === (COFFEE.y + 8) * S)).toBe(true)
  })

  it('steams the mug on an occupied desk only', () => {
    mount(seatedCrew('steam'))
    clearRecords()
    runFrames(1)               // tick 2 — steam phase off
    expect(hasColor(recorders[0], MUG_STEAM)).toBe(false)

    fastForward(15)
    clearRecords()
    runFrames(1)               // tick 18 — steam phase on
    expect(hasColor(recorders[0], MUG_STEAM)).toBe(true)
  })

  it('drifts dust motes through the room once the spawn tick comes round', () => {
    mount([])
    clearRecords()
    runFrames(1)               // tick 2 — no motes yet
    const dust = (r: Recorder) => r.fills.filter(f => f.color.startsWith('rgba(255,240,200'))
    expect(dust(recorders[0])).toHaveLength(0)

    fastForward(10)
    clearRecords()
    runFrames(1)               // past tick 6 and 12 — two motes alive and moving
    expect(dust(recorders[0]).length).toBeGreaterThan(0)
  })

  it('opens the agents eyes after the mount-frame blink', () => {
    mount(seatedCrew('blink'))
    clearRecords()
    runFrames(1)               // tick 2 — still inside the 3-frame blink window
    const closed = recorders[0].fills.filter(f => f.color === '#333' && f.h === 0.5 * S)
    expect(closed.length).toBeGreaterThan(0)

    fastForward(2)
    clearRecords()
    runFrames(1)               // tick 5 — eyes open, so square pupils
    const open = recorders[0].fills.filter(f => f.color === '#333' && f.h === 1 * S)
    expect(open.length).toBeGreaterThan(0)
  })
})

describe('OfficeScene arrivals', () => {
  it('walks an unseen agent in through the doorway, which glows on the flashing frame', () => {
    mount([agent({ id: 'slot-newcomer', name: ROSALINDA })])
    fastForward(7)
    clearRecords()
    runFrames(1)               // tick 9 — glow phase on while still entering
    expect(hasColor(recorders[0], DOOR_GLOW)).toBe(true)
    // Still short of the desk, so no cubicle nameplate yet.
    expect(hasNameplate(recorders[1], 0, ROSALINDA)).toBe(false)
    // The walker is still over by the door, not at the desk.
    const name = textAt(recorders[1], ROSALINDA)
    expect(name).toBeDefined()
    expect(name!.x).toBeLessThan(DESKS[0].x * S)
  })

  it('hangs the cubicle nameplate once the newcomer reaches its desk', () => {
    const { overlay, pixel } = mount([agent({ id: 'slot-settler', name: FERDINAND })])
    const seated = stepUntil(() => hasNameplate(overlay, 0, FERDINAND), 400)
    expect(seated).toBe(true)
    // It walked the whole way over: the label now sits on desk 0.
    expect(textAt(overlay, FERDINAND)!.x).toBeCloseTo((DESKS[0].x + 14) * S)
    // Arrival ends the entrance, so the doorway stops glowing.
    fastForward(7)
    clearRecords()
    runFrames(1)
    expect(hasColor(pixel, DOOR_GLOW)).toBe(false)
  })

  it('seats an already-known agent at once, typing, with its nameplate up', () => {
    const known = agent({ id: 'slot-returning', name: BERNADETT, running: true })
    markAgentsKnown('office', [known.id])
    const { overlay, pixel } = mount([known])
    fastForward(3)
    clearRecords()
    runFrames(1)
    expect(hasNameplate(overlay, 0, BERNADETT)).toBe(true)
    expect(labels(overlay)).toContain('active')
    // Seated at desk 0, so the name label sits over that desk.
    const name = textAt(overlay, BERNADETT)
    expect(name).toBeDefined()
    expect(name!.x).toBeCloseTo((DESKS[0].x + 14) * S)
    // Typing arms are painted in the agent's own colour beside the body.
    expect(pixel.fills.some(f => f.w === 1 * S && f.h === 3 * S)).toBe(true)
  })

  it('caps the office at its eight desks', () => {
    const many = Array.from({ length: 12 }, (_, i) => agent({ id: `slot-crowd-${i}`, running: true }))
    const { overlay } = mount(many)
    expect(labels(overlay)).toContain('8/8 agents')
    expect(labels(overlay).filter(t => t === 'empty')).toHaveLength(0)
  })

  it('badges each agent by kind: chat, cron and subagent', () => {
    const { pixel } = mount([
      agent({ id: 'slot-badge', name: ROSALINDA }),
      agent({ id: 'cron-badge', name: FERDINAND, kind: 'cron' }),
      agent({ id: 'spawn-badge', name: BERNADETT, kind: 'spawn' }),
    ])
    // Badges are drawn on the pixel canvas, not the text overlay.
    expect(labels(pixel)).toEqual(expect.arrayContaining(['💬', '⏰', '🔀']))
  })
})

describe('OfficeScene live session updates', () => {
  it('reuses a seated agent when its session is renamed and stops running', () => {
    const before = agent({ id: 'slot-rename', name: ROSALINDA, running: true, detail: '3 msgs' })
    markAgentsKnown('office', [before.id])
    const { overlay, rerender } = mount([before])
    clearRecords()
    runFrames(1)
    expect(labels(overlay)).toContain('active')

    rerender([agent({ id: 'slot-rename', name: FERDINAND, running: false, detail: '9 msgs' })])
    clearRecords()
    runFrames(1)
    expect(labels(overlay)).toContain(FERDINAND)
    expect(labels(overlay)).toContain('9 msgs')
    expect(labels(overlay)).toContain('idle')
    expect(labels(overlay)).not.toContain('active')
    // Reused, not re-entered: the desk still shows its nameplate.
    expect(hasNameplate(overlay, 0, FERDINAND)).toBe(true)
  })

  it('raises a speech bubble for a new last message, fades it, then drops it', () => {
    const base = agent({ id: 'slot-talker', name: CONSTANZA, running: true })
    markAgentsKnown('office', [base.id])
    const { overlay, rerender } = mount([base])

    rerender([{ ...base, lastMessage: 'Shipping the review lane' }])
    clearRecords()
    runFrames(1)
    expect(labels(overlay).join(' ')).toContain('Shipping')

    // 6.5s in: past the fade threshold, still on screen.
    nowMs += 6_500
    clearRecords()
    runFrames(1)
    expect(labels(overlay).join(' ')).toContain('Shipping')

    // Past the 7s lifetime the bubble is gone.
    nowMs += 1_000
    clearRecords()
    runFrames(1)
    expect(labels(overlay).join(' ')).not.toContain('Shipping')
  })

  it('leaves the bubble down when a rerender repeats the same message', () => {
    const base = agent({ id: 'slot-repeat', name: ROSALINDA, lastMessage: 'same text' })
    markAgentsKnown('office', [base.id])
    const { overlay, rerender } = mount([base])
    nowMs += 20_000
    rerender([{ ...base }])
    clearRecords()
    runFrames(1)
    expect(labels(overlay).join(' ')).not.toContain('same text')
  })

  it('toggles an agent between working and idle when its sprite is clicked', () => {
    const seated = agent({ id: 'cron-clickable', name: BERNADETT, kind: 'cron', running: true })
    markAgentsKnown('office', [seated.id])
    const view = mount([seated])
    const canvas = view.getByLabelText('Office scene') as HTMLCanvasElement

    clearRecords()
    runFrames(1)
    expect(labels(view.overlay)).toContain('active')

    const click = new MouseEvent('click', { bubbles: true })
    Object.defineProperty(click, 'offsetX', { value: (DESKS[0].x + 10) * S })
    Object.defineProperty(click, 'offsetY', { value: (DESKS[0].y + 20) * S })
    act(() => { canvas.dispatchEvent(click) })

    clearRecords()
    runFrames(1)
    expect(labels(view.overlay)).toContain('idle')
    expect(labels(view.overlay)).not.toContain('active')
  })

  it('ignores a click on empty floor', () => {
    const seated = agent({ id: 'cron-missed', name: CONSTANZA, kind: 'cron', running: true })
    markAgentsKnown('office', [seated.id])
    const view = mount([seated])
    const canvas = view.getByLabelText('Office scene') as HTMLCanvasElement

    const click = new MouseEvent('click', { bubbles: true })
    Object.defineProperty(click, 'offsetX', { value: 5 * S })
    Object.defineProperty(click, 'offsetY', { value: 290 * S })
    act(() => { canvas.dispatchEvent(click) })

    clearRecords()
    runFrames(1)
    expect(labels(view.overlay)).toContain('active')
  })
})

describe('OfficeScene daily routine', () => {
  it('sends an agent for coffee and to the whiteboard, then back to their desks', () => {
    const { overlay } = mount(seatedCrew('routine'))

    // The coffee break fires at tick 600; the walk is ~500 frames.
    const atMachine = stepUntil(() => {
      const name = textAt(overlay, ROSALINDA)
      return !!name && name.x > 340 * S
    }, 1400, 25)
    expect(atMachine).toBe(true)

    // The whiteboard visit fires at tick 900 and parks an agent up by the board.
    const atBoard = stepUntil(() => {
      const name = textAt(overlay, FERDINAND)
      return !!name && name.y < 120 * S
    }, 900, 25)
    expect(atBoard).toBe(true)

    // Both breaks are time-boxed, so the walker returns to its own cubicle.
    const backAtDesk = stepUntil(() => {
      const name = textAt(overlay, ROSALINDA)
      return !!name && name.x < (DESKS[0].x + 40) * S
    }, 1600, 25)
    expect(backAtDesk).toBe(true)
  })

  it('pairs two agents up mid-room, prints their chat lines, then breaks the huddle', () => {
    const { overlay, pixel } = mount(seatedCrew('huddle'))

    // The collaboration roll lands on tick 1800; the pair then walks to the rug
    // and starts talking 60 frames after the second one arrives.
    const talking = stepUntil(
      () => labels(overlay).includes('New audiobook?'),
      2600, 25,
    )
    expect(talking).toBe(true)
    // The reply of the pair and the dashed link between them come with it.
    expect(labels(overlay)).toContain('Adding to lib!')
    expect(pixel.dashes.count).toBeGreaterThan(0)
    expect(hasColor(pixel, COLLAB_MARK)).toBe(true)

    // The huddle is capped at 600 frames, after which the chat clears.
    const dispersed = stepUntil(
      () => !labels(overlay).includes('New audiobook?'),
      900, 25,
    )
    expect(dispersed).toBe(true)
  })
})
