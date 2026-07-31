import { SCENE_SCALE } from './config'
import { useEffect, useRef } from 'react'
import type { AgentSource } from '../../hooks/useAgentSync'
import { isKnownAgent, markAgentsKnown, pruneAgents } from '../../hooks/sceneStateCache'
import { sceneFont, drawLabel, sceneLineHeight, drawSpeechBubble, SPEECH_BUBBLE_MS, KIRO_GHOST_PIXELS, TEXT_CANVAS_STYLE, SCENE_CONTAINER_STYLE, PIXEL_CANVAS_STYLE } from '../../hooks/sceneText'
import { initSceneCanvases, runSceneLoop, useVisibleSync } from '../../hooks/sceneCanvas'
import { useSceneInteraction, type SceneTooltipTheme } from '../../hooks/useSceneInteraction'
import { i18nT } from '../../i18n/t'

const GHOST_THEME: SceneTooltipTheme = { active: 'Haunting the codebase', idle: 'Drifting through walls' }

/* ── Types ── */
interface Ghost {
  id: string; name: string; kind: 'slot' | 'cron' | 'spawn'
  x: number; y: number; tx: number; ty: number
  running: boolean; color: string; detail: string
  phase: number; blinkTimer: number; enterProgress: number
  dir: number; outfit: Outfit
  lastMessage: string; msgAt: number
}
interface Star { x: number; y: number; size: number; twinkle: number; speed: number }
interface Wisp { x: number; y: number; drift: number; speed: number; alpha: number }
interface ShootingStar { x: number; y: number; vx: number; vy: number; life: number; maxLife: number }
interface Firefly { x: number; y: number; drift: number; speed: number; blink: number }

/* ── Constants ── */
const W = 440, H = 300, S = SCENE_SCALE
// All ghosts stay classic Kiro white — hats, glasses, and capes differentiate them
const GHOST_COLOR = '#e8ecf4'
const EYE_COLOR = '#14141e'

/* ── Accessories: each ghost gets a distinct look ── */
type Hat = 'witch' | 'top' | 'party' | 'beanie' | 'crown' | 'none'
type Glasses = 'round' | 'shades' | 'none'
interface Outfit { hat: Hat; glasses: Glasses; cape: boolean; capeColor: string }
const OUTFITS: Outfit[] = [
  { hat: 'none', glasses: 'round', cape: false, capeColor: '' },
  { hat: 'witch', glasses: 'none', cape: false, capeColor: '' },
  { hat: 'none', glasses: 'none', cape: true, capeColor: '#c0392b' },
  { hat: 'top', glasses: 'none', cape: false, capeColor: '' },
  { hat: 'none', glasses: 'shades', cape: true, capeColor: '#27408b' },
  { hat: 'beanie', glasses: 'none', cape: false, capeColor: '' },
  { hat: 'party', glasses: 'round', cape: false, capeColor: '' },
  { hat: 'crown', glasses: 'none', cape: true, capeColor: '#5b2c6f' },
]

/** Anchor positions for up to 8 ghosts — a loose two-row haunt */
const GHOST_SPOTS = [
  { x: 70, y: 120 }, { x: 170, y: 105 }, { x: 270, y: 125 }, { x: 360, y: 108 },
  { x: 115, y: 205 }, { x: 220, y: 195 }, { x: 320, y: 210 }, { x: 400, y: 190 },
]

interface Props {
  agents: AgentSource[]
  visible?: boolean
}

export default function GhostScene({ agents, visible = true }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const textRef = useRef<HTMLCanvasElement>(null)
  const tickRef = useRef(0)
  const ghostsRef = useRef<Ghost[]>([])
  const visibleRef = useRef(visible)
  const starsRef = useRef<Star[]>([])
  const wispsRef = useRef<Wisp[]>([])
  const shootingRef = useRef<ShootingStar[]>([])
  const firefliesRef = useRef<Firefly[]>([])
  const { canvasProps, tooltipEl } = useSceneInteraction(canvasRef, ghostsRef, W, H, GHOST_THEME, 14, undefined, agents)

  /* ── Init background elements ── */
  useEffect(() => {
    const stars: Star[] = []
    for (let i = 0; i < 70; i++) {
      stars.push({
        x: Math.random() * W, y: Math.random() * H * 0.55,
        size: Math.random() < 0.2 ? 1.5 : 0.8,
        twinkle: Math.random() * Math.PI * 2,
        speed: 0.01 + Math.random() * 0.03,
      })
    }
    starsRef.current = stars

    const wisps: Wisp[] = []
    for (let i = 0; i < 10; i++) {
      wisps.push({
        x: Math.random() * W, y: H * 0.6 + Math.random() * H * 0.35,
        drift: Math.random() * Math.PI * 2,
        speed: 0.004 + Math.random() * 0.008,
        alpha: 0.04 + Math.random() * 0.06,
      })
    }
    wispsRef.current = wisps

    const fireflies: Firefly[] = []
    for (let i = 0; i < 12; i++) {
      fireflies.push({
        x: Math.random() * W, y: H * 0.45 + Math.random() * H * 0.45,
        drift: Math.random() * Math.PI * 2,
        speed: 0.006 + Math.random() * 0.012,
        blink: Math.random() * Math.PI * 2,
      })
    }
    firefliesRef.current = fireflies
  }, [])

  /* ── Sync agents → ghosts ── */
  useEffect(() => {
    const existing = ghostsRef.current
    const newGhosts: Ghost[] = []

    agents.slice(0, GHOST_SPOTS.length).forEach((src, i) => {
      const prev = existing.find(g => g.id === src.id)
      const spot = GHOST_SPOTS[i]

      if (prev) {
        prev.name = src.name; prev.running = src.running; prev.detail = src.detail
        prev.tx = spot.x; prev.ty = spot.y; prev.kind = src.kind
        if ((src.lastMessage || '') !== prev.lastMessage) { prev.lastMessage = src.lastMessage || ''; prev.msgAt = Date.now() }
        newGhosts.push(prev)
      } else {
        const known = isKnownAgent('ghost', src.id)
        newGhosts.push({
          id: src.id, name: src.name, kind: src.kind,
          x: spot.x, y: known ? spot.y : H + 30,
          tx: spot.x, ty: spot.y,
          running: src.running, color: GHOST_COLOR,
          detail: src.detail, phase: Math.random() * Math.PI * 2,
          blinkTimer: 120 + Math.random() * 300,
          enterProgress: known ? 1 : 0, dir: 1,
          outfit: OUTFITS[i % OUTFITS.length],
          lastMessage: src.lastMessage || '', msgAt: 0,
        })
        markAgentsKnown('ghost', [src.id])
      }
    })
    ghostsRef.current = newGhosts
    pruneAgents('ghost', newGhosts.map(g => g.id))
  }, [agents])

  /* ── Canvas render loop ── */
  useEffect(() => {
    const { X, T, d } = initSceneCanvases(canvasRef.current!, textRef.current!, W, H, S)

    /** Kiro ghost bitmap, 24×28 — shared trace from the reference art (eye holes filled; eyes drawn as overlay) */
    const GHOST_PIXELS = KIRO_GHOST_PIXELS

    /** Draw one Kiro ghost, 24×28, anchored at top-left (gx, gy) */
    const drawGhost = (gx: number, gy: number, color: string, g: Ghost, t: number) => {
      const o = g.outfit
      // Cape — drawn behind the body, fluttering with time
      if (o.cape) {
        const flutter = Math.sin(t * 0.08 + g.phase) * 1.5
        const back = g.dir > 0 ? gx - 3 : gx + 23
        d(back, gy + 6, 4, 14 + flutter, o.capeColor)
        d(back + (g.dir > 0 ? -1 : 1), gy + 9, 2, 9 + flutter, o.capeColor)
        // Collar over the shoulders
        d(gx + 4, gy + 6, 17, 1.8, o.capeColor)
      }
      // Body from bitmap
      X.fillStyle = color
      GHOST_PIXELS.forEach((row, ry) => {
        let run = -1
        for (let cx = 0; cx <= row.length; cx++) {
          const on = cx < row.length && row[cx] === '#'
          if (on && run < 0) run = cx
          else if (!on && run >= 0) {
            X.fillRect((gx + run) * S, (gy + ry) * S, (cx - run) * S, 1 * S)
            run = -1
          }
        }
      })
      // Eyes — tall rounded ovals right of center (traced from the reference); blink to 1px
      const eyeShift = g.dir > 0 ? 0.5 : -0.5
      const blinking = g.blinkTimer < 6
      if (blinking) {
        d(gx + 11 + eyeShift, gy + 9, 3, 1, EYE_COLOR)
        d(gx + 17 + eyeShift, gy + 9, 3, 1, EYE_COLOR)
      } else {
        // Rounded: 1px-narrower top and bottom caps
        d(gx + 11.5 + eyeShift, gy + 7, 2, 1, EYE_COLOR)
        d(gx + 11 + eyeShift, gy + 8, 3, 3, EYE_COLOR)
        d(gx + 11.5 + eyeShift, gy + 11, 2, 1, EYE_COLOR)
        d(gx + 17.5 + eyeShift, gy + 7, 2, 1, EYE_COLOR)
        d(gx + 17 + eyeShift, gy + 8, 3, 3, EYE_COLOR)
        d(gx + 17.5 + eyeShift, gy + 11, 2, 1, EYE_COLOR)
      }
      // Glasses
      if (o.glasses === 'round') {
        d(gx + 10 + eyeShift, gy + 6.2, 5, 0.9, '#3a3a4a')
        d(gx + 16 + eyeShift, gy + 6.2, 5, 0.9, '#3a3a4a')
        d(gx + 10 + eyeShift, gy + 6.2, 0.9, 6.5, '#3a3a4a')
        d(gx + 14.1 + eyeShift, gy + 6.2, 0.9, 6.5, '#3a3a4a')
        d(gx + 20.1 + eyeShift, gy + 6.2, 0.9, 6.5, '#3a3a4a')
        d(gx + 10 + eyeShift, gy + 11.8, 5, 0.9, '#3a3a4a')
        d(gx + 16 + eyeShift, gy + 11.8, 5, 0.9, '#3a3a4a')
        d(gx + 14.9 + eyeShift, gy + 7.5, 1.2, 0.9, '#3a3a4a')
      } else if (o.glasses === 'shades') {
        d(gx + 10 + eyeShift, gy + 6.8, 4.6, 4.4, '#111')
        d(gx + 15.6 + eyeShift, gy + 6.8, 4.6, 4.4, '#111')
        d(gx + 14.4 + eyeShift, gy + 7.4, 1.4, 1, '#111')
        // Lens glint
        d(gx + 10.8 + eyeShift, gy + 7.5, 1.2, 0.9, '#8fa8ff')
        d(gx + 16.4 + eyeShift, gy + 7.5, 1.2, 0.9, '#8fa8ff')
      }
      // Hats — perched on the dome (apex ~cols 10-16)
      if (o.hat === 'witch') {
        d(gx + 6, gy - 1, 16, 1.8, '#2d1b4e')
        d(gx + 10, gy - 4.5, 7, 3.5, '#2d1b4e')
        d(gx + 11.8, gy - 7.5, 3.4, 3.4, '#2d1b4e')
        d(gx + 10, gy - 2, 7, 1.1, '#8e44ad')
      } else if (o.hat === 'top') {
        d(gx + 7, gy - 1, 13, 1.4, '#181820')
        d(gx + 9, gy - 7, 9, 6, '#181820')
        d(gx + 9, gy - 2.2, 9, 1.2, '#b03a2e')
      } else if (o.hat === 'party') {
        d(gx + 11, gy - 2.4, 4.6, 2.4, '#f39c12')
        d(gx + 12, gy - 4.8, 2.7, 2.4, '#e74c3c')
        d(gx + 12.7, gy - 6.4, 1.2, 1.6, '#f1c40f')
      } else if (o.hat === 'beanie') {
        d(gx + 8, gy - 1.2, 11, 2.8, '#16a085')
        d(gx + 8, gy + 0.6, 11, 1, '#0e6655')
        d(gx + 12.7, gy - 2.8, 1.8, 1.8, '#f4d03f')
      } else if (o.hat === 'crown') {
        d(gx + 9.5, gy - 2.4, 7.5, 2.4, '#f1c40f')
        d(gx + 9.5, gy - 4, 1.7, 1.9, '#f1c40f')
        d(gx + 12.4, gy - 4, 1.7, 1.9, '#f1c40f')
        d(gx + 15.3, gy - 4, 1.7, 1.9, '#f1c40f')
        d(gx + 12.8, gy - 1.6, 1.1, 1.1, '#e74c3c')
      }
      // Blush when active
      if (g.running && !blinking) {
        X.globalAlpha = 0.35
        d(gx + 8 + eyeShift, gy + 12.5, 2, 1.1, '#ff8899')
        d(gx + 21 + eyeShift, gy + 12.5, 2, 1.1, '#ff8899')
        X.globalAlpha = 1
      }
    }

    const drawBackground = (t: number) => {
      // Friendly twilight gradient — cozy, only sort of spooky
      const grad = X.createLinearGradient(0, 0, 0, H * S)
      grad.addColorStop(0, '#171232')
      grad.addColorStop(0.6, '#251c48')
      grad.addColorStop(1, '#332757')
      X.fillStyle = grad
      X.fillRect(0, 0, W * S, H * S)

      // Stars
      starsRef.current.forEach(star => {
        const alpha = 0.25 + Math.sin(t * star.speed + star.twinkle) * 0.32
        X.globalAlpha = Math.max(0.06, alpha)
        X.fillStyle = '#dbe2f7'
        X.fillRect(star.x * S, star.y * S, star.size * S, star.size * S)
      })
      X.globalAlpha = 1

      // Shooting stars — spawn occasionally, streak across the sky
      if (t % 140 === 0 && Math.random() < 0.5 && shootingRef.current.length < 3) {
        const fromLeft = Math.random() < 0.5
        shootingRef.current.push({
          x: fromLeft ? -5 : W + 5, y: 10 + Math.random() * H * 0.35,
          vx: (fromLeft ? 1 : -1) * (2.2 + Math.random() * 2),
          vy: 0.4 + Math.random() * 0.9,
          life: 0, maxLife: 45 + Math.random() * 30,
        })
      }
      shootingRef.current.forEach(ss => {
        ss.x += ss.vx; ss.y += ss.vy; ss.life++
        const alpha = 1 - ss.life / ss.maxLife
        for (let i = 0; i < 7; i++) {
          const ta = alpha * (1 - i * 0.14)
          X.fillStyle = `rgba(255,240,200,${Math.max(0, ta)})`
          X.fillRect((ss.x - ss.vx * i * 0.6) * S, (ss.y - ss.vy * i * 0.6) * S, 2 * S, 1 * S)
        }
        X.fillStyle = `rgba(255,255,255,${alpha})`
        X.fillRect(ss.x * S, ss.y * S, 2 * S, 2 * S)
      })
      shootingRef.current = shootingRef.current.filter(ss => ss.life < ss.maxLife)

      // Moon with glow + craters
      const mx = W - 62, my = 46
      const glow = X.createRadialGradient(mx * S, my * S, 0, mx * S, my * S, 40 * S)
      glow.addColorStop(0, 'rgba(240,238,220,0.16)')
      glow.addColorStop(1, 'transparent')
      X.fillStyle = glow
      X.fillRect((mx - 40) * S, (my - 40) * S, 80 * S, 80 * S)
      X.fillStyle = '#efe9d4'
      X.beginPath(); X.arc(mx * S, my * S, 17 * S, 0, Math.PI * 2); X.fill()
      X.fillStyle = '#ddd5bc'
      X.beginPath(); X.arc((mx - 5) * S, (my - 4) * S, 3.4 * S, 0, Math.PI * 2); X.fill()
      X.beginPath(); X.arc((mx + 6) * S, (my + 5) * S, 2.4 * S, 0, Math.PI * 2); X.fill()
      X.beginPath(); X.arc((mx + 2) * S, (my - 8) * S, 1.6 * S, 0, Math.PI * 2); X.fill()

      // Distant hills
      X.fillStyle = '#1e1740'
      X.beginPath()
      X.moveTo(0, H * 0.82 * S)
      X.quadraticCurveTo(W * 0.25 * S, H * 0.72 * S, W * 0.5 * S, H * 0.8 * S)
      X.quadraticCurveTo(W * 0.75 * S, H * 0.88 * S, W * S, H * 0.78 * S)
      X.lineTo(W * S, H * S); X.lineTo(0, H * S)
      X.fill()

      // Fireflies — warm blinking dots drifting near the ground
      firefliesRef.current.forEach(ff => {
        const fx = ff.x + Math.sin(t * ff.speed + ff.drift) * 18
        const fy = ff.y + Math.cos(t * ff.speed * 0.8 + ff.drift) * 8
        const glow = Math.max(0, Math.sin(t * 0.03 + ff.blink))
        if (glow > 0.15) {
          X.globalAlpha = glow * 0.8
          X.fillStyle = '#ffe08a'
          X.fillRect(fx * S, fy * S, 1.2 * S, 1.2 * S)
          X.globalAlpha = glow * 0.25
          X.fillRect((fx - 1) * S, (fy - 1) * S, 3.2 * S, 3.2 * S)
        }
      })
      X.globalAlpha = 1

      // Ground fog wisps
      wispsRef.current.forEach(wp => {
        const wx = wp.x + Math.sin(t * wp.speed + wp.drift) * 24
        X.globalAlpha = wp.alpha + Math.sin(t * wp.speed * 1.4 + wp.drift) * 0.02
        X.fillStyle = '#a9b4d8'
        X.beginPath(); X.ellipse(wx * S, wp.y * S, 34 * S, 6 * S, 0, 0, Math.PI * 2); X.fill()
      })
      X.globalAlpha = 1

      // Title
      T.fillStyle = '#a89ee0'; T.font = sceneFont('title', 'bold')
      T.fillText('Kiro Haunt', (W / 2 - 22) * S, 26 * S)
      T.fillStyle = '#7a70ad'; T.font = sceneFont('detail')
      T.fillText('friendly hauntings only', (W / 2 - 20) * S, 34 * S)
    }

    /* ── Update ── */
    const update = (t: number) => {
      ghostsRef.current.forEach(g => {
        if (g.enterProgress < 1) g.enterProgress = Math.min(1, g.enterProgress + 0.02)
        const ddx = g.tx - g.x, ddy = g.ty - g.y
        if (Math.abs(ddx) > 0.5) { g.x += ddx * 0.04; g.dir = ddx > 0 ? 1 : -1 }
        if (Math.abs(ddy) > 0.5) g.y += ddy * 0.04
        g.blinkTimer -= 1
        if (g.blinkTimer <= 0) g.blinkTimer = 140 + Math.random() * 320
        void t
      })
    }

    /* ── Main draw ── */
    const draw = (t: number) => {
      T.clearRect(0, 0, W * S, H * S)
      drawBackground(t)

      const sorted = [...ghostsRef.current].sort((a, b) => a.y - b.y)
      sorted.forEach(g => {
        const bobAmp = g.running ? 2.6 : 1.1
        const bobSpeed = g.running ? 0.055 : 0.022
        const bob = Math.sin(t * bobSpeed + g.phase) * bobAmp
        const gx = g.x - 12
        const gy = g.y - 14 + bob

        X.globalAlpha = g.enterProgress * (g.running ? 1 : 0.72)

        // Spectral glow under active ghosts
        if (g.running) {
          const glow = X.createRadialGradient(g.x * S, (g.y + bob) * S, 0, g.x * S, (g.y + bob) * S, 24 * S)
          glow.addColorStop(0, g.color + '30')
          glow.addColorStop(1, 'transparent')
          X.fillStyle = glow
          X.fillRect((g.x - 24) * S, (g.y + bob - 24) * S, 48 * S, 48 * S)
        }

        // Faint shadow on the ground
        X.globalAlpha = g.enterProgress * 0.18
        X.fillStyle = '#000'
        X.beginPath(); X.ellipse(g.x * S, (g.y + 17) * S, (8.5 - bob * 0.6) * S, 2.2 * S, 0, 0, Math.PI * 2); X.fill()
        X.globalAlpha = g.enterProgress * (g.running ? 1 : 0.72)

        drawGhost(gx, gy, g.color, g, t)
        X.globalAlpha = 1

        // Status label above
        drawLabel(T, g.running ? 'haunting' : 'idle', g.x * S, (gy - 11) * S, { role: 'status', color: g.running ? '#4f4' : '#888', bgColor: 'rgba(0,0,0,0.5)', align: 'center', scale: S })
      // Real-message speech bubble — appears when the session's latest message changes
      if (g.lastMessage && Date.now() - g.msgAt < SPEECH_BUBBLE_MS) {
        const msgAge = Date.now() - g.msgAt
        const msgAlpha = msgAge > SPEECH_BUBBLE_MS - 1000 ? (SPEECH_BUBBLE_MS - msgAge) / 1000 : 1
        drawSpeechBubble(T, g.lastMessage, g.x * S, (gy - 15) * S, { scale: S, alpha: msgAlpha })
      }


        // Name (wraps up to ~45 chars) + detail below
        const nameLines = drawLabel(T, g.name, g.x * S, (g.y + 21) * S, { role: 'name', weight: 'bold', color: '#fff', align: 'center', scale: S, maxWidth: 64 * S })
        if (g.detail) {
          drawLabel(T, g.detail, g.x * S, (g.y + 26) * S + (nameLines - 1) * sceneLineHeight('name'), { role: 'detail', color: '#8f86c9', align: 'center', scale: S })
        }
      })

      // Counter
      T.fillStyle = '#4c4670'
      T.font = sceneFont('label')
      T.fillText(`kiro haunt · ${ghostsRef.current.length} ghosts`, 4 * S, (H - 4) * S)
    }

    return runSceneLoop(visibleRef, tickRef, update, draw)
  }, [])

  useVisibleSync(visibleRef, visible)

  return (
    <div style={SCENE_CONTAINER_STYLE(W, H)}>
      <canvas ref={canvasRef} aria-label={i18nT('pages.scenes.ghostScene.kiro_ghost_haunt_animation')} style={{ ...PIXEL_CANVAS_STYLE, ...canvasProps.style }} onMouseMove={canvasProps.onMouseMove} onMouseLeave={canvasProps.onMouseLeave} onClick={canvasProps.onClick} />
      <canvas ref={textRef} aria-label={i18nT('pages.scenes.ghostScene.kiro_ghost_haunt_text_overlay')} style={TEXT_CANVAS_STYLE} />
      {tooltipEl}
    </div>
  )
}
