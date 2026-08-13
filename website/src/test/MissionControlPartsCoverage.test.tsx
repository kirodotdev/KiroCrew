/**
 * Mission Control scene "parts" module — pure drawing / geometry helpers.
 *
 * Every export here takes a `DrawFn` callback instead of touching a canvas, so the
 * whole module is testable under jsdom by recording the rects it asks for. These
 * tests assert real behaviour: which glyph pixels the pixel-font emits, how the
 * agent sprite changes with facing / walking / drinking / carried item, how the
 * waypoint router picks aisles, and where the level ladder steps.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import {
  W, H, S, P, MAX_STATIONS, DOOR, WALL_H,
  STATION_POSITIONS, AGENT_COLORS, C,
  drawText, darken, lighten, drawAgent,
  spawnParticles, updateParticles, drawParticles,
  DESTINATIONS, buildEntryPath, buildExitPath, buildPath, buildReturnPath,
  getLevel, DESK_CONVOS, BREAK_CONVOS,
} from '../pages/scenes/mission-control/parts'
import type { MCAgent, Particle } from '../pages/scenes/mission-control/parts'

/* ── Recording draw sink ── */
interface Rect { x: number; y: number; w: number; h: number; c: string }

function recorder() {
  const calls: Rect[] = []
  const d = (x: number, y: number, w: number, h: number, c: string) => {
    calls.push({ x, y, w, h, c })
  }
  return { d, calls }
}

const withColor = (calls: Rect[], c: string) => calls.filter(r => r.c === c)

function makeAgent(over: Partial<MCAgent> = {}): MCAgent {
  return {
    id: 'slot-1', name: 'Alpha', label: 'default', kind: 'slot',
    x: 100, y: 200, tx: 100, ty: 200,
    stationIdx: 0, color: '#3498db', running: false,
    detail: '3 msgs', facing: 'left', activity: 'sitting',
    waypoints: [], enterProgress: 1, walkFrame: 0,
    item: 'none', destKey: null,
    idleTimer: 0, waitTimer: 0, drinkTimer: 0,
    chatTimer: 0, chatDelay: 0, chatLine: '',
    bubbleUp: false, deskOn: true, leaving: false,
    ...over,
  }
}

afterEach(() => {
  vi.restoreAllMocks()
})

/* ────────────────────────── Layout constants ────────────────────────── */

describe('layout constants', () => {
  it('exposes a 480x320 stage with a positive device scale', () => {
    expect(W).toBe(480)
    expect(H).toBe(320)
    expect(P).toBe(2)
    expect(S).toBeGreaterThan(0)
    expect(WALL_H).toBe(110)
  })

  it('has exactly MAX_STATIONS desk positions split into two rows', () => {
    expect(STATION_POSITIONS).toHaveLength(MAX_STATIONS)
    // First four are the top row, last four sit lower on the floor.
    for (const p of STATION_POSITIONS.slice(0, 4)) expect(p.y).toBe(180)
    for (const p of STATION_POSITIONS.slice(4)) expect(p.y).toBe(256)
  })

  it('gives every station its own agent colour', () => {
    expect(AGENT_COLORS).toHaveLength(MAX_STATIONS)
    expect(new Set(AGENT_COLORS).size).toBe(MAX_STATIONS)
    for (const c of AGENT_COLORS) expect(c).toMatch(/^#[0-9a-f]{6}$/)
  })

  it('places the door on the left wall inside the stage', () => {
    expect(DOOR.x).toBe(0)
    expect(DOOR.y + DOOR.h).toBeLessThanOrEqual(H)
  })

  it('palette carries the four LED states used by the desk indicators', () => {
    expect(Object.keys(C.led).sort()).toEqual(['err', 'off', 'on', 'warn'])
    expect(C.led.on).not.toBe(C.led.off)
  })
})

/* ────────────────────────── Pixel font ────────────────────────── */

describe('drawText', () => {
  it('emits one rect per lit pixel of a glyph', () => {
    const { d, calls } = recorder()
    // 'I' is [7,2,2,2,7] -> 3 + 1 + 1 + 1 + 3 lit pixels.
    drawText(d, 'I', 0, 0, '#fff')
    expect(calls).toHaveLength(9)
    expect(calls.every(r => r.w === 1 && r.h === 1 && r.c === '#fff')).toBe(true)
  })

  it('is case-insensitive', () => {
    const upper = recorder(); drawText(upper.d, 'A', 0, 0, '#fff')
    const lower = recorder(); drawText(lower.d, 'a', 0, 0, '#fff')
    expect(lower.calls).toEqual(upper.calls)
  })

  it('draws nothing for a blank glyph but still advances the cursor', () => {
    const { d, calls } = recorder()
    drawText(d, ' I', 0, 0, '#fff')
    expect(calls).toHaveLength(9)
    // The space consumed 4px, so the 'I' starts at x=4 rather than x=0.
    expect(Math.min(...calls.map(r => r.x))).toBe(4)
  })

  it('skips unknown characters entirely', () => {
    const { d, calls } = recorder()
    drawText(d, '#~', 0, 0, '#fff')
    expect(calls).toHaveLength(0)
  })

  it('scales both the pixel size and the advance', () => {
    const { d, calls } = recorder()
    drawText(d, 'II', 0, 0, '#fff', 2)
    expect(calls).toHaveLength(18)
    expect(calls.every(r => r.w === 2 && r.h === 2)).toBe(true)
    // Second glyph is 4*scale to the right of the first.
    expect(Math.max(...calls.map(r => r.x))).toBe(8 + 2 * 2)
  })

  it('covers digits and punctuation from the font table', () => {
    const { d, calls } = recorder()
    drawText(d, '0123456789:[]-.!?/', 0, 0, '#0f0')
    expect(calls.length).toBeGreaterThan(50)
  })

  it('covers the full letter range', () => {
    const { d, calls } = recorder()
    drawText(d, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 0, 0, '#0f0')
    expect(calls.length).toBeGreaterThan(200)
  })
})

/* ────────────────────────── Colour helpers ────────────────────────── */

describe('darken / lighten', () => {
  it('subtracts the amount from every channel', () => {
    expect(darken('#808080', 0x10)).toBe('#707070')
  })

  it('clamps at black instead of wrapping', () => {
    expect(darken('#010203', 40)).toBe('#000000')
  })

  it('pads a short hex result back to six digits', () => {
    // r/g/b all collapse to 0 -> toString(16) is "0" and must be padded.
    expect(darken('#000000')).toBe('#000000')
    expect(darken('#101010', 0x0f)).toBe('#010101')
  })

  it('adds the amount when lightening', () => {
    expect(lighten('#101010', 0x10)).toBe('#202020')
  })

  it('clamps at white instead of wrapping', () => {
    expect(lighten('#f0f0f0', 100)).toBe('#ffffff')
  })

  it('uses documented defaults', () => {
    expect(darken('#646464')).toBe(darken('#646464', 40))
    expect(lighten('#646464')).toBe(lighten('#646464', 30))
  })
})

/* ────────────────────────── Agent sprite ────────────────────────── */

describe('drawAgent', () => {
  it('draws the shirt logo and legs in side view', () => {
    const { d, calls } = recorder()
    drawAgent(d, makeAgent({ facing: 'left' }), 0)
    // Orange logo stripe is side-view only.
    expect(withColor(calls, '#f90').length).toBe(2)
    expect(withColor(calls, '#2a2a4a').length).toBe(2)
  })

  it('omits the shirt logo and legs when seen from behind', () => {
    const { d, calls } = recorder()
    drawAgent(d, makeAgent({ facing: 'back' }), 0)
    expect(withColor(calls, '#f90')).toHaveLength(0)
    expect(withColor(calls, '#2a2a4a')).toHaveLength(0)
  })

  it('mirrors the eyes when facing right', () => {
    const eyesOf = (facing: MCAgent['facing']) => {
      const { d, calls } = recorder()
      drawAgent(d, makeAgent({ facing }), 0)
      return withColor(calls, '#222').map(r => r.x)
    }
    expect(eyesOf('left')).toEqual([102, 104])
    expect(eyesOf('right')).toEqual([104, 106])
  })

  it('swings the legs while walking and holds them still while sitting', () => {
    const legs = (over: Partial<MCAgent>) => {
      const { d, calls } = recorder()
      drawAgent(d, makeAgent(over), 0)
      return withColor(calls, '#2a2a4a').map(r => r.h)
    }
    expect(legs({ activity: 'sitting', walkFrame: 8 })).toEqual([6, 4])
    expect(legs({ activity: 'walking', walkFrame: 0 })).toEqual([6, 4])
    expect(legs({ activity: 'walking', walkFrame: 8 })).toEqual([4, 6])
    expect(legs({ activity: 'entering', walkFrame: 8 })).toEqual([4, 6])
    expect(legs({ activity: 'leaving', walkFrame: 8 })).toEqual([4, 6])
  })

  it.each([
    ['mug', '#ddd'],
    ['cup', '#aaddff'],
    ['snack', '#f39c12'],
  ] as const)('draws a carried %s in side view', (item, color) => {
    const { d, calls } = recorder()
    drawAgent(d, makeAgent({ item }), 0)
    expect(withColor(calls, color).length).toBeGreaterThan(0)
  })

  it.each([
    ['mug', '#ddd'],
    ['cup', '#aaddff'],
    ['snack', '#f39c12'],
  ] as const)('holds a %s on the mirrored side when facing right', (item, color) => {
    const itemX = (facing: MCAgent['facing']) => {
      const { d, calls } = recorder()
      drawAgent(d, makeAgent({ facing, item }), 0)
      return withColor(calls, color)[0].x
    }
    expect(itemX('right')).toBeGreaterThan(itemX('left'))
  })

  it('draws nothing extra for an empty-handed agent', () => {
    const { d, calls } = recorder()
    drawAgent(d, makeAgent({ item: 'none' }), 0)
    expect(withColor(calls, '#ddd')).toHaveLength(0)
    expect(withColor(calls, '#aaddff')).toHaveLength(0)
    expect(withColor(calls, '#f39c12')).toHaveLength(0)
  })

  it('raises one arm and shows the drink when drinking from behind', () => {
    const { d, calls } = recorder()
    drawAgent(d, makeAgent({ facing: 'back', drinkTimer: 30, item: 'mug' }), 0)
    expect(withColor(calls, '#ddd').length).toBe(1)
    // The raised hand uses the lighter skin tone reserved for the drinking pose.
    expect(withColor(calls, '#f0c8a0').length).toBe(2)
  })

  it('shows a cup rather than a mug when drinking a cup', () => {
    const { d, calls } = recorder()
    drawAgent(d, makeAgent({ facing: 'back', drinkTimer: 30, item: 'cup' }), 0)
    expect(withColor(calls, '#aaddff')).toHaveLength(1)
    expect(withColor(calls, '#ddd')).toHaveLength(0)
  })

  it('draws no drink when the agent is drinking with empty hands', () => {
    const { d, calls } = recorder()
    drawAgent(d, makeAgent({ facing: 'back', drinkTimer: 30, item: 'none' }), 0)
    expect(withColor(calls, '#ddd')).toHaveLength(0)
    expect(withColor(calls, '#aaddff')).toHaveLength(0)
  })

  it('bounces the typing arms on alternating frames', () => {
    const armYs = (walkFrame: number) => {
      const { d, calls } = recorder()
      drawAgent(d, makeAgent({
        facing: 'back', activity: 'sitting', running: true, walkFrame,
      }), 0)
      return withColor(calls, '#f0c8a0').map(r => r.y)
    }
    // walkFrame bit 3 clear -> right arm lifts; set -> left arm lifts.
    expect(armYs(0)).toEqual([205, 204])
    expect(armYs(8)).toEqual([204, 205])
  })

  it('keeps both arms down when idle at the desk', () => {
    const { d, calls } = recorder()
    drawAgent(d, makeAgent({ facing: 'back', activity: 'sitting', running: false, walkFrame: 8 }), 0)
    expect(withColor(calls, '#f0c8a0').map(r => r.y)).toEqual([205, 205])
  })

  it('truncates a long name to eight characters in the label', () => {
    const { d, calls } = recorder()
    drawAgent(d, makeAgent({ name: 'ABCDEFGHIJKL' }), 0)
    const label = withColor(calls, '#fff')
    // 8 glyphs at 4px advance -> the label spans less than 32px.
    const span = Math.max(...label.map(r => r.x)) - Math.min(...label.map(r => r.x))
    expect(span).toBeLessThan(32)
  })

  it('rounds fractional positions to whole pixels', () => {
    const { d, calls } = recorder()
    drawAgent(d, makeAgent({ x: 100.6, y: 200.4 }), 0)
    expect(calls.every(r => Number.isInteger(r.x) && Number.isInteger(r.y))).toBe(true)
  })
})

/* ────────────────────────── Speech bubble (via drawAgent) ────────────────────────── */

describe('speech bubble', () => {
  const bubbleRects = (over: Partial<MCAgent>) => {
    const { d, calls } = recorder()
    drawAgent(d, makeAgent(over), 0)
    // The bubble body is the only white rect taller than the tail and the top edge.
    return withColor(calls, '#fff').filter(r => r.h > 2)
  }

  it('appears once the chat timer is running with a line to say', () => {
    expect(bubbleRects({ chatTimer: 40, chatLine: 'ship it' })).toHaveLength(1)
  })

  it('stays hidden while the chat is still delayed', () => {
    expect(bubbleRects({ chatTimer: 40, chatDelay: 10, chatLine: 'ship it' })).toHaveLength(0)
  })

  it('stays hidden with no timer or no line', () => {
    expect(bubbleRects({ chatTimer: 0, chatLine: 'ship it' })).toHaveLength(0)
    expect(bubbleRects({ chatTimer: 40, chatLine: '' })).toHaveLength(0)
  })

  it('widens with the length of the line', () => {
    const short = bubbleRects({ chatTimer: 40, chatLine: 'hi' })[0]
    const long = bubbleRects({ chatTimer: 40, chatLine: 'is it in prod?' })[0]
    expect(long.w).toBeGreaterThan(short.w)
  })

  it('lifts the bubble by 20px when bubbleUp avoids an overlap', () => {
    const low = bubbleRects({ chatTimer: 40, chatLine: 'hi' })[0]
    const high = bubbleRects({ chatTimer: 40, chatLine: 'hi', bubbleUp: true })[0]
    expect(low.y - high.y).toBe(20)
  })

  it('renders from behind as well as from the side', () => {
    expect(bubbleRects({ facing: 'back', chatTimer: 40, chatLine: 'hi' })).toHaveLength(1)
  })
})

/* ────────────────────────── Particles ────────────────────────── */

describe('particles', () => {
  it('spawns the requested count with the default drift', () => {
    vi.spyOn(Math, 'random').mockReturnValue(0.5)
    const pool: Particle[] = []
    spawnParticles(pool, 10, 20, '#fff', 3)
    expect(pool).toHaveLength(3)
    expect(pool[0]).toMatchObject({
      x: 10, y: 20, vx: 0, vy: -0.15, life: 0, maxLife: 100, color: '#fff', size: 1,
    })
  })

  it('honours explicit velocity, spread, lifetime and size', () => {
    vi.spyOn(Math, 'random').mockReturnValue(0.5)
    const pool: Particle[] = []
    spawnParticles(pool, 0, 0, '#0f0', 2, { vx: 1, vy: -2, spread: 4, maxLife: 50, size: 3 })
    expect(pool).toHaveLength(2)
    expect(pool[0]).toMatchObject({ vx: 1, vy: -2, maxLife: 50, size: 3 })
  })

  it('appends to an existing pool rather than replacing it', () => {
    vi.spyOn(Math, 'random').mockReturnValue(0.5)
    const pool: Particle[] = []
    spawnParticles(pool, 0, 0, '#fff', 2)
    spawnParticles(pool, 5, 5, '#f00', 1)
    expect(pool).toHaveLength(3)
    expect(pool[2].color).toBe('#f00')
  })

  it('spreads velocity across the requested range', () => {
    const seq = [0, 0, 0, 1, 1, 1]
    let i = 0
    vi.spyOn(Math, 'random').mockImplementation(() => seq[i++ % seq.length])
    const pool: Particle[] = []
    spawnParticles(pool, 0, 0, '#fff', 2, { spread: 2 })
    expect(pool[0].vx).toBeLessThan(pool[1].vx)
  })

  it('advances position and age on update', () => {
    vi.spyOn(Math, 'random').mockReturnValue(0.5)
    const pool: Particle[] = [
      { x: 0, y: 0, vx: 2, vy: -1, life: 0, maxLife: 10, color: '#fff', size: 1 },
    ]
    const next = updateParticles(pool)
    expect(next[0]).toMatchObject({ x: 2, y: -1, life: 1 })
  })

  it('drops particles that reached their lifetime', () => {
    vi.spyOn(Math, 'random').mockReturnValue(0.5)
    const pool: Particle[] = [
      { x: 0, y: 0, vx: 0, vy: 0, life: 9, maxLife: 10, color: '#a', size: 1 },
      { x: 0, y: 0, vx: 0, vy: 0, life: 0, maxLife: 10, color: '#b', size: 1 },
    ]
    const next = updateParticles(pool)
    expect(next.map(p => p.color)).toEqual(['#b'])
  })

  it('returns an empty array for an empty pool', () => {
    expect(updateParticles([])).toEqual([])
  })

  it('fades particles out and restores full opacity afterwards', () => {
    const alphas: number[] = []
    const rects: number[][] = []
    const ctx = {
      fillStyle: '',
      globalAlpha: 1,
      fillRect: (x: number, y: number, w: number, h: number) => {
        alphas.push(ctx.globalAlpha)
        rects.push([x, y, w, h])
      },
    }
    const pool: Particle[] = [
      { x: 1, y: 2, vx: 0, vy: 0, life: 0, maxLife: 10, color: '#fff', size: 2 },
      { x: 0, y: 0, vx: 0, vy: 0, life: 10, maxLife: 10, color: '#fff', size: 1 },
    ]
    drawParticles(() => {}, ctx as unknown as CanvasRenderingContext2D, pool)
    // Fresh particle is at half alpha; a spent one is clamped to zero.
    expect(alphas[0]).toBeCloseTo(0.5)
    expect(alphas[1]).toBe(0)
    // Coordinates are multiplied by the scene scale.
    expect(rects[0]).toEqual([1 * S, 2 * S, 2 * S, 2 * S])
    expect(ctx.globalAlpha).toBe(1)
  })
})

/* ────────────────────────── Waypoint navigation ────────────────────────── */

describe('destinations', () => {
  it('keeps every break-area destination on the right or far left of the floor', () => {
    expect(Object.keys(DESTINATIONS).sort()).toEqual(['coffee', 'trash', 'vending', 'water'])
    for (const p of Object.values(DESTINATIONS)) {
      expect(p.x).toBeGreaterThanOrEqual(0)
      expect(p.y).toBeGreaterThan(0)
    }
  })
})

describe('buildEntryPath', () => {
  it('routes a top-row desk down the left aisle and along the corridor', () => {
    expect(buildEntryPath(0)).toEqual([
      { x: 30, y: 228 }, { x: 81, y: 228 }, { x: 81, y: 200 },
    ])
  })

  it('routes a back-row desk behind the desks instead of the corridor', () => {
    expect(buildEntryPath(4)).toEqual([
      { x: 30, y: 290 }, { x: 81, y: 290 }, { x: 81, y: 276 },
    ])
  })

  it('always ends at the chair of the requested station', () => {
    for (let i = 0; i < MAX_STATIONS; i++) {
      const path = buildEntryPath(i)
      expect(path[path.length - 1]).toEqual({
        x: STATION_POSITIONS[i].x + 21, y: STATION_POSITIONS[i].y + 20,
      })
    }
  })
})

describe('buildExitPath', () => {
  it('steps out to the corridor, then the aisle, then the door', () => {
    expect(buildExitPath(0, 120)).toEqual([
      { x: 120, y: 228 }, { x: 30, y: 228 }, { x: DOOR.x + 6, y: DOOR.y + 20 },
    ])
  })

  it('uses the rear lane for back-row desks', () => {
    expect(buildExitPath(7, 300)[0]).toEqual({ x: 300, y: 290 })
  })

  it('always finishes at the door', () => {
    for (let i = 0; i < MAX_STATIONS; i++) {
      const path = buildExitPath(i, 200)
      expect(path[path.length - 1]).toEqual({ x: DOOR.x + 6, y: DOOR.y + 20 })
    }
  })
})

describe('buildPath', () => {
  it('walks the right aisle and changes level for the coffee machine', () => {
    expect(buildPath(0, DESTINATIONS.coffee)).toEqual([
      { x: 81, y: 228 }, { x: 340, y: 228 }, { x: 340, y: 270 }, { x: 390, y: 270 },
    ])
  })

  it('skips the aisle leg when the destination is already on the lane', () => {
    // The trash can sits on the rear lane, so no vertical walk is needed.
    expect(buildPath(4, DESTINATIONS.trash)).toEqual([
      { x: 81, y: 290 }, { x: 30, y: 290 }, { x: 25, y: 290 },
    ])
  })

  it('picks the left aisle for a destination on the left half', () => {
    expect(buildPath(0, DESTINATIONS.trash)[1]).toEqual({ x: 30, y: 228 })
  })

  it('always ends on the destination', () => {
    for (const dest of Object.values(DESTINATIONS)) {
      const path = buildPath(2, dest)
      expect(path[path.length - 1]).toEqual({ x: dest.x, y: dest.y })
    }
  })
})

describe('buildReturnPath', () => {
  it('comes back down the right aisle from the break area', () => {
    expect(buildReturnPath(0, 390, 270)).toEqual([
      { x: 340, y: 270 }, { x: 340, y: 228 }, { x: 81, y: 228 }, { x: 81, y: 200 },
    ])
  })

  it('comes back down the left aisle from the near side of the floor', () => {
    expect(buildReturnPath(4, 25, 290)).toEqual([
      { x: 30, y: 290 }, { x: 30, y: 290 }, { x: 81, y: 290 }, { x: 81, y: 276 },
    ])
  })

  it('always ends at the chair', () => {
    for (let i = 0; i < MAX_STATIONS; i++) {
      const path = buildReturnPath(i, 400, 190)
      expect(path[path.length - 1]).toEqual({
        x: STATION_POSITIONS[i].x + 21, y: STATION_POSITIONS[i].y + 20,
      })
    }
  })
})

/* ────────────────────────── Level ladder ────────────────────────── */

describe('getLevel', () => {
  it('starts at level 1 for a brand new agent', () => {
    expect(getLevel(0)).toEqual({ level: 1, title: 'Intern' })
  })

  it('never drops below level 1 even for a negative count', () => {
    expect(getLevel(-5)).toEqual({ level: 1, title: 'Intern' })
  })

  it.each([
    [10, 1, 'Intern'],
    [11, 2, 'Prompt Monkey'],
    [31, 3, 'Token Burner'],
    [81, 4, 'Hallucination Specialist'],
    [201, 5, 'Senior Gaslighter'],
    [401, 6, 'Chief Yapper'],
    [801, 7, 'Distinguished Delulu'],
    [1201, 8, 'VP of Vibes'],
    [1601, 9, 'Sentience Candidate'],
    [2001, 10, 'AGI'],
  ])('maps %i messages to level %i (%s)', (msgs, level, title) => {
    expect(getLevel(msgs)).toEqual({ level, title })
  })

  it('caps at the top rung however many messages accumulate', () => {
    expect(getLevel(1_000_000)).toEqual({ level: 10, title: 'AGI' })
  })

  it('is monotonic across the whole ladder', () => {
    let prev = 0
    for (let m = 0; m <= 2100; m += 7) {
      const { level } = getLevel(m)
      expect(level).toBeGreaterThanOrEqual(prev)
      prev = level
    }
  })
})

/* ────────────────────────── Conversation tables ────────────────────────── */

describe('conversation tables', () => {
  it('pairs every desk line with a reply', () => {
    expect(DESK_CONVOS.length).toBeGreaterThan(0)
    for (const pair of DESK_CONVOS) {
      expect(pair).toHaveLength(2)
      expect(pair[0].length).toBeGreaterThan(0)
      expect(pair[1].length).toBeGreaterThan(0)
    }
  })

  it('pairs every break-room line with a reply', () => {
    expect(BREAK_CONVOS.length).toBeGreaterThan(0)
    for (const pair of BREAK_CONVOS) {
      expect(pair).toHaveLength(2)
    }
  })

  it('keeps every line short enough for a speech bubble on the 480px stage', () => {
    for (const [a, b] of [...DESK_CONVOS, ...BREAK_CONVOS]) {
      // Bubble width is line length * 4 + 4 pixels.
      expect(a.length * 4 + 4).toBeLessThan(W)
      expect(b.length * 4 + 4).toBeLessThan(W)
    }
  })
})
