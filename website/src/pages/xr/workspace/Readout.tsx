/**
 * The readout of `/xr?fps=1` (`readoutLines.ts`): a small block of lines in a corner of the room's picture that lies on
 * no page (`ReadoutPlace.tsx`), outside the canvas and outside the box that counts fingers, so a finger on it is no
 * finger on the canvas. Never wider than the picture; each line it shows stays one line (it scrolls on a narrow
 * screen). It reads the pages' sources and the room's last frame times twice a second from refs. A click copies all
 * its lines as text, so that they can be handed back.
 */
import { useEffect, useRef, useState } from 'react'
import { Btn } from '../../../components/ui'
import { i18nT } from '../../../i18n/t'
import { deviceBook } from './deviceLines'
import { READOUT, paceLines, readoutLines, rateOf, roomLine, roomRate, sinceWindow, type ReadoutGl, type ReadoutPage, type ReadoutSample } from './readoutLines'
import { boxAt, cornerFor, type ScreenRect } from './ReadoutPlace'
import type { PageSource } from './source'

export interface ReadoutProps {
  /** The open pages, in the room's order. */
  pages: () => readonly { id: string; source: PageSource }[]
  /** The times of the room's last frames (`performance.now()`), newest last. */
  frames: () => readonly number[]
  /** The draw calls of the last frame, and the textures that the renderer holds. */
  draws: () => { calls: number; textures: number }
  /** The canvas the room is drawn on: what the renderer is is read from its context. */
  canvas: () => HTMLCanvasElement | null
  /** What the room says first, before the pages' lines: a page that is handed to the stream, and why. */
  notes?: () => readonly string[]
}

/** What the renderer says of itself, read once for its name and at each read for its size. */
function glOf(canvas: HTMLCanvasElement | null, name: { current: string | null }, draws: { calls: number; textures: number }): ReadoutGl | null {
  if (!canvas) return null
  // The canvas's own context: asking for it again hands back the one the room draws with, and makes none.
  const gl = canvas.getContext('webgl2')
  if (!gl) return null
  if (name.current === null) {
    const info = gl.getExtension('WEBGL_debug_renderer_info')
    name.current = String(info ? gl.getParameter(info.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER))
  }
  return { dpr: devicePixelRatio, canvas: [canvas.width, canvas.height], samples: Number(gl.getParameter(gl.SAMPLES)) || 0, draws: draws.calls, textures: draws.textures, renderer: name.current }
}

/** What the readout says: its lines, and how many of the first ones are pace lines (`paceLines`), drawn in a font whose digits are all as wide. */
export interface ReadoutRead { lines: string[]; pace: number; room: string }

/** The readout's lines, read twice a second: the same for the block on the page and for the plate in a session (`ReadoutPlate.tsx`). */
export function useReadoutLines({ pages, frames, draws, canvas, notes }: ReadoutProps): ReadoutRead {
  const [lines, setLines] = useState<ReadoutRead>({ lines: [], pace: 0, room: '' })
  const samples = useRef<ReadoutSample[]>([])
  const name = useRef<string | null>(null)
  useEffect(() => {
    const read = () => {
      const now = performance.now()
      const open: ReadoutPage[] = []
      for (const p of pages()) {
        const facts = p.source.readout?.()
        if (facts) open.push({ id: p.id, path: p.source.pagePath?.() ?? p.id, facts })
      }
      const before = sinceWindow(samples.current, now)
      const rates = new Map(open.map(p => [p.id, rateOf(p.id, p.facts.count, now, before)]))
      samples.current.push({ at: now, counts: new Map(open.map(p => [p.id, p.facts.count])) })
      // The pace lines go first: they are what a photograph of the plate is for (D57 2), and the plate holds 20 rows.
      const layers = deviceBook.live ? deviceBook.facts?.layers.granted ?? null : null
      const pace = paceLines(open, rates, layers)
      const room = roomRate(frames(), now)
      setLines({ lines: [...pace, ...(notes?.() ?? []), ...readoutLines(open, rates, room, glOf(canvas(), name, draws()))], pace: pace.length, room: roomLine(room) })
    }
    read()
    const timer = setInterval(read, READOUT.everyMs)
    return () => clearInterval(timer)
  }, [pages, frames, draws, canvas, notes])
  return lines
}

/**
 * The block in the wide view. It shows the pace lines and the room's line (the numbers the room is judged by), each
 * on one line; the other lines are there for a screen reader and for the copy, not drawn. It stands in a corner of
 * the room's picture that lies on no page (`ReadoutPlace.tsx`): the pages' boxes come from `avoid`.
 */
export function Readout({ avoid, ...props }: ReadoutProps & { avoid?: () => readonly ScreenRect[] }) {
  const { lines, pace, room } = useReadoutLines(props)
  const me = useRef<HTMLButtonElement>(null)
  const [at, setAt] = useState<{ left: number; top: number } | null>(null)
  const { canvas } = props
  useEffect(() => {
    const place = () => {
      const el = me.current
      const c = canvas()
      const parent = el?.offsetParent
      if (!el || !c || !parent) return
      const r = c.getBoundingClientRect()
      const area = { x0: r.left, y0: r.top, x1: r.right, y1: r.bottom }
      const size = { w: el.offsetWidth, h: el.offsetHeight }
      const box = boxAt(cornerFor(size, area, avoid?.() ?? []), size, area)
      const p = parent.getBoundingClientRect()
      const next = { left: Math.round(box.x0 - p.left), top: Math.round(box.y0 - p.top) }
      setAt(was => (was && was.left === next.left && was.top === next.top ? was : next))
    }
    place()
    const timer = setInterval(place, READOUT.everyMs)
    return () => clearInterval(timer)
  }, [canvas, avoid])
  const [copied, setCopied] = useState(false)
  useEffect(() => {
    if (!copied) return
    const done = setTimeout(() => setCopied(false), 1500)
    return () => clearTimeout(done)
  }, [copied])
  const copy = () => { void navigator.clipboard?.writeText(lines.join('\n')).then(() => setCopied(true), () => undefined) }
  return (
    <Btn
      ref={me}
      type="button"
      data-testid="xr-readout"
      aria-label={i18nT('xr.readout_copy')}
      title={i18nT('xr.readout_copy')}
      onClick={copy}
      style={at ?? undefined}
      // The button lays its children out in a row by itself: the lines stand one under another.
      className={`absolute ${at ? '' : 'bottom-2 left-2'} z-10 m-0 w-max max-w-[calc(100%-1rem)] max-h-[calc(100%-1rem)] flex-col items-start gap-0 overflow-auto cursor-copy rounded-md border border-border bg-bg-elevated px-2 py-1 text-left font-mono text-[11px] leading-snug text-text`}
    >
      {lines.slice(0, pace).map((line, i) => <span key={`p${i}`} className="block whitespace-pre">{line}</span>)}
      {room && <span className="block whitespace-pre">{room}</span>}
      {lines.slice(pace).filter(line => line !== room).map((line, i) => <span key={`r${i}`} className="sr-only">{line}</span>)}
      {copied && <span role="status" className="block text-muted">{i18nT('xr.readout_copied')}</span>}
    </Btn>
  )
}
