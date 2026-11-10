/**
 * A shape's dashed outline, drawn as SVG strokes rather than a CSS dashed
 * border, whose dashes the browser sets and nothing can tune.
 *
 * Dashes are fitted to each straight side, a whole number of them from corner to
 * corner, and the rounded corners are drawn solid, so a corner is never caught in
 * a gap, as in FigJam. (One dash pattern run round the whole perimeter would miss
 * whichever corners it happened to end on.)
 */

import { fitDash, ovalDash } from './dashFit'

interface Props {
  w: number
  h: number
  /** The outline's width and colour. */
  width: number
  color: string
  /** Corner radius of a rectangle; ignored for an ellipse and a diamond. */
  radius?: number
  ellipse?: boolean
  diamond?: boolean
  /** Where the box's outer edge is, from the svg's origin: a CSS border is outside the padding box. */
  offset?: number
}

export default function DashedOutline({ w, h, width, color, radius = 0, ellipse, diamond, offset = width }: Props) {
  const stroke = { fill: 'none', stroke: color, strokeWidth: width } as const
  const svg = (children: React.ReactNode) => (
    <svg aria-hidden data-testid="dashed-outline" width={w} height={h} viewBox={`0 0 ${w} ${h}`}
      style={{ position: 'absolute', left: -offset, top: -offset, overflow: 'visible', pointerEvents: 'none' }}>
      {children}
    </svg>
  )
  const side = (key: string, x1: number, y1: number, x2: number, y2: number) => {
    const d = fitDash(Math.hypot(x2 - x1, y2 - y1), width)
    return <line key={key} x1={x1} y1={y1} x2={x2} y2={y2} {...stroke} strokeDasharray={d} strokeLinecap="butt" />
  }

  if (ellipse) {
    const rx = Math.max(0, (w - width) / 2)
    const ry = Math.max(0, (h - width) / 2)
    return svg(<ellipse cx={w / 2} cy={h / 2} rx={rx} ry={ry} {...stroke} strokeDasharray={ovalDash(rx, ry, width)} strokeLinecap="butt" />)
  }

  if (diamond) {
    // The same four corners the filled polygon has; a small disc at each corner fills the notch two butt ends leave.
    const v = [{ x: w / 2, y: width }, { x: w - width, y: h / 2 }, { x: w / 2, y: h - width }, { x: width, y: h / 2 }]
    return svg(
      <>
        {v.map((p, i) => side(`s${i}`, p.x, p.y, v[(i + 1) % 4].x, v[(i + 1) % 4].y))}
        {v.map((p, i) => <circle key={`c${i}`} cx={p.x} cy={p.y} r={width / 2} fill={color} />)}
      </>,
    )
  }

  const x0 = width / 2
  const y0 = width / 2
  const x1 = w - width / 2
  const y1 = h - width / 2
  const r = Math.max(0, Math.min(radius - width / 2, (x1 - x0) / 2, (y1 - y0) / 2))
  return svg(
    <>
      {side('t', x0 + r, y0, x1 - r, y0)}
      {side('r', x1, y0 + r, x1, y1 - r)}
      {side('b', x1 - r, y1, x0 + r, y1)}
      {side('l', x0, y1 - r, x0, y0 + r)}
      {r > 0 && (
        // One corner arc per subpath, joined: the same path data, built so no
        // `${…} M` (a path command) sits where it reads as a number and a unit.
        <path d={[
          `M${x1 - r},${y0} A${r},${r} 0 0 1 ${x1},${y0 + r}`,
          `M${x1},${y1 - r} A${r},${r} 0 0 1 ${x1 - r},${y1}`,
          `M${x0 + r},${y1} A${r},${r} 0 0 1 ${x0},${y1 - r}`,
          `M${x0},${y0 + r} A${r},${r} 0 0 1 ${x0 + r},${y0}`,
        ].join(' ')}
          {...stroke} strokeLinecap="butt" data-testid="dashed-corners" />
      )}
    </>,
  )
}
