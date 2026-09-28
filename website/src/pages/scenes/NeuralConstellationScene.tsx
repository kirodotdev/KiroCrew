import { SCENE_SCALE } from './config'
import { useEffect, useRef } from 'react'
import type { AgentSource } from '../../hooks/useAgentSync'
import { isKnownAgent, markAgentsKnown, pruneAgents } from '../../hooks/sceneStateCache'
import { sceneFont, TEXT_CANVAS_STYLE, SCENE_CONTAINER_STYLE, PIXEL_CANVAS_STYLE } from '../../hooks/sceneText'
import { initSceneCanvases, runSceneLoop, useVisibleSync } from '../../hooks/sceneCanvas'
import { useSceneInteraction, type SceneTooltipTheme } from '../../hooks/useSceneInteraction'
import { i18nT } from '../../i18n/t'

const NEURAL_THEME: SceneTooltipTheme = { active: 'Processing neural pathways', idle: 'Awaiting signal' }

/* ── Types ── */
interface NeuralNode {
  id: string; name: string; kind: 'slot' | 'cron' | 'spawn'
  x: number; y: number; tx: number; ty: number
  radius: number; pulse: number; running: boolean
  color: string; detail: string; angle: number; orbitSpeed: number
  energy: number; burstCooldown: number
  ringAngle: number; ringSpeed: number
}
interface Synapse { from: number; to: number; strength: number; flow: number; arcPhase: number }
interface DataPacket {
  x: number; y: number; tx: number; ty: number; progress: number
  color: string; speed: number; trail: { x: number; y: number }[]
}
interface Star { x: number; y: number; size: number; twinkle: number; speed: number; color: string }
interface Nebula { x: number; y: number; r: number; color: string; drift: number; phase: number }
interface EnergyWave { x: number; y: number; radius: number; maxRadius: number; color: string; life: number }
interface ShootingStar { x: number; y: number; vx: number; vy: number; life: number; maxLife: number }

/* ── Constants ── */
const W = 480, H = 320, S = SCENE_SCALE
const NODE_COLORS = ['#ff6b6b', '#4ecdc4', '#ffe66d', '#a29bfe', '#55efc4', '#fd79a8', '#74b9ff', '#e17055']
const CENTER = { x: W / 2, y: H / 2 }

interface Props {
  agents: AgentSource[]
  visible?: boolean
}

export default function NeuralConstellationScene({ agents, visible = true }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const textRef = useRef<HTMLCanvasElement>(null)
  const tickRef = useRef(0)
  const nodesRef = useRef<NeuralNode[]>([])
  const synapsesRef = useRef<Synapse[]>([])
  const packetsRef = useRef<DataPacket[]>([])
  const visibleRef = useRef(visible)
  const starsRef = useRef<Star[]>([])
  const nebulaeRef = useRef<Nebula[]>([])
  const wavesRef = useRef<EnergyWave[]>([])
  const shootingRef = useRef<ShootingStar[]>([])
  const { canvasProps, tooltipEl } = useSceneInteraction(canvasRef, nodesRef, W, H, NEURAL_THEME, 10, undefined, agents)

  /* ── Init background elements ── */
  useEffect(() => {
    const stars: Star[] = []
    const starColors = ['#c8dcff', '#ffe8c8', '#ffc8c8', '#c8ffd4', '#d4c8ff']
    for (let i = 0; i < 120; i++) {
      stars.push({
        x: Math.random() * W, y: Math.random() * H,
        size: Math.random() < 0.15 ? 2 : Math.random() < 0.4 ? 1.2 : 0.6,
        twinkle: Math.random() * Math.PI * 2,
        speed: 0.008 + Math.random() * 0.025,
        color: starColors[i % starColors.length],
      })
    }
    starsRef.current = stars

    const nebulae: Nebula[] = []
    const nebColors = ['rgba(100,50,180,', 'rgba(50,100,180,', 'rgba(180,50,80,', 'rgba(50,150,120,']
    for (let i = 0; i < 6; i++) {
      nebulae.push({
        x: 40 + Math.random() * (W - 80), y: 40 + Math.random() * (H - 80),
        r: 30 + Math.random() * 50, color: nebColors[i % nebColors.length],
        drift: Math.random() * Math.PI * 2, phase: 0.002 + Math.random() * 0.003,
      })
    }
    nebulaeRef.current = nebulae
  }, [])

  /* ── Sync agents → nodes ── */
  useEffect(() => {
    const existing = nodesRef.current
    const newNodes: NeuralNode[] = []
    const count = agents.length

    agents.forEach((src, i) => {
      const prev = existing.find(n => n.id === src.id)
      const angle = (i / Math.max(count, 1)) * Math.PI * 2 - Math.PI / 2
      const orbitR = 75 + (i % 2) * 25
      const tx = CENTER.x + Math.cos(angle) * orbitR
      const ty = CENTER.y + Math.sin(angle) * orbitR

      if (prev) {
        prev.name = src.name; prev.running = src.running; prev.detail = src.detail
        prev.tx = tx; prev.ty = ty; prev.kind = src.kind
        newNodes.push(prev)
      } else {
        const known = isKnownAgent('neural', src.id)
        newNodes.push({
          id: src.id, name: src.name, kind: src.kind,
          x: known ? tx : CENTER.x + (Math.random() - 0.5) * 20,
          y: known ? ty : CENTER.y + (Math.random() - 0.5) * 20,
          tx, ty, radius: 9, pulse: Math.random() * Math.PI * 2,
          running: src.running, color: NODE_COLORS[i % NODE_COLORS.length],
          detail: src.detail, angle, orbitSpeed: 0.0005 + Math.random() * 0.0003,
          energy: 0, burstCooldown: 0,
          ringAngle: Math.random() * Math.PI * 2, ringSpeed: 0.02 + Math.random() * 0.02,
        })
        markAgentsKnown('neural', [src.id])
      }
    })
    nodesRef.current = newNodes

    pruneAgents('neural', newNodes.map(n => n.id))

    const synapses: Synapse[] = []
    for (let i = 0; i < newNodes.length; i++) {
      for (let j = i + 1; j < newNodes.length; j++) {
        synapses.push({ from: i, to: j, strength: 0.3 + Math.random() * 0.4, flow: Math.random() * Math.PI * 2, arcPhase: Math.random() * Math.PI * 2 })
      }
    }
    synapsesRef.current = synapses
  }, [agents])


  /* ── Canvas render loop ── */
  useEffect(() => {
    const { X, T, d } = initSceneCanvases(canvasRef.current!, textRef.current!, W, H, S)

    /* ── Cached glow sprites ── */
    const glowCache = new Map<string, HTMLCanvasElement>()
    const getGlowSprite = (color: string, maxR: number): HTMLCanvasElement => {
      const key = color + '|' + maxR
      if (glowCache.has(key)) return glowCache.get(key)!
      const outerScale = 1 + 3 * 0.3 // 1.9 — fits largest gradient layer
      const size = Math.ceil(maxR * 2 * outerScale * S)
      const c = document.createElement('canvas')
      c.width = size; c.height = size
      const ctx = c.getContext('2d')!
      const cx = size / 2
      for (let layer = 3; layer >= 0; layer--) {
        const lr = maxR * (1 + layer * 0.3) * S
        const alpha = 0.06 - layer * 0.012
        const grad = ctx.createRadialGradient(cx, cx, 0, cx, cx, lr)
        grad.addColorStop(0, color + Math.round(alpha * 255).toString(16).padStart(2, '0'))
        grad.addColorStop(1, 'transparent')
        ctx.fillStyle = grad
        ctx.fillRect(0, 0, size, size)
      }
      glowCache.set(key, c)
      return c
    }

    /* ── Hub glow sprite (static layers) ── */
    const hubGlowSize = Math.ceil(16 * (1.5 + 4 * 0.8) * 2 * 1.2 * S)
    const hubGlowCanvas = document.createElement('canvas')
    hubGlowCanvas.width = hubGlowSize; hubGlowCanvas.height = hubGlowSize
    const hubCtx = hubGlowCanvas.getContext('2d')!
    const hc = hubGlowSize / 2
    for (let layer = 4; layer >= 0; layer--) {
      const lr = 16 * (1.5 + layer * 0.8) * S
      const alpha = 0.04 - layer * 0.007
      const grad = hubCtx.createRadialGradient(hc, hc, 0, hc, hc, lr)
      grad.addColorStop(0, `rgba(255,153,0,${alpha})`)
      grad.addColorStop(1, 'transparent')
      hubCtx.fillStyle = grad
      hubCtx.fillRect(0, 0, hubGlowSize, hubGlowSize)
    }

    /* ── Nebula offscreen canvas ── */
    const nebulaCanvas = document.createElement('canvas')
    nebulaCanvas.width = W * S; nebulaCanvas.height = H * S
    const nebulaCtx = nebulaCanvas.getContext('2d')!
    let nebulaFrame = -1

    const renderNebulae = (t: number) => {
      // Only re-render every 60 frames (nebulae drift very slowly)
      if (Math.floor(t / 60) === nebulaFrame) return
      nebulaFrame = Math.floor(t / 60)
      nebulaCtx.clearRect(0, 0, W * S, H * S)
      nebulaeRef.current.forEach(n => {
        const nx = n.x + Math.sin(t * n.phase + n.drift) * 8
        const ny = n.y + Math.cos(t * n.phase * 0.7 + n.drift) * 5
        const breathe = n.r + Math.sin(t * 0.008 + n.drift) * 6
        for (let layer = 3; layer >= 0; layer--) {
          const lr = breathe * (1 + layer * 0.4)
          const alpha = 0.02 - layer * 0.004
          const grad = nebulaCtx.createRadialGradient(nx * S, ny * S, 0, nx * S, ny * S, lr * S)
          grad.addColorStop(0, n.color + alpha + ')')
          grad.addColorStop(0.6, n.color + (alpha * 0.5) + ')')
          grad.addColorStop(1, 'transparent')
          nebulaCtx.fillStyle = grad
          nebulaCtx.fillRect((nx - lr) * S, (ny - lr) * S, lr * 2 * S, lr * 2 * S)
        }
      })
    }

    const drawNebulae = (t: number) => {
      renderNebulae(t)
      X.drawImage(nebulaCanvas, 0, 0)
    }

    const drawStars = (t: number) => {
      starsRef.current.forEach(star => {
        const alpha = 0.25 + Math.sin(t * star.speed + star.twinkle) * 0.35
        X.globalAlpha = Math.max(0.05, alpha)
        X.fillStyle = star.color
        X.fillRect(star.x * S, star.y * S, star.size * S, star.size * S)
        // Bright stars get a cross flare
        if (star.size > 1.5 && alpha > 0.4) {
          X.globalAlpha = alpha * 0.3
          X.fillRect((star.x - 1) * S, star.y * S, (star.size + 2) * S, star.size * 0.5 * S)
          X.fillRect(star.x * S, (star.y - 1) * S, star.size * 0.5 * S, (star.size + 2) * S)
        }
      })
      X.globalAlpha = 1
    }

    const drawShootingStars = (t: number) => {
      // Spawn occasionally
      if (t % 180 === 0 && Math.random() < 0.4 && shootingRef.current.length < 2) {
        const fromLeft = Math.random() < 0.5
        shootingRef.current.push({
          x: fromLeft ? -5 : W + 5, y: Math.random() * H * 0.4,
          vx: (fromLeft ? 1 : -1) * (2 + Math.random() * 2),
          vy: 0.5 + Math.random() * 1,
          life: 0, maxLife: 40 + Math.random() * 30,
        })
      }
      shootingRef.current.forEach(ss => {
        ss.x += ss.vx; ss.y += ss.vy; ss.life++
        const alpha = 1 - ss.life / ss.maxLife
        // Trail
        for (let i = 0; i < 6; i++) {
          const ta = alpha * (1 - i * 0.15)
          X.fillStyle = `rgba(255,255,200,${Math.max(0, ta)})`
          X.fillRect((ss.x - ss.vx * i * 0.5) * S, (ss.y - ss.vy * i * 0.5) * S, 2 * S, 1 * S)
        }
        // Head
        X.fillStyle = `rgba(255,255,255,${alpha})`
        X.fillRect(ss.x * S, ss.y * S, 2 * S, 2 * S)
      })
      shootingRef.current = shootingRef.current.filter(ss => ss.life < ss.maxLife)
    }

    const drawSynapses = (t: number) => {
      const nodes = nodesRef.current
      synapsesRef.current.forEach(syn => {
        if (syn.from >= nodes.length || syn.to >= nodes.length) return
        const a = nodes[syn.from], b = nodes[syn.to]
        const bothActive = a.running && b.running
        const anyActive = a.running || b.running

        const dx = b.x - a.x, dy = b.y - a.y
        const dist = Math.sqrt(dx * dx + dy * dy)

        // Animated dashed line
        X.setLineDash(anyActive ? [4 * S, 3 * S] : [2 * S, 6 * S])
        X.lineDashOffset = -t * (bothActive ? 0.8 : 0.3)
        const alpha = bothActive ? 0.4 : anyActive ? 0.15 : 0.04
        X.strokeStyle = bothActive ? a.color + '88' : `rgba(100,180,255,${alpha})`
        X.lineWidth = (bothActive ? 2 : 0.8) * S
        X.beginPath(); X.moveTo(a.x * S, a.y * S); X.lineTo(b.x * S, b.y * S); X.stroke()
        X.setLineDash([])

        // Electric arc effect on active connections
        if (bothActive && dist > 20) {
          const midX = (a.x + b.x) / 2, midY = (a.y + b.y) / 2
          const perpX = -dy / dist, perpY = dx / dist
          const arcOff = Math.sin(t * 0.08 + syn.arcPhase) * 8
          X.strokeStyle = a.color + '44'; X.lineWidth = S
          X.beginPath()
          X.moveTo(a.x * S, a.y * S)
          X.quadraticCurveTo((midX + perpX * arcOff) * S, (midY + perpY * arcOff) * S, b.x * S, b.y * S)
          X.stroke()
        }

        // Data packets
        if (anyActive && t % 120 === 0 && Math.random() < 0.15) {
          packetsRef.current.push({
            x: a.x, y: a.y, tx: b.x, ty: b.y,
            progress: 0, color: bothActive ? '#fff' : '#6cf',
            speed: 0.012 + Math.random() * 0.008, trail: [],
          })
        }
      })
    }

    const drawPackets = () => {
      packetsRef.current.forEach(p => {
        p.progress += p.speed
        const px = p.x + (p.tx - p.x) * p.progress
        const py = p.y + (p.ty - p.y) * p.progress
        p.trail.push({ x: px, y: py })
        if (p.trail.length > 8) p.trail.shift()

        // Trail
        p.trail.forEach((pt, i) => {
          const ta = (i / p.trail.length) * 0.4
          X.fillStyle = p.color
          X.globalAlpha = ta
          const sz = 1 + (i / p.trail.length)
          X.fillRect((pt.x - sz / 2) * S, (pt.y - sz / 2) * S, sz * S, sz * S)
        })

        // Head with glow
        const alpha = 1 - Math.abs(p.progress - 0.5) * 1.2
        X.globalAlpha = Math.max(0, alpha)
        X.fillStyle = '#fff'
        X.fillRect((px - 1) * S, (py - 1) * S, 2 * S, 2 * S)
        const grad = X.createRadialGradient(px * S, py * S, 0, px * S, py * S, 5 * S)
        grad.addColorStop(0, p.color); grad.addColorStop(1, 'transparent')
        X.fillStyle = grad
        X.globalAlpha = Math.max(0, alpha * 0.4)
        X.fillRect((px - 5) * S, (py - 5) * S, 10 * S, 10 * S)
        X.globalAlpha = 1
      })
      packetsRef.current = packetsRef.current.filter(p => p.progress < 1)
    }

    const drawEnergyWaves = () => {
      wavesRef.current.forEach(w => {
        const alpha = Math.max(0, 0.5 * (1 - w.radius / w.maxRadius))
        X.beginPath(); X.arc(w.x * S, w.y * S, w.radius * S, 0, Math.PI * 2)
        X.strokeStyle = w.color; X.lineWidth = 1.5 * S; X.globalAlpha = alpha; X.stroke()
        X.globalAlpha = 1
      })
    }

    const drawNode = (node: NeuralNode, t: number) => {
      const { x, y, radius, running, color, name, kind, detail } = node
      const pulseScale = running ? 1 + Math.sin(t * 0.07 + node.pulse) * 0.18 : 0.8
      const r = radius * pulseScale

      // Deep glow layers (cached sprite — fixed max size, scaled per frame)
      if (running) {
        const maxR = radius * 1.18 * (2 + 3 * 1.2)
        const sprite = getGlowSprite(color, maxR)
        const spriteExtent = maxR * 1.9 // matches outerScale in sprite
        X.drawImage(sprite, (x - spriteExtent) * S, (y - spriteExtent) * S, spriteExtent * 2 * S, spriteExtent * 2 * S)
      }

      // Outer orbit ring (rotating)
      if (running) {
        node.ringAngle += node.ringSpeed
        const ringR = r + 5
        // Dashed orbit
        X.setLineDash([3 * S, 4 * S])
        X.lineDashOffset = -node.ringAngle * 20
        X.beginPath(); X.arc(x * S, y * S, ringR * S, 0, Math.PI * 2)
        X.strokeStyle = color + '50'; X.lineWidth = S; X.stroke()
        X.setLineDash([])
        // Orbiting dot
        const dotX = x + Math.cos(node.ringAngle) * ringR
        const dotY = y + Math.sin(node.ringAngle) * ringR
        X.fillStyle = '#fff'
        X.beginPath(); X.arc(dotX * S, dotY * S, 1.5 * S, 0, Math.PI * 2); X.fill()
        // Second orbiting dot (opposite)
        const dot2X = x + Math.cos(node.ringAngle + Math.PI) * ringR
        const dot2Y = y + Math.sin(node.ringAngle + Math.PI) * ringR
        X.fillStyle = color
        X.beginPath(); X.arc(dot2X * S, dot2Y * S, 1 * S, 0, Math.PI * 2); X.fill()
      }

      // Core with gradient fill
      const coreGrad = X.createRadialGradient(
        (x - r * 0.3) * S, (y - r * 0.3) * S, 0,
        x * S, y * S, r * S
      )
      coreGrad.addColorStop(0, running ? '#fff' : color + '80')
      coreGrad.addColorStop(0.4, running ? color : color + '40')
      coreGrad.addColorStop(1, running ? color + 'cc' : color + '20')
      X.beginPath(); X.arc(x * S, y * S, r * S, 0, Math.PI * 2)
      X.fillStyle = coreGrad; X.fill()

      // Spinning arc (double for active)
      if (running) {
        const a1 = t * 0.05 + node.pulse
        const a2 = t * 0.05 + node.pulse + Math.PI
        X.beginPath(); X.arc(x * S, y * S, (r + 3) * S, a1, a1 + Math.PI * 0.6)
        X.strokeStyle = '#fff8'; X.lineWidth = 2 * S; X.stroke()
        X.beginPath(); X.arc(x * S, y * S, (r + 3) * S, a2, a2 + Math.PI * 0.4)
        X.strokeStyle = color + 'aa'; X.lineWidth = 1.5 * S; X.stroke()
      }

      // Kind icon
      const kindChar = kind === 'cron' ? '⏰' : kind === 'spawn' ? '🔀' : '💬'
      X.font = (2.5 * S) + 'px monospace'
      X.textAlign = 'center'
      X.fillText(kindChar, x * S, (y + 1.5) * S)

      // Name label with glow
      if (running) {
        T.shadowColor = color; T.shadowBlur = 6 * S
      }
      T.fillStyle = running ? '#fff' : '#778'
      T.font = sceneFont('name', 'bold')
      T.textAlign = 'center'
      T.fillText(name, x * S, (y + r + 7) * S)
      T.shadowBlur = 0

      // Status dot + text
      X.fillStyle = running ? '#4f4' : '#556'
      X.beginPath(); X.arc((x - 8) * S, (y + r + 11) * S, 1.5 * S, 0, Math.PI * 2); X.fill()
      T.fillStyle = running ? '#4f4' : '#556'
      T.font = sceneFont('status')
      T.textAlign = 'center'
      T.fillText(running ? i18nT('pages.scenes.neuralConstellationScene.active') : i18nT('pages.scenes.neuralConstellationScene.idle'), (x - 5) * S, (y + r + 12) * S)

      if (detail) {
        T.fillStyle = '#667'
        T.font = sceneFont('detail')
        T.fillText(detail, x * S, (y + r + 16) * S)
      }
      T.textAlign = 'start'
    }

    const drawCenterHub = (t: number) => {
      const pulse = 1 + Math.sin(t * 0.025) * 0.1
      const r = 16 * pulse
      const activeCount = nodesRef.current.filter(n => n.running).length

      // Multi-layer glow (cached sprite, scaled by pulse)
      const scaledGlowSize = hubGlowSize * pulse
      X.drawImage(hubGlowCanvas, CENTER.x * S - scaledGlowSize / 2, CENTER.y * S - scaledGlowSize / 2, scaledGlowSize, scaledGlowSize)

      // Core with gradient
      const coreGrad = X.createRadialGradient(
        (CENTER.x - 3) * S, (CENTER.y - 3) * S, 0,
        CENTER.x * S, CENTER.y * S, r * S
      )
      coreGrad.addColorStop(0, '#fff4')
      coreGrad.addColorStop(0.3, '#f90')
      coreGrad.addColorStop(1, '#c60')
      X.beginPath(); X.arc(CENTER.x * S, CENTER.y * S, r * S, 0, Math.PI * 2)
      X.fillStyle = '#0a0a1a'; X.fill()
      X.strokeStyle = '#f90'; X.lineWidth = 2 * S; X.stroke()

      // Inner glow ring
      X.beginPath(); X.arc(CENTER.x * S, CENTER.y * S, (r - 3) * S, 0, Math.PI * 2)
      X.strokeStyle = '#f904'; X.lineWidth = S; X.stroke()

      // Triple rotating arcs
      for (let i = 0; i < 3; i++) {
        const a = t * (0.015 + i * 0.005) + i * Math.PI * 0.67
        const arcR = r + 4 + i * 3
        X.beginPath(); X.arc(CENTER.x * S, CENTER.y * S, arcR * S, a, a + 0.8 + i * 0.2)
        X.strokeStyle = i === 0 ? '#f908' : i === 1 ? '#fa06' : '#f904'
        X.lineWidth = (2 - i * 0.5) * S; X.stroke()
      }

      // Data ring — dots orbiting the hub
      const dataRingR = r + 12
      for (let i = 0; i < 8; i++) {
        const da = (i / 8) * Math.PI * 2 + t * 0.01
        const dx = CENTER.x + Math.cos(da) * dataRingR
        const dy = CENTER.y + Math.sin(da) * dataRingR
        const dotAlpha = 0.2 + Math.sin(t * 0.04 + i) * 0.15
        X.fillStyle = `rgba(255,200,100,${dotAlpha})`
        X.beginPath(); X.arc(dx * S, dy * S, 1 * S, 0, Math.PI * 2); X.fill()
      }

      // Label
      T.shadowColor = '#f90'; T.shadowBlur = 4 * S
      T.fillStyle = '#f90'
      T.font = sceneFont('title', 'bold')
      T.textAlign = 'center'
      T.fillText(i18nT('pages.scenes.neuralConstellationScene.neural_network'), CENTER.x * S, (CENTER.y + 1.5) * S)
      T.shadowBlur = 0

      T.fillStyle = '#998'
      T.font = sceneFont('title')
      T.fillText(`${activeCount} active · ${nodesRef.current.length} nodes`, CENTER.x * S, (CENTER.y + 6) * S)
      T.textAlign = 'start'
    }

    const drawCounter = () => {
      T.fillStyle = '#445'
      T.font = sceneFont('label')
      T.fillText(`neural network · ${nodesRef.current.length} nodes`, 4 * S, (H - 4) * S)
    }

    /* ── Update ── */
    const update = (t: number) => {
      nodesRef.current.forEach(node => {
        const ddx = node.tx - node.x, ddy = node.ty - node.y
        const dist = Math.sqrt(ddx * ddx + ddy * ddy)
        if (dist > 0.5) {
          node.x += ddx * 0.03; node.y += ddy * 0.03
        }
        // Very subtle breathing
        node.x += Math.sin(t * 0.003 + node.pulse) * 0.015
        node.y += Math.cos(t * 0.002 + node.pulse) * 0.01

        // Energy accumulation for active nodes → burst
        if (node.running) {
          node.energy += 0.008
          node.burstCooldown = Math.max(0, node.burstCooldown - 1)
          if (node.energy > 1 && node.burstCooldown <= 0) {
            wavesRef.current.push({
              x: node.x, y: node.y, radius: node.radius,
              maxRadius: 35 + Math.random() * 20, color: node.color + '88', life: 0,
            })
            node.energy = 0
            node.burstCooldown = 300 + Math.random() * 200
          }
        }
      })

      // Advance and filter energy waves (decoupled from draw)
      wavesRef.current.forEach(w => { w.radius += 1.2; w.life++ })
      wavesRef.current = wavesRef.current.filter(w => w.radius < w.maxRadius)
    }

    /* ── Main draw ── */
    const draw = (t: number) => {
      T.clearRect(0, 0, W * S, H * S)
      // Deep space
      d(0, 0, W, H, '#06060f')
      const bgGrad = X.createRadialGradient(CENTER.x * S, CENTER.y * S, 0, CENTER.x * S, CENTER.y * S, 220 * S)
      bgGrad.addColorStop(0, 'rgba(15,20,45,0.5)')
      bgGrad.addColorStop(0.5, 'rgba(10,10,25,0.3)')
      bgGrad.addColorStop(1, 'transparent')
      X.fillStyle = bgGrad; X.fillRect(0, 0, W * S, H * S)

      drawNebulae(t)
      drawStars(t)
      drawShootingStars(t)
      drawSynapses(t)
      drawPackets()
      drawEnergyWaves()
      drawCenterHub(t)

      const sorted = [...nodesRef.current].sort((a, b) => a.y - b.y)
      sorted.forEach(n => drawNode(n, t))

      drawCounter()
    }

    return runSceneLoop(visibleRef, tickRef, update, draw)
  }, [])  

  useVisibleSync(visibleRef, visible)

  return (
    <div style={SCENE_CONTAINER_STYLE(W, H)}>
      <canvas ref={canvasRef} aria-label={i18nT('pages.scenes.neuralConstellationScene.neural_constellation_animation')} style={{ ...PIXEL_CANVAS_STYLE, ...canvasProps.style }} onMouseMove={canvasProps.onMouseMove} onMouseLeave={canvasProps.onMouseLeave} onClick={canvasProps.onClick} />
      <canvas ref={textRef} aria-label={i18nT('pages.scenes.neuralConstellationScene.neural_constellation_text_overlay')} style={TEXT_CANVAS_STYLE} />
      {tooltipEl}
    </div>
  )
}
