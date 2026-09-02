import { useEffect, useRef, useCallback, useState } from 'react'
import type { AgentSource } from '../../../hooks/useAgentSync'
import { isKnownAgent, markAgentsKnown, pruneAgents } from '../../../hooks/sceneStateCache'
import { type MCAgent, W, H, S, P, MAX_STATIONS, DOOR, STATION_POSITIONS, AGENT_COLORS, C, WALL_H, drawText, drawAgent, type Particle, spawnParticles, updateParticles, drawParticles, DESTINATIONS, type DestKey, buildPath, buildReturnPath, buildEntryPath, buildExitPath, getLevel, DESK_CONVOS, BREAK_CONVOS } from './parts'
import { drawCoffeeStation, drawVendingMachine, drawEquipmentRack, drawTrashCan, drawPlant, drawSpeaker, drawWaterCooler } from './props'
import { SCENE_CONTAINER_STYLE, PIXEL_CANVAS_STYLE } from '../../../hooks/sceneText'
import { useSceneInteraction } from '../../../hooks/useSceneInteraction'

import { i18nT } from '../../../i18n/t'

const MC_THEME = { active: 'Vibe coding', idle: 'Doomscrolling TikTok' }

export default function MissionControlScene({ agents, visible = true }: { agents: AgentSource[]; visible?: boolean }) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const tickRef = useRef(0)
  const agentsRef = useRef<MCAgent[]>([])
  const visibleRef = useRef(visible)
  const particlesRef = useRef<Particle[]>([])
  const vendingDropRef = useRef<{ y: number; targetY: number; color: string; linger: number } | null>(null)
  const chatCooldownRef = useRef(0)
  const winUpdateRef = useRef(0)
  const [, setCount] = useState(0)
  const { canvasProps, tooltipEl } = useSceneInteraction(canvasRef, agentsRef, W, H, MC_THEME, 10,
    (a) => { const { level, title } = getLevel(parseInt(a.detail || '0') || 0); return <div style={{ color: '#888' }}>{i18nT('pages.scenes.missionControl.missionControlScene.lv_level_title', { level, title })}</div> },
    agents,
  )

  useEffect(() => { visibleRef.current = visible }, [visible])

  /* ── Pixel helper: snaps to P-unit grid ── */
  const dp = useCallback((X: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, c: string) => {
    X.fillStyle = c; X.fillRect(x * S, y * S, w * S, h * S)
  }, [])

  /* ── Reconcile agents ── */
  useEffect(() => {
    const capped = agents.slice(0, MAX_STATIONS)
    const existing = agentsRef.current
    const currentIds = new Set(capped.map(s => s.id))

    const next: MCAgent[] = []

    // Pass through agents already leaving (don't interfere with animation)
    existing.forEach(prev => {
      if (prev.leaving) {
        if (prev.activity !== 'gone') next.push(prev)
        return
      }
      if (currentIds.has(prev.id)) {
        const src = capped.find(s => s.id === prev.id)!
        prev.name = src.name; prev.detail = src.detail; prev.running = src.running
        next.push(prev)
      } else {
        // Agent just departed — start leaving
        prev.leaving = true
        prev.activity = 'leaving'
        prev.waypoints = buildExitPath(prev.stationIdx, Math.round(prev.x))
        prev.facing = 'left'
        prev.deskOn = false
        next.push(prev)
      }
    })

    // Occupied desks = non-leaving agents
    const occupied = new Set(next.filter(a => !a.leaving).map(a => a.stationIdx))

    // Add new agents
    let newCount = 0
    capped.forEach(src => {
      if (existing.find(a => a.id === src.id)) return
      const idx = STATION_POSITIONS.findIndex((_, i) => !occupied.has(i))
      if (idx < 0) return
      occupied.add(idx)
      const pos = STATION_POSITIONS[idx]
      const known = isKnownAgent('missioncontrol', src.id)
      next.push({
        id: src.id, name: src.name, label: src.label, kind: src.kind,
        x: known ? pos.x + 21 : DOOR.x + 6, y: known ? pos.y + 20 : DOOR.y + 20,
        tx: pos.x + 21, ty: pos.y + 20,
        stationIdx: idx, color: AGENT_COLORS[idx % AGENT_COLORS.length],
        running: src.running, detail: src.detail,
        facing: known ? 'back' : 'right', activity: known ? 'sitting' : 'entering',
        enterProgress: known ? 1 : 0, walkFrame: 0,
        waypoints: known ? [] : buildEntryPath(idx),
        item: 'none', destKey: null, idleTimer: 1800 + Math.floor(Math.random() * 3600),
        waitTimer: known ? 0 : newCount * 40 + Math.floor(Math.random() * 20),
        drinkTimer: 0,
        chatTimer: 0, chatDelay: 0, chatLine: '', bubbleUp: false,
        deskOn: known, leaving: false,
      })
      if (!known) newCount++
      markAgentsKnown('missioncontrol', [src.id])
    })

    agentsRef.current = next
    setCount(next.length)
    pruneAgents('missioncontrol', next.filter(a => !a.leaving).map(a => a.id))
  }, [agents])

  /* ── Render loop ── */
  useEffect(() => {
    const cv = canvasRef.current!
    const X = cv.getContext('2d')!
    cv.width = W * S; cv.height = H * S; X.imageSmoothingEnabled = false
    const d = dp.bind(null, X)
    let raf = 0

    const draw = () => {
      if (!visibleRef.current) { raf = requestAnimationFrame(draw); return }
      const t = tickRef.current++

      /* — Background — */
      d(0, 0, W, H, C.bg)

      /* — Starfield through top windows — */
      for (let i = 0; i < 20; i++) {
        const sx = (i * 37 + t * 0.02) % W
        const sy = 4 + (i * 13) % 50
        if (Math.sin(t * 0.03 + i) > 0.2) d(sx, sy, P, P, C.star + '44')
      }

      /* — Back wall with panels — */
      d(0, 0, W, WALL_H, C.wall)
      for (let px = 30; px < W; px += 60) d(px, 0, 1, WALL_H - 2, '#121630')
      d(0, WALL_H - 2, W, P, C.wallTrim)
      d(0, WALL_H - 4, W, 1, '#1a1e3a')
      // Ceiling lights
      for (let lx = 80; lx < W; lx += 120) {
        d(lx, WALL_H, 40, 2, '#333')
        d(lx + 4, WALL_H + 2, 32, 1, '#555')
      }

      /* — Main screen (center) — */
      const bx = 100, by = 4, bw = 280, bh = 50
      d(bx - 2, by - 2, bw + 4, bh + 4, '#111')
      d(bx - 1, by - 1, bw + 2, bh + 2, C.bigScreenBorder)

      // Windows Update easter egg
      if (t > 0 && t % 3600 === 0 && winUpdateRef.current <= 0 && visibleRef.current && Math.random() < 0.05) {
        winUpdateRef.current = 480
        // Panic! Everyone gets up and runs around
        agentsRef.current.forEach(a => {
          if (a.activity === 'sitting' && !a.leaving) {
            a.activity = 'walking'; a.deskOn = false; a.facing = 'left'
            const rx = 40 + Math.floor(Math.random() * (W - 80))
            const ry = WALL_H + 40 + Math.floor(Math.random() * (H - WALL_H - 80))
            a.waypoints = [{ x: rx, y: ry }]
            a.idleTimer = 200 + Math.floor(Math.random() * 100)
          }
        })
      }
      const drawWinUpdate = () => {
        d(bx, by, bw, bh, '#0078d4')
        const pct = 100 - Math.floor(winUpdateRef.current / 4.8)
        const spin = ['|', '/', '-', '\\'][Math.floor(t / 8) % 4]
        drawText(d, 'Working on updates...', bx + 70, by + 10, '#fff', P)
        drawText(d, `${pct}% complete`, bx + 110, by + 26, '#fff', P)
        drawText(d, `Dont turn off your computer ${spin}`, bx + 60, by + 40, '#ddd', 1)
      }
      const drawNormalScreen = () => {
        d(bx, by, bw, bh, C.bigScreen)
        for (let gx = bx + 20; gx < bx + bw; gx += 20) d(gx, by, 1, bh, C.bigScreenGrid)
        for (let gy = by + 12; gy < by + bh; gy += 12) d(bx, gy, bw, 1, C.bigScreenGrid)
        drawText(d, 'GRADATIM FEROCITER', bx + 68, by + 4, C.bigScreenText, P)
        const statusText = agentsRef.current.length > 0
          ? agentsRef.current.map(a => `[${a.name}:${a.running ? 'RUN' : 'IDLE'}]`).join(' ')
          : '[ ALL SYSTEMS NOMINAL ]'
        const textW = statusText.length * 4
        const scrollTotal = textW + bw
        const scrollX = bx + bw - ((t * 0.3) % scrollTotal)
        X.save(); X.beginPath(); X.rect(bx * S, (by + 14) * S, bw * S, 14 * S); X.clip()
        drawText(d, statusText, scrollX, by + 18, C.screenText, 1)
        X.restore()
        const dots = [[bx+30,by+32],[bx+45,by+28],[bx+80,by+36],[bx+120,by+30],[bx+160,by+38],[bx+200,by+26],[bx+230,by+34],[bx+260,by+30]]
        dots.forEach(([dx,dy],i) => {
          d(dx, dy, 2, 2, (t + i * 40) % 200 < 100 ? '#4466aa' : '#6688cc')
          if (i % 3 === 0) d(dx + 1, dy - 3, 1, 3, '#4466aa44')
        })
      }
      if (winUpdateRef.current > 0) { winUpdateRef.current--; drawWinUpdate() } else { drawNormalScreen() }

      /* — Left panel: Voyager 1 tracker — */
      d(8, 4, 82, 48, '#111')
      d(10, 6, 78, 44, C.consoleEdge)
      d(10, 6, 78, 1, '#3a4070')
      d(12, 8, 74, 40, C.screen)
      for (let sl = 0; sl < 40; sl += 2) d(12, 8 + sl, 74, 1, C.screenGlow)
      drawText(d, 'VOYAGER 1', 16, 12, '#88ccff', 1)
      // Distance — real: ~24.8 billion km, we animate the last digits
      const vDist = 24802 + Math.floor(t * 0.001)
      drawText(d, `${vDist} M KM`, 16, 20, C.screenText, 1)
      drawText(d, 'SIGNAL: 22H 36M', 16, 28, '#668899', 1)
      // Tiny voyager icon
      d(62, 36, 6, 1, '#aaa'); d(64, 34, 2, 4, '#888')
      d(60, 36, 2, 1, '#666'); d(68, 36, 2, 1, '#666')
      if ((t >> 5) & 1) d(70, 36, 1, 1, C.led.on)

      /* — Right panel: ISS tracker — */
      d(392, 4, 82, 48, '#111')
      d(394, 6, 78, 44, C.consoleEdge)
      d(394, 6, 78, 1, '#3a4070')
      d(396, 8, 74, 40, C.screen)
      for (let sl = 0; sl < 40; sl += 2) d(396, 8 + sl, 74, 1, C.screenGlow)
      drawText(d, 'ISS ZARYA', 400, 12, '#88ccff', 1)
      // Orbit position cycles
      const issOrbit = Math.floor((t * 0.5) % 360)
      drawText(d, `ORB: ${issOrbit} DEG`, 400, 20, C.screenText, 1)
      drawText(d, 'ALT: 408 KM', 400, 28, '#668899', 1)
      drawText(d, 'CREW: 7', 400, 36, '#668899', 1)
      if ((t >> 4) & 1) d(460, 40, P, P, C.led.on)

      /* — Bottom row of small panels on wall — */
      // Panel 1: Agent count
      d(10, 58, 60, 46, '#111')
      d(12, 60, 56, 42, C.consoleEdge)
      d(14, 62, 52, 38, C.screen)
      const active = agentsRef.current.filter(a => a.running).length
      drawText(d, 'AGENTS', 18, 66, C.bigScreenText, 1)
      drawText(d, `${agentsRef.current.length} ON  ${active} RUN`, 18, 74, C.screenText, 1)
      // Mini bar graph
      for (let bi = 0; bi < MAX_STATIONS; bi++) {
        const hasAgent = bi < agentsRef.current.length
        const isRunning = hasAgent && agentsRef.current[bi].running
        d(18 + bi * 6, 86, 4, 8, isRunning ? C.led.on : hasAgent ? C.led.warn : '#1a1a2a')
      }

      // Panel 2: Uptime / clock
      d(80, 58, 60, 46, '#111')
      d(82, 60, 56, 42, C.consoleEdge)
      d(84, 62, 52, 38, C.screen)
      drawText(d, 'MISSION', 88, 66, C.bigScreenText, 1)
      const hrs = Math.floor(t / 3600) % 100
      const mins = Math.floor(t / 60) % 60
      const secs = t % 60
      drawText(d, `T ${String(hrs).padStart(2,'0')}:${String(mins).padStart(2,'0')}:${String(secs).padStart(2,'0')}`, 88, 74, C.screenText, 1)
      // Heartbeat line
      for (let hx = 0; hx < 48; hx++) {
        const hv = Math.sin((hx + t) * 0.3) * 4
        d(86 + hx, 90 - hv, 1, 1, C.led.on)
      }

      // Panel 3: Comms
      d(150, 58, 60, 46, '#111')
      d(152, 60, 56, 42, C.consoleEdge)
      d(154, 62, 52, 38, C.screen)
      drawText(d, 'COMMS', 158, 66, C.bigScreenText, 1)
      drawText(d, 'FREQ 14.2GHZ', 158, 74, C.screenText, 1)
      // Signal wave
      for (let wx = 0; wx < 48; wx++) {
        const wv = Math.sin((wx - t * 0.8) * 0.2) * 3
        d(156 + wx, 90 - wv, 1, 1, '#88aaff')
      }

      // Panel 4: Power
      d(220, 58, 60, 46, '#111')
      d(222, 60, 56, 42, C.consoleEdge)
      d(224, 62, 52, 38, C.screen)
      drawText(d, 'POWER', 228, 66, C.bigScreenText, 1)
      drawText(d, 'RTG 39.4 PCT', 228, 74, C.screenText, 1)
      // Power bar
      d(228, 86, 44, 6, '#1a1a2a')
      d(228, 86, 17, 6, '#2a6a2a')
      d(228, 86, 17, 2, '#3a8a3a')

      // Panel 5: Pale Blue Dot
      d(290, 58, 60, 46, '#111')
      d(292, 60, 56, 42, C.consoleEdge)
      d(294, 62, 52, 38, C.screen)
      drawText(d, 'CAM 06', 298, 66, '#668899', 1)
      // Tiny pale blue dot
      d(318, 84, 2, 2, '#6688cc')
      // Sunbeam
      d(310, 72, 1, 22, '#ffffff11')
      d(320, 70, 1, 24, '#ffffff11')
      d(330, 74, 1, 18, '#ffffff11')

      // Panel 6: Random telemetry
      d(360, 58, 60, 46, '#111')
      d(362, 60, 56, 42, C.consoleEdge)
      d(364, 62, 52, 38, C.screen)
      drawText(d, 'TELEM', 368, 66, C.bigScreenText, 1)
      // Scrolling hex data
      const hexOff = Math.floor(t * 0.05) % 256
      for (let row = 0; row < 3; row++) {
        const alt = hexOff >= 42 && hexOff <= 46
        const altHex = ['6E 69 63 6B', '70 61 70 40', 'FF FF FF FF']
        const hex = alt ? altHex[row] : Array.from({length: 4}, (_, i) => ((hexOff + i + row * 4) * 0x1B3D % 0xFF).toString(16).padStart(2, '0').toUpperCase()).join(' ')
        drawText(d, hex, 368, 74 + row * 8, C.screenText, 1)
      }

      // Panel 7: right side — Mars distance
      d(430, 58, 44, 46, '#111')
      d(432, 60, 40, 42, C.consoleEdge)
      d(434, 62, 36, 38, C.screen)
      drawText(d, 'MARS', 438, 66, '#cc6644', 1)
      const marsDist = 228 + Math.floor(Math.sin(t * 0.0005) * 173)
      drawText(d, `${marsDist}M`, 438, 76, C.screenText, 1)
      // Tiny mars
      d(448, 86, 4, 4, '#cc4422')
      d(449, 87, 2, 2, '#dd6644')

      /* — Floor — */
      d(0, WALL_H, W, H - WALL_H, C.floor)
      for (let fx = 0; fx < W; fx += 40) d(fx, WALL_H, 1, H - WALL_H, C.floorLine)
      for (let fy = WALL_H; fy < H; fy += 40) d(0, fy, W, 1, C.floorLine)

      /* — Door — */
      d(DOOR.x, DOOR.y - 4, DOOR.w + 2, DOOR.h + 6, C.doorFrame)
      d(DOOR.x + 1, DOOR.y - 2, DOOR.w, DOOR.h + 2, C.door)
      // Door handle
      d(DOOR.x + DOOR.w - 3, DOOR.y + 16, 2, 4, '#888')
      // EXIT sign above
      d(DOOR.x, DOOR.y - 10, DOOR.w + 2, 6, '#440000')
      drawText(d, 'EXIT', DOOR.x + 1, DOOR.y - 9, '#ff4444', 1)

      /* — Console stations — */
      const drawables: { y: number; draw: () => void }[] = []
      STATION_POSITIONS.forEach((pos, i) => {
        drawables.push({ y: pos.y + 30, draw: () => {
        // Desk with depth
        d(pos.x - 1, pos.y - 1, 52, 1, '#2e3468') // top edge highlight
        d(pos.x, pos.y, 50, 6, C.consoleTop)
        d(pos.x, pos.y + 6, 50, 20, C.console)
        d(pos.x, pos.y + 6, 50, 2, '#1e2238') // shadow under top
        d(pos.x, pos.y + 24, 50, 2, '#0e1220') // bottom shadow
        d(pos.x, pos.y, 1, 26, C.consoleEdge) // left edge
        d(pos.x + 49, pos.y, 1, 26, C.consoleEdge) // right edge
        // Keyboard on desk
        d(pos.x + 10, pos.y + 2, 30, 3, '#222')
        d(pos.x + 11, pos.y + 2, 28, 1, '#333') // key row 1
        d(pos.x + 11, pos.y + 4, 28, 1, '#333') // key row 2
        // Buttons on console face — green when agent active, red when idle, off when empty
        const agent = agentsRef.current.find(a => a.stationIdx === i)
        const btnColor = agent ? (agent.running ? '#2a5a2a' : '#5a2a2a') : '#333'
        d(pos.x + 4, pos.y + 10, 3, 3, '#444')
        d(pos.x + 4, pos.y + 10, 3, 1, '#555')
        d(pos.x + 10, pos.y + 10, 3, 3, btnColor)
        d(pos.x + 36, pos.y + 10, 3, 3, '#444')
        d(pos.x + 42, pos.y + 10, 3, 3, '#444')
        // Screen with bezel
        d(pos.x + 2, pos.y - 20, 46, 20, '#111')
        d(pos.x + 3, pos.y - 19, 44, 1, '#333') // top bezel highlight
        d(pos.x + 4, pos.y - 18, 42, 16, C.consoleEdge) // bezel
        d(pos.x + 6, pos.y - 16, 38, 12, C.screen) // screen
        // Screen scanlines
        for (let sl = 0; sl < 12; sl += 2) d(pos.x + 6, pos.y - 16 + sl, 38, 1, C.screenGlow)
        // Screen content
        if (winUpdateRef.current > 0) {
          d(pos.x + 6, pos.y - 16, 38, 12, '#0012a0')
          drawText(d, ':[  ERR!', pos.x + 10, pos.y - 12, '#fff', 1)
        } else if (agent && agent.deskOn) {
          if (agent.running && agent.activity === 'sitting') {
            // Typewriter code generation — lines appear one by one, then scroll away
            const seed = i * 1337
            const LINES = 5, LINE_DELAY = 90, PAUSE = 80, SCROLL_SPEED = 100
            const cycle = LINES * LINE_DELAY + PAUSE + SCROLL_SPEED
            const phase = (t + seed) % cycle
            const cycleSeed = seed + Math.floor((t + seed) / cycle) * 97 // different code each cycle
            const dim = '#1a5a3a'
            for (let row = 0; row < LINES; row++) {
              const lineStart = row * LINE_DELAY
              if (phase < lineStart) continue
              const rs = cycleSeed + row * 31
              const lineLen = 3 + (rs % 16)
              const indent = (rs % 4) * 3
              const typed = phase < lineStart + LINE_DELAY
                ? Math.floor(((phase - lineStart) / LINE_DELAY) * lineLen)
                : lineLen
              const scrollPhase = phase - (LINES * LINE_DELAY + PAUSE)
              const scrollOff = scrollPhase > 0 ? Math.floor(scrollPhase * 0.15) : 0
              const ry = pos.y - 15 + row * 2 - scrollOff
              if (ry < pos.y - 16 || ry > pos.y - 5) continue
              for (let cx = 0; cx < typed; cx++) {
                if (((rs + cx * 7) % 4) === 0) continue
                d(pos.x + 7 + indent + cx, ry, 1, 1, dim)
              }
            }
            const typingPhase = LINES * LINE_DELAY
            if (phase < typingPhase) {
              const curLine = Math.floor(phase / LINE_DELAY)
              const crs = cycleSeed + curLine * 31
              const curIndent = (crs % 4) * 3
              const curLen = Math.floor(((phase - curLine * LINE_DELAY) / LINE_DELAY) * (3 + (crs % 16)))
              if ((t >> 3) & 1) d(pos.x + 7 + curIndent + curLen, pos.y - 15 + curLine * 2, 1, 1, C.screenText)
            }
            d(pos.x + 44, pos.y - 14, P, P, C.led.on)
          } else {
            // DVD screensaver — bouncing logo that never hits the corner
            // Use irrational speed ratio so X/Y bounces never sync at corners
            const sw = 38, sh = 12, lw = 12, lh = 5
            const spdX = 0.05, spdY = 0.05 * Math.SQRT2
            const phase = t + i * 500
            const cx = phase * spdX, cy = phase * spdY
            const periodX = (sw - lw) * 2, periodY = (sh - lh) * 2
            const mx = cx % periodX, my = cy % periodY
            const bx2 = mx < (sw - lw) ? mx : periodX - mx
            const by2 = my < (sh - lh) ? my : periodY - my
            const col = AGENT_COLORS[(i + Math.floor(cx / (sw - lw))) % AGENT_COLORS.length]
            const lx = pos.x + 6 + bx2, ly = pos.y - 16 + by2
            drawText(d, 'AWS', lx, ly, col, 1)
            d(pos.x + 44, pos.y - 14, P, P, C.led.warn)
          }
        } else {
          d(pos.x + 6, pos.y - 16, 38, 12, C.screenOff)
        }
        // Chair with depth
        d(pos.x + 14, pos.y + 28, 22, 2, '#222') // seat top
        d(pos.x + 16, pos.y + 30, 18, 8, C.chair)
        d(pos.x + 16, pos.y + 30, 18, 2, '#242440') // seat highlight
        d(pos.x + 24, pos.y + 38, 2, 4, '#333') // chair leg
        // Item on desk
        if (agent && agent.activity === 'sitting' && agent.item !== 'none' && agent.drinkTimer <= 0) {
          if (agent.item === 'mug') { d(pos.x + 42, pos.y - 1, 3, 3, '#ddd'); d(pos.x + 41, pos.y, 1, 1, '#ccc') }
          else if (agent.item === 'cup') { d(pos.x + 43, pos.y - 2, 2, 4, '#aaddff') }
          else if (agent.item === 'snack') { d(pos.x + 42, pos.y, 3, 2, '#f39c12') }
        }
        }})
      })

      /* — Agents — */
      agentsRef.current.forEach(a => {
        const speed = 0.6
        const moveToward = (target: { x: number; y: number }) => {
          const dx = target.x - a.x, dy = target.y - a.y
          const dist = Math.sqrt(dx * dx + dy * dy)
          if (dist < 1.5) return true // arrived
          a.x += (dx / dist) * speed; a.y += (dy / dist) * speed
          a.facing = dx > 0 ? 'right' : 'left'
          a.walkFrame++
          return false
        }

        if (a.activity === 'entering') {
          if (a.waitTimer > 0) { a.waitTimer--; }
          else if (a.waypoints.length > 0) {
            if (moveToward(a.waypoints[0])) a.waypoints.shift()
          } else {
            a.activity = 'sitting'; a.facing = 'back'; a.deskOn = true
          }
        } else if (a.activity === 'leaving') {
          if (a.waypoints.length > 0) {
            if (moveToward(a.waypoints[0])) a.waypoints.shift()
          } else { a.activity = 'gone' }
        } else if (a.activity === 'walking') {
          if (a.chatDelay > 0) { a.chatDelay--; }
          else if (a.chatTimer > 0) { a.chatTimer--; }
          else if (a.waypoints.length > 0) {
            if (moveToward(a.waypoints[0])) a.waypoints.shift()
          } else if (a.leaving === false && a.idleTimer > 0) {
            // At destination — pick up item on first frame, then pause
            if (a.destKey && a.item === 'none') {
              if (a.destKey === 'coffee') a.item = 'mug'
              else if (a.destKey === 'water') {
                a.item = 'cup'
                // Water fill particles
                for (let p = 0; p < 5; p++) {
                  spawnParticles(particlesRef.current, 428, 264, '#88ccff', 1, { vy: 0.15, spread: 0.03, maxLife: 40, size: 1 })
                }
              }
              else if (a.destKey === 'vending') {
                a.item = 'snack'
                // Trigger drop animation inside machine
                const colors = ['#e74c3c','#3498db','#f39c12','#2ecc71','#9b59b6','#e67e22']
                vendingDropRef.current = { y: 160, targetY: 183, color: colors[Math.floor(Math.random() * colors.length)], linger: 30 }
              }
              else if (a.destKey === 'trash') a.item = 'none'
            }
            // Water drip while filling
            if (a.destKey === 'water' && t % 8 === 0) {
              spawnParticles(particlesRef.current, 428, 264, '#88ccff', 1, { vy: 0.12, spread: 0.02, maxLife: 30, size: 1 })
            }
            a.idleTimer--
            if (a.idleTimer <= 0) {
              // If at trash, item already dropped. If carrying, go back to desk
              if (a.destKey === 'trash') a.item = 'none'
              a.destKey = null
              a.waypoints = buildReturnPath(a.stationIdx, Math.round(a.x), Math.round(a.y))
            }
          } else {
            a.activity = 'sitting'; a.facing = 'back'; a.deskOn = true
            a.x = a.tx; a.y = a.ty
            a.idleTimer = 1800 + Math.floor(Math.random() * 3600)
          }
        } else if (a.activity === 'sitting') {
          a.x = a.tx; a.y = a.ty
          a.walkFrame++
          if (a.chatDelay > 0) a.chatDelay--
          else if (a.chatTimer > 0) a.chatTimer--
          // Drinking animation
          if (a.drinkTimer > 0) {
            a.drinkTimer--
          } else if ((a.item === 'mug' || a.item === 'cup') && Math.random() < 0.002) {
            a.drinkTimer = 60
          }
          // Random idle actions — only when not running
          if (!a.running) {
            a.idleTimer--
            if (a.idleTimer <= 0) {
              const dests: DestKey[] = a.item !== 'none'
                ? ['trash'] // carrying something → go throw it away
                : ['coffee', 'water', 'vending']
              const dest = dests[Math.floor(Math.random() * dests.length)]
              a.waypoints = buildPath(a.stationIdx, DESTINATIONS[dest])
              a.activity = 'walking'
              a.destKey = dest
              a.idleTimer = dest === 'trash' ? 30 : 80
            }
          }
        }
      })
      // Remove agents that finished their exit animation
      agentsRef.current = agentsRef.current.filter(a => a.activity !== 'gone')

      // Conversation detection — max 1 pair, 1 min cooldown
      chatCooldownRef.current--
      const chatting = agentsRef.current.filter(a => a.chatTimer > 0).length
      if (chatting < 2 && chatCooldownRef.current <= 0) {
        const mobile = agentsRef.current.filter(a => a.activity !== 'gone' && a.chatTimer <= 0)
        for (let i = 0; i < mobile.length; i++) {
          let found = false
          for (let j = i + 1; j < mobile.length; j++) {
            const a = mobile[i], b = mobile[j]
            const dist = Math.abs(a.x - b.x) + Math.abs(a.y - b.y)
            if (dist < 80 && Math.random() < 0.0005) {
              const bothSitting = a.activity === 'sitting' && b.activity === 'sitting'
              const pool = bothSitting ? DESK_CONVOS : BREAK_CONVOS
              const convo = pool[Math.floor(Math.random() * pool.length)]
              a.chatTimer = 480; a.chatDelay = 0; a.chatLine = convo[0]; a.bubbleUp = false
              b.chatTimer = 480; b.chatDelay = 60; b.chatLine = convo[1]; b.bubbleUp = true
              chatCooldownRef.current = 3600 // ~60s cooldown
              found = true; break
            }
          }
          if (found) break
        }
      }

      /* — Props + agents sorted by Y (z-ordering) — */
      // Props with their Y positions
      drawables.push({ y: 150, draw: () => {
        drawVendingMachine(d, 440, 150, t); drawEquipmentRack(d, 400, 150, t)
        const drop = vendingDropRef.current
        if (drop) {
          d(drop.y < drop.targetY ? 449 : 447, drop.y, 3, 5, drop.color)
          if (drop.y < drop.targetY) {
            drop.y += 1.5
          } else {
            drop.linger--
            if (drop.linger <= 0) vendingDropRef.current = null
          }
        }
      } })
      drawables.push({ y: 168, draw: () => drawPlant(d, 340, 168, 0) })
      drawables.push({ y: 260, draw: () => { drawCoffeeStation(d, 380, 260, t); drawWaterCooler(d, 420, 252) } })
      drawables.push({ y: 310, draw: () => drawTrashCan(d, 20, 296) })
      drawables.push({ y: 300, draw: () => drawPlant(d, 460, 300, 1) })
      drawables.push({ y: WALL_H + 2, draw: () => drawSpeaker(d, DOOR.x, WALL_H + 2, t) })
      // All agents sorted by feet Y
      agentsRef.current.forEach(a => {
        drawables.push({ y: a.y + 16, draw: () => drawAgent(d, a, t) })
      })
      drawables.sort((a, b) => a.y - b.y)
      drawables.forEach(item => item.draw())

      /* — Particles — */
      // Coffee steam (spawn every ~20 frames, max 15 particles)
      if (t % 20 === 0 && particlesRef.current.length < 15) {
        spawnParticles(particlesRef.current, 387, 254, '#aaaacc', 1, { vy: -0.08, spread: 0.06, maxLife: 120, size: 1 })
      }
      particlesRef.current = updateParticles(particlesRef.current)
      drawParticles(d, X, particlesRef.current)

      /* — Scanline overlay — */
      X.fillStyle = 'rgba(0,0,0,0.03)'
      for (let sy = 0; sy < H * S; sy += 3) X.fillRect(0, sy, W * S, 1)

      raf = requestAnimationFrame(draw)
    }
    raf = requestAnimationFrame(draw)
    return () => cancelAnimationFrame(raf)
  }, [dp])

  return (
    <div style={SCENE_CONTAINER_STYLE(W, H)}>
      <canvas ref={canvasRef} aria-label={i18nT('pages.scenes.missionControl.missionControlScene.mission_control_scene')} style={{ ...PIXEL_CANVAS_STYLE, ...canvasProps.style }}
        onMouseMove={canvasProps.onMouseMove} onMouseLeave={canvasProps.onMouseLeave} onClick={canvasProps.onClick} />
      {tooltipEl}
    </div>
  )
}
