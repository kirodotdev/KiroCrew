import { useRef, useEffect, useCallback } from 'react'

import { i18nT } from '../i18n/t'
export type SlotState = 'empty' | 'typing'
export interface SlotData { state: SlotState; label?: string }
export interface PixelCanvasProps { slots: SlotData[] }

const NUM_SLOTS = 7, SCALE = 3, W = 256, H = 256
const CHAR_POSITIONS = [
  { cx: 54, cy: 135, topY: 117 }, { cx: 96, cy: 114, topY: 96 },
  { cx: 120, cy: 118, topY: 108 }, { cx: 155, cy: 115, topY: 105 },
  { cx: 146, cy: 148, topY: 130 }, { cx: 95, cy: 149, topY: 138 },
  { cx: 133, cy: 168, topY: 158 },
]
const DRAW_ORDER = [1, 3, 2, 0, 4, 5, 6]

interface SlotMachine { state: SlotState; frame: number; timer: number }

export default function PixelCanvas({ slots }: PixelCanvasProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const floorRef = useRef<HTMLImageElement | null>(null)
  const charsRef = useRef<HTMLImageElement[][]>([])
  const loadedRef = useRef(0)
  const machinesRef = useRef<SlotMachine[]>(Array.from({ length: NUM_SLOTS }, () => ({ state: 'empty', frame: 0, timer: 0 })))
  const slotsRef = useRef(slots)
  slotsRef.current = slots

  useEffect(() => {
    const onLoad = () => { loadedRef.current++ }
    const floor = new Image(); floor.onload = onLoad; floor.src = '/sprites/floor.png'; floorRef.current = floor
    const chars: HTMLImageElement[][] = []
    for (let i = 0; i < NUM_SLOTS; i++) {
      const f1 = new Image(); f1.onload = onLoad; f1.src = `/sprites/char${i}-frame1.png`
      const f2 = new Image(); f2.onload = onLoad; f2.src = `/sprites/char${i}-frame2.png`
      chars.push([f1, f2])
    }
    charsRef.current = chars
  }, [])

  const render = useCallback(() => {
    const canvas = canvasRef.current; if (!canvas) return
    const ctx = canvas.getContext('2d'); if (!ctx) return
    const now = Date.now()
    ctx.clearRect(0, 0, W * SCALE, H * SCALE)
    ctx.save(); ctx.scale(SCALE, SCALE)

    // Floor
    if (floorRef.current?.complete && floorRef.current.naturalWidth > 0) ctx.drawImage(floorRef.current, 0, 0, W, H)

    // Update machines from props
    const sl = slotsRef.current
    for (let i = 0; i < NUM_SLOTS; i++) {
      const m = machinesRef.current[i]
      const target = sl[i]?.state || 'empty'
      if (m.state !== target) {
        m.state = target; m.frame = 0; m.timer = now
      }
    }

    // Draw characters in Y-sorted order
    for (const idx of DRAW_ORDER) {
      const m = machinesRef.current[idx]
      if (m.state === 'empty') continue
      const pos = CHAR_POSITIONS[idx]
      const frames = charsRef.current[idx]
      if (!frames?.[0]?.complete || !frames[0].naturalWidth) continue

      ctx.save()
      if (m.state === 'typing') {
        m.frame = Math.floor(now / 500) % 2
      }

      ctx.drawImage(frames[m.frame] || frames[0], 0, 0, W, H)
      ctx.restore()

      // Label
      const label = sl[idx]?.label
      if (label) {
        const text = label.length > 12 ? label.slice(0, 10) + '..' : label
        ctx.font = '5px monospace'
        const tw = ctx.measureText(text).width
        const px = pos.cx, py = pos.topY - 4
        ctx.fillStyle = 'rgba(0,0,0,0.55)'
        ctx.fillRect(px - tw / 2 - 2, py - 5, tw + 4, 7)
        ctx.fillStyle = '#fff'
        ctx.textAlign = 'center'
        ctx.fillText(text, px, py)
        ctx.textAlign = 'start'
      }
    }

    ctx.restore()
  }, [])

  useEffect(() => {
    let raf: number
    const loop = () => { render(); raf = requestAnimationFrame(loop) }
    raf = requestAnimationFrame(loop)
    return () => cancelAnimationFrame(raf)
  }, [render])

  return <canvas ref={canvasRef} aria-label={i18nT('components.pixelCanvas.agent_activity_animation')} width={W * SCALE} height={H * SCALE} style={{ imageRendering: 'pixelated', aspectRatio: '1/1' }} className="w-full rounded-lg" />
}
