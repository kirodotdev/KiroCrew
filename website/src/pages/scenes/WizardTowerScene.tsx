import { SCENE_SCALE } from './config'
import { useEffect, useRef } from 'react'
import type { AgentSource } from '../../hooks/useAgentSync'
import { isKnownAgent, markAgentsKnown, pruneAgents } from '../../hooks/sceneStateCache'
import { sceneFont, drawLabel, sceneLineHeight, drawSpeechBubble, SPEECH_BUBBLE_MS, TEXT_CANVAS_STYLE, SCENE_CONTAINER_STYLE, PIXEL_CANVAS_STYLE } from '../../hooks/sceneText'
import { initSceneCanvases, runSceneLoop, useVisibleSync } from '../../hooks/sceneCanvas'
import { useSceneInteraction, type SceneTooltipTheme } from '../../hooks/useSceneInteraction'

const WIZARD_THEME: SceneTooltipTheme = { active: 'Casting spells', idle: 'Studying grimoire' }

/* ── Types ── */
interface Wizard {
  id: string; name: string; kind: 'slot' | 'cron' | 'spawn'
  x: number; y: number; tx: number; ty: number
  benchIdx: number; color: string; robeColor: string
  running: boolean; detail: string
  hatBob: number; castTimer: number; activity: 'entering' | 'bench' | 'circle' | 'shelf'
  lastMessage: string; msgAt: number
}
interface MagicParticle {
  x: number; y: number; vx: number; vy: number
  life: number; maxLife: number; color: string; size: number
}

/* ── Constants ── */
const W = 480, H = 340, S = SCENE_SCALE
const MAX_BENCHES = 8
const ROBE_COLORS = ['#8e44ad', '#2980b9', '#c0392b', '#27ae60', '#d35400', '#16a085', '#e84393', '#f39c12']
const HAT_COLORS = ['#6c3483', '#1f618d', '#922b21', '#1e8449', '#a04000', '#117a65', '#c2185b', '#d4ac0d']
const BENCH_POSITIONS = [
  { x: 30, y: 140 }, { x: 120, y: 140 }, { x: 210, y: 140 }, { x: 300, y: 140 },
  { x: 30, y: 230 }, { x: 120, y: 230 }, { x: 210, y: 230 }, { x: 300, y: 230 },
]
const CIRCLE_POS = { x: 390, y: 190 }
const DOOR_POS = { x: 0, y: 130, w: 14, h: 55 }

const COL = {
  stone: '#3a3040', stoneDark: '#2a2030', stoneLight: '#4a4050',
  floor: '#2e2438', floorAlt: '#362c40',
  wood: '#5c3a1e', woodDark: '#4a2e16', woodLight: '#7a5030',
  cauldron: '#333', cauldronRim: '#555', potion: '#4cff88',
  potionBubble: '#7fff9f', potionGlow: 'rgba(76,255,136,0.15)',
  fire: '#f90', fireRed: '#e74c3c', ember: '#ff6',
  book: '#8e44ad', bookAlt: '#c0392b', bookBlue: '#2980b9',
  scroll: '#f5e6c8', scrollDark: '#d4c4a0',
  crystal: '#9b59b6', crystalGlow: 'rgba(155,89,182,0.2)',
  candle: '#f1c40f', candleWax: '#eee', candleFlame: '#ff9',
  shelf: '#5c4033', window: '#1a2a4a', moonlight: 'rgba(200,220,255,0.06)',
  rune: '#f90', runeGlow: 'rgba(255,153,0,0.12)',
}

const SPELL_WORDS = [
  'Compilo!', 'Deployo!', 'Refactorus!', 'Debuggify!',
  'Mergicus!', 'Testifex!', 'Optimax!', 'Commitum!',
]

interface Props {
  agents: AgentSource[]
  visible?: boolean
}

export default function WizardTowerScene({ agents, visible = true }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const textRef = useRef<HTMLCanvasElement>(null)
  const tickRef = useRef(0)
  const wizardsRef = useRef<Wizard[]>([])
  const particlesRef = useRef<MagicParticle[]>([])
  const spellRef = useRef<{ text: string; x: number; y: number; life: number } | null>(null)
  const visibleRef = useRef(visible)
  const { canvasProps, tooltipEl } = useSceneInteraction(canvasRef, wizardsRef, W, H, WIZARD_THEME, 10, undefined, agents)

  /* ── Sync agents → wizards ── */
  useEffect(() => {
    const existing = wizardsRef.current
    const newWizards: Wizard[] = []

    agents.forEach((src, i) => {
      const prev = existing.find(w => w.id === src.id)
      if (prev) {
        prev.name = src.name; prev.running = src.running; prev.detail = src.detail; prev.kind = src.kind
        if ((src.lastMessage || '') !== prev.lastMessage) { prev.lastMessage = src.lastMessage || ''; prev.msgAt = Date.now() }
        newWizards.push(prev)
      } else {
        const benchIdx = BENCH_POSITIONS.findIndex((_, bi) => !existing.some(w => w.benchIdx === bi) && !newWizards.some(w => w.benchIdx === bi))
        if (benchIdx < 0) return
        const bp = BENCH_POSITIONS[benchIdx]
        const known = isKnownAgent('wizard', src.id)
        newWizards.push({
          id: src.id, name: src.name, kind: src.kind,
          x: known ? bp.x + 15 : DOOR_POS.x + 7,
          y: known ? bp.y + 20 : DOOR_POS.y + 28,
          tx: bp.x + 15, ty: bp.y + 20,
          benchIdx, color: ROBE_COLORS[i % ROBE_COLORS.length],
          robeColor: HAT_COLORS[i % HAT_COLORS.length],
          running: src.running, detail: src.detail,
          lastMessage: src.lastMessage || '', msgAt: 0,
          hatBob: Math.random() * Math.PI * 2, castTimer: 0,
          activity: known ? 'bench' : 'entering',
        })
        markAgentsKnown('wizard', [src.id])
      }
    })
    wizardsRef.current = newWizards

    pruneAgents('wizard', newWizards.map(w => w.id))
  }, [agents])

  /* ── Pixel helper ── */

  /* ── Canvas render loop ── */
  useEffect(() => {
    const { X, T, d } = initSceneCanvases(canvasRef.current!, textRef.current!, W, H, S)

    const spawnMagic = (mx: number, my: number, color: string, count = 3) => {
      for (let i = 0; i < count; i++) {
        particlesRef.current.push({
          x: mx, y: my,
          vx: (Math.random() - 0.5) * 0.4, vy: -0.2 - Math.random() * 0.3,
          life: 0, maxLife: 80 + Math.random() * 120,
          color, size: 0.8 + Math.random() * 1.2,
        })
      }
    }

    /* ── Draw: environment ── */
    const drawWalls = () => {
      // Stone walls
      d(0, 0, W, 90, COL.stone)
      for (let i = 0; i < W; i += 20) {
        for (let j = 0; j < 90; j += 12) {
          const off = (j / 12 & 1) ? 10 : 0
          d(i + off, j, 19, 11, ((i + j) % 40 < 20) ? COL.stoneDark : COL.stoneLight)
          d(i + off, j + 11, 19, 1, COL.stoneDark)
          d(i + off + 19, j, 1, 12, COL.stoneDark)
        }
      }
      // Floor
      for (let i = 0; i < W; i += 16) {
        for (let j = 90; j < H; j += 16) {
          d(i, j, 16, 16, ((i / 16 + j / 16) & 1) ? COL.floorAlt : COL.floor)
        }
      }
      // Trim
      d(0, 88, W, 3, COL.woodDark)
    }

    const drawArchwayDoor = (t: number) => {
      const { x, y, w, h } = DOOR_POS
      // Stone arch
      d(x, y, w, h, COL.stoneDark)
      d(x + 2, y + 4, w - 4, h - 4, COL.woodDark)
      // Arch top
      for (let i = 0; i < 5; i++) {
        d(x + 2 + i, y + 1 - Math.abs(i - 2), w - 4 - i * 2, 1, COL.stoneLight)
      }
      // Glow when entering
      const entering = wizardsRef.current.some(w => w.activity === 'entering')
      if (entering && ((t >> 3) & 1)) {
        X.fillStyle = 'rgba(155,89,182,0.1)'
        X.fillRect(x * S, y * S, w * S, h * S)
      }
      // Runes on arch
      X.fillStyle = COL.rune; X.font = (2 * S) + 'px monospace'
      X.fillText('⚡', (x + 3) * S, (y - 2) * S)
    }

    const drawWorkbench = (bx: number, by: number, occupied: boolean, idx: number, t: number) => {
      // Bench surface
      d(bx, by + 16, 40, 3, COL.woodLight)
      d(bx, by + 15, 40, 1, COL.wood)
      // Legs
      d(bx + 2, by + 19, 2, 10, COL.wood)
      d(bx + 36, by + 19, 2, 10, COL.wood)

      // Cauldron (left side)
      d(bx + 2, by + 8, 10, 8, COL.cauldron)
      d(bx + 1, by + 7, 12, 1, COL.cauldronRim)
      if (occupied) {
        // Bubbling potion
        d(bx + 3, by + 9, 8, 5, COL.potion)
        // Bubbles
        if (t % 12 < 6) d(bx + 4 + (t % 5), by + 8, 2, 2, COL.potionBubble)
        if (t % 18 < 9) d(bx + 7 + (t % 3), by + 7, 1, 1, COL.potionBubble)
        // Glow
        X.fillStyle = COL.potionGlow
        X.fillRect((bx + 1) * S, (by + 14) * S, 12 * S, 6 * S)
        // Fire under cauldron
        const flicker = (t >> 2) & 1
        d(bx + 3, by + 16, 3, 1, COL.fire)
        d(bx + 7, by + 16, 2, 1, COL.fireRed)
        if (flicker) d(bx + 5, by + 15, 2, 1, COL.ember)
      } else {
        d(bx + 3, by + 12, 8, 2, '#222')
      }

      // Spell book (right side)
      d(bx + 22, by + 10, 12, 6, occupied ? COL.book : '#444')
      d(bx + 22, by + 10, 1, 6, COL.woodDark)
      if (occupied) {
        // Text lines on book
        for (let i = 0; i < 3; i++) {
          d(bx + 24, by + 11 + i * 1.5, 4 + (i % 2) * 2, 0.5, COL.scroll)
        }
        // Floating rune above book
        if ((t + idx * 40) % 80 < 40) {
          X.fillStyle = COL.rune; X.font = (2.5 * S) + 'px monospace'
          X.fillText('✦', (bx + 26) * S, (by + 7) * S)
        }
      }

      // Candle
      const cx = bx + 16, cy = by + 8
      d(cx, cy + 2, 2, 5, COL.candleWax)
      if (occupied) {
        const fh = 2 + ((t >> 3) & 1)
        d(cx, cy - fh + 2, 2, fh, COL.candleFlame)
        d(cx + 0.5, cy - fh + 1, 1, 1, '#fff')
        // Candle glow
        const grad = X.createRadialGradient((cx + 1) * S, cy * S, 0, (cx + 1) * S, cy * S, 12 * S)
        grad.addColorStop(0, 'rgba(255,200,50,0.08)')
        grad.addColorStop(1, 'transparent')
        X.fillStyle = grad
        X.fillRect((cx - 12) * S, (cy - 12) * S, 26 * S, 26 * S)
      }

      // Nameplate
      if (!occupied) {
        drawLabel(T, 'empty', (bx + 20) * S, (by + 4) * S, { role: 'label', color: '#555', bgColor: '#2a2030', align: 'center', scale: S })
      }
    }

    const drawSummoningCircle = (t: number) => {
      const { x, y } = CIRCLE_POS
      // Outer ring
      X.strokeStyle = COL.rune
      X.lineWidth = 1.5 * S
      X.beginPath(); X.arc(x * S, y * S, 28 * S, 0, Math.PI * 2); X.stroke()
      // Inner ring
      X.beginPath(); X.arc(x * S, y * S, 20 * S, 0, Math.PI * 2); X.stroke()
      // Rotating runes
      for (let i = 0; i < 6; i++) {
        const a = (i / 6) * Math.PI * 2 + t * 0.01
        const rx = x + Math.cos(a) * 24, ry = y + Math.sin(a) * 24
        X.fillStyle = COL.rune; X.font = (2 * S) + 'px monospace'
        X.fillText('✧', rx * S, ry * S)
      }
      // Center glow
      const grad = X.createRadialGradient(x * S, y * S, 0, x * S, y * S, 30 * S)
      grad.addColorStop(0, COL.runeGlow)
      grad.addColorStop(1, 'transparent')
      X.fillStyle = grad
      X.fillRect((x - 30) * S, (y - 30) * S, 60 * S, 60 * S)
      // Pentagram
      X.strokeStyle = 'rgba(255,153,0,0.15)'; X.lineWidth = S
      for (let i = 0; i < 5; i++) {
        const a1 = (i / 5) * Math.PI * 2 - Math.PI / 2 + t * 0.005
        const a2 = ((i + 2) / 5) * Math.PI * 2 - Math.PI / 2 + t * 0.005
        X.beginPath()
        X.moveTo((x + Math.cos(a1) * 18) * S, (y + Math.sin(a1) * 18) * S)
        X.lineTo((x + Math.cos(a2) * 18) * S, (y + Math.sin(a2) * 18) * S)
        X.stroke()
      }
    }

    const drawShelf = (sx: number, sy: number, t: number) => {
      d(sx, sy, 30, 2, COL.shelf)
      d(sx, sy - 18, 30, 2, COL.shelf)
      // Potions on shelf
      const potionColors = ['#e74c3c', '#3498db', '#2ecc71', '#f39c12', '#9b59b6']
      for (let i = 0; i < 4; i++) {
        const px = sx + 2 + i * 7, py = sy - 8
        d(px, py, 4, 8, '#555')
        d(px + 0.5, py + 1, 3, 6, potionColors[i % potionColors.length])
        d(px + 1, py - 1, 2, 1, '#777')
        // Glow
        if (Math.sin(t * 0.02 + i * 1.5) > 0.5) {
          X.fillStyle = potionColors[i % potionColors.length] + '15'
          X.fillRect((px - 1) * S, (py - 1) * S, 6 * S, 10 * S)
        }
      }
      // Books on upper shelf
      const bookColors = [COL.book, COL.bookAlt, COL.bookBlue, '#27ae60']
      let bx = sx + 1
      for (let i = 0; i < 3; i++) {
        const bh = 8 + (i * 3 % 4)
        d(bx, sy - 18 - bh, 6, bh, bookColors[i % bookColors.length])
        bx += 8
      }
    }

    const drawWindow = (wx: number, wy: number, t: number) => {
      // Gothic arch window
      d(wx, wy, 24, 30, '#444')
      d(wx + 1, wy + 1, 22, 28, COL.window)
      // Moon
      d(wx + 14, wy + 5, 6, 6, '#dde')
      d(wx + 15, wy + 4, 4, 4, COL.window)
      // Stars
      for (let i = 0; i < 4; i++) {
        const stx = wx + 3 + (i * 6 % 18), sty = wy + 3 + (i * 5 % 20)
        if (Math.sin(t * 0.03 + i * 2) > 0.2) d(stx, sty, 1, 1, '#aac')
      }
      // Arch divider
      d(wx + 11, wy, 2, 30, '#444')
      // Moonlight beam
      X.fillStyle = COL.moonlight
      X.fillRect((wx + 5) * S, (wy + 30) * S, 14 * S, 40 * S)
    }

    const drawCrystalBall = (cx: number, cy: number, t: number) => {
      // Stand
      d(cx + 2, cy + 8, 6, 3, COL.woodDark)
      d(cx + 3, cy + 6, 4, 2, COL.wood)
      // Ball
      X.beginPath(); X.arc((cx + 5) * S, (cy + 3) * S, 5 * S, 0, Math.PI * 2)
      X.fillStyle = '#2a1a3a'; X.fill()
      X.strokeStyle = COL.crystal; X.lineWidth = S; X.stroke()
      // Inner swirl
      const sa = t * 0.03
      X.beginPath(); X.arc((cx + 5) * S, (cy + 3) * S, 3 * S, sa, sa + 2)
      X.strokeStyle = COL.crystal + '60'; X.lineWidth = S; X.stroke()
      // Glow
      const grad = X.createRadialGradient((cx + 5) * S, (cy + 3) * S, 0, (cx + 5) * S, (cy + 3) * S, 8 * S)
      grad.addColorStop(0, COL.crystalGlow)
      grad.addColorStop(1, 'transparent')
      X.fillStyle = grad
      X.fillRect((cx - 3) * S, (cy - 5) * S, 16 * S, 16 * S)
    }

    const drawWizard = (w: Wizard, t: number) => {
      const bx = w.x | 0, by = w.y | 0
      const mv = Math.abs(w.x - w.tx) > 1.5 || Math.abs(w.y - w.ty) > 1.5
      const bob = mv ? (Math.sin(t * 0.12 + w.x) | 0) : 0

      // Shadow
      X.fillStyle = 'rgba(0,0,0,0.15)'
      X.fillRect((bx + 1) * S, (by + 11) * S, 6 * S, 2 * S)

      // Robe (body)
      d(bx, by + bob, 8, 10, w.color)
      d(bx - 1, by + 6 + bob, 10, 4, w.color) // robe flare

      // Head
      d(bx + 1, by - 5 + bob, 6, 5, '#fdd')

      // Wizard hat
      d(bx - 1, by - 6 + bob, 10, 2, w.robeColor)
      d(bx + 1, by - 10 + bob, 6, 4, w.robeColor)
      d(bx + 2, by - 13 + bob, 4, 3, w.robeColor)
      d(bx + 3, by - 15 + bob, 2, 2, w.robeColor)
      // Hat star
      if (w.running) {
        X.fillStyle = '#ff0'; X.font = (1.5 * S) + 'px monospace'
        X.fillText('★', (bx + 3) * S, (by - 11 + bob) * S)
      }

      // Eyes
      const blink = (t % 100) < 3
      if (!blink) {
        d(bx + 2, by - 3 + bob, 1, 1, '#333')
        d(bx + 5, by - 3 + bob, 1, 1, '#333')
      }
      // Beard
      d(bx + 2, by + bob, 4, 3, '#ccc')
      d(bx + 3, by + 3 + bob, 2, 2, '#bbb')

      // Staff (when walking)
      if (mv) {
        d(bx + 8, by - 8 + bob, 1, 18, COL.wood)
        d(bx + 7, by - 10 + bob, 3, 3, COL.crystal)
        // Staff glow
        if (w.running) spawnMagic(bx + 8, by - 10 + bob, w.color, 1)
      }

      // Legs
      const st = (t >> 3) & 1
      if (mv) {
        d(bx + 1 + (st ? 2 : 0), by + 10 + bob, 2, 3, w.robeColor)
        d(bx + 5 - (st ? 2 : 0), by + 10 + bob, 2, 3, w.robeColor)
      } else {
        d(bx + 2, by + 10, 2, 3, w.robeColor)
        d(bx + 4, by + 10, 2, 3, w.robeColor)
      }

      // Casting animation at bench
      if (!mv && w.activity === 'bench' && w.running) {
        const armBob = (t >> 2) & 1
        d(bx - 2, by + 2 + bob + armBob, 2, 3, w.color)
        d(bx + 8, by + 2 + bob + (1 - armBob), 2, 3, w.color)
        // Magic sparkles from hands
        if (t % 8 === 0) {
          spawnMagic(bx - 2, by + 3 + bob, w.color, 2)
          spawnMagic(bx + 9, by + 3 + bob, w.color, 2)
        }
      }

      // Kind badge
      const kindEmoji = w.kind === 'cron' ? '⏳' : w.kind === 'spawn' ? '🔮' : '📜'
      X.font = (2 * S) + 'px monospace'
      X.fillText(kindEmoji, (bx + 8) * S, (by - 14 + bob) * S)

      // Nameplate (fixed on bench to show occupancy)
      if (w.benchIdx >= 0 && w.activity !== 'entering') {
        const bp = BENCH_POSITIONS[w.benchIdx]
        drawLabel(T, w.name.slice(0, 8), (bp.x + 20) * S, (bp.y - 4) * S, { role: 'label', color: '#fff', bgColor: w.color, align: 'center', scale: S })
      }

      // Status
      drawLabel(T, w.running ? 'casting' : 'resting', (bx + 4) * S, (by - 18 + bob) * S, { role: 'status', color: w.running ? '#4f4' : '#888', bgColor: 'rgba(0,0,0,0.5)', align: 'center', scale: S })
      // Real-message speech bubble — appears when the session's latest message changes
      if (w.lastMessage && Date.now() - w.msgAt < SPEECH_BUBBLE_MS) {
        const msgAge = Date.now() - w.msgAt
        const msgAlpha = msgAge > SPEECH_BUBBLE_MS - 1000 ? (SPEECH_BUBBLE_MS - msgAge) / 1000 : 1
        drawSpeechBubble(T, w.lastMessage, (bx + 4) * S, (by - 22 + bob) * S, { scale: S, alpha: msgAlpha })
      }


      // Name (wraps up to ~45 chars)
      const nameLines = drawLabel(T, w.name, (bx + 4) * S, (by + 16) * S, { role: 'name', weight: 'bold', color: '#fff', align: 'center', scale: S, maxWidth: 64 * S })

      // Detail
      if (w.detail) {
        drawLabel(T, w.detail, (bx - 2) * S, (by + 20) * S + (nameLines - 1) * sceneLineHeight('name'), { role: 'detail', color: '#999', scale: S })
      }
    }

    const drawParticles = () => {
      particlesRef.current.forEach(p => {
        const alpha = Math.max(0, 0.6 * (1 - p.life / p.maxLife))
        X.fillStyle = p.color
        X.globalAlpha = alpha
        X.fillRect(p.x * S, p.y * S, p.size * S, p.size * S)
        X.globalAlpha = 1
      })
    }

    const drawSpellText = (_t: number) => {
      const sp = spellRef.current
      if (!sp || sp.life <= 0) return
      sp.life--; sp.y -= 0.15
      const alpha = Math.min(1, sp.life / 30)
      T.fillStyle = `rgba(255,200,50,${alpha})`
      T.font = sceneFont('spell', 'bold')
      T.textAlign = 'center'
      T.fillText(sp.text, sp.x * S, sp.y * S)
      T.textAlign = 'start'
    }

    const drawTitle = () => {
      T.fillStyle = '#f90'; T.font = sceneFont('title', 'bold')
      T.textAlign = 'center'
      T.fillText('Arcane Academy', (W / 2) * S, 20 * S)
      T.textAlign = 'start'
      T.fillStyle = '#666'; T.font = sceneFont('status')
      T.fillText(`${wizardsRef.current.length}/${MAX_BENCHES} wizards`, 4 * S, (H - 4) * S)
    }

    /* ── Update ── */
    const update = (t: number) => {
      // Advance and filter particles (decoupled from draw)
      particlesRef.current.forEach(p => {
        p.x += p.vx; p.y += p.vy
        p.vx += (Math.random() - 0.5) * 0.02
        p.life++
      })
      particlesRef.current = particlesRef.current.filter(p => p.life < p.maxLife)

      const wizards = wizardsRef.current
      wizards.forEach(w => {
        const ddx = w.tx - w.x, ddy = w.ty - w.y
        const dist = Math.sqrt(ddx * ddx + ddy * ddy)
        if (dist > 1.5) {
          w.x += ddx / dist * 0.6; w.y += ddy / dist * 0.6
        } else {
          w.x = w.tx; w.y = w.ty
          if (w.activity === 'entering') w.activity = 'bench'
          if (w.activity === 'circle') {
            w.castTimer++
          }
          if (w.activity === 'shelf') {
            w.castTimer++
            if (w.castTimer % 240 === 60 && (!spellRef.current || spellRef.current.life <= 0)) {
              spellRef.current = { text: 'I see!', x: w.x, y: w.y - 20, life: 60 }
            }
            if (w.castTimer > 600) {
              const bp = BENCH_POSITIONS[w.benchIdx]
              w.tx = bp.x + 15; w.ty = bp.y + 20; w.activity = 'bench'; w.castTimer = 0
            }
          }
        }
      })

      // Spell emission — only when ALL circle wizards have arrived
      const circleWizards = wizards.filter(w => w.activity === 'circle')
      if (circleWizards.length >= 2) {
        const allArrived = circleWizards.every(w => Math.abs(w.x - w.tx) < 2 && Math.abs(w.y - w.ty) < 2)
        const minTimer = Math.min(...circleWizards.map(w => w.castTimer))
        if (allArrived && minTimer === 60) {
          const spell = SPELL_WORDS[Math.random() * SPELL_WORDS.length | 0]
          spellRef.current = { text: spell, x: CIRCLE_POS.x, y: CIRCLE_POS.y - 20, life: 80 }
          for (let i = 0; i < 20; i++) spawnMagic(CIRCLE_POS.x, CIRCLE_POS.y, '#f90', 1)
        }
        if (minTimer > 600) {
          circleWizards.forEach(w => {
            const bp = BENCH_POSITIONS[w.benchIdx]
            w.tx = bp.x + 15; w.ty = bp.y + 20; w.activity = 'bench'; w.castTimer = 0
          })
        }
      }

      // Summoning circle visit
      const benchWizards = wizards.filter(w => w.activity === 'bench')
      if (t % 2400 === 600 && benchWizards.length >= 2 && Math.random() < 0.25) {
        const a = benchWizards[0], b = benchWizards[1]
        a.activity = 'circle'; b.activity = 'circle'; a.castTimer = 0; b.castTimer = 0
        a.tx = CIRCLE_POS.x - 15; a.ty = CIRCLE_POS.y + 5
        b.tx = CIRCLE_POS.x + 10; b.ty = CIRCLE_POS.y + 5
      }

      // Shelf visit
      if (t % 3000 === 1200 && benchWizards.length > 0 && Math.random() < 0.2) {
        const w = benchWizards[Math.random() * benchWizards.length | 0]
        if (w.activity === 'bench') {
          w.activity = 'shelf'; w.castTimer = 0; w.tx = 380; w.ty = 80
        }
      }

      // Ambient magic particles
      if (t % 10 === 0) spawnMagic(Math.random() * W, 85 + Math.random() * (H - 90), '#9b59b640', 1)
    }

    /* ── Main draw ── */
    const draw = (t: number) => {
      T.clearRect(0, 0, W * S, H * S)
      drawWalls()
      drawArchwayDoor(t)
      drawWindow(80, 20, t)
      drawWindow(180, 20, t)
      drawTitle()

      // Shelves
      drawShelf(370, 80, t)
      drawShelf(420, 80, t)

      // Crystal ball
      drawCrystalBall(340, 100, t)

      // Summoning circle
      drawSummoningCircle(t)

      // Workbenches
      const occupied = new Set(wizardsRef.current.map(w => w.benchIdx))
      BENCH_POSITIONS.forEach((bp, i) => drawWorkbench(bp.x, bp.y, occupied.has(i), i, t))

      // Particles
      drawParticles()

      // Wizards (y-sorted)
      const sorted = [...wizardsRef.current].sort((a, b) => a.y - b.y)
      sorted.forEach(w => drawWizard(w, t))

      // Spell text
      drawSpellText(t)
    }

    const cancelLoop = runSceneLoop(visibleRef, tickRef, update, draw)
    return () => {
      cancelLoop()
    }
  }, [])  

  useVisibleSync(visibleRef, visible)

  return (
    <div style={SCENE_CONTAINER_STYLE(W, H)}>
      <canvas ref={canvasRef} aria-label="Wizard tower scene" style={{ ...PIXEL_CANVAS_STYLE, ...canvasProps.style }} onMouseMove={canvasProps.onMouseMove} onMouseLeave={canvasProps.onMouseLeave} onClick={canvasProps.onClick} />
      <canvas ref={textRef} aria-label="Wizard tower spell text" style={TEXT_CANVAS_STYLE} />
      {tooltipEl}
    </div>
  )
}
