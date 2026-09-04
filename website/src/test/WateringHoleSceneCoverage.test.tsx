/**
 * WateringHoleScene — the Worlds Serengeti savanna — driven frame by frame.
 *
 * `Scenes.test.tsx` only proves the scene mounts a canvas, so everything the
 * savanna actually DOES after mount (species selection by agent id, the home
 * spot reservation pass, the giraffe reaching into an acacia, the warthog
 * rooting in a dirt patch, the elephant drinking and ringing the hole with
 * ripples, the amble and the shade rest, and the return home once a session
 * stops running) never ran. happy-dom has no 2D context and no frame clock, so
 * the harness supplies both:
 *
 *   - a RECORDING canvas context, so a test can ask "was the drinking trunk
 *     painted?" or "which labels landed on the overlay?" instead of reading
 *     pixels. Strokes are recorded too, because the water ripples are the one
 *     thing this scene draws with `stroke()` rather than a filled rect;
 *   - a hand-driven `requestAnimationFrame`, so a test advances exactly N
 *     frames with no dependence on a real animation clock;
 *   - a pinned `Math.random` and `Date.now`, so the activity dice rolls and the
 *     speech-bubble expiry are deterministic.
 *
 * The scene itself arms no timers, so real timers are left alone; nothing here
 * opens the thread popover, which is the only interval `useSceneInteraction`
 * ever starts.
 *
 * Recording is switched off while fast-forwarding the hundreds of frames an
 * elephant needs to walk to the water, and switched back on for the single
 * frame under assertion, so memory stays flat.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, act, fireEvent } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer from '../store/chatSlice'
import WateringHoleScene from '../pages/scenes/WateringHoleScene'
import { markAgentsKnown } from '../hooks/sceneStateCache'
import { SCENE_SCALE } from '../pages/scenes/config'
import type { AgentSource } from '../hooks/useAgentSync'

/** The thread popover and the in-world "New session" sign are the only api
 *  callers reachable from this scene; stub them so nothing dials the network. */
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

/* ── Scene geometry mirrored from WateringHoleScene (it exports none of it) ── */
const S = SCENE_SCALE
const W = 480, H = 340
const HORIZON_Y = 130
const HOLE = { cx: 240, cy: 240, rx: 70, ry: 26 }
/** Home spots 0 and 1 — the only two this file ever needs to name. */
const HOME_0 = { x: 90, y: 200 }
const HOME_1 = { x: 175, y: 200 }
/** First foreground acacia; a browsing giraffe stands at (x - 12, y + 10). */
const TREE_0 = { x: 70, y: 120 }
/** Distant tree, which is where an elephant goes to rest in the shade. */
const TREE_2 = { x: 235, y: 95 }
/** First dirt patch, where a foraging warthog roots. */
const FORAGE_0 = { x: 130, y: 270 }
/** First rim station, where an elephant drinks. */
const DRINK_0 = { x: HOLE.cx - 50, y: HOLE.cy - 8 }

/* ── Palette mirrored from the scene ── */
const GIRAFFE_BODY = '#f3d27a'
const WARTHOG_BODY = '#7c5a3c'
const ELEPHANT_BODY = '#9aa0a6'
const TREE_LEAVES = '#3f6e36'
const TREE_TRUNK = '#6b3f24'
const DIRT = '#7a4f28'
const GRASS_LIGHT = '#d7b14a'
const GRASS_MID = '#b88a36'
const GRASS_DARK = '#94682a'
/** Ripple strokes are built as `rgba(207, 230, 244, <alpha>)`. */
const RIPPLE_RGB = 'rgba(207, 230, 244'

/* ── Species selection, mirrored from the scene's djb2 hash ── */
const ANIMAL_KINDS = ['giraffe', 'warthog', 'elephant'] as const
type AnimalKind = typeof ANIMAL_KINDS[number]

function speciesOf(id: string): AnimalKind {
  let h = 5381
  for (let i = 0; i < id.length; i++) h = ((h << 5) + h + id.charCodeAt(i)) >>> 0
  return ANIMAL_KINDS[h % ANIMAL_KINDS.length]
}

/**
 * An agent id that the scene will turn into `kind`. Species is a pure function
 * of the id, so the search is deterministic; `prefix` keeps each test's ids
 * distinct so the module-level known-agent cache cannot leak between tests.
 */
function idOf(kind: AnimalKind, prefix: string): string {
  for (let i = 0; i < 400; i++) {
    const id = `slot-${prefix}-${i}`
    if (speciesOf(id) === kind) return id
  }
  throw new Error(`no ${kind} id found for prefix ${prefix}`)
}

/* ── Recording 2D context ── */

interface FillRecord { x: number; y: number; w: number; h: number; color: string }
interface TextRecord { text: string; x: number; y: number }

interface Recorder {
  ctx: CanvasRenderingContext2D
  fills: FillRecord[]
  texts: TextRecord[]
  /** strokeStyle of every `stroke()` call — the ripples are stroked, not filled. */
  strokes: string[]
}

/**
 * A 2D context that remembers what it was told to draw. Unknown members become
 * no-op spies on first access, so a helper reaching for `ellipse`, `arc` or
 * `roundRect` never has to be enumerated here.
 */
function createRecorder(): Recorder {
  const fills: FillRecord[] = []
  const texts: TextRecord[] = []
  const strokes: string[] = []
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
  store.stroke = () => {
    if (capturing) strokes.push(String(store.strokeStyle))
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

  return { ctx, fills, texts, strokes }
}

/* ── Harness state ── */

/** Every context handed out this test, in creation order. */
let recorders: Recorder[] = []
/** Frame callbacks queued by the scene loop, keyed by the id rAF handed back. */
let queuedFrames = new Map<number, FrameRequestCallback>()
let frameSeq = 0
/** Value `Math.random()` returns — tests reassign it to steer the dice rolls. */
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
  recorders.forEach(r => { r.fills.length = 0; r.texts.length = 0; r.strokes.length = 0 })
}

/**
 * Step the loop in bursts, recording only the last frame of each burst, until
 * `pred` holds. Frame-exact expectations would be brittle — a walk's length
 * depends on the species' speed and the spot the dice picked — so poll instead
 * of guessing.
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

/** An agent the savanna has already met, so it starts at its home spot. */
function known(over: Partial<AgentSource> & { id: string }): AgentSource {
  const src = agent(over)
  markAgentsKnown('serengeti', [src.id])
  return src
}

function mount(agents: AgentSource[], visible = true) {
  const store = configureStore({ reducer: { chat: chatReducer } })
  const base = recorders.length
  const view = render(
    <Provider store={store}>
      <MemoryRouter><WateringHoleScene agents={agents} visible={visible} /></MemoryRouter>
    </Provider>,
  )
  const rerender = (next: AgentSource[], nextVisible = visible) => view.rerender(
    <Provider store={store}>
      <MemoryRouter><WateringHoleScene agents={next} visible={nextVisible} /></MemoryRouter>
    </Provider>,
  )
  // initSceneCanvases takes the pixel context first, then the text overlay.
  return { ...view, rerender, pixel: recorders[base], overlay: recorders[base + 1] }
}

/* ── Assertion helpers ── */

const labels = (rec: Recorder) => rec.texts.map(t => t.text)
/** Where a given label landed, or undefined when it was not drawn. */
const textAt = (rec: Recorder, text: string) => rec.texts.find(t => t.text === text)
/** Whether a rect of exactly this colour and size was painted anywhere. */
const hasRect = (rec: Recorder, color: string, w: number, h: number) =>
  rec.fills.some(f => f.color === color && f.w === w * S && f.h === h * S)
/** Whether a rect of exactly this colour, size and position was painted. */
const fillAt = (rec: Recorder, color: string, x: number, y: number, w: number, h: number) =>
  rec.fills.some(f => f.color === color && f.x === x * S && f.y === y * S && f.w === w * S && f.h === h * S)
const hasRipple = (rec: Recorder) => rec.strokes.some(s => s.startsWith(RIPPLE_RGB))

/** Posture fingerprints — each is a rect size only that posture ever paints. */
/** Neck stretched 15 tall into the canopy. */
const giraffeBrowsing = (rec: Recorder) => hasRect(rec, GIRAFFE_BODY, 3, 15)
/** Forelegs splayed to 9 tall instead of the standing 11. */
const giraffeDrinking = (rec: Recorder) => hasRect(rec, GIRAFFE_BODY, 2, 9)
/** Head dipped to 3 tall instead of the standing 4. */
const warthogForaging = (rec: Recorder) => hasRect(rec, WARTHOG_BODY, 4, 3)
/** Trunk extended 12 straight down into the water. */
const elephantDrinking = (rec: Recorder) => hasRect(rec, ELEPHANT_BODY, 2, 12)

const TITLE = 'Watering Hole'
const CANVAS_LABEL = 'Watering hole scene'
const OVERLAY_LABEL = 'Watering hole scene labels'
const RUNNING_BADGE = '●'
const IDLE_BADGE = '○'

/** Names are 9 characters, comfortably inside the 24-character wrap width the
 *  recorder's proportional `measureText` implies for a name label. */
const ROSALINDA = 'Rosalinda'
const FERDINAND = 'Ferdinand'
const BERNADETT = 'Bernadett'

/* ── Tests ── */

describe('WateringHoleScene savanna', () => {
  it('renders the pixel canvas and the text overlay with accessible labels', () => {
    const view = mount([])
    expect(view.getByLabelText(CANVAS_LABEL)).toBeInTheDocument()
    expect(view.getByLabelText(OVERLAY_LABEL)).toBeInTheDocument()
  })

  it('keeps the pixel canvas unsmoothed and sized to the scene', () => {
    const view = mount([])
    const canvas = view.getByLabelText(CANVAS_LABEL) as HTMLCanvasElement
    expect(canvas.style.imageRendering).toBe('pixelated')
    expect(canvas.width).toBe(W * S)
    expect(canvas.height).toBe(H * S)
  })

  it('titles the scene and counts an empty savanna on the very first frame', () => {
    const { overlay } = mount([])
    expect(textAt(overlay, TITLE)).toBeDefined()
    expect(textAt(overlay, TITLE)!.x).toBe((W / 2) * S)
    expect(labels(overlay)).toContain('0/8 on the savanna')
  })

  it('bands the ground, digs the dirt patches and plants the acacia trunks', () => {
    const { pixel } = mount([])
    expect(fillAt(pixel, GRASS_LIGHT, 0, HORIZON_Y, W, 30)).toBe(true)
    expect(fillAt(pixel, GRASS_MID, 0, HORIZON_Y + 30, W, 60)).toBe(true)
    expect(fillAt(pixel, GRASS_DARK, 0, HORIZON_Y + 90, W, H - HORIZON_Y - 90)).toBe(true)
    // Forage patches are 14 wide, centred on the spot.
    expect(fillAt(pixel, DIRT, FORAGE_0.x - 7, FORAGE_0.y, 14, 5)).toBe(true)
    // The nearest acacia's trunk is 5 wide and 70 tall, centred under the canopy.
    expect(fillAt(pixel, TREE_TRUNK, TREE_0.x - 2, TREE_0.y, 5, 70)).toBe(true)
  })

  it('draws nothing while hidden and starts drawing once it becomes visible', () => {
    const { overlay, rerender } = mount([agent({ id: 'slot-hidden' })], false)
    runFrames(5)
    expect(overlay.texts).toHaveLength(0)

    rerender([agent({ id: 'slot-hidden' })], true)
    runFrames(1)
    expect(labels(overlay)).toContain(TITLE)
  })

  it('caps the savanna at its eight home spots', () => {
    const many = Array.from({ length: 12 }, (_, i) => agent({ id: `slot-crowd-${i}`, running: true }))
    const { overlay } = mount(many)
    expect(labels(overlay)).toContain('8/8 on the savanna')
  })

  it('cancels the frame loop on unmount so no callback outlives the scene', () => {
    const view = mount([known({ id: idOf('elephant', 'unmount'), name: ROSALINDA })])
    runFrames(3)
    expect(queuedFrames.size).toBe(1)
    act(() => { view.unmount() })
    expect(queuedFrames.size).toBe(0)
  })
})

describe('WateringHoleScene arrivals', () => {
  it('stands a known animal at its home spot and slides an unseen one in from the edge', () => {
    const settled = known({ id: idOf('elephant', 'settled'), name: ROSALINDA })
    const newcomer = agent({ id: idOf('elephant', 'newcomer'), name: FERDINAND })
    const { overlay } = mount([settled, newcomer])

    // Labels are centred nine pixels right of the animal's own x.
    expect(textAt(overlay, ROSALINDA)!.x).toBe((HOME_0.x + 9) * S)
    // The newcomer starts at x = -20, still off the left edge of the savanna.
    expect(textAt(overlay, FERDINAND)!.x).toBeLessThan(0)
  })

  it('keeps an existing animal on its spot when a new agent is inserted ahead of it', () => {
    const resident = known({ id: idOf('warthog', 'resident'), name: ROSALINDA })
    const { overlay, rerender } = mount([resident])
    expect(textAt(overlay, ROSALINDA)!.x).toBe((HOME_0.x + 9) * S)

    // The arrival is listed FIRST, so a naive spot search would hand it the
    // resident's spot and stack two animals on one patch of grass.
    const arrival = agent({ id: idOf('warthog', 'arrival'), name: FERDINAND })
    rerender([arrival, resident])

    const parked = stepUntil(
      () => textAt(overlay, FERDINAND)?.x === (HOME_1.x + 9) * S,
      600, 25,
    )
    expect(parked).toBe(true)
    expect(textAt(overlay, ROSALINDA)!.x).toBe((HOME_0.x + 9) * S)
  })

  it('gives each agent id a stable species, so the savanna shows a mix', () => {
    const ids = {
      giraffe: idOf('giraffe', 'mix'),
      warthog: idOf('warthog', 'mix'),
      elephant: idOf('elephant', 'mix'),
    }
    const crew = [
      known({ id: ids.giraffe, name: ROSALINDA }),
      known({ id: ids.warthog, name: FERDINAND }),
      known({ id: ids.elephant, name: BERNADETT }),
    ]
    const first = mount(crew)
    expect(hasRect(first.pixel, GIRAFFE_BODY, 14, 7)).toBe(true)
    expect(hasRect(first.pixel, WARTHOG_BODY, 11, 5)).toBe(true)
    expect(hasRect(first.pixel, ELEPHANT_BODY, 15, 9)).toBe(true)

    // Species is hashed from the id, not drawn from a queue, so remounting the
    // same ids reproduces the same three animals.
    act(() => { first.unmount() })
    const again = mount(crew)
    expect(hasRect(again.pixel, GIRAFFE_BODY, 14, 7)).toBe(true)
    expect(hasRect(again.pixel, WARTHOG_BODY, 11, 5)).toBe(true)
    expect(hasRect(again.pixel, ELEPHANT_BODY, 15, 9)).toBe(true)
  })

  it('badges a running animal filled and an idle one hollow', () => {
    const busy = known({ id: idOf('elephant', 'busy'), name: ROSALINDA, running: true })
    const resting = known({ id: idOf('elephant', 'resting'), name: FERDINAND })
    const { overlay } = mount([busy, resting])

    // Non-giraffe badges ride four pixels above the animal's anchor.
    expect(textAt(overlay, RUNNING_BADGE)).toEqual(
      expect.objectContaining({ x: (HOME_0.x + 9) * S, y: (HOME_0.y - 4) * S }),
    )
    expect(textAt(overlay, IDLE_BADGE)).toEqual(
      expect.objectContaining({ x: (HOME_1.x + 9) * S, y: (HOME_0.y - 4) * S }),
    )
  })

  it('reuses a settled animal when its session is renamed instead of re-entering it', () => {
    const before = known({ id: idOf('elephant', 'rename'), name: ROSALINDA, detail: '3 msgs' })
    const { overlay, rerender } = mount([before])
    expect(labels(overlay)).toContain('3 msgs')
    // Name sits below the animal, detail six pixels below the name.
    expect(textAt(overlay, ROSALINDA)!.y).toBe((HOME_0.y + 32) * S)
    expect(textAt(overlay, '3 msgs')!.y).toBe((HOME_0.y + 38) * S)

    rerender([{ ...before, name: FERDINAND, detail: '9 msgs' }])
    clearRecords()
    runFrames(1)
    expect(labels(overlay)).toContain(FERDINAND)
    expect(labels(overlay)).toContain('9 msgs')
    expect(labels(overlay)).not.toContain(ROSALINDA)
    // Reused, not re-entered: it never left its home spot.
    expect(textAt(overlay, FERDINAND)!.x).toBe((HOME_0.x + 9) * S)
  })
})

describe('WateringHoleScene messages', () => {
  it('raises a speech bubble for a new last message, fades it, then drops it', () => {
    const base = known({ id: idOf('elephant', 'talker'), name: ROSALINDA })
    const { overlay, rerender } = mount([base])

    rerender([{ ...base, lastMessage: 'Drinking deep' }])
    clearRecords()
    runFrames(1)
    expect(labels(overlay)).toContain('Drinking deep')

    // 6.5s in: past the fade threshold, still on screen.
    nowMs += 6_500
    clearRecords()
    runFrames(1)
    expect(labels(overlay)).toContain('Drinking deep')

    // Past the 7s lifetime the bubble is gone.
    nowMs += 1_000
    clearRecords()
    runFrames(1)
    expect(labels(overlay)).not.toContain('Drinking deep')
  })

  it('leaves the bubble down when a rerender repeats the same message', () => {
    const base = known({ id: idOf('elephant', 'repeat'), name: ROSALINDA, lastMessage: 'same text' })
    const { overlay, rerender } = mount([base])
    rerender([{ ...base }])
    clearRecords()
    runFrames(1)
    expect(labels(overlay)).not.toContain('same text')
  })
})

describe('WateringHoleScene activities', () => {
  it('sends a running giraffe to an acacia and stretches its neck into the leaves', () => {
    // rand 0.05 loses the 50/50 against water and picks the nearest foreground
    // tree; the distant one would put the giraffe above the horizon.
    const g = known({ id: idOf('giraffe', 'browse'), name: ROSALINDA, running: true })
    const { pixel, overlay } = mount([g])

    expect(giraffeBrowsing(pixel)).toBe(true)
    // A leaf pixel at the mouth is the only filled rect in the canopy colour.
    expect(hasRect(pixel, TREE_LEAVES, 2, 1)).toBe(true)
    // The badge lifts clear of the raised head while browsing.
    expect(textAt(overlay, RUNNING_BADGE)!.y).toBe((HOME_0.y - 14) * S)

    const arrived = stepUntil(
      () => textAt(overlay, ROSALINDA)!.x === (TREE_0.x - 12 + 9) * S,
      600, 25,
    )
    expect(arrived).toBe(true)
    expect(textAt(overlay, ROSALINDA)!.y).toBe((TREE_0.y + 10 + 32) * S)
  })

  it('splays a giraffes forelegs when the dice send it to the water instead', () => {
    rand = 0.9
    const g = known({ id: idOf('giraffe', 'sip'), name: ROSALINDA, running: true })
    const { pixel } = mount([g])
    expect(giraffeDrinking(pixel)).toBe(true)
    expect(giraffeBrowsing(pixel)).toBe(false)
  })

  it('drops a running warthogs snout into the nearest dirt patch', () => {
    const w = known({ id: idOf('warthog', 'root'), name: FERDINAND, running: true })
    const { pixel, overlay } = mount([w])
    expect(warthogForaging(pixel)).toBe(true)

    const arrived = stepUntil(
      () => textAt(overlay, FERDINAND)!.x === (FORAGE_0.x - 7 + 9) * S,
      600, 25,
    )
    expect(arrived).toBe(true)
  })

  it('sends a warthog to the rim instead when the dice lose the forage roll', () => {
    rand = 0.9
    const w = known({ id: idOf('warthog', 'thirsty'), name: FERDINAND, running: true })
    const { pixel, overlay } = mount([w])
    // A drinking warthog keeps its head up — only foraging dips the snout.
    expect(warthogForaging(pixel)).toBe(false)
    expect(hasRect(pixel, WARTHOG_BODY, 4, 4)).toBe(true)

    const atRim = stepUntil(
      () => textAt(overlay, FERDINAND)!.y === (HOLE.cy + 12 + 32) * S,
      600, 25,
    )
    expect(atRim).toBe(true)
    // Last rim station, eight pixels back from the water's edge.
    expect(textAt(overlay, FERDINAND)!.x).toBe((HOLE.cx + 35 - 8 + 9) * S)
  })

  it('walks a running elephant to the rim, drops its trunk in and rings the water', () => {
    const e = known({ id: idOf('elephant', 'drink'), name: BERNADETT, running: true })
    const { pixel, overlay } = mount([e])
    expect(elephantDrinking(pixel)).toBe(true)
    // Nothing has reached the water yet, so the surface is still.
    expect(hasRipple(pixel)).toBe(false)

    const rippling = stepUntil(() => hasRipple(pixel), 900, 24)
    expect(rippling).toBe(true)
    // Ripples only spawn once the animal is standing at its rim station.
    expect(textAt(overlay, BERNADETT)!.x).toBe((DRINK_0.x - 8 + 9) * S)

    // Rings are retired once they age out, so a long drink cannot accumulate
    // one ring per spawn: at a ring every 24 frames and a 40-frame life, only
    // a couple are ever alive at once.
    fastForward(120)
    clearRecords()
    runFrames(1)
    expect(pixel.strokes.length).toBeGreaterThan(0)
    expect(pixel.strokes.length).toBeLessThanOrEqual(3)
  })

  it('ambles an elephant across the open savanna on the prowl roll', () => {
    rand = 0.5
    const e = known({ id: idOf('elephant', 'prowl'), name: BERNADETT, running: true })
    const { pixel, overlay } = mount([e])
    // Ambling keeps the standing posture: the trunk hangs and curls.
    expect(elephantDrinking(pixel)).toBe(false)
    expect(hasRect(pixel, ELEPHANT_BODY, 2, 5)).toBe(true)

    const ambled = stepUntil(
      () => textAt(overlay, BERNADETT)!.x === (240 + 9) * S,
      900, 25,
    )
    expect(ambled).toBe(true)
    expect(textAt(overlay, BERNADETT)!.y).toBe((260 + 32) * S)
  })

  it('parks an elephant in the shade of a tree on the rest roll', () => {
    rand = 0.9
    const e = known({ id: idOf('elephant', 'shade'), name: BERNADETT, running: true })
    const { overlay } = mount([e])

    const shaded = stepUntil(
      () => textAt(overlay, BERNADETT)!.y === (TREE_2.y + 55 + 32) * S,
      900, 25,
    )
    expect(shaded).toBe(true)
    expect(textAt(overlay, BERNADETT)!.x).toBe((TREE_2.x - 12 + 9) * S)
  })

  it('abandons the activity and walks home once the session stops running', () => {
    const w = known({ id: idOf('warthog', 'stopped'), name: FERDINAND, running: true })
    const { pixel, overlay, rerender } = mount([w])

    const rooting = stepUntil(
      () => textAt(overlay, FERDINAND)!.x === (FORAGE_0.x - 7 + 9) * S,
      600, 25,
    )
    expect(rooting).toBe(true)

    rerender([{ ...w, running: false }])
    // A stationary animal whose session went quiet holds its pose for 30 frames
    // and then heads back to its home spot.
    const home = stepUntil(
      () => textAt(overlay, FERDINAND)!.x === (HOME_0.x + 9) * S,
      900, 25,
    )
    expect(home).toBe(true)
    expect(labels(overlay)).toContain(IDLE_BADGE)
    expect(warthogForaging(pixel)).toBe(false)
  })

  it('resets an animal to idle when its session changes kind mid-activity', () => {
    const e = known({ id: idOf('elephant', 'flipped'), name: BERNADETT, running: true })
    const { pixel, rerender } = mount([e])
    expect(elephantDrinking(pixel)).toBe(true)

    rerender([{ ...e, kind: 'cron' }])
    clearRecords()
    runFrames(1)
    // Back to the hanging trunk, aimed at its home spot rather than the water.
    expect(elephantDrinking(pixel)).toBe(false)
    expect(hasRect(pixel, ELEPHANT_BODY, 2, 5)).toBe(true)
  })

  it('starts a newly-running animal wandering without waiting out a full dwell', () => {
    const base = known({ id: idOf('elephant', 'woken'), name: BERNADETT })
    const { pixel, rerender } = mount([base])
    expect(elephantDrinking(pixel)).toBe(false)

    rerender([{ ...base, running: true }])
    clearRecords()
    runFrames(1)
    expect(elephantDrinking(pixel)).toBe(true)
  })
})

describe('WateringHoleScene hover', () => {
  it('names the hovered animal and gives it the savannas own status flavour', () => {
    const e = known({ id: idOf('elephant', 'hover'), name: BERNADETT, running: true, detail: '3 msgs' })
    const view = mount([e])
    const canvas = view.getByLabelText(CANVAS_LABEL)

    fireEvent.mouseMove(canvas, { clientX: HOME_0.x * S, clientY: HOME_0.y * S })
    expect(view.getByText(BERNADETT)).toBeInTheDocument()
    expect(view.getByText('On the move')).toBeInTheDocument()
    expect(view.getByText(/Working/)).toBeInTheDocument()

    fireEvent.mouseLeave(canvas)
    expect(view.queryByText('On the move')).toBeNull()
  })

  it('calls an idle animal resting and ignores empty grass', () => {
    const e = known({ id: idOf('elephant', 'idlehover'), name: ROSALINDA, kind: 'cron', detail: 'hourly' })
    const view = mount([e])
    const canvas = view.getByLabelText(CANVAS_LABEL)

    fireEvent.mouseMove(canvas, { clientX: HOME_0.x * S, clientY: HOME_0.y * S })
    expect(view.getByText('Resting')).toBeInTheDocument()

    // Far corner of the savanna: nothing is standing there.
    fireEvent.mouseMove(canvas, { clientX: 5 * S, clientY: 330 * S })
    expect(view.queryByText('Resting')).toBeNull()
  })
})
