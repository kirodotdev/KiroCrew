/** The short whole-chat effect when Ultra Mode turns on (`fireUltraEffect`, sent
 *  only by the composer chip's turn-on click, so never on mount, a chat switch or
 *  turning off). In about 0.85 s: a bolt flashes out of the chip, a purple-to-blue
 *  band of light sweeps across the chat area, its edge traces round, and the
 *  Board tab glows once as the side panel opens it. Under prefers-reduced-motion
 *  it is one quick tint fade instead. Everything is one pointer-events-none layer
 *  in the overlay root, taken away when it ends, so no real UI moves or is covered
 *  after it. */
import { useEffect, useState } from 'react'
import { motion, useReducedMotion } from 'framer-motion'
import { Zap } from 'lucide-react'
import { OverlayPortal } from '../overlayRoot'
import { ULTRA_EFFECT_EVENT } from '../../lib/ultraModeEffect'

/** How long the layer stays, in ms: the full effect, and the reduced-motion fade. */
export const ULTRA_EFFECT_MS = 900
export const ULTRA_EFFECT_FADE_MS = 300
/** How long to look for the Board tab: the side panel opens it just after the click.
 *  Its 0.6 s glow then still ends inside `ULTRA_EFFECT_MS`. */
const BOARD_LOOK_MS = 250
/** The Board tab's chip on the side-panel strip: `TAB_CHIP_ID_ATTR` (pages/chat/SidePanelTabNav)
 *  on view id `workers`. Spelled here, since only the SidePanel facade may import its owners. */
const BOARD_TAB = '[data-tab-chip-id="workers"]'

interface Box { left: number; top: number; width: number; height: number }
interface Shot { id: number; x: number; y: number; area: Box; fade: boolean }

function box(r: DOMRect): Box { return { left: r.left, top: r.top, width: r.width, height: r.height } }

function chatArea(): Box {
  const dock = document.querySelector('[data-testid="composer-dock-root"]')
  const pane = dock?.parentElement
  return pane ? box(pane.getBoundingClientRect()) : { left: 0, top: 0, width: window.innerWidth, height: window.innerHeight }
}

function boardTab(): Box | null {
  const el = document.querySelector(BOARD_TAB)
  if (!el) return null
  const r = el.getBoundingClientRect()
  return r.width > 0 && r.height > 0 ? box(r) : null
}

function prefersReduced(): boolean {
  return !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
}

/** The Board tab's one glow: looked for until it shows, then drawn over it. */
function BoardGlow() {
  const [tab, setTab] = useState<Box | null>(null)
  useEffect(() => {
    let frame = 0
    const until = performance.now() + BOARD_LOOK_MS
    const look = () => {
      const found = boardTab()
      if (found) { setTab(found); return }
      if (performance.now() < until) frame = requestAnimationFrame(look)
    }
    look()
    return () => cancelAnimationFrame(frame)
  }, [])
  if (!tab) return null
  return (
    <motion.span data-testid="ultra-effect-board" className="ultra-fx-glow absolute rounded-md"
      style={{ left: tab.left - 2, top: tab.top - 2, width: tab.width + 4, height: tab.height + 4 }}
      initial={{ opacity: 0 }} animate={{ opacity: [0, 1, 0] }}
      transition={{ duration: 0.6, ease: 'easeOut', times: [0, 0.35, 1] }} />
  )
}

function FullEffect({ shot }: { shot: Shot }) {
  const { area, x, y } = shot
  return (
    <>
      <div className="absolute overflow-hidden rounded-xl" style={{ ...area }}>
        <motion.div data-testid="ultra-effect-sweep" className="ultra-fx-band absolute top-0 left-0 h-full w-[45%]"
          style={{ skewX: -12 }}
          initial={{ x: '-120%' }} animate={{ x: '260%' }}
          transition={{ duration: 0.75, ease: [0.4, 0, 0.2, 1] }} />
      </div>
      <svg className="absolute" style={{ ...area }} width={area.width} height={area.height} aria-hidden="true">
        <defs>
          <linearGradient id={`ultra-edge-${shot.id}`} x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" style={{ stopColor: "var(--aim)" }} />
            <stop offset="100%" style={{ stopColor: "var(--info)" }} />
          </linearGradient>
        </defs>
        <motion.rect x={1.5} y={1.5} width={Math.max(0, area.width - 3)} height={Math.max(0, area.height - 3)} rx={12} fill="none"
          stroke={`url(#ultra-edge-${shot.id})`} strokeWidth={2}
          initial={{ pathLength: 0, opacity: 1 }} animate={{ pathLength: 1, opacity: [1, 1, 0] }}
          transition={{ duration: 0.7, ease: 'easeOut', times: [0, 0.75, 1] }} />
      </svg>
      <motion.span data-testid="ultra-effect-flash" className="ultra-fx-flash absolute rounded-full"
        style={{ left: x - 60, top: y - 60, width: 120, height: 120 }}
        initial={{ scale: 0.3, opacity: 1 }} animate={{ scale: [0.3, 1.6, 3], opacity: [1, 1, 0] }}
        transition={{ duration: 0.6, ease: 'easeOut', times: [0, 0.35, 1] }} />
      {/* The bolt: full strength and at its largest for the first ~200 ms, then gone. */}
      <motion.span className="ultra-fx-bolt absolute grid place-items-center" style={{ left: x - 18, top: y - 18, width: 36, height: 36 }}
        initial={{ scale: 0.6, opacity: 1 }} animate={{ scale: [0.6, 2.2, 2.6], opacity: [1, 1, 0] }}
        transition={{ duration: 0.5, ease: 'easeOut', times: [0, 0.4, 1] }}>
        <Zap size={28} fill="currentColor" />
      </motion.span>
      <BoardGlow />
    </>
  )
}

export default function UltraModeEffect() {
  const reduce = useReducedMotion()
  const [shot, setShot] = useState<Shot | null>(null)
  useEffect(() => {
    const on = (e: Event) => {
      const area = chatArea()
      const d = (e as CustomEvent<{ x: number; y: number } | null>).detail
      setShot({ id: Date.now(), x: d?.x ?? area.left + area.width / 2, y: d?.y ?? area.top + area.height / 2, area, fade: !!reduce || prefersReduced() })
    }
    window.addEventListener(ULTRA_EFFECT_EVENT, on)
    return () => window.removeEventListener(ULTRA_EFFECT_EVENT, on)
  }, [reduce])
  useEffect(() => {
    if (!shot) return
    const t = window.setTimeout(() => setShot(null), shot.fade ? ULTRA_EFFECT_FADE_MS : ULTRA_EFFECT_MS)
    return () => window.clearTimeout(t)
  }, [shot])
  if (!shot) return null
  const body = shot.fade
    ? <motion.div className="ultra-fx-fade absolute rounded-xl" style={{ ...shot.area }}
        initial={{ opacity: 0 }} animate={{ opacity: [0, 1, 0] }} transition={{ duration: 0.25 }} />
    : <FullEffect shot={shot} />
  return (
    <OverlayPortal>
      <div key={shot.id} aria-hidden="true" data-testid="ultra-effect" data-variant={shot.fade ? 'fade' : 'full'}
        className="fixed inset-0 pointer-events-none overflow-hidden z-[60]">
        {body}
      </div>
    </OverlayPortal>
  )
}
