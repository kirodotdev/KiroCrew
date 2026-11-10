/**
 * A canvas artifact's library thumbnail: its shapes drawn small, fitted to the
 * tile, in their own colours on the canvas's dotted ground. No text: at this
 * size it would be noise, and the card already carries the name.
 */
import { useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { boundsOf, parseCanvas, zOrdered, type Shape } from './canvasModel'
import { THUMB_GROUND } from './canvasPaint'
import { shapeLook } from './shapeLook'

const PAD = 24

function ThumbShape({ s }: { s: Shape }) {
  const look = shapeLook(s)
  const stroke = look.dashed || s.border === 'none' ? 'none' : look.border
  const common = { fill: s.kind === 'section' ? 'none' : look.fill, stroke, strokeWidth: look.width }
  if (s.kind === 'text') return <rect x={s.x} y={s.y + s.h / 2 - 3} width={s.w * 0.8} height={6} rx={3} fill="var(--muted)" opacity={0.5} />
  if (s.kind === 'ellipse') return <ellipse cx={s.x + s.w / 2} cy={s.y + s.h / 2} rx={s.w / 2} ry={s.h / 2} {...common} />
  if (s.kind === 'diamond') {
    return <polygon points={`${s.x + s.w / 2},${s.y} ${s.x + s.w},${s.y + s.h / 2} ${s.x + s.w / 2},${s.y + s.h} ${s.x},${s.y + s.h / 2}`} {...common} />
  }
  if (s.kind === 'section') {
    return <rect x={s.x} y={s.y} width={s.w} height={s.h} rx={14} fill={look.fill} fillOpacity={0.5} stroke="var(--border)" strokeWidth={2} />
  }
  return <rect x={s.x} y={s.y} width={s.w} height={s.h} rx={s.kind === 'sticky' ? 4 : 8} {...common} />
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
  const view = `${box.x - PAD} ${box.y - PAD} ${box.w + PAD * 2} ${box.h + PAD * 2}`
  return (
    <div data-testid="canvas-thumb" style={{ height, ...ground }}>
      <svg viewBox={view} width="100%" height="100%" preserveAspectRatio="xMidYMid meet" aria-hidden>
        {zOrdered(shapes).map(s => <ThumbShape key={s.id} s={s} />)}
      </svg>
    </div>
  )
}
