/**
 * PandaOfficeScene — the Worlds panda den — driven frame by frame.
 *
 * `Scenes.test.tsx` only proves the scene mounts a canvas, so everything the
 * den actually DOES after mount (the doorway glow while a panda walks in, the
 * blinking monitors, the steaming mug, the bamboo-coffee run, the whiteboard
 * visit, the pair-programming huddle and its chat lines) never ran. happy-dom
 * has no 2D context and no frame clock, so the harness supplies both:
 *
 *   - a RECORDING canvas context, so a test can ask "was the doorway glow
 *     painted?" or "which labels landed on the overlay?" instead of reading
 *     pixels;
 *   - a hand-driven `requestAnimationFrame`, so a test advances exactly N
 *     frames with no dependence on a real animation clock;
 *   - a pinned `Math.random` and `Date.now`, so the huddle, coffee and
 *     whiteboard dice rolls and the speech-bubble expiry are deterministic.
 *
 * Timers are FAKE but deliberately NOT auto-advancing: the scene arms 3s-4.5s
 * `window.setTimeout`s for its breaks, and fast-forwarding ~1800 frames burns
 * more than that in real time, so `shouldAdvanceTime` would let a break expire
 * mid-walk and make these assertions flaky. Every timer is therefore stepped
 * explicitly and the queue is dropped in `afterEach`, so no scene timer can
 * survive teardown and fail the run with an unhandled callback.
 *
 * Recording is switched off while fast-forwarding thousands of frames (the
 * huddle only rolls at tick 1800) and switched back on for the single frame
 * under assertion, so memory stays flat.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, act, fireEvent } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer from '../store/chatSlice'
import PandaOfficeScene from '../pages/scenes/PandaOfficeScene'
import { markAgentsKnown } from '../hooks/sceneStateCache'
import type { AgentSource } from '../hooks/useAgentSync'

/** The popover and the in-world "New session" sign are the only api callers
 *  reachable from this scene; stub them so nothing dials the network. */
vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      chatSlotDetail: vi.fn(() => Promise.resolve({ messages: [] })),
      createChatSlot: vi.fn(() => Promise.resolve({})),
    },
  }
})

/* ── Scene geometry mirrored from PandaOfficeScene (it exports none of it) ── */
/** The scene hardcodes S = 3 rather than reading SCENE_SCALE. */
const S = 3
const W = 440, H = 300
/** Desk anchors; a panda sits at (x + 10, y + 20). */
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
  /** How many dashed-line runs the huddle link asked for. */
  dashes: { count: number }
}

/**
 * A 2D context that remembers what it was told to draw. Unknown members become
 * no-op spies on first access, so a helper reaching for `ellipse`, `roundRect`
 * or `setLineDash` never has to be enumerated here.
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
  vi.useFakeTimers()
  HTMLCanvasElement.prototype.getContext = vi.fn(() => {
    const rec = createRecorder()
    recorders.push(rec)
    return rec.ctx
  }) as unknown as HTMLCanvasElement['getContext']
  // happy-dom lays nothing out, so the hit-test would divide by a zero-width
  // rect. Give the canvas its real on-screen box instead.
  vi.spyOn(HTMLCanvasElement.prototype, 'getBoundingClientRect').mockReturnValue({
    left: 0, top: 0, right: W * S, bottom: H * S, width: W * S, height: H * S,
    x: 0, y: 0, toJSON: () => ({}),
  } as DOMRect)
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
  vi.clearAllTimers()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  vi.useRealTimers()
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

/** Fire the scene's break timers that come due within `ms`. */
function advanceTimers(ms: number) {
  act(() => { vi.advanceTimersByTime(ms) })
}

/** Drop everything recorded so far, so the next frame stands alone. */
function clearRecords() {
  recorders.forEach(r => { r.fills.length = 0; r.texts.length = 0; r.dashes.count = 0 })
}

/**
 * Step the loop in bursts, recording only the last frame of each burst, until
 * `pred` holds. Frame-exact expectations would be brittle — a panda's walk
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
      <MemoryRouter><PandaOfficeScene agents={agents} visible={visible} /></MemoryRouter>
    </Provider>,
  )
  const rerender = (next: AgentSource[], nextVisible = visible) => view.rerender(
    <Provider store={store}>
      <MemoryRouter><PandaOfficeScene agents={next} visible={nextVisible} /></MemoryRouter>
    </Provider>,
  )
  // initSceneCanvases takes the pixel context first, then the text overlay.
  return { ...view, rerender, pixel: recorders[base], overlay: recorders[base + 1] }
}

/** Text drawn on a context, in draw order. */
const labels = (rec: Recorder) => rec.texts.map(t => t.text)
const hasColor = (rec: Recorder, color: string) => rec.fills.some(f => f.color === color)
/** Where a given label landed, or undefined when it was not drawn. */
const textAt = (rec: Recorder, text: string) => rec.texts.find(t => t.text === text)
/** Whether a pixel of exactly this colour and size landed on this spot. */
const fillAt = (rec: Recorder, color: string, x: number, y: number, w: number, h: number) =>
  rec.fills.some(f => f.color === color && f.x === x && f.y === y && f.w === w && f.h === h)

/**
 * Whether the cubicle nameplate for `name` is hanging on desk `deskIdx`.
 * Matched by position, because the whiteboard prints the same 8-character
 * clipping of a running panda's name on its kanban cards.
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
const PANDA_BLACK = '#222'
const BAMBOO = '#6b8e23'
const DEN_SIGN = '🐼 Panda Den'

/** Names are 9 characters so the full label never collides with the
 *  8-character cubicle nameplate the scene derives from it. */
const ROSALINDA = 'Rosalinda'
const FERDINAND = 'Ferdinand'
const BERNADETT = 'Bernadett'
const CONSTANZA = 'Constanza'

/** Four pandas the den has already met, so they start seated at desks 0-3. */
function seatedCrew(prefix: string, running = true): AgentSource[] {
  const names = [ROSALINDA, FERDINAND, BERNADETT, CONSTANZA]
  const sources = names.map((name, i) => agent({ id: `slot-${prefix}-${i}`, name, running, detail: '3 msgs' }))
  markAgentsKnown('panda-office', sources.map(s => s.id))
  return sources
}

/* ── Tests ── */

describe('PandaOfficeScene fittings', () => {
  it('renders the pixel canvas and the text overlay with accessible labels', () => {
    const view = mount([])
    expect(view.getByLabelText('Panda office scene')).toBeInTheDocument()
    expect(view.getByLabelText('Panda office scene labels')).toBeInTheDocument()
  })

  it('keeps the pixel canvas unsmoothed and sized to the scene', () => {
    const view = mount([])
    const canvas = view.getByLabelText('Panda office scene') as HTMLCanvasElement
    expect(canvas.style.imageRendering).toBe('pixelated')
    expect(canvas.width).toBe(W * S)
    expect(canvas.height).toBe(H * S)
  })

  it('paints the den signage, the kanban columns and the gym on the first frame', () => {
    const { overlay } = mount([])
    expect(labels(overlay)).toContain(DEN_SIGN)
    expect(labels(overlay)).toEqual(expect.arrayContaining(['To Do', 'Active', 'Done']))
    expect(labels(overlay)).toContain('pull-ups')
    // The doorway sign is painted on the overlay too.
    expect(labels(overlay)).toContain('IN')
    // Nothing is running, so the board shows the placeholder card.
    expect(labels(overlay)).toContain('No tasks')
  })

  it('counts an empty den and marks all eight cubicles vacant', () => {
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
    expect(labels(overlay)).toContain(DEN_SIGN)
  })

  it('lists the running pandas on the whiteboard instead of the placeholder', () => {
    const { overlay } = mount(seatedCrew('board'))
    expect(labels(overlay)).not.toContain('No tasks')
    // Cards are clipped to eight characters.
    expect(labels(overlay)).toContain(ROSALINDA.slice(0, 8))
    expect(labels(overlay)).toContain(CONSTANZA.slice(0, 8))
  })
})

describe('PandaOfficeScene animated fittings', () => {
  it('flashes the clock second dot on the alternating 16-frame phase', () => {
    const { pixel } = mount([])
    clearRecords()
    runFrames(1)               // tick 2 — dot off
    expect(hasColor(pixel, CLOCK_HAND_DOT)).toBe(false)

    fastForward(15)
    clearRecords()
    runFrames(1)               // tick 18 — dot on
    expect(fillAt(pixel, CLOCK_HAND_DOT, (CLOCK.x - 0.5) * S, (CLOCK.y - 0.5) * S, S, S)).toBe(true)
  })

  it('blinks a cursor on an occupied monitor', () => {
    const { pixel } = mount(seatedCrew('cursor'))
    fastForward(7)
    clearRecords()
    runFrames(1)               // tick 9 — cursor phase on
    // The typed lines are 0.8 tall; the cursor is the square one.
    const cursor = pixel.fills.filter(f => f.color === SCREEN_GREEN && f.h === 1 * S)
    expect(cursor.length).toBeGreaterThan(0)
    expect(cursor[0].x).toBe((DESKS[0].x + 13) * S)
  })

  it('shows a standby dot on a vacant monitor and drips the coffee machine', () => {
    const { pixel } = mount([agent({ id: 'slot-standby' })])
    fastForward(31)
    clearRecords()
    runFrames(1)               // tick 33 — both 32-frame phases on
    const vacant = DESKS[7]
    expect(fillAt(pixel, '#333', (vacant.x + 14) * S, (vacant.y + 9) * S, S, S)).toBe(true)
    expect(pixel.fills.some(f => f.x === (COFFEE.x + 5) * S && f.y === (COFFEE.y + 8) * S)).toBe(true)
  })

  it('steams the mug on an occupied desk only', () => {
    const { pixel } = mount(seatedCrew('steam'))
    clearRecords()
    runFrames(1)               // tick 2 — steam phase off
    expect(hasColor(pixel, MUG_STEAM)).toBe(false)

    fastForward(15)
    clearRecords()
    runFrames(1)               // tick 18 — steam phase on
    expect(hasColor(pixel, MUG_STEAM)).toBe(true)
  })

  it('drifts pollen through the den once the spawn tick comes round', () => {
    const { pixel } = mount([])
    const pollen = (r: Recorder) => r.fills.filter(f => f.color.startsWith('rgba(255,240,200'))
    clearRecords()
    runFrames(1)               // tick 2 — nothing airborne yet
    expect(pollen(pixel)).toHaveLength(0)

    fastForward(10)
    clearRecords()
    runFrames(1)               // past ticks 6 and 12 — motes alive and drifting
    expect(pollen(pixel).length).toBeGreaterThan(0)
  })

  it('opens the pandas eyes after the mount-frame blink', () => {
    const { pixel } = mount(seatedCrew('blink'))
    const seat = { x: DESKS[0].x + 10, y: DESKS[0].y + 20 }
    // Eye whites sit inside the black patches, offset by the facing direction.
    const leftEye = () => fillAt(pixel, '#fff', (seat.x + 2.5) * S, (seat.y - 4) * S, S, S)

    clearRecords()
    runFrames(1)               // tick 2 — still inside the 3-frame blink window
    expect(leftEye()).toBe(false)

    fastForward(2)
    clearRecords()
    runFrames(1)               // tick 5 — eyes open
    expect(leftEye()).toBe(true)
  })
})

describe('PandaOfficeScene arrivals', () => {
  it('walks an unseen panda in through the doorway, which glows on the flashing frame', () => {
    const { pixel, overlay } = mount([agent({ id: 'slot-newcomer', name: ROSALINDA })])
    fastForward(7)
    clearRecords()
    runFrames(1)               // tick 9 — glow phase on while still entering
    expect(hasColor(pixel, DOOR_GLOW)).toBe(true)
    // Still short of the desk, so no cubicle nameplate yet.
    expect(hasNameplate(overlay, 0, ROSALINDA)).toBe(false)
    // The walker is still over by the door, not at the desk.
    const name = textAt(overlay, ROSALINDA)
    expect(name).toBeDefined()
    expect(name!.x).toBeLessThan(DESKS[0].x * S)
  })

  it('hangs the cubicle nameplate once the newcomer reaches its desk', () => {
    const { overlay, pixel } = mount([agent({ id: 'slot-settler', name: FERDINAND })])
    const seated = stepUntil(() => hasNameplate(overlay, 0, FERDINAND), 400)
    expect(seated).toBe(true)
    // It walked the whole way over: the label now sits on desk 0.
    expect(textAt(overlay, FERDINAND)!.x).toBe((DESKS[0].x + 14) * S)
    // Arrival ends the entrance, so the doorway stops glowing.
    fastForward(7)
    clearRecords()
    runFrames(1)
    expect(hasColor(pixel, DOOR_GLOW)).toBe(false)
  })

  it('seats an already-known panda at once, typing, with its nameplate up', () => {
    const known = agent({ id: 'slot-returning', name: BERNADETT, running: true, detail: '7 msgs' })
    markAgentsKnown('panda-office', [known.id])
    const { overlay, pixel } = mount([known])
    fastForward(7)
    clearRecords()
    runFrames(1)               // tick 9 — the typing arm phase that idles never draws
    expect(hasNameplate(overlay, 0, BERNADETT)).toBe(true)
    expect(labels(overlay)).toContain('active')
    expect(labels(overlay)).toContain('7 msgs')
    // Seated at desk 0, so the name label sits over that desk.
    expect(textAt(overlay, BERNADETT)!.x).toBe((DESKS[0].x + 14) * S)
    // A typing arm is raised one pixel above where a resting arm hangs.
    const seat = { x: DESKS[0].x + 10, y: DESKS[0].y + 20 }
    expect(fillAt(pixel, PANDA_BLACK, (seat.x - 1) * S, (seat.y + 2) * S, 2 * S, 3 * S)).toBe(true)
  })

  it('caps the den at its eight desks', () => {
    const many = Array.from({ length: 12 }, (_, i) => agent({ id: `slot-crowd-${i}`, running: true }))
    const { overlay } = mount(many)
    expect(labels(overlay)).toContain('8/8 agents')
    expect(labels(overlay).filter(t => t === 'empty')).toHaveLength(0)
  })

  it('badges each panda by kind: chat, cron and subagent', () => {
    const { pixel } = mount([
      agent({ id: 'slot-badge', name: ROSALINDA }),
      agent({ id: 'cron-badge', name: FERDINAND, kind: 'cron' }),
      agent({ id: 'spawn-badge', name: BERNADETT, kind: 'spawn' }),
    ])
    // Badges are drawn on the pixel canvas, not the text overlay.
    expect(labels(pixel)).toEqual(expect.arrayContaining(['💬', '⏰', '🔀']))
  })
})

describe('PandaOfficeScene live session updates', () => {
  it('reuses a seated panda when its session is renamed and stops running', () => {
    const before = agent({ id: 'slot-rename', name: ROSALINDA, running: true, detail: '3 msgs' })
    markAgentsKnown('panda-office', [before.id])
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
    // Idle now, so the board falls back to the placeholder card.
    expect(labels(overlay)).toContain('No tasks')
  })

  it('raises a speech bubble for a new last message, fades it, then drops it', () => {
    const base = agent({ id: 'slot-talker', name: CONSTANZA, running: true })
    markAgentsKnown('panda-office', [base.id])
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
    markAgentsKnown('panda-office', [base.id])
    const { overlay, rerender } = mount([base])
    nowMs += 20_000
    rerender([{ ...base }])
    clearRecords()
    runFrames(1)
    expect(labels(overlay).join(' ')).not.toContain('same text')
  })
})

describe('PandaOfficeScene daily routine', () => {
  it('sends one panda for bamboo and another to the whiteboard, then walks them back', () => {
    const { overlay, pixel } = mount(seatedCrew('routine'))

    // The bamboo break rolls at tick 600; the walk across the den is ~525 frames.
    const atMachine = stepUntil(() => {
      const name = textAt(overlay, ROSALINDA)
      return !!name && name.x > 340 * S
    }, 1400, 25)
    expect(atMachine).toBe(true)
    // A panda on a break carries a bamboo stalk, not a mug: 1 wide, 5 tall.
    expect(pixel.fills.some(f => f.color === BAMBOO && f.w === 1 * S && f.h === 5 * S)).toBe(true)

    // Stop the dice rolling so the tick-1800 huddle cannot hijack the walkers.
    rand = 0.9
    // The whiteboard visit rolled at tick 900 and parks a panda up by the board.
    const atBoard = stepUntil(() => {
      const name = textAt(overlay, FERDINAND)
      return !!name && name.y < 120 * S
    }, 900, 25)
    expect(atBoard).toBe(true)

    // Both breaks are time-boxed by real timeouts, so expiring them sends the
    // walkers home to their own cubicles.
    advanceTimers(4_000)
    const backAtDesk = stepUntil(() => {
      const name = textAt(overlay, ROSALINDA)
      return !!name && name.x < (DESKS[0].x + 40) * S
    }, 900, 25)
    expect(backAtDesk).toBe(true)
  })

  it('pairs two pandas mid-room, prints both chat lines, then breaks the huddle', () => {
    const { overlay, pixel } = mount(seatedCrew('huddle'))

    // Refuse every break roll on the way up so all four stay seated, then arm a
    // value that passes the huddle roll (< 0.3) and picks the second chat pair.
    rand = 0.9
    fastForward(1798)          // ticks 2 … 1799
    rand = 0.2
    clearRecords()
    runFrames(1)               // tick 1800 — the huddle rolls
    expect(labels(overlay)).toContain('CR approved!')
    // The partner has not answered yet, and the pair is linked by a dashed line.
    expect(labels(overlay)).not.toContain('Ship it! 🐼')
    expect(pixel.dashes.count).toBeGreaterThan(0)
    expect(hasColor(pixel, COLLAB_MARK)).toBe(true)

    // The reply lands 1.2s later.
    advanceTimers(1_300)
    clearRecords()
    runFrames(1)
    expect(labels(overlay)).toContain('Ship it! 🐼')

    // The huddle is capped at 4.5s, after which both go quiet.
    advanceTimers(3_300)
    clearRecords()
    runFrames(1)
    expect(labels(overlay)).not.toContain('CR approved!')
    expect(labels(overlay)).not.toContain('Ship it! 🐼')
    expect(hasColor(pixel, COLLAB_MARK)).toBe(false)
  })

  it('sends the surviving partner home when the other leaves mid-huddle', () => {
    const crew = seatedCrew('departed')
    const { overlay, rerender } = mount(crew)

    rand = 0.9
    fastForward(1798)
    rand = 0.2
    clearRecords()
    runFrames(1)               // tick 1800 — pandas 0 and 1 pair up
    expect(labels(overlay)).toContain('CR approved!')

    // The first partner's session ends while the two are still talking.
    rerender(crew.slice(1))
    advanceTimers(5_000)
    clearRecords()
    runFrames(1)

    // The huddle is torn down rather than steering a departed panda's desk.
    expect(labels(overlay)).not.toContain('CR approved!')
    expect(labels(overlay)).not.toContain(ROSALINDA)
    // Its cubicle is free again and the survivor keeps its own nameplate.
    expect(labels(overlay)).toContain('empty')
    expect(hasNameplate(overlay, 1, FERDINAND)).toBe(true)
  })
})

describe('PandaOfficeScene hover', () => {
  it('names the hovered panda and gives it the dens own status flavour', () => {
    const seated = agent({ id: 'slot-hovered', name: BERNADETT, running: true, detail: '3 msgs' })
    markAgentsKnown('panda-office', [seated.id])
    const view = mount([seated])
    const canvas = view.getByLabelText('Panda office scene')
    const seat = { x: DESKS[0].x + 10, y: DESKS[0].y + 20 }

    fireEvent.mouseMove(canvas, { clientX: seat.x * S, clientY: seat.y * S })
    expect(view.getByText(BERNADETT)).toBeInTheDocument()
    expect(view.getByText('Grinding PRs')).toBeInTheDocument()
    expect(view.getByText(/Working/)).toBeInTheDocument()

    fireEvent.mouseLeave(canvas)
    expect(view.queryByText('Grinding PRs')).toBeNull()
  })

  it('gives an idle panda the waiting-on-review flavour and ignores empty floor', () => {
    const seated = agent({ id: 'slot-idler', name: CONSTANZA, kind: 'cron', running: false, detail: 'hourly' })
    markAgentsKnown('panda-office', [seated.id])
    const view = mount([seated])
    const canvas = view.getByLabelText('Panda office scene')
    const seat = { x: DESKS[0].x + 10, y: DESKS[0].y + 20 }

    fireEvent.mouseMove(canvas, { clientX: seat.x * S, clientY: seat.y * S })
    expect(view.getByText('Waiting for CR approval')).toBeInTheDocument()

    // Far corner of the floor: nobody is standing there.
    fireEvent.mouseMove(canvas, { clientX: 5 * S, clientY: 290 * S })
    expect(view.queryByText('Waiting for CR approval')).toBeNull()
  })
})
