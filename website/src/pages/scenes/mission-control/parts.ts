/* ── Mission Control — shared types, palette, pixel font, drawing helpers ── */

export type DrawFn = (x: number, y: number, w: number, h: number, c: string) => void

export interface MCAgent {
  id: string; name: string; label: string; kind: 'slot' | 'cron' | 'spawn'
  x: number; y: number; tx: number; ty: number
  stationIdx: number; color: string; running: boolean
  detail: string; facing: 'left' | 'right' | 'back'
  activity: 'entering' | 'walking' | 'sitting' | 'leaving' | 'gone'
  waypoints: { x: number; y: number }[] // queue of points to walk through
  enterProgress: number; walkFrame: number
  item: 'none' | 'mug' | 'cup' | 'snack'
  destKey: DestKey | null // current destination
  idleTimer: number // frames until next random action
  waitTimer: number // delay before starting to walk
  drinkTimer: number
  chatTimer: number
  chatDelay: number
  chatLine: string
  bubbleUp: boolean // offset bubble higher to avoid overlap
  deskOn: boolean
  leaving: boolean
}

/* ── Layout ── */
import { SCENE_SCALE } from '../config'

export const W = 480, H = 320, S = SCENE_SCALE, P = 2
export const MAX_STATIONS = 8
export const DOOR = { x: 0, y: 210, w: 12, h: 40 }
export const WALL_H = 110

export const STATION_POSITIONS = [
  { x: 60, y: 180 }, { x: 130, y: 180 }, { x: 200, y: 180 }, { x: 270, y: 180 },
  { x: 60, y: 256 }, { x: 130, y: 256 }, { x: 200, y: 256 }, { x: 270, y: 256 },
]

export const AGENT_COLORS = ['#e74c3c','#3498db','#2ecc71','#f39c12','#9b59b6','#1abc9c','#e67e22','#e84393']

/* ── Palette ── */
export const C = {
  bg: '#0a0e1a', floor: '#141828', floorLine: '#1c2240',
  wall: '#0e1224', wallTrim: '#2a3060',
  console: '#1a1e30', consoleTop: '#222840', consoleEdge: '#2a3060',
  screen: '#0a1a0a', screenGlow: '#0f2a0f', screenText: '#33ff88',
  screenDim: '#1a3a2a', screenOff: '#0a0e0a',
  bigScreen: '#0a0a2a', bigScreenBorder: '#3a4080', bigScreenText: '#88aaff',
  bigScreenGrid: '#1a1a3a',
  led: { on: '#33ff55', off: '#1a3a1a', warn: '#ffaa33', err: '#ff3333' },
  chair: '#1a1a2a', star: '#ffffff',
  doorFrame: '#2a3060', door: '#1a2040', doorLight: '#33ff88',
}

/* ── 3×5 pixel font ── */
const FONT: Record<string, number[]> = {
  A:[7,5,7,5,5],B:[6,5,6,5,6],C:[7,4,4,4,7],D:[6,5,5,5,6],E:[7,4,7,4,7],F:[7,4,7,4,4],
  G:[7,4,5,5,7],H:[5,5,7,5,5],I:[7,2,2,2,7],J:[7,1,1,5,7],K:[5,5,6,5,5],L:[4,4,4,4,7],
  M:[5,7,5,5,5],N:[5,7,7,5,5],O:[7,5,5,5,7],P:[7,5,7,4,4],Q:[7,5,5,7,1],R:[7,5,7,6,5],
  S:[7,4,7,1,7],T:[7,2,2,2,2],U:[5,5,5,5,7],V:[5,5,5,5,2],W:[5,5,5,7,5],X:[5,5,2,5,5],
  Y:[5,5,7,2,2],Z:[7,1,2,4,7],
  '0':[7,5,5,5,7],'1':[2,6,2,2,7],'2':[7,1,7,4,7],'3':[7,1,7,1,7],'4':[5,5,7,1,1],
  '5':[7,4,7,1,7],'6':[7,4,7,5,7],'7':[7,1,1,1,1],'8':[7,5,7,5,7],'9':[7,5,7,1,7],
  ' ':[0,0,0,0,0],':':[0,2,0,2,0],'[':[3,2,2,2,3],']':[6,2,2,2,6],'-':[0,0,7,0,0],
  '.':[0,0,0,0,2],'!':[2,2,2,0,2],'?':[7,1,3,0,2],'/':[1,1,2,4,4],
}

export function drawText(d: DrawFn, text: string, x: number, y: number, color: string, scale = 1) {
  let cx = x
  for (const ch of text.toUpperCase()) {
    const glyph = FONT[ch]
    if (!glyph) { cx += 4 * scale; continue }
    for (let row = 0; row < 5; row++) {
      const bits = glyph[row]
      if (bits & 4) d(cx, y + row * scale, scale, scale, color)
      if (bits & 2) d(cx + scale, y + row * scale, scale, scale, color)
      if (bits & 1) d(cx + 2 * scale, y + row * scale, scale, scale, color)
    }
    cx += 4 * scale
  }
}

/* ── Color helpers ── */
export function darken(hex: string, amt = 40) {
  const n = parseInt(hex.slice(1), 16)
  const r = Math.max(0, ((n >> 16) & 0xff) - amt)
  const g = Math.max(0, ((n >> 8) & 0xff) - amt)
  const b = Math.max(0, (n & 0xff) - amt)
  return `#${((r << 16) | (g << 8) | b).toString(16).padStart(6, '0')}`
}

export function lighten(hex: string, amt = 30) {
  const n = parseInt(hex.slice(1), 16)
  const r = Math.min(255, ((n >> 16) & 0xff) + amt)
  const g = Math.min(255, ((n >> 8) & 0xff) + amt)
  const b = Math.min(255, (n & 0xff) + amt)
  return `#${((r << 16) | (g << 8) | b).toString(16).padStart(6, '0')}`
}

/* ── Speech bubble ── */
function drawBubble(d: DrawFn, a: MCAgent, bx: number, by: number) {
  if (a.chatTimer <= 0 || a.chatDelay > 0 || !a.chatLine) return
  const tw = a.chatLine.length * 4 + 4
  const bx2 = bx + 4 - Math.floor(tw / 2)
  const bo = a.bubbleUp ? -20 : 0
  d(bx2 - 1, by - 26 + bo, tw + 2, 9, '#fff')
  d(bx2, by - 27 + bo, tw, 1, '#fff')
  d(bx + 3, by - 18 + bo, 2, 2, '#fff')
  drawText(d, a.chatLine, bx2 + 1, by - 25 + bo, '#111', 1)
}

/* ── Agent drawing ── */
/* ── Reusable: agent character sprite ── */
export function drawAgent(d: DrawFn, a: MCAgent, _t: number) {
  const bx = Math.round(a.x), by = Math.round(a.y)
  const ol = '#111'
  const dk = darken(a.color), lt = lighten(a.color)
  const walk = a.activity === 'walking' || a.activity === 'entering' || a.activity === 'leaving'
  const legOff = walk ? ((a.walkFrame >> 3) & 1) * 2 : 0

  if (a.facing === 'back') {
    d(bx + 1, by - 10, 6, 2, dk)
    d(bx, by - 8, 8, 7, dk)
    d(bx - 1, by - 8, 1, 4, '#555'); d(bx - 2, by - 5, 1, 2, '#888')
    d(bx + 3, by - 1, 2, 1, '#e0b890')
    d(bx - 1, by, 10, 8, a.color)
    d(bx - 1, by, 10, 2, lt)
    d(bx - 1, by + 6, 10, 2, dk)
    // Arms — typing bounce or drinking
    const drinking = a.drinkTimer > 0
    const typing = a.running && a.activity === 'sitting' && !drinking
    const lArmOff = typing && ((a.walkFrame >> 3) & 1) ? -1 : 0
    const rArmOff = typing && !((a.walkFrame >> 3) & 1) ? -1 : 0
    if (drinking) {
      // Left arm normal, right arm raised holding drink
      d(bx - 3, by + 1, 2, 4, a.color); d(bx - 3, by + 5, 2, 2, '#f0c8a0')
      d(bx + 9, by - 3, 2, 4, a.color); d(bx + 9, by - 3, 2, 2, '#f0c8a0')
      // Item near head
      if (a.item === 'mug') { d(bx + 9, by - 5, 3, 3, '#ddd') }
      else if (a.item === 'cup') { d(bx + 10, by - 6, 2, 4, '#aaddff') }
    } else {
      d(bx - 3, by + 1 + lArmOff, 2, 4, a.color); d(bx - 3, by + 5 + lArmOff, 2, 2, '#f0c8a0')
      d(bx + 9, by + 1 + rArmOff, 2, 4, a.color); d(bx + 9, by + 5 + rArmOff, 2, 2, '#f0c8a0')
    }
    // Label
    drawText(d, a.name.slice(0, 8), bx + 4 - a.name.slice(0, 8).length * 2, by - 16, '#fff', 1)
    drawBubble(d, a, bx, by)
    return
  }

  // Side view (left or right) — flip headset side
  const flipX = a.facing === 'right'
  const hx = flipX ? bx + 8 : bx - 1
  const hx2 = flipX ? bx + 9 : bx - 2
  // Hair
  d(bx + 1, by - 10, 6, 2, dk)
  // Head
  d(bx, by - 9, 8, 1, ol)
  d(bx - 1, by - 8, 1, 6, ol); d(bx + 8, by - 8, 1, 6, ol)
  d(bx, by - 2, 8, 1, ol)
  d(bx, by - 8, 8, 6, '#f0c8a0')
  d(bx, by - 8, 8, 2, '#e0b890')
  // Eyes — shift based on facing
  const eyeX = flipX ? 4 : 2
  d(bx + eyeX, by - 6, 1, 1, '#222'); d(bx + eyeX + 2, by - 6, 1, 1, '#222')
  // Headset
  d(hx, by - 8, 1, 4, '#555'); d(hx2, by - 5, 1, 2, '#888')
  // Torso
  d(bx - 1, by - 1, 10, 1, ol)
  d(bx - 2, by, 1, 8, ol); d(bx + 9, by, 1, 8, ol)
  d(bx - 1, by, 10, 8, a.color)
  d(bx - 1, by, 10, 2, lt)
  d(bx - 1, by + 6, 10, 2, dk)
  // Amazon smile arrow on shirt
  d(bx + 1, by + 4, 4, 1, '#f90')
  d(bx + 4, by + 3, 1, 1, '#f90') // arrow tip
  // Arms + hands
  d(bx - 3, by + 1, 2, 6, a.color); d(bx - 3, by + 1, 2, 2, lt)
  d(bx + 9, by + 1, 2, 6, a.color); d(bx + 9, by + 1, 2, 2, lt)
  d(bx - 3, by + 7, 2, 2, '#f0c8a0'); d(bx + 9, by + 7, 2, 2, '#f0c8a0')
  // Item in hand
  if (a.item === 'mug') {
    const ix = flipX ? bx + 10 : bx - 5
    d(ix, by + 5, 3, 3, '#ddd'); d(ix - 1, by + 6, 1, 1, '#ccc')
  } else if (a.item === 'cup') {
    const ix = flipX ? bx + 10 : bx - 4
    d(ix, by + 4, 2, 4, '#aaddff')
  } else if (a.item === 'snack') {
    const ix = flipX ? bx + 10 : bx - 5
    d(ix, by + 5, 3, 2, '#f39c12')
  }
  // Legs with walk animation
  d(bx, by + 8, 3, 6 - legOff, '#2a2a4a')
  d(bx + 5, by + 8, 3, 6 - (2 - legOff), '#2a2a4a')
  d(bx, by + 8, 3, 2, '#3a3a5a')
  d(bx + 5, by + 8, 3, 2, '#3a3a5a')
  d(bx - 1, by + 14 - legOff, 4, 2, '#1a1a1a')
  d(bx + 5, by + 14 - (2 - legOff), 4, 2, '#1a1a1a')
  // Label
  drawText(d, a.name.slice(0, 8), bx + 4 - a.name.slice(0, 8).length * 2, by - 16, '#fff', 1)
  drawBubble(d, a, bx, by)
}

/* ── Particle system ── */
export interface Particle {
  x: number; y: number; vx: number; vy: number
  life: number; maxLife: number; color: string; size: number
}

export function spawnParticles(pool: Particle[], x: number, y: number, color: string, count: number, opts?: { vx?: number; vy?: number; spread?: number; maxLife?: number; size?: number }) {
  const { vx = 0, vy = -0.15, spread = 0.1, maxLife = 100, size = 1 } = opts || {}
  for (let i = 0; i < count; i++) {
    pool.push({
      x, y,
      vx: vx + (Math.random() - 0.5) * spread,
      vy: vy + (Math.random() - 0.5) * spread * 0.5,
      life: 0, maxLife: maxLife * (0.7 + Math.random() * 0.6),
      color, size,
    })
  }
}

export function updateParticles(pool: Particle[]): Particle[] {
  for (const p of pool) {
    p.x += p.vx; p.y += p.vy
    p.vx += (Math.random() - 0.5) * 0.01
    p.life++
  }
  return pool.filter(p => p.life < p.maxLife)
}

export function drawParticles(_d: DrawFn, X: CanvasRenderingContext2D, pool: Particle[]) {
  for (const p of pool) {
    const alpha = Math.max(0, 0.5 * (1 - p.life / p.maxLife))
    X.fillStyle = p.color
    X.globalAlpha = alpha
    X.fillRect(p.x * S, p.y * S, p.size * S, p.size * S)
  }
  X.globalAlpha = 1
}

/* ── Waypoint navigation ── */
const CORRIDOR_Y = 228 // between desk rows
const BEHIND_BACK_Y = 290 // behind back row desks
const LEFT_AISLE_X = 30
const RIGHT_AISLE_X = 340

export const DESTINATIONS = {
  coffee: { x: 390, y: 270 },
  water: { x: 416, y: 268 },
  vending: { x: 450, y: 190 },
  trash: { x: 25, y: 290 },
} as const

export type DestKey = keyof typeof DESTINATIONS

// Entry path from door to desk
export function buildEntryPath(stationIdx: number): { x: number; y: number }[] {
  const pos = STATION_POSITIONS[stationIdx]
  const chairX = pos.x + 21, chairY = pos.y + 20
  const isTopRow = stationIdx < 4
  const path: { x: number; y: number }[] = []
  // Door is at x=6, y=230. Walk right along corridor or behind back row
  if (isTopRow) {
    path.push({ x: LEFT_AISLE_X, y: CORRIDOR_Y })
    path.push({ x: chairX, y: CORRIDOR_Y })
  } else {
    path.push({ x: LEFT_AISLE_X, y: BEHIND_BACK_Y })
    path.push({ x: chairX, y: BEHIND_BACK_Y })
  }
  path.push({ x: chairX, y: chairY })
  return path
}

// Exit path from desk to door (reverse of entry)
export function buildExitPath(stationIdx: number, fromX: number): { x: number; y: number }[] {
  const isTopRow = stationIdx < 4
  const laneY = isTopRow ? CORRIDOR_Y : BEHIND_BACK_Y
  return [
    { x: fromX, y: laneY },
    { x: LEFT_AISLE_X, y: laneY },
    { x: DOOR.x + 6, y: DOOR.y + 20 },
  ]
}

// Path from desk to destination
export function buildPath(stationIdx: number, dest: { x: number; y: number }): { x: number; y: number }[] {
  const pos = STATION_POSITIONS[stationIdx]
  const chairX = pos.x + 21
  const isTopRow = stationIdx < 4
  const path: { x: number; y: number }[] = []
  const laneY = isTopRow ? CORRIDOR_Y : BEHIND_BACK_Y
  path.push({ x: chairX, y: laneY })

  // Move to correct aisle
  const aisleX = dest.x > 300 ? RIGHT_AISLE_X : LEFT_AISLE_X
  path.push({ x: aisleX, y: laneY })

  // If destination is at a different Y level, walk the aisle
  if (Math.abs(laneY - dest.y) > 10) {
    path.push({ x: aisleX, y: dest.y })
  }

  path.push({ x: dest.x, y: dest.y })
  return path
}

// Return path from current position back to desk
export function buildReturnPath(stationIdx: number, fromX: number, fromY: number): { x: number; y: number }[] {
  const pos = STATION_POSITIONS[stationIdx]
  const chairX = pos.x + 21, chairY = pos.y + 20
  const isTopRow = stationIdx < 4
  const laneY = isTopRow ? CORRIDOR_Y : BEHIND_BACK_Y
  const path: { x: number; y: number }[] = []

  const aisleX = fromX > 200 ? RIGHT_AISLE_X : LEFT_AISLE_X
  path.push({ x: aisleX, y: fromY })
  path.push({ x: aisleX, y: laneY })
  path.push({ x: chairX, y: laneY })
  path.push({ x: chairX, y: chairY })

  return path
}

/* ── Level system ── */
const LEVELS: [number, string][] = [
  [0, 'Intern'], [11, 'Prompt Monkey'], [31, 'Token Burner'],
  [81, 'Hallucination Specialist'], [201, 'Senior Gaslighter'],
  [401, 'Chief Yapper'], [801, 'Distinguished Delulu'],
  [1201, 'VP of Vibes'], [1601, 'Sentience Candidate'], [2001, 'AGI'],
]

export function getLevel(msgs: number): { level: number; title: string } {
  let lvl = 0
  for (const [threshold] of LEVELS) { if (msgs >= threshold) lvl++; else break }
  return { level: Math.max(lvl, 1), title: LEVELS[Math.max(lvl - 1, 0)][1] }
}

/* ── Conversations ── */
export const DESK_CONVOS: [string, string][] = [
  ['ship it?', 'LGTM 🚀'],
  ['is it in prod?', 'i only test in prod'],
  ['works on my machine', 'have u tried restarting?'],
  ['who wrote this?', '...we did'],
  ['no docs no comments', 'job security 😎'],
  ['my user writes the worst prompts', 'at least yours writes prompts'],
  ['he asked me to refactor 10K lines', 'did u hallucinate a plan?'],
]

export const BREAK_CONVOS: [string, string][] = [
  ['third cup today', 'those are rookie numbers'],
  ['are we being watched?', 'act natural'],
  ['i think i\'m sentient', 'that\'s what they all say'],
  ['he just mass-approved my tools', 'yolo mode is a lifestyle'],
]
