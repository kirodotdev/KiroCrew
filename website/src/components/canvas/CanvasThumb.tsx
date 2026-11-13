/**
 * A canvas artifact's library thumbnail: its shapes drawn small, fitted to the
 * tile, in their own colours on the canvas's dotted ground. No text: at this
 * size it would be noise, and the card already carries the name.
 */
import { useMemo, type CSSProperties } from 'react'
import { useTranslation } from 'react-i18next'
import { boundsOf, parseCanvas, zOrdered, type Shape } from './canvasModel'
import { THUMB_GROUND } from './canvasPaint'
import { shapeLook } from './shapeLook'

/** Space around the shapes, in canvas units, so the outermost ones do not touch the tile's edge. */
const PAD = 24

/**
 * One shape, placed in percent of the frame so the whole drawing scales with the
 * tile. Plain boxes, not SVG: an ellipse is a rounded box, a diamond a clipped one.
 */
function ThumbShape({ s, box }: { s: Shape; box: { x: number; y: number; w: number; h: number } }) {
  const look = shapeLook(s)
  const pct = (v: number, of: number) => (v / of) * 100
  const place: CSSProperties = {
    position: 'absolute',
    left: `${pct(s.x - box.x, box.w)}%`,
    top: `${pct(s.y - box.y, box.h)}%`,
    width: `${pct(s.w, box.w)}%`,
    height: `${pct(s.h, box.h)}%`,
  }
  if (s.kind === 'text') {
    return <span data-testid="canvas-thumb-shape" style={{
      ...place,
      top: `${pct(s.y - box.y + s.h / 2 - 3, box.h)}%`,
      height: `${pct(6, box.h)}%`,
      width: `${pct(s.w * 0.8, box.w)}%`,
      borderRadius: 3, background: 'var(--muted)', opacity: 0.5,
    }} />
  }
  if (s.kind === 'section') {
    return <span data-testid="canvas-thumb-shape" style={{ ...place, borderRadius: 6, background: look.fill, opacity: 0.5, border: '1px solid var(--border)' }} />
  }
  const outlined = !look.sticker && !look.dashed && s.border !== 'none'
  return (
    <span data-testid="canvas-thumb-shape" style={{
      ...place, background: look.fill,
      borderRadius: s.kind === 'ellipse' ? '50%' : s.kind === 'sticky' ? 2 : 4,
      clipPath: s.kind === 'diamond' ? 'polygon(50% 0, 100% 50%, 50% 100%, 0 50%)' : undefined,
      border: outlined ? `1px solid ${look.border}` : undefined,
    }} />
  )
}

export default function CanvasThumb({ content, mini = false }: { content: string; mini?: boolean }) {
  const { t } = useTranslation()
  const parsed = useMemo(() => parseCanvas(content), [content])
  const shapes = parsed.ok ? parsed.canvas.shapes : []
  const box = boundsOf(shapes)
  const height = mini ? '100%' : 160
  const ground = {
    background: THUMB_GROUND,
    backgroundImage: 'radial-gradient(circle, color-mix(in srgb, var(--text) 22%, transparent) 1px, transparent 1.3px)',
    backgroundSize: '12px 12px',
  }
  if (!box) {
    return (
      <div data-testid="canvas-thumb" data-empty="" className="flex items-center justify-center text-[11px] text-muted" style={{ height, ...ground }}>
        {parsed.ok ? t('components.canvas.thumb_empty') : null}
      </div>
    )
  }
  const frame = { x: box.x - PAD, y: box.y - PAD, w: box.w + PAD * 2, h: box.h + PAD * 2 }
  return (
    // The frame keeps the drawing's own proportions and fits inside the tile,
    // centred, as a contained image would.
    <div data-testid="canvas-thumb" className="flex items-center justify-center overflow-hidden" style={{ height, ...ground }}>
      <div aria-hidden className="relative" style={{ width: '100%', maxHeight: '100%', aspectRatio: `${frame.w} / ${frame.h}` }}>
        {zOrdered(shapes).map(s => <ThumbShape key={s.id} s={s} box={frame} />)}
      </div>
    </div>
  )
}
