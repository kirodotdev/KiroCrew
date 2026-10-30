import { useEffect, useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from 'react'
import { motion, useReducedMotion } from 'framer-motion'
import { ArrowDown, ArrowLeft, ArrowRight, ArrowUp, Globe, Keyboard } from 'lucide-react'

import { i18nT } from '../../i18n/t'
import { sanitizeLlmOutput } from '../../utils/sanitize'
import { decode } from './BrowserLiveCardCanvas'
import { scrollDirection, toPercent, type Recording, type ReplayEvent, type ReplayFrame } from '../../hooks/useBrowserLive.replay'

/**
 * The big view's picture while it replays: one `<canvas>` that paints the
 * recording's frame for the play head, and the layer over it that draws what
 * the agent did at that moment (a ripple where it clicked, the path it
 * dragged, an arrow for a scroll, a chip for what it typed or pressed, a banner
 * for a new URL).
 *
 * Geometry. The picture box takes the recording's own aspect ratio and is
 * fitted inside its container (`containSize`), so the canvas fills it exactly
 * and a point in frame pixels is a percentage of the box (`toPercent`). That is
 * the whole coordinate mapping: no letterbox offset to add.
 *
 * Frames are either the gateway's JPEG URLs (loaded through `Image`, cached by
 * URL, the next few preloaded) or frames this page holds in memory (decoded the
 * way the live canvas decodes them, password boxes painted over).
 */

/** The largest `w x h` with ratio `ratio` that fits `cw x ch`. */
export function containSize(cw: number, ch: number, ratio: number): { w: number; h: number } {
  if (!(cw > 0) || !(ch > 0) || !(ratio > 0)) return { w: 0, h: 0 }
  const w = Math.min(cw, ch * ratio)
  return { w: Math.floor(w), h: Math.floor(w / ratio) }
}

/** The fitted picture box for a container, kept current as it resizes. */
export function useContainFit(ref: RefObject<HTMLElement>, ratio: number): { w: number; h: number } | null {
  const [size, setSize] = useState<{ w: number; h: number } | null>(null)
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    const measure = () => {
      const next = containSize(el.clientWidth, el.clientHeight, ratio)
      setSize(prev => (prev && prev.w === next.w && prev.h === next.h ? prev : next))
    }
    measure()
    if (typeof ResizeObserver !== 'function') return
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [ref, ratio])
  return size
}

/** The box the picture fills: fitted when the container can be measured, else ratio-only. */
export function PictureBox({ ratio, children, testId }: { ratio: number; children: ReactNode; testId?: string }) {
  const hostRef = useRef<HTMLDivElement>(null)
  const fit = useContainFit(hostRef, ratio)
  const style = fit && fit.w > 0
    ? { width: fit.w, height: fit.h }
    : { width: '100%', aspectRatio: String(ratio) }
  return (
    <div ref={hostRef} className="relative flex-1 min-h-0 min-w-0 grid place-items-center">
      <div className="relative overflow-hidden rounded-md border border-border bg-card" style={style} data-testid={testId}>
        {children}
      </div>
    </div>
  )
}

const imageCache = new Map<string, HTMLImageElement>()
const imageLoading = new Map<string, Promise<HTMLImageElement>>()
const IMAGE_CACHE_MAX = 120

/** One request per URL: callers share the pending load, and a failed load is forgotten so a retry fetches again. */
function loadImage(url: string): Promise<HTMLImageElement> {
  const hit = imageCache.get(url)
  if (hit) return Promise.resolve(hit)
  const pending = imageLoading.get(url)
  if (pending) return pending
  const img = new Image()
  img.decoding = 'async'
  const load = new Promise<HTMLImageElement>((resolve, reject) => {
    img.onload = () => {
      imageLoading.delete(url)
      imageCache.set(url, img)
      if (imageCache.size > IMAGE_CACHE_MAX) imageCache.delete(imageCache.keys().next().value as string)
      resolve(img)
    }
    img.onerror = () => {
      imageLoading.delete(url)
      reject(new Error('frame'))
    }
    // An image element, not a script: url is a recorded frame on the gateway's own API.
    // nosemgrep: semgrep.kirocrew.frontend-external-script-inject
    img.src = url
  })
  imageLoading.set(url, load)
  return load
}

/** Paints `rec.frames[index]` into a canvas sized to the recording. */
export function ReplayCanvas({ rec, index, label }: { rec: Recording; index: number; label: string }) {
  const ref = useRef<HTMLCanvasElement>(null)
  const drawn = useRef<ReplayFrame | null>(null)
  useEffect(() => {
    const frame = rec.frames[index]
    const canvas = ref.current
    if (!frame || !canvas || drawn.current === frame) return
    let cancelled = false
    const w = rec.width || frame.w
    const h = rec.height || frame.h
    void (async () => {
      let img: ImageBitmap | HTMLImageElement
      try {
        img = typeof frame.src === 'string' ? await loadImage(frame.src) : await decode(frame.src)
      } catch { return }
      if (cancelled) return
      if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h }
      const ctx = canvas.getContext('2d')
      if (!ctx) return
      ctx.drawImage(img, 0, 0, w, h)
      if (typeof frame.src !== 'string') {
        const mask = frame.src.header.mask
        if (mask.length) {
          const sx = w / frame.src.header.w
          const sy = h / frame.src.header.h
          ctx.fillStyle = getComputedStyle(canvas).getPropertyValue('--card').trim() || '#000'
          for (const [x, y, rw, rh] of mask) ctx.fillRect(x * sx, y * sy, rw * sx, rh * sy)
        }
        if ('close' in img) img.close()
      }
      drawn.current = frame
    })()
    // Preload the next few server frames so playback does not wait on the network.
    for (let k = index + 1; k < Math.min(rec.frames.length, index + 6); k++) {
      const src = rec.frames[k].src
      if (typeof src === 'string') void loadImage(src).catch(() => {})
    }
    return () => { cancelled = true }
  }, [rec, index])
  return (
    <div role="img" aria-label={label} className="block w-full h-full">
      <canvas ref={ref} aria-hidden="true" data-testid="browser-replay-canvas" data-frame-index={index} className="block w-full h-full bg-[var(--card)]" />
    </div>
  )
}

/** What an event says in a few words: its own label, else a description of its op. */
export function eventLabel(e: ReplayEvent): string {
  if (e.op === 'person') return personLabel(e)
  const own = sanitizeLlmOutput(e.label).slice(0, 80)
  if (own) return own
  switch (e.op) {
    case 'click': return i18nT('pages.chat.browserLiveStage.op_click')
    case 'type': return i18nT('pages.chat.browserLiveStage.op_type')
    case 'key': return i18nT('pages.chat.browserLiveStage.op_key')
    case 'drag': return i18nT('pages.chat.browserLiveStage.op_drag')
    case 'scroll': return i18nT('pages.chat.browserLiveStage.op_scroll')
    case 'navigate': return e.url ? e.url : i18nT('pages.chat.browserLiveStage.op_navigate')
    default: return i18nT('pages.chat.browserLiveStage.op_other')
  }
}

/** "Sign-in · you filled it": our own words for a quick ask the person answered. */
function personLabel(e: ReplayEvent): string {
  const p = e.person
  const kinds: Record<string, string> = {
    credentials: i18nT('pages.chat.quickAsk.replay_kind_credentials'),
    text: i18nT('pages.chat.quickAsk.replay_kind_text'),
    otp: i18nT('pages.chat.quickAsk.replay_kind_otp'),
    human_check: i18nT('pages.chat.quickAsk.replay_kind_human_check'),
    confirm: i18nT('pages.chat.quickAsk.replay_kind_confirm'),
  }
  const actions: Record<string, string> = {
    fill: i18nT('pages.chat.quickAsk.replay_action_fill'),
    decline: i18nT('pages.chat.quickAsk.replay_action_decline'),
    take_over: i18nT('pages.chat.quickAsk.replay_action_take_over'),
    done: i18nT('pages.chat.quickAsk.replay_action_done'),
    confirm: i18nT('pages.chat.quickAsk.replay_action_confirm'),
    cancel: i18nT('pages.chat.quickAsk.replay_action_cancel'),
    ask: i18nT('pages.chat.quickAsk.replay_action_ask'),
    timeout: i18nT('pages.chat.quickAsk.replay_action_timeout'),
    hand_back: i18nT('pages.chat.quickAsk.replay_action_hand_back'),
    page_moved: i18nT('pages.chat.quickAsk.replay_action_page_moved'),
    auto: i18nT('pages.chat.quickAsk.replay_action_auto'),
  }
  const kind = (p && kinds[p.kind]) || i18nT('pages.chat.quickAsk.replay_kind_other')
  const action = p ? actions[p.action] : ''
  return action ? i18nT('pages.chat.quickAsk.replay_label', { kind, action }) : kind
}

const SCROLL_ICON = { up: ArrowUp, down: ArrowDown, left: ArrowLeft, right: ArrowRight } as const

/**
 * The action at the play head, drawn over the picture. `event` null draws
 * nothing. Reduced motion keeps every mark and drops the motion.
 */
export function ReplayOverlay({ event, space }: { event: ReplayEvent | null; space: { w: number; h: number } }) {
  const reduce = useReducedMotion()
  if (!event) return null
  const at = event.point ? toPercent(event.point, space) : null
  const label = eventLabel(event)
  const tone = event.status === 'error' ? 'var(--danger)' : 'var(--accent)'
  const chip = (left: number, top: number) => (
    <span
      className="absolute -translate-x-1/2 -translate-y-[160%] whitespace-nowrap rounded-full border bg-bg/95 px-2.5 py-0.5 text-[12px] text-text-strong shadow-md max-w-[70%] truncate"
      style={{ left: `${left}%`, top: `${top}%`, borderColor: tone }}
      data-testid="browser-replay-overlay-label"
    >
      {label}
    </span>
  )
  return (
    <div className="pointer-events-none absolute inset-0" data-testid="browser-replay-overlay" data-op={event.op} aria-hidden="true">
      {event.op === 'navigate' && (
        <span className="absolute left-1/2 top-2.5 -translate-x-1/2 flex items-center gap-1.5 rounded-full border border-border-strong bg-bg/95 px-3 py-1 text-[12px] text-text-strong shadow-md max-w-[80%]" data-testid="browser-replay-overlay-url">
          <Globe size={12} aria-hidden="true" className="shrink-0 text-accent" />
          <span className="truncate">{event.url || label}</span>
        </span>
      )}
      {event.op === 'drag' && event.path && space.w > 0 && (
        <DragPath path={event.path} space={space} tone={tone} reduce={!!reduce} />
      )}
      {at && (event.op === 'click' || event.op === 'drag') && (
        <span className="absolute" style={{ left: `${at.left}%`, top: `${at.top}%` }} data-testid="browser-replay-overlay-point" data-left={at.left.toFixed(2)} data-top={at.top.toFixed(2)}>
          {!reduce && (
            <motion.span
              key={event.id}
              className="absolute -left-6 -top-6 size-12 rounded-full border-2"
              style={{ borderColor: tone }}
              initial={{ scale: 0.3, opacity: 0.9 }}
              animate={{ scale: 1.4, opacity: 0 }}
              transition={{ duration: 0.8, ease: 'easeOut' }}
            />
          )}
          <span className="absolute -left-2 -top-2 size-4 rounded-full border-2 border-bg" style={{ background: tone }} />
        </span>
      )}
      {at && event.op === 'scroll' && (() => {
        const Icon = SCROLL_ICON[scrollDirection(event.args)]
        return (
          <span className="absolute -translate-x-1/2 -translate-y-1/2 grid place-items-center size-9 rounded-full border bg-bg/90 shadow-md" style={{ left: `${at.left}%`, top: `${at.top}%`, borderColor: tone }} data-testid="browser-replay-overlay-point" data-left={at.left.toFixed(2)} data-top={at.top.toFixed(2)}>
            <Icon size={18} aria-hidden="true" style={{ color: tone }} />
          </span>
        )
      })()}
      {(event.op === 'type' || event.op === 'key') && (
        at ? chip(at.left, at.top) : (
          <span className="absolute left-1/2 bottom-3 -translate-x-1/2 flex items-center gap-1.5 rounded-full border bg-bg/95 px-3 py-1 text-[12px] text-text-strong shadow-md max-w-[80%]" style={{ borderColor: tone }} data-testid="browser-replay-overlay-label">
            <Keyboard size={12} aria-hidden="true" className="shrink-0" />
            <span className="truncate">{label}</span>
          </span>
        )
      )}
      {at && (event.op === 'click' || event.op === 'drag' || event.op === 'scroll') && chip(at.left, at.top)}
    </div>
  )
}

/** A drag's path over the replayed frame, drawn on a canvas sized to the
 *  frame's own coordinate space and stretched over it like the frame. */
function DragPath({ path, space, tone, reduce }: {
  path: { x: number; y: number }[]
  space: { w: number; h: number }
  tone: string
  reduce: boolean
}) {
  const ref = useRef<HTMLCanvasElement>(null)
  useEffect(() => {
    const c = ref.current
    const ctx = c?.getContext('2d')
    if (!c || !ctx || path.length < 2) return
    ctx.clearRect(0, 0, c.width, c.height)
    // The tone is a CSS variable; the canvas needs the colour it resolves to.
    ctx.strokeStyle = getComputedStyle(c).color
    ctx.lineWidth = Math.max(2, space.w / 300)
    ctx.lineCap = 'round'
    ctx.lineJoin = 'round'
    ctx.beginPath()
    ctx.moveTo(path[0].x, path[0].y)
    for (const p of path.slice(1)) ctx.lineTo(p.x, p.y)
    ctx.stroke()
  }, [path, space.w, space.h])
  return (
    <motion.canvas
      ref={ref}
      width={space.w}
      height={space.h}
      aria-hidden="true"
      className="absolute inset-0 w-full h-full pointer-events-none"
      style={{ color: tone }}
      data-testid="browser-replay-overlay-path"
      initial={reduce ? false : { opacity: 0 }}
      animate={{ opacity: 1 }}
      transition={reduce ? { duration: 0 } : { duration: 0.3, ease: 'easeOut' }}
    />
  )
}
