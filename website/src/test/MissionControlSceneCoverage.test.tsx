import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer from '../store/chatSlice'
import MissionControlScene from '../pages/scenes/mission-control/MissionControlScene'
import { markAgentsKnown } from '../hooks/sceneStateCache'
import { W, H, C, DOOR, STATION_POSITIONS, DESTINATIONS } from '../pages/scenes/mission-control/parts'
import type { AgentSource } from '../hooks/useAgentSync'

/**
 * Drives MissionControlScene's animation loop deterministically.
 *
 * The scene paints everything into a 2D canvas from inside a
 * requestAnimationFrame loop, so nothing past mount runs unless the frame
 * callback is invoked by hand. The harness therefore:
 *   - captures the rAF callback instead of scheduling it,
 *   - records every fillStyle the loop paints with (the palette is the only
 *     observable output of a canvas branch under happy-dom),
 *   - pins Math.random so agent timers, destinations and easter-egg rolls are
 *     reproducible rather than flaky.
 */

interface Painted {
  colors: Set<string>
  rects: number
  clips: number
}

let painted: Painted
let frameCb: FrameRequestCallback | null = null
/** Value returned by the mocked Math.random — tests retune it mid-run. */
let roll = 0

function createRecordingCtx(rec: Painted): CanvasRenderingContext2D {
  const ctx = {
    fillStyle: '' as string,
    globalAlpha: 1,
    imageSmoothingEnabled: false,
    fillRect: () => { rec.rects++; rec.colors.add(String(ctx.fillStyle)) },
    save: () => {},
    restore: () => {},
    beginPath: () => {},
    rect: () => {},
    clip: () => { rec.clips++ },
    clearRect: () => {},
    measureText: () => ({ width: 10 }),
  }
  return ctx as unknown as CanvasRenderingContext2D
}

/** Invoke the captured frame callback n times inside act(). */
function runFrames(n: number) {
  act(() => {
    for (let i = 0; i < n; i++) {
      const cb = frameCb
      if (!cb) return
      cb(i)
    }
  })
}

function renderScene(props: { agents: AgentSource[]; visible?: boolean }) {
  const store = configureStore({ reducer: { chat: chatReducer } })
  const view = render(
    <Provider store={store}>
      <MemoryRouter>
        <MissionControlScene {...props} />
      </MemoryRouter>
    </Provider>,
  )
  const rerender = (next: { agents: AgentSource[]; visible?: boolean }) => view.rerender(
    <Provider store={store}>
      <MemoryRouter>
        <MissionControlScene {...next} />
      </MemoryRouter>
    </Provider>,
  )
  return { ...view, rerender }
}

/** Hover the scene canvas at a logical scene coordinate. */
function hover(x: number, y: number) {
  const canvas = screen.getByLabelText('Mission control scene')
  fireEvent.mouseMove(canvas, { clientX: x, clientY: y })
}

function leave() {
  fireEvent.mouseLeave(screen.getByLabelText('Mission control scene'))
}

/** Chair coordinates the scene parks a seated agent on. */
function chair(stationIdx: number) {
  const pos = STATION_POSITIONS[stationIdx]
  return { x: pos.x + 21, y: pos.y + 20 }
}

/**
 * Step the loop in short bursts until `name` is hoverable at (x, y).
 * Frame-exact expectations would be brittle — an agent only lingers at a
 * destination for 80 frames — so poll instead of guessing an arrival frame.
 */
function reaches(name: string, x: number, y: number, maxFrames: number): boolean {
  for (let elapsed = 0; elapsed < maxFrames; elapsed += 10) {
    runFrames(10)
    hover(x, y)
    const there = screen.queryByText(name) !== null
    leave()
    if (there) return true
  }
  return false
}

function agent(id: string, over: Partial<AgentSource> = {}): AgentSource {
  return {
    id, name: id.toUpperCase(), label: 'default', kind: 'slot',
    running: false, detail: '250 msgs', ...over,
  }
}

beforeEach(() => {
  painted = { colors: new Set<string>(), rects: 0, clips: 0 }
  frameCb = null
  roll = 0
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(
    () => createRecordingCtx(painted) as unknown as ReturnType<HTMLCanvasElement['getContext']>,
  )
  vi.spyOn(HTMLCanvasElement.prototype, 'getBoundingClientRect').mockReturnValue({
    left: 0, top: 0, width: W, height: H, right: W, bottom: H, x: 0, y: 0,
    toJSON: () => ({}),
  } as DOMRect)
  vi.spyOn(Math, 'random').mockImplementation(() => roll)
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { frameCb = cb; return 1 })
  vi.stubGlobal('cancelAnimationFrame', () => { frameCb = null })
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('MissionControlScene canvas surface', () => {
  it('renders a labelled pixel canvas sized to the scene', () => {
    renderScene({ agents: [] })
    const canvas = screen.getByLabelText('Mission control scene') as HTMLCanvasElement
    expect(canvas.style.imageRendering).toBe('pixelated')
    expect(canvas.width).toBeGreaterThanOrEqual(W)
    expect(canvas.height).toBeGreaterThanOrEqual(H)
  })

  it('paints the empty control room: walls, floor and the main screen', () => {
    renderScene({ agents: [] })
    runFrames(3)
    expect(painted.rects).toBeGreaterThan(500)
    expect(painted.colors.has(C.wall)).toBe(true)
    expect(painted.colors.has(C.floor)).toBe(true)
    expect(painted.colors.has(C.bigScreen)).toBe(true)
    // Unmanned consoles show a dark screen, never a live one.
    expect(painted.colors.has(C.screenOff)).toBe(true)
    // The status ticker is drawn through a clipped region.
    expect(painted.clips).toBeGreaterThan(0)
  })

  it('skips painting while hidden and resumes once visible again', () => {
    const { rerender } = renderScene({ agents: [], visible: false })
    runFrames(5)
    expect(painted.rects).toBe(0)

    rerender({ agents: [], visible: true })
    runFrames(2)
    expect(painted.rects).toBeGreaterThan(500)
  })
})

describe('MissionControlScene agent stations', () => {
  it('seats already-known agents and lights their console by run state', () => {
    markAgentsKnown('missioncontrol', ['slot-busy', 'slot-bored'])
    renderScene({
      agents: [agent('slot-busy', { running: true }), agent('slot-bored')],
    })
    runFrames(4)
    // Running agent → typewriter code screen with a green LED.
    expect(painted.colors.has(C.led.on)).toBe(true)
    // Idle agent → bouncing-logo screensaver with an amber LED.
    expect(painted.colors.has(C.led.warn)).toBe(true)
  })

  it('shows a hover tooltip with the agent name and its level line', () => {
    markAgentsKnown('missioncontrol', ['slot-hover'])
    renderScene({ agents: [agent('slot-hover', { running: true })] })
    runFrames(2)

    const seat = chair(0)
    hover(seat.x, seat.y)
    expect(screen.getByText('SLOT-HOVER')).toBeInTheDocument()
    expect(screen.getByText('Lv.5 Senior Gaslighter')).toBeInTheDocument()

    leave()
    expect(screen.queryByText('SLOT-HOVER')).not.toBeInTheDocument()
  })

  it('adopts a renamed / restarted agent in place instead of re-seating it', () => {
    markAgentsKnown('missioncontrol', ['slot-same'])
    const { rerender } = renderScene({ agents: [agent('slot-same')] })
    runFrames(4)

    rerender({ agents: [{ ...agent('slot-same', { running: true }), name: 'RENAMED' }] })
    runFrames(4)

    const seat = chair(0)
    hover(seat.x, seat.y)
    // Same chair, new name — no entrance animation was restarted.
    expect(screen.getByText('RENAMED')).toBeInTheDocument()
    expect(screen.queryByText('SLOT-SAME')).not.toBeInTheDocument()
    leave()
    // Now running, so the console shows the live code screen.
    expect(painted.colors.has(C.led.on)).toBe(true)
  })

  it('starts a desk conversation between two neighbouring agents', () => {
    markAgentsKnown('missioncontrol', ['slot-chat-a', 'slot-chat-b'])
    renderScene({ agents: [agent('slot-chat-a'), agent('slot-chat-b')] })
    // roll = 0 makes the per-frame pairing roll always succeed, so the two
    // neighbours strike up one of the desk conversations immediately.
    runFrames(90)
    expect(painted.rects).toBeGreaterThan(1000)
    // Speech bubbles paint white; nothing else in the room does.
    expect(painted.colors.has('#fff')).toBe(true)
  })

  it('caps the crew at the eight console stations', () => {
    const ids = Array.from({ length: 11 }, (_, i) => `slot-cap-${i}`)
    markAgentsKnown('missioncontrol', ids)
    renderScene({ agents: ids.map(id => agent(id)) })
    runFrames(2)

    const seated: string[] = []
    for (let i = 0; i < STATION_POSITIONS.length; i++) {
      const seat = chair(i)
      hover(seat.x, seat.y)
      const name = ids.map(id => id.toUpperCase()).find(n => screen.queryByText(n))
      if (name) seated.push(name)
      leave()
    }
    expect(seated).toEqual(ids.slice(0, 8).map(id => id.toUpperCase()))
  })
})

describe('MissionControlScene arrivals and departures', () => {
  it('walks first-time agents in from the door, staggering the second one', () => {
    renderScene({
      agents: [agent('slot-arrival-1', { running: true }), agent('slot-arrival-2')],
    })
    runFrames(1)
    // Both spawn at the door, not at their consoles.
    hover(DOOR.x + 6, DOOR.y + 20)
    expect(screen.getByText('SLOT-ARRIVAL-1')).toBeInTheDocument()
    leave()

    // The second arrival holds at the door for its stagger delay.
    runFrames(30)
    hover(DOOR.x + 6, DOOR.y + 20)
    expect(screen.getByText('SLOT-ARRIVAL-2')).toBeInTheDocument()
    leave()

    runFrames(500)
    for (const [idx, name] of [[0, 'SLOT-ARRIVAL-1'], [1, 'SLOT-ARRIVAL-2']] as const) {
      const seat = chair(idx)
      hover(seat.x, seat.y)
      expect(screen.getByText(name)).toBeInTheDocument()
      leave()
    }
    hover(DOOR.x + 6, DOOR.y + 20)
    expect(screen.queryByText('SLOT-ARRIVAL-1')).not.toBeInTheDocument()
  })

  it('walks a removed agent out of the room and drops it', () => {
    markAgentsKnown('missioncontrol', ['slot-leaver'])
    const { rerender } = renderScene({ agents: [agent('slot-leaver')] })
    runFrames(2)

    rerender({ agents: [] })
    runFrames(60)
    // A second reconcile mid-exit must not interrupt the walk.
    rerender({ agents: [] })
    runFrames(400)

    for (const point of [chair(0), { x: DOOR.x + 6, y: DOOR.y + 20 }]) {
      hover(point.x, point.y)
      expect(screen.queryByText('SLOT-LEAVER')).not.toBeInTheDocument()
      leave()
    }
  })
})

describe('MissionControlScene idle errands', () => {
  it('sends an idle agent for coffee, then to the bin with the empty mug', () => {
    markAgentsKnown('missioncontrol', ['slot-coffee'])
    renderScene({ agents: [agent('slot-coffee')] })
    const seat = chair(0)
    // roll = 0 → idleTimer of 1800 frames, then destination index 0 (coffee).
    expect(reaches('SLOT-COFFEE', DESTINATIONS.coffee.x, DESTINATIONS.coffee.y, 3000)).toBe(true)
    // Carries the mug home and sits back down.
    expect(reaches('SLOT-COFFEE', seat.x, seat.y, 1500)).toBe(true)
    // Mug in hand, the only errand left is the bin…
    expect(reaches('SLOT-COFFEE', DESTINATIONS.trash.x, DESTINATIONS.trash.y, 3500)).toBe(true)
    // …and then back to work empty-handed.
    expect(reaches('SLOT-COFFEE', seat.x, seat.y, 1500)).toBe(true)
  })

  it('fills a cup at the water cooler and carries it back to the desk', () => {
    markAgentsKnown('missioncontrol', ['slot-water'])
    renderScene({ agents: [agent('slot-water')] })
    // Destination index 1 (water) once the idle timer expires.
    roll = 0.5
    expect(reaches('SLOT-WATER', DESTINATIONS.water.x, DESTINATIONS.water.y, 3200)).toBe(true)
    const seat = chair(0)
    expect(reaches('SLOT-WATER', seat.x, seat.y, 1500)).toBe(true)
    // Settle into the chair so the cup is set down on the desk.
    runFrames(40)
    expect(painted.colors.has(C.led.warn)).toBe(true)
  })

  it('collects a snack from the vending machine and carries it back', () => {
    markAgentsKnown('missioncontrol', ['slot-snack'])
    renderScene({ agents: [agent('slot-snack')] })
    // Destination index 2 (vending) once the idle timer expires.
    roll = 0.9
    expect(reaches('SLOT-SNACK', DESTINATIONS.vending.x, DESTINATIONS.vending.y, 6000)).toBe(true)
    const seat = chair(0)
    expect(reaches('SLOT-SNACK', seat.x, seat.y, 1500)).toBe(true)
    // Settle into the chair so the snack is set down on the desk.
    runFrames(40)
    expect(painted.colors.has(C.led.warn)).toBe(true)
  })
})

describe('MissionControlScene easter egg', () => {
  it('takes over every screen with a forced update at the 3600th frame', () => {
    markAgentsKnown('missioncontrol', ['slot-egg-a', 'slot-egg-b'])
    renderScene({
      agents: [agent('slot-egg-a', { running: true }), agent('slot-egg-b', { running: true })],
    })
    runFrames(3599)
    expect(painted.colors.has('#0078d4')).toBe(false)

    runFrames(60)
    // Big screen turns update-blue and every console shows the crash screen.
    expect(painted.colors.has('#0078d4')).toBe(true)
    expect(painted.colors.has('#0012a0')).toBe(true)
  })
})
