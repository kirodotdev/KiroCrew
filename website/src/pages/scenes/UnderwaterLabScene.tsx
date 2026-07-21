import { SCENE_SCALE } from './config'
import { useEffect, useRef } from 'react'
import type { AgentSource } from '../../hooks/useAgentSync'
import { isKnownAgent, markAgentsKnown, pruneAgents } from '../../hooks/sceneStateCache'
import { sceneFont, drawLabel, TEXT_CANVAS_STYLE, SCENE_CONTAINER_STYLE, PIXEL_CANVAS_STYLE } from '../../hooks/sceneText'
import { initSceneCanvases, runSceneLoop, useVisibleSync } from '../../hooks/sceneCanvas'
import { useSceneInteraction, type SceneTooltipTheme } from '../../hooks/useSceneInteraction'

const UNDERWATER_THEME: SceneTooltipTheme = { active: 'Exploring the deep', idle: 'Surfacing for air' }

/* ── Types ── */
interface Diver {
  id: string; name: string; kind: 'slot' | 'cron' | 'spawn'
  x: number; y: number; tx: number; ty: number
  domeIdx: number; color: string; suitColor: string
  running: boolean; detail: string
  swimPhase: number; activity: 'entering' | 'dome' | 'tube' | 'sonar'; actTimer: number
}
interface Bubble { x: number; y: number; r: number; speed: number; wobble: number; phase: number }
interface Fish { x: number; y: number; vx: number; color: string; size: number; tailPhase: number }
interface Jelly { x: number; y: number; vy: number; phase: number; color: string; size: number }

/* ── Constants ── */
const W = 480, H = 340, S = SCENE_SCALE
const MAX_DOMES = 8
const SUIT_COLORS = ['#e74c3c', '#3498db', '#f39c12', '#9b59b6', '#1abc9c', '#e67e22', '#2ecc71', '#e84393']
const HELMET_COLORS = ['#c0392b', '#2471a3', '#d4ac0d', '#7d3c98', '#148f77', '#ca6f1e', '#1e8449', '#c2185b']

const DOME_POSITIONS = [
  { x: 30, y: 130 }, { x: 130, y: 130 }, { x: 230, y: 130 }, { x: 330, y: 130 },
  { x: 30, y: 230 }, { x: 130, y: 230 }, { x: 230, y: 230 }, { x: 330, y: 230 },
]
const AIRLOCK = { x: 0, y: 120, w: 14, h: 60 }
const SONAR_POS = { x: 430, y: 180 }

const COL = {
  deepWater: '#0a1628', midWater: '#0e1e38', lightWater: '#122848',
  sand: '#3a3020', sandLight: '#4a4030', sandDark: '#2a2018',
  glass: 'rgba(100,180,255,0.15)', glassEdge: 'rgba(100,180,255,0.3)',
  metal: '#556', metalLight: '#778', metalDark: '#334',
  screen: '#0a2a0a', screenText: '#33ff88', screenGlow: 'rgba(50,255,130,0.06)',
  coral: '#e74c3c', coralPink: '#ff6b9d', coralOrange: '#ff8c42',
  kelp: '#2d6a4f', kelpLight: '#40916c',
  tube: 'rgba(100,180,255,0.12)', tubeEdge: 'rgba(100,180,255,0.25)',
  light: '#4af', lightGlow: 'rgba(70,170,255,0.08)',
  bubbleColor: 'rgba(150,220,255,0.3)',
  warning: '#f90', warningGlow: 'rgba(255,153,0,0.1)',
}

const SONAR_PINGS = ['Scanning…', 'All clear', 'Signal OK', 'Depth: 200m', 'Sync done', 'Data recv']

interface Props {
  agents: AgentSource[]
  visible?: boolean
}

export default function UnderwaterLabScene({ agents, visible = true }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const textRef = useRef<HTMLCanvasElement>(null)
  const tickRef = useRef(0)
  const diversRef = useRef<Diver[]>([])
  const bubblesRef = useRef<Bubble[]>([])
  const fishRef = useRef<Fish[]>([])
  const jellyRef = useRef<Jelly[]>([])
  const sonarMsgRef = useRef('')
  const visibleRef = useRef(visible)
  const { canvasProps, tooltipEl } = useSceneInteraction(canvasRef, diversRef, W, H, UNDERWATER_THEME, 10, undefined, agents)

  /* ── Init sea life ── */
  useEffect(() => {
    const fish: Fish[] = []
    for (let i = 0; i < 8; i++) {
      fish.push({
        x: Math.random() * W, y: 30 + Math.random() * (H - 60),
        vx: (Math.random() < 0.5 ? 1 : -1) * (0.15 + Math.random() * 0.25),
        color: ['#f39c12', '#e74c3c', '#3498db', '#2ecc71', '#9b59b6', '#1abc9c'][i % 6],
        size: 2 + Math.random() * 2, tailPhase: Math.random() * Math.PI * 2,
      })
    }
    fishRef.current = fish
    const jellies: Jelly[] = []
    for (let i = 0; i < 4; i++) {
      jellies.push({
        x: 50 + Math.random() * (W - 100), y: Math.random() * H,
        vy: -0.08 - Math.random() * 0.06, phase: Math.random() * Math.PI * 2,
        color: ['rgba(200,100,255,0.4)', 'rgba(100,200,255,0.35)', 'rgba(255,150,200,0.35)', 'rgba(100,255,200,0.3)'][i],
        size: 4 + Math.random() * 3,
      })
    }
    jellyRef.current = jellies
  }, [])

  /* ── Sync agents → divers ── */
  useEffect(() => {
    const existing = diversRef.current
    const newDivers: Diver[] = []
    agents.forEach((src, i) => {
      const prev = existing.find(d => d.id === src.id)
      if (prev) {
        prev.name = src.name; prev.running = src.running; prev.detail = src.detail; prev.kind = src.kind
        newDivers.push(prev)
      } else {
        const domeIdx = DOME_POSITIONS.findIndex((_, di) =>
          !existing.some(d => d.domeIdx === di) && !newDivers.some(d => d.domeIdx === di))
        if (domeIdx < 0) return
        const dp = DOME_POSITIONS[domeIdx]
        const known = isKnownAgent('underwater', src.id)
        newDivers.push({
          id: src.id, name: src.name, kind: src.kind,
          x: known ? dp.x + 20 : AIRLOCK.x + 7,
          y: known ? dp.y + 25 : AIRLOCK.y + 30,
          tx: dp.x + 20, ty: dp.y + 25,
          domeIdx, color: SUIT_COLORS[i % SUIT_COLORS.length],
          suitColor: HELMET_COLORS[i % HELMET_COLORS.length],
          running: src.running, detail: src.detail,
          swimPhase: Math.random() * Math.PI * 2,
          activity: known ? 'dome' : 'entering', actTimer: 0,
        })
        markAgentsKnown('underwater', [src.id])
      }
    })
    diversRef.current = newDivers

    pruneAgents('underwater', newDivers.map(d => d.id))
  }, [agents])

  /* ── Pixel helper ── */

  /* ── Canvas render loop ── */
  useEffect(() => {
    const { X, T, d } = initSceneCanvases(canvasRef.current!, textRef.current!, W, H, S)

    const spawnBubble = (bx: number, by: number) => {
      if (bubblesRef.current.length < 40) {
        bubblesRef.current.push({
          x: bx, y: by, r: 0.5 + Math.random() * 1.5,
          speed: 0.15 + Math.random() * 0.2,
          wobble: Math.random() * Math.PI * 2,
          phase: Math.random() * 0.05,
        })
      }
    }

    /* ── Draw: environment ── */
    const drawOcean = (t: number) => {
      // Gradient background
      const grad = X.createLinearGradient(0, 0, 0, H * S)
      grad.addColorStop(0, COL.midWater)
      grad.addColorStop(0.4, COL.deepWater)
      grad.addColorStop(1, '#060e1a')
      X.fillStyle = grad; X.fillRect(0, 0, W * S, H * S)

      // Light rays from surface
      for (let i = 0; i < 5; i++) {
        const rx = 60 + i * 90 + Math.sin(t * 0.005 + i) * 15
        const grad2 = X.createLinearGradient(rx * S, 0, (rx + 20) * S, H * S)
        grad2.addColorStop(0, 'rgba(70,170,255,0.04)')
        grad2.addColorStop(0.5, 'rgba(70,170,255,0.015)')
        grad2.addColorStop(1, 'transparent')
        X.fillStyle = grad2
        X.beginPath()
        X.moveTo((rx - 5) * S, 0)
        X.lineTo((rx + 25) * S, 0)
        X.lineTo((rx + 40) * S, H * S)
        X.lineTo((rx - 20) * S, H * S)
        X.fill()
      }
    }

    const drawSeafloor = (t: number) => {
      // Sandy bottom
      d(0, H - 30, W, 30, COL.sand)
      for (let i = 0; i < W; i += 12) {
        d(i, H - 30, 12, 1, COL.sandLight)
        d(i + 6, H - 28, 8, 1, COL.sandDark)
      }
      // Coral clusters
      const corals = [
        { x: 20, y: H - 35, c: COL.coral },
        { x: 100, y: H - 38, c: COL.coralPink },
        { x: 200, y: H - 33, c: COL.coralOrange },
        { x: 350, y: H - 36, c: COL.coral },
        { x: 440, y: H - 34, c: COL.coralPink },
      ]
      corals.forEach(cr => {
        const sway = Math.sin(t * 0.01 + cr.x) * 0.5
        d(cr.x, cr.y, 4, 6, cr.c)
        d(cr.x - 2 + sway, cr.y - 3, 3, 5, cr.c)
        d(cr.x + 3 + sway, cr.y - 2, 3, 4, cr.c)
      })
      // Kelp
      const kelps = [60, 160, 280, 400, 460]
      kelps.forEach((kx, i) => {
        for (let j = 0; j < 4; j++) {
          const sway = Math.sin(t * 0.012 + i + j * 0.5) * 2
          d(kx + sway, H - 35 - j * 8, 2, 9, j % 2 ? COL.kelp : COL.kelpLight)
        }
      })
    }

    const drawDome = (dx: number, dy: number, occupied: boolean, idx: number, t: number) => {
      const r = 22
      // Glass dome
      X.beginPath(); X.arc((dx + 20) * S, (dy + 15) * S, r * S, Math.PI, 0)
      X.lineTo((dx + 42) * S, (dy + 30) * S)
      X.lineTo((dx - 2) * S, (dy + 30) * S)
      X.closePath()
      X.fillStyle = COL.glass; X.fill()
      X.strokeStyle = COL.glassEdge; X.lineWidth = S; X.stroke()

      // Metal base
      d(dx - 4, dy + 28, 48, 4, COL.metal)
      d(dx - 4, dy + 32, 48, 2, COL.metalDark)
      // Legs
      d(dx, dy + 34, 3, 8, COL.metalDark)
      d(dx + 37, dy + 34, 3, 8, COL.metalDark)

      // Interior floor
      d(dx, dy + 24, 40, 4, COL.metalLight)

      // Monitor inside dome
      d(dx + 12, dy + 10, 16, 12, COL.metalDark)
      d(dx + 13, dy + 11, 14, 10, occupied ? COL.screen : '#111')
      if (occupied) {
        for (let i = 0; i < 4; i++) {
          const lw = 3 + ((t + i * 5) % 4)
          d(dx + 14, dy + 12 + i * 2, lw, 1, COL.screenText)
        }
        if ((t >> 3) & 1) d(dx + 14 + ((t >> 2) % 6), dy + 12 + ((t >> 4) % 4) * 2, 1, 1, '#fff')
        // Screen glow
        X.fillStyle = COL.screenGlow
        X.fillRect((dx + 10) * S, (dy + 20) * S, 20 * S, 6 * S)
      }

      // Dome light on top
      if (occupied) {
        const flicker = 0.6 + Math.sin(t * 0.04 + idx) * 0.2
        const grad = X.createRadialGradient((dx + 20) * S, (dy + 5) * S, 0, (dx + 20) * S, (dy + 5) * S, 15 * S)
        grad.addColorStop(0, `rgba(70,170,255,${0.1 * flicker})`)
        grad.addColorStop(1, 'transparent')
        X.fillStyle = grad
        X.fillRect((dx + 5) * S, (dy - 10) * S, 30 * S, 30 * S)
      }

      // Bubbles rising from active domes
      if (occupied && t % 20 === idx * 2) {
        spawnBubble(dx + 10 + Math.random() * 20, dy - 5)
      }

      // Nameplate
      if (!occupied) {
        drawLabel(T, 'vacant', (dx + 20) * S, (dy + 5) * S, { role: 'label', color: '#556', bgColor: COL.metalDark, align: 'center', scale: S })
      }
    }

    const drawConnectingTubes = () => {
      // Horizontal tubes between domes in same row
      for (let row = 0; row < 2; row++) {
        const base = row * 4
        for (let i = 0; i < 3; i++) {
          const a = DOME_POSITIONS[base + i], b = DOME_POSITIONS[base + i + 1]
          const ty = a.y + 26
          X.fillStyle = COL.tube
          X.fillRect((a.x + 42) * S, ty * S, (b.x - a.x - 42) * S, 6 * S)
          X.strokeStyle = COL.tubeEdge; X.lineWidth = S
          X.strokeRect((a.x + 42) * S, ty * S, (b.x - a.x - 42) * S, 6 * S)
        }
      }
      // Vertical tubes between rows
      for (let i = 0; i < 4; i++) {
        const top = DOME_POSITIONS[i], bot = DOME_POSITIONS[i + 4]
        const tx = top.x + 18
        X.fillStyle = COL.tube
        X.fillRect(tx * S, (top.y + 34) * S, 6 * S, (bot.y - top.y - 34) * S)
        X.strokeStyle = COL.tubeEdge; X.lineWidth = S
        X.strokeRect(tx * S, (top.y + 34) * S, 6 * S, (bot.y - top.y - 34) * S)
      }
    }

    const drawAirlock = (t: number) => {
      const { x, y, w, h } = AIRLOCK
      d(x, y, w, h, COL.metalDark)
      d(x + 2, y + 3, w - 4, h - 6, COL.metal)
      // Circular hatch detail
      X.beginPath(); X.arc((x + 7) * S, (y + h / 2) * S, 5 * S, 0, Math.PI * 2)
      X.strokeStyle = COL.metalLight; X.lineWidth = S; X.stroke()
      // Warning light
      const entering = diversRef.current.some(d => d.activity === 'entering')
      if (entering) {
        const blink = (t >> 3) & 1
        d(x + 5, y - 3, 4, 3, blink ? COL.warning : COL.metalDark)
        if (blink) {
          X.fillStyle = COL.warningGlow
          X.fillRect(x * S, (y - 5) * S, w * S, 8 * S)
        }
      }
      T.fillStyle = '#556'; T.font = sceneFont('label')
      T.fillText('LOCK', (x + 2) * S, (y - 1) * S)
    }

    const drawSonarStation = (t: number) => {
      const { x, y } = SONAR_POS
      // Console body
      d(x - 18, y - 15, 36, 30, COL.metalDark)
      d(x - 16, y - 13, 32, 20, '#0a1a0a')
      // Sonar sweep
      const sweepAngle = (t * 0.03) % (Math.PI * 2)
      X.strokeStyle = '#0f03'; X.lineWidth = S
      X.beginPath(); X.arc(x * S, (y - 3) * S, 14 * S, 0, Math.PI * 2); X.stroke()
      X.beginPath(); X.arc(x * S, (y - 3) * S, 8 * S, 0, Math.PI * 2); X.stroke()
      // Sweep line
      X.strokeStyle = '#3f8'; X.lineWidth = 1.5 * S
      X.beginPath()
      X.moveTo(x * S, (y - 3) * S)
      X.lineTo((x + Math.cos(sweepAngle) * 14) * S, (y - 3 + Math.sin(sweepAngle) * 14) * S)
      X.stroke()
      // Blips for active agents
      diversRef.current.forEach((dv, i) => {
        if (!dv.running) return
        const ba = (i / diversRef.current.length) * Math.PI * 2
        const br = 5 + (i % 3) * 3
        d(x + Math.cos(ba) * br - 0.5, y - 3 + Math.sin(ba) * br - 0.5, 1.5, 1.5, '#4f8')
      })
      // Sonar message
      if (sonarMsgRef.current) {
        T.fillStyle = COL.screenText; T.font = sceneFont('label')
        T.fillText(sonarMsgRef.current, (x - 14) * S, (y + 10) * S)
      }
      // Label
      T.fillStyle = '#556'; T.font = sceneFont('label')
      T.fillText('SONAR', (x - 8) * S, (y + 18) * S)
    }

    const drawFish = (_t: number) => {
      fishRef.current.forEach(f => {
        f.x += f.vx
        if (f.x > W + 10) f.x = -10
        if (f.x < -10) f.x = W + 10
        f.tailPhase += 0.08
        const tailOff = Math.sin(f.tailPhase) * 1.5
        const dir = f.vx > 0 ? 1 : -1
        // Body
        d(f.x, f.y, f.size * 2, f.size, f.color)
        // Tail
        d(f.x - dir * f.size + tailOff, f.y - 0.5, f.size, f.size + 1, f.color)
        // Eye
        d(f.x + (dir > 0 ? f.size * 1.5 : 0.5), f.y + 0.5, 1, 1, '#fff')
      })
    }

    const drawJellyfish = (t: number) => {
      jellyRef.current.forEach(j => {
        j.y += j.vy
        j.x += Math.sin(t * 0.008 + j.phase) * 0.15
        if (j.y < -20) { j.y = H + 10; j.x = 50 + Math.random() * (W - 100) }
        const pulse = Math.sin(t * 0.04 + j.phase) * 0.3
        // Bell
        X.beginPath()
        X.arc(j.x * S, j.y * S, (j.size + pulse) * S, Math.PI, 0)
        X.fillStyle = j.color; X.fill()
        // Tentacles
        for (let i = 0; i < 3; i++) {
          const tx = j.x - j.size + 1 + i * j.size
          const sway = Math.sin(t * 0.02 + i + j.phase) * 2
          X.strokeStyle = j.color; X.lineWidth = S * 0.5
          X.beginPath()
          X.moveTo(tx * S, (j.y + 1) * S)
          X.quadraticCurveTo((tx + sway) * S, (j.y + j.size + 3) * S, (tx + sway * 0.5) * S, (j.y + j.size * 2) * S)
          X.stroke()
        }
        // Glow
        const grad = X.createRadialGradient(j.x * S, j.y * S, 0, j.x * S, j.y * S, (j.size + 4) * S)
        grad.addColorStop(0, j.color); grad.addColorStop(1, 'transparent')
        X.fillStyle = grad; X.globalAlpha = 0.15
        X.fillRect((j.x - j.size - 4) * S, (j.y - j.size - 4) * S, (j.size * 2 + 8) * S, (j.size * 3 + 8) * S)
        X.globalAlpha = 1
      })
    }

    const drawBubbles = () => {
      bubblesRef.current.forEach(b => {
        X.beginPath()
        X.arc(b.x * S, b.y * S, b.r * S, 0, Math.PI * 2)
        X.strokeStyle = COL.bubbleColor; X.lineWidth = S * 0.5; X.stroke()
        // Highlight
        X.fillStyle = 'rgba(200,240,255,0.15)'
        X.fillRect((b.x - b.r * 0.3) * S, (b.y - b.r * 0.3) * S, b.r * 0.5 * S, b.r * 0.5 * S)
      })
    }

    const drawDiver = (dv: Diver, t: number) => {
      const bx = dv.x | 0, by = dv.y | 0
      const mv = Math.abs(dv.x - dv.tx) > 1.5 || Math.abs(dv.y - dv.ty) > 1.5
      const swim = mv ? Math.sin(t * 0.1 + dv.swimPhase) * 1.5 : 0
      const dir = dv.tx > dv.x ? 1 : -1

      if (mv) {
        // ── Swimming pose (horizontal) ──
        const kick = Math.sin(t * 0.15 + dv.swimPhase)
        const bodyBob = Math.sin(t * 0.06 + dv.swimPhase) * 1.2

        // Shadow / water distortion
        X.fillStyle = 'rgba(70,170,255,0.06)'
        X.fillRect((bx - 2) * S, (by + 4 + bodyBob) * S, 16 * S, 4 * S)

        // Body (horizontal)
        d(bx, by + bodyBob, 12, 6, dv.color)

        // Helmet (front)
        const hx = dir > 0 ? bx + 10 : bx - 5
        X.beginPath(); X.arc((hx + 2.5) * S, (by + 3 + bodyBob) * S, 4 * S, 0, Math.PI * 2)
        X.fillStyle = dv.suitColor; X.fill()
        // Visor
        const vx = dir > 0 ? hx + 1 : hx - 1
        d(vx, by + 1 + bodyBob, 4, 3, '#1a3050')
        d(vx + 0.5, by + 1.5 + bodyBob, 3, 2, '#2a5080')
        // Eye behind visor
        const blink = (t % 110) < 3
        if (!blink) {
          d(vx + 1, by + 2 + bodyBob, 1, 1, '#adf')
        }

        // Oxygen tanks (on back, opposite of direction)
        const tankX = dir > 0 ? bx - 3 : bx + 13
        d(tankX, by - 1 + bodyBob, 3, 4, COL.metalDark)
        d(tankX, by + 3 + bodyBob, 3, 4, COL.metalDark)
        d(tankX + 1, by - 2 + bodyBob, 1, 1, COL.metalLight)
        // Hose from tank to helmet
        X.strokeStyle = COL.metalLight; X.lineWidth = S * 0.8
        X.beginPath()
        X.moveTo((tankX + 1.5) * S, (by + bodyBob) * S)
        X.quadraticCurveTo((bx + 6) * S, (by - 2 + bodyBob) * S, (hx + 2.5) * S, (by + 1 + bodyBob) * S)
        X.stroke()

        // Arms (reaching forward in swim stroke)
        const armSwing = Math.sin(t * 0.12 + dv.swimPhase) * 2
        const armX = dir > 0 ? hx + 3 : hx - 2
        d(armX, by - 1 + bodyBob + armSwing, 2, 2, dv.color)
        d(armX, by + 4 + bodyBob - armSwing, 2, 2, dv.color)

        // Legs + flippers (kicking)
        const legX = dir > 0 ? bx - 2 : bx + 12
        const kick1 = kick * 3
        const kick2 = -kick * 3
        // Leg 1
        d(legX, by + 1 + bodyBob + kick1, 3, 2, dv.suitColor)
        d(legX + (dir > 0 ? -2 : 2), by + 1 + bodyBob + kick1, 3, 1.5, '#2a5a2a') // flipper
        // Leg 2
        d(legX, by + 4 + bodyBob + kick2, 3, 2, dv.suitColor)
        d(legX + (dir > 0 ? -2 : 2), by + 4 + bodyBob + kick2, 3, 1.5, '#2a5a2a') // flipper

        // Bubble trail from helmet
        if (t % 6 === 0) spawnBubble(hx + 2, by - 2 + bodyBob)
        if (t % 10 === 0) spawnBubble(hx + 1 + Math.random() * 3, by - 3 + bodyBob)

        // Wake trail behind swimmer
        const wakeX = dir > 0 ? bx - 4 : bx + 14
        X.fillStyle = 'rgba(150,220,255,0.08)'
        X.fillRect((wakeX - 2) * S, (by + 1 + bodyBob) * S, 6 * S, 4 * S)

      } else {
        // ── Idle pose (upright, inside dome) ──
        // Shadow
        X.fillStyle = 'rgba(0,0,0,0.1)'
        X.fillRect((bx + 1) * S, (by + 11) * S, 6 * S, 2 * S)

        // Suit body
        d(bx, by, 8, 9, dv.color)

        // Helmet
        X.beginPath(); X.arc((bx + 4) * S, (by - 3) * S, 4.5 * S, 0, Math.PI * 2)
        X.fillStyle = dv.suitColor; X.fill()
        // Visor
        d(bx + 1, by - 5, 6, 4, '#1a3050')
        d(bx + 2, by - 4, 4, 2, '#2a5080')
        // Eyes
        const blink = (t % 110) < 3
        if (!blink) {
          d(bx + 2, by - 4, 1, 1, '#adf')
          d(bx + 5, by - 4, 1, 1, '#adf')
        }

        // Oxygen tank
        d(bx + 7, by - 2, 3, 7, COL.metalDark)
        d(bx + 8, by - 3, 1, 1, COL.metalLight)

        // Legs
        d(bx + 2, by + 9, 2, 4, dv.suitColor)
        d(bx + 4, by + 9, 2, 4, dv.suitColor)
        // Flippers
        d(bx + 1, by + 13, 3, 1, '#2a5a2a')
        d(bx + 4, by + 13, 3, 1, '#2a5a2a')

        // Arms typing at console
        if (dv.activity === 'dome' && dv.running) {
          const armBob = (t >> 2) & 1
          d(bx - 1, by + 2 + armBob, 1, 3, dv.color)
          d(bx + 8, by + 2 + (1 - armBob), 1, 3, dv.color)
        }

        // Idle bubbles (slow, from helmet)
        if (t % 40 === 0) spawnBubble(bx + 4, by - 7)
      }

      // Kind badge
      const kindEmoji = dv.kind === 'cron' ? '⏱️' : dv.kind === 'spawn' ? '🐙' : '🫧'
      X.font = (2 * S) + 'px monospace'
      X.fillText(kindEmoji, (bx + 9) * S, (by - 5 + swim) * S)

      // Status
      drawLabel(T, dv.running ? 'online' : 'standby', (bx + 4) * S, (by - 11 + swim) * S, { role: 'status', color: dv.running ? '#4ff' : '#668', bgColor: 'rgba(0,0,0,0.5)', align: 'center', scale: S })

      // Name
      T.fillStyle = '#cef'; T.font = sceneFont('name', 'bold')
      T.textAlign = 'center'
      T.fillText(dv.name, (bx + 4) * S, (by + 16) * S)
      T.textAlign = 'start'

      // Detail
      if (dv.detail) {
        T.fillStyle = '#668'; T.font = sceneFont('detail')
        T.fillText(dv.detail, (bx - 4) * S, (by + 20) * S)
      }

      // Nameplate on dome
      if (dv.domeIdx >= 0 && dv.activity !== 'entering') {
        const dp = DOME_POSITIONS[dv.domeIdx]
        drawLabel(T, dv.name.slice(0, 8), (dp.x + 20) * S, (dp.y + 5) * S, { role: 'label', color: '#fff', bgColor: dv.color, align: 'center', scale: S })
      }
    }

    const drawTitle = () => {
      T.fillStyle = COL.light; T.font = sceneFont('title', 'bold')
      T.textAlign = 'center'
      T.fillText('Deep Sea Lab', (W / 2) * S, 20 * S)
      T.fillStyle = '#446'; T.font = sceneFont('status')
      T.fillText('research station', (W / 2) * S, 30 * S)
      T.textAlign = 'start'
      T.fillStyle = '#446'; T.font = sceneFont('label')
      T.fillText(`${diversRef.current.length}/${MAX_DOMES} divers`, 4 * S, (H - 4) * S)
    }

    /* ── Update ── */
    const update = (t: number) => {
      // Advance and filter bubbles (decoupled from draw)
      bubblesRef.current.forEach(b => {
        b.y -= b.speed
        b.x += Math.sin(b.wobble) * 0.2
        b.wobble += b.phase
      })
      bubblesRef.current = bubblesRef.current.filter(b => b.y > -5)

      const divers = diversRef.current
      divers.forEach(dv => {
        const ddx = dv.tx - dv.x, ddy = dv.ty - dv.y
        const dist = Math.sqrt(ddx * ddx + ddy * ddy)
        if (dist > 1.5) {
          dv.x += ddx / dist * 0.4; dv.y += ddy / dist * 0.4
        } else {
          dv.x = dv.tx; dv.y = dv.ty
          if (dv.activity === 'entering') dv.activity = 'dome'
          if (dv.activity === 'sonar' || dv.activity === 'tube') {
            dv.actTimer++
            if (dv.activity === 'sonar' && dv.actTimer === 1) {
              sonarMsgRef.current = SONAR_PINGS[Math.random() * SONAR_PINGS.length | 0]
            }
            if (dv.activity === 'sonar' && dv.actTimer > 300) {
              const dp = DOME_POSITIONS[dv.domeIdx]
              dv.tx = dp.x + 20; dv.ty = dp.y + 25; dv.activity = 'dome'; dv.actTimer = 0
              sonarMsgRef.current = ''
            }
            if (dv.activity === 'tube' && dv.actTimer > 300) {
              const dp = DOME_POSITIONS[dv.domeIdx]
              dv.tx = dp.x + 20; dv.ty = dp.y + 25; dv.activity = 'dome'; dv.actTimer = 0
            }
          }
        }
      })

      // Sonar visit
      const domeAgents = divers.filter(d => d.activity === 'dome')
      if (t % 2400 === 600 && domeAgents.length > 0 && Math.random() < 0.2) {
        const dv = domeAgents[Math.random() * domeAgents.length | 0]
        dv.activity = 'sonar'; dv.actTimer = 0; dv.tx = SONAR_POS.x - 20; dv.ty = SONAR_POS.y + 5
      }

      // Tube swim between domes
      if (t % 3000 === 1200 && domeAgents.length >= 2 && Math.random() < 0.2) {
        const a = domeAgents[0], b = domeAgents[1]
        a.activity = 'tube'; b.activity = 'tube'; a.actTimer = 0; b.actTimer = 0
        const dpB = DOME_POSITIONS[b.domeIdx], dpA = DOME_POSITIONS[a.domeIdx]
        a.tx = dpB.x + 20; a.ty = dpB.y + 25
        b.tx = dpA.x + 20; b.ty = dpA.y + 25
      }

      // Ambient bubbles
      if (t % 15 === 0) spawnBubble(Math.random() * W, H - 25)
    }

    /* ── Main draw ── */
    const draw = (t: number) => {
      T.clearRect(0, 0, W * S, H * S)
      drawOcean(t)
      drawSeafloor(t)
      drawConnectingTubes()
      drawAirlock(t)

      // Domes
      const occupied = new Set(diversRef.current.map(d => d.domeIdx))
      DOME_POSITIONS.forEach((dp, i) => drawDome(dp.x, dp.y, occupied.has(i), i, t))

      drawSonarStation(t)
      drawFish(t)
      drawJellyfish(t)
      drawBubbles()
      drawTitle()

      // Divers (y-sorted)
      const sorted = [...diversRef.current].sort((a, b) => a.y - b.y)
      sorted.forEach(dv => drawDiver(dv, t))
    }

    const cancelLoop = runSceneLoop(visibleRef, tickRef, update, draw)
    return () => {
      cancelLoop()
    }
  }, [])  

  useVisibleSync(visibleRef, visible)

  return (
    <div style={SCENE_CONTAINER_STYLE(W, H)}>
      <canvas ref={canvasRef} aria-label="Underwater lab scene" style={{ ...PIXEL_CANVAS_STYLE, ...canvasProps.style }} onMouseMove={canvasProps.onMouseMove} onMouseLeave={canvasProps.onMouseLeave} onClick={canvasProps.onClick} />
      <canvas ref={textRef} aria-label="Underwater lab scene labels" style={TEXT_CANVAS_STYLE} />
      {tooltipEl}
    </div>
  )
}
