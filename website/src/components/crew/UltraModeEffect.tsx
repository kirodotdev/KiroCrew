/** The short whole-chat effect when Ultra Mode turns on (`fireUltraEffect`): a thin
 *  accent border sweeps around the chat area in 0.5 s. Pointer-events none, and
 *  nothing at all under prefers-reduced-motion. */
import { useEffect, useState } from 'react'
import { motion, useReducedMotion } from 'framer-motion'
import { OverlayPortal } from '../overlayRoot'
import { ULTRA_EFFECT_EVENT } from '../../lib/ultraModeEffect'

interface Shot { id: number; x: number; y: number; area: DOMRect }

function chatArea(): DOMRect {
  const dock = document.querySelector('[data-testid="composer-dock-root"]')
  const pane = dock?.parentElement
  return pane ? pane.getBoundingClientRect() : new DOMRect(0, 0, window.innerWidth, window.innerHeight)
}

export default function UltraModeEffect() {
  const reduce = useReducedMotion()
  const [shot, setShot] = useState<Shot | null>(null)
  useEffect(() => {
    const on = (e: Event) => {
      if (reduce || window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return
      const area = chatArea()
      const d = (e as CustomEvent<{ x: number; y: number } | null>).detail
      setShot({ id: Date.now(), x: d?.x ?? area.left + area.width / 2, y: d?.y ?? area.top + area.height / 2, area })
    }
    window.addEventListener(ULTRA_EFFECT_EVENT, on)
    return () => window.removeEventListener(ULTRA_EFFECT_EVENT, on)
  }, [reduce])
  useEffect(() => {
    if (!shot) return
    const t = window.setTimeout(() => setShot(null), 600)
    return () => window.clearTimeout(t)
  }, [shot])
  if (!shot) return null
  const { area } = shot
  const clip = { left: area.left, top: area.top, width: area.width, height: area.height }
  const body = (
    <svg className="absolute" style={clip} width={area.width} height={area.height} aria-hidden="true">
      <motion.rect x={1.5} y={1.5} width={area.width - 3} height={area.height - 3} rx={12} fill="none"
        stroke="var(--aim)" strokeWidth={2}
        initial={{ pathLength: 0, opacity: 1 }} animate={{ pathLength: 1, opacity: [1, 1, 0] }}
        transition={{ duration: 0.5, ease: 'easeOut', times: [0, 0.75, 1] }} />
    </svg>
  )
  return <OverlayPortal><div key={shot.id} aria-hidden="true" data-testid="ultra-effect" className="fixed inset-0 pointer-events-none z-[60]">{body}</div></OverlayPortal>
}
