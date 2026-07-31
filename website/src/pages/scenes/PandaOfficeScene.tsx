import { useEffect, useRef, useState } from 'react'
import type { AgentSource } from '../../hooks/useAgentSync'
import { isKnownAgent, markAgentsKnown, pruneAgents } from '../../hooks/sceneStateCache'
import { sceneFont, drawLabel, sceneLineHeight, drawSpeechBubble, SPEECH_BUBBLE_MS, TEXT_CANVAS_STYLE, SCENE_CONTAINER_STYLE, PIXEL_CANVAS_STYLE } from '../../hooks/sceneText'
import { initSceneCanvases, runSceneLoop, useVisibleSync } from '../../hooks/sceneCanvas'
import { useSceneInteraction, type SceneTooltipTheme } from '../../hooks/useSceneInteraction'
import { i18nT } from '../../i18n/t'

const OFFICE_THEME: SceneTooltipTheme = { active: 'Grinding PRs', idle: 'Waiting for CR approval' }
const PANDA_WHITE = '#f5f5f5'
const PANDA_BLACK = '#222'
const PANDA_PINK = '#f4a5b0'

/* ── Types ── */
type AgentActivity = 'desk' | 'entering' | 'collab' | 'coffee' | 'whiteboard'
interface OfficeAgent {
  id: string; name: string; label: string; kind: 'slot' | 'cron' | 'spawn'
  x: number; y: number; tx: number; ty: number
  deskIdx: number; color: string; detail: string
  dir: number; activity: AgentActivity
  running: boolean
  lastMessage: string; msgAt: number
}
interface Desk {
  x: number; y: number; occupied: boolean
  accent: string; items: DeskItem[]
}
interface DeskItem { kind: 'mug' | 'photo' | 'plant' | 'notebook' | 'headphones'; ox: number; oy: number }
interface Particle { x: number; y: number; vx: number; vy: number; life: number; maxLife: number }

/* ── Layout constants ── */
const W = 440, H = 300, S = 3
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
  floor: '#2a3a1a', floorAlt: '#1e2e14', wall: '#0d1a08', wallTrim: '#3a5a2a',
  desk: '#5c4033', deskTop: '#7a5c47', monitor: '#333', screen: '#0a2a0a',
  screenText: '#33ff33', screenCursor: '#33ff33', screenOff: '#1a1a1a',
  cubicleWall: '#3a4a2a', cubicleTop: '#4a5a3a',
  plant: '#2d8a4e', plantPot: '#8b4513', plantLight: '#3cb371',
  chair: '#3a2a1a', coffee: '#6b4226', coffeeMug: '#ddd',
  whiteboard: '#e8e8e0', whiteboardFrame: '#888',
  lightFixture: '#555', rug: '#2a3a1a', rugPattern: '#354a25',
  window: '#1a3050', windowFrame: '#666', sky: '#2a4a6a', star: '#fff',
  door: '#6b4226', doorFrame: '#4a3520', doorKnob: '#d4a017',
  mug: '#e74c3c', photo: '#f1c40f', notebook: '#3498db', headphones: '#555',
  bamboo: '#6b8e23', bambooLight: '#8fbc5f', bambooDark: '#4a6a14',
  bambooLeaf: '#3a7a2a', bambooLeafLight: '#5a9a4a',
  bark: '#5a4a2a', barkDark: '#3a2a1a',
  chess: '#ddd', chessDark: '#333', chessBoard: '#8b6914',
  pullBar: '#666', pullBarDark: '#444',
}
const CHAT_LINES = [
  ['Token expired?', 'Refreshing!'], ['CR approved!', 'Ship it! 🐼'],
  ['Deploy ready?', 'Bamboo break?'], ['Review the PR?', 'LGTM!'],
  ['Aztec timeout', 'Scaling now!'], ['Latency up...', 'Checking AAO'],
  ['Chess later?', 'Always!'], ['Pull-ups?', 'After deploy!'],
]
const COFFEE_MACHINE = { x: 390, y: 240 }
const WHITEBOARD = { x: 350, y: 14, w: 60, h: 36 }
const CLOCK_POS = { x: 170, y: 22 }
const WINDOW_POS = { x: 60, y: 10, w: 50, h: 40 }
const PLANTS = [
  { x: 15, y: 98 }, { x: 95, y: 98 }, { x: 175, y: 98 }, { x: 255, y: 98 },
  { x: 335, y: 98 }, { x: 15, y: 260 }, { x: 200, y: 260 }, { x: 420, y: 260 },
]
const BAMBOO_STALKS = [
  { x: 5, y: 30, h: 55 }, { x: 20, y: 25, h: 60 }, { x: 415, y: 28, h: 57 },
  { x: 430, y: 22, h: 63 }, { x: 100, y: 35, h: 48 }, { x: 340, y: 32, h: 52 },
  { x: 155, y: 38, h: 45 }, { x: 380, y: 30, h: 55 },
]
const PULLUP_BAR = { x: 370, y: 170 }
const CHESS_TABLE = { x: 340, y: 230 }


/* ── Helper: build desks ── */
function buildDesks(): Desk[] {
  return DESK_POSITIONS.map((pos, i) => ({
    x: pos.x, y: pos.y, occupied: false,
    accent: DESK_ACCENTS[i % DESK_ACCENTS.length],
    items: DESK_ITEM_SETS[i % DESK_ITEM_SETS.length],
  }))
}

/* ── Component ── */
export default function PandaOfficeScene({ agents, visible = true }: { agents: AgentSource[]; visible?: boolean }) {
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
    // Two-pass: mark existing agents' desks first, then assign remaining desks to new agents.
    // Prevents a new agent from stealing an existing agent's desk due to array ordering.
    const newAgents: OfficeAgent[] = []
    const newSrcs: AgentSource[] = []
    capped.forEach((src) => {
      const prev = existing.find(a => a.id === src.id)
      if (prev) {
        prev.name = src.name; prev.label = src.label; prev.detail = src.detail
        prev.running = src.running; prev.kind = src.kind
        if ((src.lastMessage || '') !== prev.lastMessage) { prev.lastMessage = src.lastMessage || ''; prev.msgAt = Date.now() }
        if (prev.deskIdx >= 0) desks[prev.deskIdx].occupied = true
        newAgents.push(prev)
      } else {
        newSrcs.push(src)
      }
    })
    newSrcs.forEach((src) => {
      const deskIdx = desks.findIndex(dk => !dk.occupied)
      if (deskIdx < 0) return
      desks[deskIdx].occupied = true
      const dk = desks[deskIdx]
      const known = isKnownAgent('panda-office', src.id)
      newAgents.push({
        id: src.id, name: src.name, label: src.label, kind: src.kind,
        x: known ? dk.x + 10 : DOOR.x + 6,
        y: known ? dk.y + 20 : DOOR.y + 25,
        tx: dk.x + 10, ty: dk.y + 20,
        deskIdx, color: AGENT_COLORS[deskIdx % AGENT_COLORS.length],
        detail: src.detail, dir: 1,
        activity: known ? 'desk' : 'entering',
        running: src.running,
        lastMessage: src.lastMessage || '', msgAt: 0,
      })
      markAgentsKnown('panda-office', [src.id])
    })

    kanbanRef.current = capped.filter(s => s.running).map(s => s.name).slice(0, 4)
    if (kanbanRef.current.length === 0) kanbanRef.current = ['No tasks']

    agentsRef.current = newAgents
    setAgentCount(newAgents.length)

    pruneAgents('panda-office', newAgents.map(a => a.id))
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
      // Forest backdrop behind the wall — visible through a large opening
      const { x, y, w, h } = WINDOW_POS
      d(x - 1, y - 1, w + 2, h + 2, COL.bambooDark)
      // Sky through canopy
      d(x, y, w, h, '#1a3a2a')
      // Distant bamboo silhouettes
      for (let i = 0; i < 6; i++) {
        const bx = x + 3 + i * 8, bh = 15 + (i * 7 % 12)
        d(bx, y + h - bh, 2, bh, '#2a5a3a')
        // Leaves
        d(bx - 2, y + h - bh + 3, 6, 2, '#3a6a4a')
        d(bx - 1, y + h - bh - 2, 4, 2, '#3a6a4a')
      }
      // Fireflies
      for (let i = 0; i < 4; i++) {
        const fx = x + 5 + ((i * 13 + t * 0.015) % (w - 10))
        const fy = y + 4 + ((i * 9) % (h - 8))
        if (Math.sin(t * 0.05 + i * 2.1) > 0.4) {
          d(fx, fy, 1, 1, '#afa')
          X.fillStyle = 'rgba(150,255,150,0.08)'
          X.fillRect((fx - 2) * S, (fy - 2) * S, 5 * S, 5 * S)
        }
      }
      d(x + w / 2, y, 1, h, COL.bambooDark)
      d(x, y + h / 2, w, 1, COL.bambooDark)
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
      // Small bamboo shoot
      d(px + 3, py + 2, 2, 12, COL.bamboo)
      d(px + 3.5, py + 2, 1, 12, COL.bambooLight)
      // Nodes
      d(px + 2.5, py + 6, 3, 1, COL.bambooDark)
      d(px + 2.5, py + 10, 3, 1, COL.bambooDark)
      // Leaves swaying
      const sw = Math.sin(t * 0.015 + px) * 2 | 0
      d(px + sw, py, 3, 2, COL.bambooLeaf)
      d(px + 5 + sw, py + 3, 3, 1, COL.bambooLeafLight)
      d(px - 1 + sw, py + 4, 3, 1, COL.bambooLeaf)
    }

    const drawPullUpBar = (_t: number) => {
      const { x: px, y: py } = PULLUP_BAR
      // Vertical posts
      d(px, py, 2, 28, COL.pullBar)
      d(px + 24, py, 2, 28, COL.pullBar)
      // Horizontal bar
      d(px, py, 26, 2, COL.pullBarDark)
      d(px + 1, py + 1, 24, 1, '#888')
      // Base plates
      d(px - 1, py + 26, 4, 2, COL.pullBarDark)
      d(px + 23, py + 26, 4, 2, COL.pullBarDark)
      // Label
      drawLabel(T, 'pull-ups', (px + 13) * S, (py - 3) * S, { role: 'label', color: '#888', bgColor: 'rgba(0,0,0,0.4)', align: 'center', scale: S })
    }

    const drawChessTable = (_t: number) => {
      const { x: cx, y: cy } = CHESS_TABLE
      // Table legs
      d(cx + 2, cy + 10, 2, 8, COL.bark)
      d(cx + 18, cy + 10, 2, 8, COL.bark)
      // Table surface
      d(cx, cy + 8, 22, 3, COL.barkDark)
      d(cx + 1, cy + 8, 20, 2, COL.bark)
      // Chess board (4x4 visible squares)
      for (let r = 0; r < 4; r++) {
        for (let c = 0; c < 4; c++) {
          d(cx + 3 + c * 4, cy + 1 + r * 2, 4, 2, ((r + c) & 1) ? COL.chessDark : COL.chess)
        }
      }
      // A few pieces
      d(cx + 5, cy, 1, 1, '#fff')   // white piece
      d(cx + 13, cy, 1, 1, '#222')  // black piece
      d(cx + 9, cy - 1, 1, 2, '#fff')
      d(cx + 17, cy + 1, 1, 1, '#222')
      // Chairs
      d(cx - 3, cy + 10, 4, 3, COL.chair)
      d(cx + 21, cy + 10, 4, 3, COL.chair)
    }

    const drawRug = () => {
      // Mossy patch on forest floor
      X.fillStyle = '#2a4a1a'
      X.beginPath(); X.ellipse(220 * S, 175 * S, 40 * S, 14 * S, 0, 0, Math.PI * 2); X.fill()
      X.fillStyle = '#354a25'
      X.beginPath(); X.ellipse(220 * S, 175 * S, 34 * S, 10 * S, 0, 0, Math.PI * 2); X.fill()
      X.fillStyle = '#2a4a1a'
      X.beginPath(); X.ellipse(220 * S, 175 * S, 28 * S, 7 * S, 0, 0, Math.PI * 2); X.fill()
    }

    const drawBookshelves = () => {
      // Bamboo stalks growing along the back wall
      BAMBOO_STALKS.forEach(({ x: bx, y: by, h: bh }) => {
        // Stalk
        d(bx, by, 3, bh, COL.bamboo)
        d(bx + 1, by, 1, bh, COL.bambooLight)
        // Nodes (joints)
        for (let n = 0; n < bh; n += 10) {
          d(bx - 0.5, by + n, 4, 1, COL.bambooDark)
        }
        // Leaves at top
        d(bx - 3, by - 2, 4, 2, COL.bambooLeaf)
        d(bx + 2, by - 3, 4, 2, COL.bambooLeafLight)
        d(bx - 2, by + 5, 3, 1, COL.bambooLeaf)
      })
    }

    const drawLogo = () => {
      T.fillStyle = '#8fbc5f'; T.font = sceneFont('title', 'bold')
      T.fillText('🐼 Panda Den', (W / 2 - 28) * S, 32 * S)
      d(W / 2 - 30, 34, 60, 1, '#6b8e23')
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

    /* ── Shared bob helper ── */
    const agentBob = (a: OfficeAgent, t: number): number => {
      const mv = Math.abs(a.x - a.tx) > 1.5 || Math.abs(a.y - a.ty) > 1.5
      return mv ? (Math.sin(t * 0.12 + a.x) * 2 | 0) : 0
    }

    /* ── Draw: panda ── */
    const drawAgent = (a: OfficeAgent, t: number) => {
      const bx = a.x | 0, by = a.y | 0
      const mv = Math.abs(a.x - a.tx) > 1.5 || Math.abs(a.y - a.ty) > 1.5
      const bob = agentBob(a, t)

      // Shadow
      X.fillStyle = 'rgba(0,0,0,0.12)'
      X.fillRect((bx + 1) * S, (by + 10) * S, 6 * S, 2 * S)

      // Body (white, slightly rounded look via trim)
      d(bx, by + bob, 8, 8, PANDA_WHITE)
      // Black shoulder patch (classic panda saddle)
      d(bx, by + bob, 8, 2, PANDA_BLACK)
      // Accent collar/bow (tiny — uses desk accent color for identity)
      d(bx + 3, by + 2 + bob, 2, 1, a.color)

      // Head (white, larger than body for chibi look)
      d(bx, by - 7 + bob, 8, 7, PANDA_WHITE)
      // Ears (black)
      d(bx - 1, by - 8 + bob, 2, 2, PANDA_BLACK)
      d(bx + 7, by - 8 + bob, 2, 2, PANDA_BLACK)
      d(bx, by - 7 + bob, 1, 1, PANDA_BLACK)
      d(bx + 7, by - 7 + bob, 1, 1, PANDA_BLACK)

      // Eye patches (black ovals)
      const ex = a.dir > 0 ? 1 : 0
      d(bx + 1 + ex, by - 5 + bob, 2, 3, PANDA_BLACK)
      d(bx + 4 + ex, by - 5 + bob, 2, 3, PANDA_BLACK)

      // Eyes — white dots inside patches, blink
      const blink = (t % 120) < 3
      if (!blink) {
        d(bx + 1.5 + ex, by - 4 + bob, 1, 1, '#fff')
        d(bx + 4.5 + ex, by - 4 + bob, 1, 1, '#fff')
      }

      // Nose (pink/black small)
      d(bx + 3.5, by - 2 + bob, 1, 1, PANDA_BLACK)
      // Mouth when running
      if (a.running) d(bx + 3, by - 1 + bob, 2, 0.5, PANDA_PINK)

      // Arms (black)
      const st = (t >> 3) & 1
      if (!mv && a.activity === 'desk' && a.running) {
        const armBob = (t >> 2) & 1
        d(bx - 1, by + 2 + bob + armBob, 2, 3, PANDA_BLACK)
        d(bx + 7, by + 2 + bob + (1 - armBob), 2, 3, PANDA_BLACK)
      } else {
        d(bx - 1, by + 3 + bob, 2, 3, PANDA_BLACK)
        d(bx + 7, by + 3 + bob, 2, 3, PANDA_BLACK)
      }

      // Legs (black, walk cycle)
      if (mv) {
        d(bx + 1 + (st ? 2 : 0), by + 8 + bob, 2, 3, PANDA_BLACK)
        d(bx + 5 - (st ? 2 : 0), by + 8 + bob, 2, 3, PANDA_BLACK)
      } else {
        d(bx + 1, by + 8, 2, 3, PANDA_BLACK)
        d(bx + 5, by + 8, 2, 3, PANDA_BLACK)
      }

      // Coffee/bamboo when at coffee machine (bamboo stalk — it's a panda after all)
      if (a.activity === 'coffee') {
        const cx = bx + (a.dir > 0 ? 9 : -3)
        d(cx, by + 1 + bob, 1, 5, '#6b8e23')
        d(cx - 1, by + 2 + bob, 3, 1, '#8fbc5f')
      }

      // Kind badge
      const kindEmoji = a.kind === 'cron' ? '⏰' : a.kind === 'spawn' ? '🔀' : '💬'
      X.font = (2 * S) + 'px monospace'
      X.fillText(kindEmoji, (bx + 8) * S, (by - 6 + bob) * S)

      // Status label
      drawLabel(T, a.running ? 'active' : 'idle', (bx + 4) * S, (by - 11 + bob) * S, { role: 'status', color: a.running ? '#4f4' : '#888', bgColor: 'rgba(0,0,0,0.5)', align: 'center', scale: S })
      // Real-message speech bubble — appears when the session's latest message changes
      if (a.lastMessage && Date.now() - a.msgAt < SPEECH_BUBBLE_MS) {
        const msgAge = Date.now() - a.msgAt
        const msgAlpha = msgAge > SPEECH_BUBBLE_MS - 1000 ? (SPEECH_BUBBLE_MS - msgAge) / 1000 : 1
        drawSpeechBubble(T, a.lastMessage, (bx + 4) * S, (by - 15 + bob) * S, { scale: S, alpha: msgAlpha })
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
      const bob = agentBob(a, tickRef.current)
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
    const timeouts: number[] = []
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
        }
      })

      // Collaboration events
      const deskAgents = agents.filter(a => a.activity === 'desk')
      if (t % 1800 === 0 && !collabRef.current && deskAgents.length >= 2 && Math.random() < 0.3) {
        const i = Math.random() * deskAgents.length | 0
        const j = (i + 1 + (Math.random() * (deskAgents.length - 1) | 0)) % deskAgents.length
        const a = deskAgents[i], b = deskAgents[j]
        a.tx = 210; a.ty = 170; b.tx = 230; b.ty = 170
        a.activity = 'collab'; b.activity = 'collab'
        collabRef.current = [a, b]
        const lines = CHAT_LINES[Math.random() * CHAT_LINES.length | 0]
        speechRef.current = { a: lines[0], b: '' }
        timeouts.push(window.setTimeout(() => { speechRef.current.b = lines[1] }, 1200))
        timeouts.push(window.setTimeout(() => {
          const live = agentsRef.current
          if (!live.includes(a) || !live.includes(b)) {
            for (const agent of [a, b]) {
              if (live.includes(agent) && agent.deskIdx >= 0) {
                const dk = desksRef.current[agent.deskIdx]
                agent.tx = dk.x + 10; agent.ty = dk.y + 20
                agent.activity = 'desk'
              }
            }
            collabRef.current = null; speechRef.current = { a: '', b: '' }; return
          }
          const dkA = desksRef.current[a.deskIdx], dkB = desksRef.current[b.deskIdx]
          a.tx = dkA.x + 10; a.ty = dkA.y + 20
          b.tx = dkB.x + 10; b.ty = dkB.y + 20
          a.activity = 'desk'; b.activity = 'desk'
          collabRef.current = null
          speechRef.current = { a: '', b: '' }
        }, 4500))
      }

      // Coffee break
      if (t % 2400 === 600 && !collabRef.current && deskAgents.length > 0 && Math.random() < 0.25) {
        const a = deskAgents[Math.random() * deskAgents.length | 0]
        if (a.activity === 'desk') {
          a.activity = 'coffee'
          a.tx = COFFEE_MACHINE.x - 15; a.ty = COFFEE_MACHINE.y + 2
          timeouts.push(window.setTimeout(() => {
            if (!agentsRef.current.includes(a)) return
            const dk = desksRef.current[a.deskIdx]
            a.tx = dk.x + 10; a.ty = dk.y + 20
            a.activity = 'desk'
          }, 3500))
        }
      }

      // Whiteboard visit
      if (t % 3000 === 900 && !collabRef.current && deskAgents.length > 0 && Math.random() < 0.2) {
        const a = deskAgents[Math.random() * deskAgents.length | 0]
        if (a.activity === 'desk') {
          a.activity = 'whiteboard'
          a.tx = WHITEBOARD.x + 20; a.ty = WHITEBOARD.y + 50
          timeouts.push(window.setTimeout(() => {
            if (!agentsRef.current.includes(a)) return
            const dk = desksRef.current[a.deskIdx]
            a.tx = dk.x + 10; a.ty = dk.y + 20
            a.activity = 'desk'
          }, 3000))
        }
      }

      if (t % 6 === 0) spawnParticle()
    }

    /* ── Main draw ── */
    const draw = (t: number) => {
      const agents = agentsRef.current
      T.clearRect(0, 0, W * S, H * S)

      // Background — dense bamboo wall + forest floor
      d(0, 0, W, 80, '#0d1a08')
      // Thick bamboo wall — packed stalks across the entire back
      for (let i = 0; i < W; i += 6) {
        const shade = (i / 6 & 1) ? '#4a7a1a' : '#3a6a14'
        const highlight = (i / 6 & 1) ? '#5a8a2a' : '#4a7a1a'
        d(i, 0, 5, 80, shade)
        d(i + 2, 0, 1, 80, highlight)
        // Nodes every ~15px, staggered
        for (let j = 8 + (i % 12); j < 80; j += 15) {
          d(i - 0.5, j, 6, 1, '#2a5a0a')
        }
      }
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

      // Pull-up bar & chess table
      drawPullUpBar(t)
      drawChessTable(t)

      // Bamboo shoots
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

    /* ── Loop ── */
    const cancelLoop = runSceneLoop(visibleRef, tickRef, update, draw)
    return () => {
      cancelLoop()
      timeouts.forEach(clearTimeout)
    }
  }, [])  

  return (
    <div style={SCENE_CONTAINER_STYLE(W, H)}>
      <canvas ref={canvasRef} aria-label={i18nT('pages.scenes.pandaOfficeScene.panda_office_scene')} style={{ ...PIXEL_CANVAS_STYLE, ...canvasProps.style }} onMouseMove={canvasProps.onMouseMove} onMouseLeave={canvasProps.onMouseLeave} onClick={canvasProps.onClick} />
      <canvas ref={textRef} aria-label={i18nT('pages.scenes.pandaOfficeScene.panda_office_scene_labels')} style={TEXT_CANVAS_STYLE} />
      {tooltipEl}
    </div>
  )
}
