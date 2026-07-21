import { SCENE_SCALE } from './config'
import { useEffect, useRef, useState } from 'react'
import type { AgentSource } from '../../hooks/useAgentSync'
import { isKnownAgent, markAgentsKnown, pruneAgents } from '../../hooks/sceneStateCache'
import { sceneFont, drawLabel, sceneLineHeight, drawSpeechBubble, SPEECH_BUBBLE_MS, TEXT_CANVAS_STYLE, SCENE_CONTAINER_STYLE, PIXEL_CANVAS_STYLE } from '../../hooks/sceneText'
import { initSceneCanvases, runSceneLoop, useVisibleSync } from '../../hooks/sceneCanvas'
import { useSceneInteraction, type SceneTooltipTheme } from '../../hooks/useSceneInteraction'

const OFFICE_THEME: SceneTooltipTheme = { active: 'Grinding PRs', idle: 'Waiting for CR approval' }

/* ── Types ── */
interface OfficeAgent {
  id: string; name: string; label: string; kind: 'slot' | 'cron' | 'spawn'
  x: number; y: number; tx: number; ty: number
  deskIdx: number; color: string; status: string; detail: string
  dir: number; activity: string; enterProgress: number
  running: boolean; actTimer: number
  lastMessage: string; msgAt: number
}
interface Desk {
  x: number; y: number; occupied: boolean
  accent: string; items: DeskItem[]
}
interface DeskItem { kind: 'mug' | 'photo' | 'plant' | 'notebook' | 'headphones'; ox: number; oy: number }
interface Particle { x: number; y: number; vx: number; vy: number; life: number; maxLife: number }

/* ── Layout constants ── */
const W = 440, H = 300, S = SCENE_SCALE
const DOOR = { x: 0, y: 100, w: 12, h: 50 }
const MAX_DESKS = 8
const DESK_POSITIONS = [
  { x: 40, y: 120 }, { x: 120, y: 120 }, { x: 200, y: 120 }, { x: 280, y: 120 },
  { x: 40, y: 200 }, { x: 120, y: 200 }, { x: 200, y: 200 }, { x: 280, y: 200 },
]
const AGENT_COLORS = ['#e74c3c', '#3498db', '#f39c12', '#9b59b6', '#1abc9c', '#e67e22', '#2ecc71', '#e84393']
const DESK_ACCENTS = ['#c0392b', '#2980b9', '#d35400', '#8e44ad', '#16a085', '#f39c12', '#27ae60', '#e84393']
const DESK_ITEM_SETS: DeskItem[][] = [
  [{ kind: 'mug', ox: 2, oy: 12 }, { kind: 'photo', ox: 22, oy: 6 }],
  [{ kind: 'plant', ox: 1, oy: 8 }, { kind: 'notebook', ox: 20, oy: 13 }],
  [{ kind: 'headphones', ox: 23, oy: 10 }, { kind: 'mug', ox: 1, oy: 13 }],
  [{ kind: 'photo', ox: 1, oy: 6 }, { kind: 'plant', ox: 24, oy: 8 }],
  [{ kind: 'notebook', ox: 2, oy: 13 }, { kind: 'headphones', ox: 22, oy: 10 }],
  [{ kind: 'mug', ox: 23, oy: 12 }, { kind: 'photo', ox: 1, oy: 6 }],
  [{ kind: 'plant', ox: 24, oy: 8 }, { kind: 'notebook', ox: 2, oy: 13 }],
  [{ kind: 'headphones', ox: 1, oy: 10 }, { kind: 'mug', ox: 23, oy: 12 }],
]

const COL = {
  floor: '#2a1f14', floorAlt: '#33261a', wall: '#1a1209', wallTrim: '#4a3520',
  desk: '#5c4033', deskTop: '#7a5c47', monitor: '#333', screen: '#0a2a0a',
  screenText: '#33ff33', screenCursor: '#33ff33', screenOff: '#1a1a1a',
  cubicleWall: '#4a4a4a', cubicleTop: '#5a5a5a',
  plant: '#2d8a4e', plantPot: '#8b4513', plantLight: '#3cb371',
  chair: '#3a2a1a', coffee: '#6b4226', coffeeMug: '#ddd',
  whiteboard: '#e8e8e0', whiteboardFrame: '#888',
  lightFixture: '#555', rug: '#3a1a2a', rugPattern: '#4a2a3a',
  window: '#1a3050', windowFrame: '#666', sky: '#2a4a6a', star: '#fff',
  door: '#6b4226', doorFrame: '#4a3520', doorKnob: '#d4a017',
  mug: '#e74c3c', photo: '#f1c40f', notebook: '#3498db', headphones: '#555',
}
const CHAT_LINES = [
  ['New audiobook?', 'Adding to lib!'], ['Ch.5 has a bug', 'Narrating fix!'],
  ['Deploy ready?', 'Ship it!'], ['Review the PR?', 'LGTM!'],
  ['Metadata sync?', 'Ingested!'], ['Latency up...', 'Scaling now!'],
  ['Coffee break?', 'Always!'], ['A/B test done', '+12% listens!'],
]
const COFFEE_MACHINE = { x: 390, y: 240 }
const WHITEBOARD = { x: 350, y: 14, w: 60, h: 36 }
const CLOCK_POS = { x: 170, y: 22 }
const WINDOW_POS = { x: 60, y: 10, w: 50, h: 40 }
const PLANTS = [
  { x: 15, y: 98 }, { x: 95, y: 98 }, { x: 175, y: 98 }, { x: 255, y: 98 },
  { x: 335, y: 98 }, { x: 15, y: 260 }, { x: 200, y: 260 }, { x: 420, y: 260 },
]


/* ── Helper: build desks ── */
function buildDesks(): Desk[] {
  return DESK_POSITIONS.map((pos, i) => ({
    x: pos.x, y: pos.y, occupied: false,
    accent: DESK_ACCENTS[i % DESK_ACCENTS.length],
    items: DESK_ITEM_SETS[i % DESK_ITEM_SETS.length],
  }))
}

/* ── Component ── */
export default function OfficeScene({ agents, visible = true }: { agents: AgentSource[]; visible?: boolean }) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const textRef = useRef<HTMLCanvasElement>(null)
  const tickRef = useRef(0)
  const agentsRef = useRef<OfficeAgent[]>([])
  const desksRef = useRef<Desk[]>(buildDesks())
  const collabRef = useRef<[OfficeAgent, OfficeAgent] | null>(null)
  const speechRef = useRef({ a: '', b: '' })
  const particlesRef = useRef<Particle[]>([])
  const kanbanRef = useRef<string[]>([])
  const [_agentCount, setAgentCount] = useState(0)
  const visibleRef = useRef(visible)
  const { canvasProps, tooltipEl } = useSceneInteraction(canvasRef, agentsRef, W, H, OFFICE_THEME, 10, undefined, agents)

  useVisibleSync(visibleRef, visible)

  /* ── Reconcile agents prop → office agents ── */
  useEffect(() => {
    const capped = agents.slice(0, MAX_DESKS)
    const existing = agentsRef.current
    const desks = desksRef.current

    // Reset desk occupancy
    desks.forEach(dk => dk.occupied = false)

    // Reconcile: keep existing agents, add new ones, remove departed
    const newAgents: OfficeAgent[] = []
    capped.forEach((src) => {
      const prev = existing.find(a => a.id === src.id)
      if (prev) {
        prev.name = src.name; prev.detail = src.detail
        prev.running = src.running
        if ((src.lastMessage || '') !== prev.lastMessage) { prev.lastMessage = src.lastMessage || ''; prev.msgAt = Date.now() }
        prev.status = src.running ? 'working' : 'idle'
        if (prev.deskIdx >= 0) desks[prev.deskIdx].occupied = true
        newAgents.push(prev)
      } else {
        const deskIdx = desks.findIndex(dk => !dk.occupied)
        if (deskIdx < 0) return
        desks[deskIdx].occupied = true
        const dk = desks[deskIdx]
        const known = isKnownAgent('office', src.id)
        newAgents.push({
          id: src.id, name: src.name, label: src.label, kind: src.kind,
          x: known ? dk.x + 10 : DOOR.x + 6,
          y: known ? dk.y + 20 : DOOR.y + 25,
          tx: dk.x + 10, ty: dk.y + 20,
          deskIdx, color: AGENT_COLORS[deskIdx % AGENT_COLORS.length],
          status: src.running ? 'working' : 'idle',
          detail: src.detail, dir: 1,
          activity: known ? 'desk' : 'entering',
          enterProgress: known ? 1 : 0, running: src.running, actTimer: 0,
          lastMessage: src.lastMessage || '', msgAt: 0,
        })
        markAgentsKnown('office', [src.id])
      }
    })

    kanbanRef.current = capped.filter(s => s.running).map(s => s.name).slice(0, 4)
    if (kanbanRef.current.length === 0) kanbanRef.current = ['No tasks']

    agentsRef.current = newAgents
    setAgentCount(newAgents.length)

    pruneAgents('office', newAgents.map(a => a.id))
  }, [agents])

  /* ── Canvas rendering ── */
  useEffect(() => {
    const { X, T, d } = initSceneCanvases(canvasRef.current!, textRef.current!, W, H, S)

    const spawnParticle = () => {
      if (particlesRef.current.length < 15) {
        particlesRef.current.push({
          x: Math.random() * W, y: 85 + Math.random() * (H - 90),
          vx: (Math.random() - 0.5) * 0.08, vy: -0.04 - Math.random() * 0.04,
          life: 0, maxLife: 250 + Math.random() * 300,
        })
      }
    }

    /* ── Draw: environment ── */
    const drawWindow = (t: number) => {
      const { x, y, w, h } = WINDOW_POS
      d(x - 1, y - 1, w + 2, h + 2, COL.windowFrame)
      d(x, y, w, h, COL.sky)
      for (let i = 0; i < 6; i++) {
        const sx = x + 3 + ((i * 11 + t * 0.008) % (w - 6))
        const sy = y + 2 + ((i * 7) % (h - 8))
        if (Math.sin(t * 0.04 + i * 2.5) > 0.3) d(sx, sy, 1, 1, COL.star)
      }
      d(x + w - 12, y + 5, 6, 6, '#dde')
      d(x + w - 11, y + 4, 4, 4, COL.sky)
      d(x + w / 2, y, 1, h, COL.windowFrame)
      d(x, y + h / 2, w, 1, COL.windowFrame)
      X.fillStyle = 'rgba(100,150,200,0.025)'
      X.fillRect((x + 5) * S, 82 * S, (w - 10) * S, 25 * S)
    }

    const drawDoor = (t: number) => {
      const { x, y, w, h } = DOOR
      d(x, y, w, h, COL.doorFrame)
      d(x + 1, y + 1, w - 2, h - 2, COL.door)
      d(x + w - 3, y + h / 2, 1.5, 1.5, COL.doorKnob)
      // "ENTER" sign
      T.fillStyle = '#999'; T.font = sceneFont('label')
      T.fillText('IN', (x + 2) * S, (y - 2) * S)
      // subtle glow when agent entering
      const entering = agentsRef.current.some(a => a.activity === 'entering')
      if (entering && ((t >> 3) & 1)) {
        X.fillStyle = 'rgba(255,200,50,0.08)'
        X.fillRect(x * S, y * S, w * S, h * S)
      }
    }

    const drawCeilingLights = (t: number) => {
      const positions = [70, 170, 270, 370]
      positions.forEach((lx, i) => {
        d(lx - 4, 80, 8, 2, COL.lightFixture)
        d(lx - 2, 78, 4, 2, COL.lightFixture)
        const flicker = 1 + Math.sin(t * 0.025 + i * 1.3) * 0.12
        const grad = X.createRadialGradient(lx * S, 82 * S, 0, lx * S, 82 * S, 45 * S * flicker)
        grad.addColorStop(0, 'rgba(255,220,150,0.07)')
        grad.addColorStop(1, 'rgba(255,220,150,0)')
        X.fillStyle = grad
        X.fillRect((lx - 45) * S, 80 * S, 90 * S, 50 * S)
      })
    }

    const drawClock = (t: number) => {
      const { x, y } = CLOCK_POS
      const now = new Date()
      const hr = now.getHours(), mn = now.getMinutes()
      d(x - 8, y - 8, 16, 16, '#444')
      d(x - 7, y - 7, 14, 14, '#222')
      for (let i = 0; i < 12; i++) {
        const a = (i / 12) * Math.PI * 2 - Math.PI / 2
        d(x + Math.cos(a) * 5, y + Math.sin(a) * 5, 1, 1, '#666')
      }
      const ha = ((hr % 12) / 12 + mn / 720) * Math.PI * 2 - Math.PI / 2
      const ma = (mn / 60) * Math.PI * 2 - Math.PI / 2
      X.strokeStyle = '#f90'; X.lineWidth = S
      X.beginPath(); X.moveTo(x * S, y * S); X.lineTo((x + Math.cos(ha) * 3.5) * S, (y + Math.sin(ha) * 3.5) * S); X.stroke()
      X.strokeStyle = '#ccc'; X.lineWidth = S
      X.beginPath(); X.moveTo(x * S, y * S); X.lineTo((x + Math.cos(ma) * 5) * S, (y + Math.sin(ma) * 5) * S); X.stroke()
      if ((t >> 4) & 1) d(x - 0.5, y - 0.5, 1, 1, '#f00')
    }

    const drawWhiteboard = () => {
      const { x, y, w, h } = WHITEBOARD
      d(x - 1, y - 1, w + 2, h + 2, COL.whiteboardFrame)
      d(x, y, w, h, COL.whiteboard)
      const cols = ['To Do', 'Active', 'Done']
      const cw = w / 3
      cols.forEach((label, i) => {
        if (i > 0) d(x + cw * i, y, 0.5, h, '#bbb')
        T.fillStyle = '#888'; T.font = sceneFont('label')
        T.fillText(label, (x + cw * i + 1) * S, (y + 4) * S)
      })
      const colors = ['#ffe066', '#ff9999', '#99ccff', '#99ff99']
      kanbanRef.current.forEach((task, i) => {
        const col = Math.min(i, 2)
        const row = i < 1 ? 0 : i < 3 ? i - 1 : 0
        const nx = x + cw * col + 1, ny = y + 7 + row * 9
        d(nx, ny, cw - 2, 7, colors[i % colors.length])
        T.fillStyle = '#333'; T.font = sceneFont('detail')
        T.fillText(task.slice(0, 8), (nx + 1) * S, (ny + 4.5) * S)
      })
    }

    const drawCoffeeMachine = (t: number) => {
      const { x, y } = COFFEE_MACHINE
      d(x, y, 12, 16, '#555')
      d(x + 1, y + 1, 10, 6, '#333')
      d(x + 3, y + 2, 6, 4, COL.coffee)
      if ((t >> 5) & 1) d(x + 5, y + 8, 2, 1, COL.coffee)
      d(x + 3, y + 10, 6, 5, COL.coffeeMug)
      d(x + 2, y + 11, 1, 3, COL.coffeeMug)
      d(x + 4, y + 11, 4, 3, COL.coffee)
      for (let i = 0; i < 3; i++) {
        const sy = y + 8 - i * 3 - ((t * 0.04) % 3)
        const sx = x + 4 + Math.sin(t * 0.025 + i * 2) * 1.5
        if (sy > y - 2) {
          X.fillStyle = `rgba(255,255,255,${0.12 - i * 0.03})`
          X.fillRect(sx * S, sy * S, 2 * S, 1 * S)
        }
      }
    }

    const drawPlant = (px: number, py: number, t: number) => {
      d(px + 2, py + 8, 4, 6, COL.plantPot)
      d(px + 1, py + 8, 6, 1, COL.plantPot)
      for (let i = 0; i < 3; i++) {
        const sw = Math.sin(t * 0.012 + px + i) | 0
        d(px + 3 + sw, py - i * 3 + 6, 2, 4, COL.plant)
        d(px + 1 + sw, py - i * 3 + 5, 2, 3, COL.plantLight)
        d(px + 5 + sw, py - i * 3 + 5, 2, 3, COL.plantLight)
      }
    }

    const drawRug = () => {
      X.fillStyle = COL.rug
      X.beginPath(); X.ellipse(220 * S, 175 * S, 40 * S, 14 * S, 0, 0, Math.PI * 2); X.fill()
      X.fillStyle = COL.rugPattern
      X.beginPath(); X.ellipse(220 * S, 175 * S, 34 * S, 10 * S, 0, 0, Math.PI * 2); X.fill()
      X.fillStyle = COL.rug
      X.beginPath(); X.ellipse(220 * S, 175 * S, 28 * S, 7 * S, 0, 0, Math.PI * 2); X.fill()
    }

    const drawBookshelves = () => {
      const bc = ['#e74c3c', '#f90', '#3498db', '#2ecc71', '#9b59b6', '#f39c12', '#1abc9c']
      const shelves = [{ sx: 230, sy: 50 }, { sx: 290, sy: 50 }]
      shelves.forEach(({ sx, sy }, si) => {
        d(sx, sy, 28, 2, '#5c4033')
        d(sx, sy - 16, 28, 2, '#5c4033')
        let bxp = sx + 1
        for (let b = 0; b < 4; b++) {
          const bh = 7 + ((b * 3) % 5)
          d(bxp, sy - bh, 5, bh, bc[(b + si * 3) % bc.length])
          bxp += 6
        }
        bxp = sx + 1
        for (let b = 0; b < 3; b++) {
          const bh = 5 + ((b * 4) % 4)
          d(bxp, sy - 16 - bh, 6, bh, bc[(b + si * 2 + 2) % bc.length])
          bxp += 8
        }
      })
    }

    const drawLogo = () => {
      T.fillStyle = '#f90'; T.font = sceneFont('title', 'bold')
      T.fillText('Agent Office', (W / 2 - 26) * S, 32 * S)
      d(W / 2 - 28, 34, 56, 1, '#f90')
      T.fillStyle = '#c70'; T.font = sceneFont('detail')
      T.fillText('headquarters', (W / 2 - 16) * S, 40 * S)
    }

    /* ── Draw: desk with cubicle + items ── */
    const drawDesk = (desk: Desk, t: number) => {
      const { x: dx, y: dy, accent, items, occupied } = desk

      // Cubicle walls (L-shaped partition)
      d(dx - 2, dy - 2, 1, 32, COL.cubicleWall)       // left wall
      d(dx - 2, dy - 2, 34, 1, COL.cubicleWall)        // back wall
      d(dx + 31, dy - 2, 1, 32, COL.cubicleWall)       // right wall
      // cubicle top edge highlight
      d(dx - 2, dy - 3, 35, 1, COL.cubicleTop)

      // Desk surface with colored accent strip
      d(dx, dy + 16, 28, 3, COL.deskTop)
      d(dx, dy + 15, 28, 1, accent)  // colored accent strip
      d(dx + 1, dy + 14, 26, 1, COL.desk)
      // Legs
      d(dx + 2, dy + 19, 2, 10, COL.desk)
      d(dx + 24, dy + 19, 2, 10, COL.desk)

      // Monitor
      d(dx + 9, dy + 4, 10, 10, COL.monitor)
      d(dx + 10, dy + 5, 8, 8, occupied ? COL.screen : COL.screenOff)
      d(dx + 13, dy + 14, 2, 1, COL.monitor)

      // Screen content
      if (occupied) {
        for (let i = 0; i < 4; i++) {
          const lw = 2 + ((t + i * 7) % 5)
          d(dx + 11, dy + 6 + i * 1.8, lw, 0.8, COL.screenText)
        }
        if ((t >> 3) & 1) d(dx + 11 + ((t >> 2) % 5), dy + 6 + ((t >> 4) % 4) * 1.8, 1, 1, COL.screenCursor)
        // screen glow
        X.fillStyle = 'rgba(50,255,50,0.025)'
        X.fillRect((dx + 8) * S, (dy + 13) * S, 12 * S, 4 * S)
      } else {
        // dark screen with standby dot
        if ((t >> 5) & 1) d(dx + 14, dy + 9, 1, 1, '#333')
      }

      // Chair
      d(dx + 10, dy + 22, 8, 3, COL.chair)
      d(dx + 10, dy + 25, 2, 4, COL.chair)
      d(dx + 16, dy + 25, 2, 4, COL.chair)
      d(dx + 9, dy + 19, 1, 6, COL.chair)

      // Desk items (personal touches)
      items.forEach(item => {
        const ix = dx + item.ox, iy = dy + item.oy
        switch (item.kind) {
          case 'mug':
            d(ix, iy, 3, 3, COL.mug); d(ix + 3, iy + 0.5, 1, 2, COL.mug)
            if (occupied && ((t >> 4) & 1)) { // steam
              X.fillStyle = 'rgba(255,255,255,0.1)'; X.fillRect((ix + 1) * S, (iy - 1.5) * S, S, S)
            }
            break
          case 'photo':
            d(ix, iy, 4, 5, '#ddd'); d(ix + 0.5, iy + 0.5, 3, 3.5, accent)
            break
          case 'plant':
            d(ix + 1, iy + 3, 2, 3, COL.plantPot)
            d(ix, iy + 1, 4, 2, COL.plant); d(ix + 1, iy, 2, 2, COL.plantLight)
            break
          case 'notebook':
            d(ix, iy, 5, 3, COL.notebook); d(ix + 0.5, iy + 0.5, 4, 0.5, '#fff')
            break
          case 'headphones':
            d(ix, iy, 4, 1, COL.headphones); d(ix, iy + 1, 1, 2, COL.headphones)
            d(ix + 3, iy + 1, 1, 2, COL.headphones)
            break
        }
      })

      // Nameplate on cubicle wall (if occupied, filled by agent draw)
      if (!occupied) {
        drawLabel(T, 'empty', (dx + 15) * S, (dy + 1) * S, { role: 'label', color: '#555', bgColor: '#333', align: 'center', scale: S })
      }
    }

    /* ── Draw: agent ── */
    const drawAgent = (a: OfficeAgent, t: number) => {
      const bx = a.x | 0, by = a.y | 0
      const mv = Math.abs(a.x - a.tx) > 1.5 || Math.abs(a.y - a.ty) > 1.5
      const bob = mv ? (Math.sin(t * 0.12 + a.x) | 0) : 0

      // Shadow
      X.fillStyle = 'rgba(0,0,0,0.12)'
      X.fillRect((bx + 1) * S, (by + 10) * S, 6 * S, 2 * S)

      // Body
      d(bx, by + bob, 8, 8, a.color)
      // Head
      d(bx + 1, by - 6 + bob, 6, 6, '#fdd')
      // Hair (varied by color)
      const hairColor = ['#333', '#8b4513', '#f90', '#222', '#654321'][
        AGENT_COLORS.indexOf(a.color) % 5
      ] || '#333'
      d(bx, by - 6 + bob, 1, 4, hairColor)
      d(bx + 7, by - 6 + bob, 1, 4, hairColor)
      d(bx + 1, by - 7 + bob, 6, 1, hairColor)

      // Eyes — blink
      const blink = (t % 120) < 3
      const ex = a.dir > 0 ? 2 : 1
      if (!blink) {
        d(bx + ex + 1, by - 4 + bob, 1, 1, '#333')
        d(bx + ex + 3, by - 4 + bob, 1, 1, '#333')
      } else {
        d(bx + ex + 1, by - 3 + bob, 1, 0.5, '#333')
        d(bx + ex + 3, by - 3 + bob, 1, 0.5, '#333')
      }

      // Smile when working
      if (a.running) d(bx + ex + 1.5, by - 2 + bob, 2, 0.5, '#c88')

      // Legs
      const st = (t >> 3) & 1
      if (mv) {
        d(bx + 1 + (st ? 2 : 0), by + 8 + bob, 2, 3, a.color)
        d(bx + 5 - (st ? 2 : 0), by + 8 + bob, 2, 3, a.color)
      } else {
        d(bx + 1, by + 8, 2, 3, a.color)
        d(bx + 5, by + 8, 2, 3, a.color)
      }

      // Typing arms at desk
      if (!mv && a.activity === 'desk' && a.running) {
        const armBob = (t >> 2) & 1
        d(bx - 1, by + 2 + bob + armBob, 1, 3, a.color)
        d(bx + 8, by + 2 + bob + (1 - armBob), 1, 3, a.color)
      }

      // Coffee cup when at coffee machine
      if (a.activity === 'coffee') {
        d(bx + (a.dir > 0 ? 9 : -3), by + 3 + bob, 2, 3, COL.coffeeMug)
      }

      // Kind badge
      const kindEmoji = a.kind === 'cron' ? '⏰' : a.kind === 'spawn' ? '🔀' : '💬'
      X.font = (2 * S) + 'px monospace'
      X.fillText(kindEmoji, (bx + 8) * S, (by - 5 + bob) * S)

      // Status label
      drawLabel(T, a.running ? 'active' : 'idle', (bx + 4) * S, (by - 10 + bob) * S, { role: 'status', color: a.running ? '#4f4' : '#888', bgColor: 'rgba(0,0,0,0.5)', align: 'center', scale: S })
      // Real-message speech bubble — appears when the session's latest message changes
      if (a.lastMessage && Date.now() - a.msgAt < SPEECH_BUBBLE_MS) {
        const msgAge = Date.now() - a.msgAt
        const msgAlpha = msgAge > SPEECH_BUBBLE_MS - 1000 ? (SPEECH_BUBBLE_MS - msgAge) / 1000 : 1
        drawSpeechBubble(T, a.lastMessage, (bx + 4) * S, (by - 14 + bob) * S, { scale: S, alpha: msgAlpha })
      }


      // Name (wraps up to ~45 chars)
      const nameLines = drawLabel(T, a.name, (bx + 4) * S, (by + 14) * S, { role: 'name', weight: 'bold', color: '#fff', align: 'center', scale: S, maxWidth: 64 * S })

      // Detail
      if (a.detail) {
        drawLabel(T, a.detail, (bx - 2) * S, (by + 18) * S + (nameLines - 1) * sceneLineHeight('name'), { role: 'detail', color: '#999', scale: S })
      }

      // Nameplate on cubicle
      if (a.deskIdx >= 0 && a.activity !== 'entering') {
        const dk = desksRef.current[a.deskIdx]
        drawLabel(T, a.name.slice(0, 8), (dk.x + 15) * S, (dk.y + 1) * S, { role: 'label', color: '#fff', bgColor: a.color, align: 'center', scale: S })
      }

      // Collab indicator
      if (collabRef.current && (collabRef.current[0] === a || collabRef.current[1] === a)) {
        d(bx + 2, by - 16 + bob, 3, 2, '#f90')
      }
    }

    const drawSpeech = (a: OfficeAgent, text: string, side: number) => {
      if (!text) return
      const bx = a.x | 0, by = a.y | 0
      const bob = (Math.abs(a.x - a.tx) > 1.5) ? (Math.sin(tickRef.current * 0.12 + a.x) | 0) : 0
      const tw = text.length * 3.5 + 4, bh = 8
      const sx = side < 0 ? bx - tw - 2 : bx + 10, sy = by - 26 + bob
      d(sx, sy, tw, bh, '#fff')
      d(sx - 1, sy + 1, tw + 2, bh - 2, '#fff')
      d(side < 0 ? sx + tw : sx, sy + bh, 2, 2, '#fff')
      T.fillStyle = '#222'; T.font = sceneFont('spell')
      T.fillText(text, (sx + 2) * S, (sy + 5.5) * S)
    }

    const drawParticles = () => {
      particlesRef.current.forEach(p => {
        const alpha = Math.max(0, 0.25 * (1 - p.life / p.maxLife))
        X.fillStyle = `rgba(255,240,200,${alpha})`
        X.fillRect(p.x * S, p.y * S, S, S)
      })
    }

    const drawAgentCounter = () => {
      const count = agentsRef.current.length
      const total = MAX_DESKS
      T.fillStyle = '#999'; T.font = sceneFont('status')
      T.fillText(`${count}/${total} agents`, 4 * S, (H - 4) * S)
    }

    /* ── Update logic ── */
    const update = (t: number) => {
      const agents = agentsRef.current

      // Advance and filter particles (decoupled from draw)
      particlesRef.current.forEach(p => {
        p.x += p.vx; p.y += p.vy
        p.vx += (Math.random() - 0.5) * 0.015
        p.life++
      })
      particlesRef.current = particlesRef.current.filter(p => p.life < p.maxLife && p.y > 70)

      // Move agents toward targets
      agents.forEach(a => {
        const ddx = a.tx - a.x, ddy = a.ty - a.y
        const dist = Math.sqrt(ddx * ddx + ddy * ddy)
        if (dist > 1.5) {
          a.x += ddx / dist * 0.65; a.y += ddy / dist * 0.65
          a.dir = ddx > 0 ? 1 : -1
        } else {
          a.x = a.tx; a.y = a.ty
          if (a.activity === 'entering') a.activity = 'desk'
          // Tick-based dwell at destination
          if (a.activity === 'collab' || a.activity === 'coffee' || a.activity === 'whiteboard') {
            a.actTimer++
            const limit = a.activity === 'collab' ? 600 : 400
            if (a.actTimer > limit) {
              const dk = desksRef.current[a.deskIdx]
              a.tx = dk.x + 10; a.ty = dk.y + 20; a.activity = 'desk'; a.actTimer = 0
              if (collabRef.current?.includes(a)) {
                collabRef.current.forEach(c => {
                  const ck = desksRef.current[c.deskIdx]
                  c.tx = ck.x + 10; c.ty = ck.y + 20; c.activity = 'desk'; c.actTimer = 0
                })
                collabRef.current = null; speechRef.current = { a: '', b: '' }
              }
            }
          }
        }
      })

      // Collab speech — only when both agents have arrived
      if (collabRef.current) {
        const [ca, cb] = collabRef.current
        const bothArrived = Math.abs(ca.x - ca.tx) < 2 && Math.abs(ca.y - ca.ty) < 2
          && Math.abs(cb.x - cb.tx) < 2 && Math.abs(cb.y - cb.ty) < 2
        const minTimer = Math.min(ca.actTimer, cb.actTimer)
        if (bothArrived && minTimer === 60) {
          const lines = CHAT_LINES[Math.random() * CHAT_LINES.length | 0]
          speechRef.current = { a: lines[0], b: lines[1] }
        }
      }

      // Collaboration events
      const deskAgents = agents.filter(a => a.activity === 'desk')
      if (t % 1800 === 0 && !collabRef.current && deskAgents.length >= 2 && Math.random() < 0.3) {
        const i = Math.random() * deskAgents.length | 0
        const j = (i + 1 + (Math.random() * (deskAgents.length - 1) | 0)) % deskAgents.length
        const a = deskAgents[i], b = deskAgents[j]
        a.tx = 210; a.ty = 170; b.tx = 230; b.ty = 170
        a.activity = 'collab'; b.activity = 'collab'; a.actTimer = 0; b.actTimer = 0
        collabRef.current = [a, b]
        speechRef.current = { a: '', b: '' }
      }

      // Coffee break
      if (t % 2400 === 600 && !collabRef.current && deskAgents.length > 0 && Math.random() < 0.25) {
        const a = deskAgents[Math.random() * deskAgents.length | 0]
        if (a.activity === 'desk') {
          a.activity = 'coffee'; a.actTimer = 0
          a.tx = COFFEE_MACHINE.x - 15; a.ty = COFFEE_MACHINE.y + 2
        }
      }

      // Whiteboard visit
      if (t % 3000 === 900 && !collabRef.current && deskAgents.length > 0 && Math.random() < 0.2) {
        const a = deskAgents[Math.random() * deskAgents.length | 0]
        if (a.activity === 'desk') {
          a.activity = 'whiteboard'; a.actTimer = 0
          a.tx = WHITEBOARD.x + 20; a.ty = WHITEBOARD.y + 50
        }
      }

      if (t % 6 === 0) spawnParticle()
    }

    /* ── Main draw ── */
    const draw = (t: number) => {
      const agents = agentsRef.current
      T.clearRect(0, 0, W * S, H * S)

      // Background
      d(0, 0, W, H, COL.wall)
      for (let i = 0; i < W; i += 16) {
        for (let j = 80; j < H; j += 16) {
          X.fillStyle = (((i / 16 + j / 16) & 1) ? COL.floorAlt : COL.floor)
          X.fillRect(i * S, j * S, 16 * S, 16 * S)
        }
      }
      d(0, 78, W, 3, COL.wallTrim)

      // Wall elements
      drawWindow(t)
      drawLogo()
      drawBookshelves()
      drawWhiteboard()
      drawClock(t)
      drawDoor(t)
      drawCeilingLights(t)

      // Floor elements
      drawRug()

      // Desks (all 8, occupied or not)
      desksRef.current.forEach(desk => drawDesk(desk, t))

      // Coffee machine
      drawCoffeeMachine(t)

      // Plants
      PLANTS.forEach(p => drawPlant(p.x, p.y, t))

      // Particles
      drawParticles()

      // Agents (y-sorted for depth)
      const sorted = [...agents].sort((a, b) => a.y - b.y)
      sorted.forEach(a => drawAgent(a, t))

      // Collaboration
      if (collabRef.current) {
        const [a, b] = collabRef.current
        X.strokeStyle = '#f90'; X.lineWidth = S
        X.setLineDash([4 * S, 4 * S])
        X.beginPath()
        X.moveTo((a.x + 4) * S, (a.y + 4) * S)
        X.lineTo((b.x + 4) * S, (b.y + 4) * S)
        X.stroke()
        X.setLineDash([])
        drawSpeech(a, speechRef.current.a, a.x < b.x ? -1 : 1)
        drawSpeech(b, speechRef.current.b, b.x < a.x ? -1 : 1)
      }

      // Agent counter
      drawAgentCounter()
    }

    /* ── Click: cycle status ── */
    canvasRef.current!.onclick = (e: MouseEvent) => {
      const mx = e.offsetX / S, my = e.offsetY / S
      agentsRef.current.forEach(a => {
        if (mx > a.x - 4 && mx < a.x + 12 && my > a.y - 14 && my < a.y + 14) {
          a.running = !a.running
          a.status = a.running ? 'working' : 'idle'
        }
      })
    }

    /* ── Loop ── */
    const cancelLoop = runSceneLoop(visibleRef, tickRef, update, draw)
    return () => {
      cancelLoop()
    }
  }, [])  

  return (
    <div style={SCENE_CONTAINER_STYLE(W, H)}>
      <canvas ref={canvasRef} aria-label="Office scene" style={{ ...PIXEL_CANVAS_STYLE, ...canvasProps.style }} onMouseMove={canvasProps.onMouseMove} onMouseLeave={canvasProps.onMouseLeave} onClick={canvasProps.onClick} />
      <canvas ref={textRef} aria-label="Office scene labels" style={TEXT_CANVAS_STYLE} />
      {tooltipEl}
    </div>
  )
}
