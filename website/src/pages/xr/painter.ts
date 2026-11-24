/**
 * A small canvas painter for XR panels. A panel is a picture: text and shapes
 * drawn from data, plus a list of rectangles the scene can aim at. Nothing a
 * Bot wrote is ever parsed as markup or run; it is measured, wrapped and drawn.
 */
import type { XrPalette } from './palette'
import { wrapLines } from './text'

export interface Hit {
  id: string
  x: number
  y: number
  w: number
  h: number
  /** Needs the aim-rest and hold of `PressGuard`. */
  guarded?: boolean
  /** What a guarded press here would decide. A change of it disarms the button. */
  version?: string
  /** Pressing and moving does this instead of a click. */
  drag?: 'move' | 'resize' | 'thing' | 'grip'
}

export type Tone = 'plain' | 'accent' | 'warn' | 'danger' | 'ok'

export interface PaintState {
  /** The hit under the pointer. */
  hot: string
  /** A guarded hold in progress. */
  hold: { id: string; p: number } | null
}

export class Painter {
  readonly ctx: CanvasRenderingContext2D
  hits: Hit[] = []
  private oy = 0
  private clip: { y: number; h: number } | null = null

  constructor(
    readonly canvas: HTMLCanvasElement,
    readonly pal: XrPalette,
    readonly icons: ReadonlyMap<string, CanvasImageSource>,
    readonly state: PaintState,
  ) {
    const ctx = canvas.getContext('2d')
    if (!ctx) throw new Error('no 2d canvas')
    this.ctx = ctx
  }

  get w(): number { return this.canvas.width }
  get h(): number { return this.canvas.height }

  begin(radius = 36): void {
    const { ctx } = this
    this.hits = []
    this.oy = 0
    this.clip = null
    ctx.setTransform(1, 0, 0, 1, 0, 0)
    ctx.clearRect(0, 0, this.w, this.h)
    ctx.textBaseline = 'alphabetic'
    ctx.globalAlpha = 0.94
    this.box(2, 2, this.w - 4, this.h - 4, radius, this.pal.glass, this.pal.edge)
    ctx.globalAlpha = 1
  }

  tone(t: Tone): string {
    const p = this.pal
    return t === 'accent' ? p.accent : t === 'warn' ? p.warn : t === 'danger' ? p.danger : t === 'ok' ? p.ok : p.ink
  }

  font(px: number, o: { bold?: boolean; mono?: boolean; italic?: boolean } = {}): void {
    this.ctx.font = `${o.italic ? 'italic ' : ''}${o.bold ? '600 ' : '400 '}${px}px ${o.mono ? this.pal.mono : this.pal.sans}`
  }

  measure = (s: string): number => this.ctx.measureText(s).width

  /** One line, cut with an ellipsis when it is wider than `max`. */
  text(s: string, x: number, y: number, color: string, max = Infinity, align: CanvasTextAlign = 'left'): number {
    const { ctx } = this
    let out = s
    if (this.measure(out) > max) {
      const chars = [...out]
      while (chars.length && this.measure(chars.join('') + '…') > max) chars.pop()
      out = chars.join('') + '…'
    }
    ctx.fillStyle = color
    ctx.textAlign = align
    ctx.fillText(out, x, y)
    ctx.textAlign = 'left'
    return this.measure(out)
  }

  /** Wrapped text. Returns the y under the last line and whether text was cut. */
  para(s: string, x: number, y: number, max: number, lineH: number, color: string, maxLines = Infinity): { y: number; clipped: boolean } {
    const { lines, clipped } = wrapLines(this.measure, s, max, maxLines)
    this.ctx.fillStyle = color
    lines.forEach((l, i) => this.ctx.fillText(l, x, y + i * lineH))
    return { y: y + lines.length * lineH, clipped }
  }

  lines(s: string, max: number, maxLines = Infinity): string[] {
    return wrapLines(this.measure, s, max, maxLines).lines
  }

  box(x: number, y: number, w: number, h: number, r: number, fill: string | null, stroke?: string | null, lineW = 2): void {
    const { ctx } = this
    ctx.beginPath()
    ctx.roundRect(x, y, w, h, r)
    if (fill) { ctx.fillStyle = fill; ctx.fill() }
    if (stroke) { ctx.strokeStyle = stroke; ctx.lineWidth = lineW; ctx.stroke() }
  }

  icon(name: string, x: number, y: number, size: number, alpha = 1): void {
    const img = this.icons.get(name)
    if (!img) return
    this.ctx.globalAlpha = alpha
    this.ctx.drawImage(img, x, y, size, size)
    this.ctx.globalAlpha = 1
  }

  /** A rectangle the scene can aim at. Rectangles outside the scroll window are dropped. */
  hit(id: string, x: number, y: number, w: number, h: number, extra: Pick<Hit, 'guarded' | 'drag' | 'version'> = {}): void {
    let top = y - this.oy
    let height = h
    if (this.clip) {
      const lo = Math.max(top, this.clip.y)
      const hi = Math.min(top + height, this.clip.y + this.clip.h)
      if (hi - lo < 12) return
      top = lo
      height = hi - lo
    }
    this.hits.push({ id, x, y: top, w, h: height, ...extra })
  }

  /** A button: label, optional icon, hover and hold feedback. `off` draws it muted but still pressable, so a try can explain itself. */
  btn(id: string, label: string, x: number, y: number, w: number, h: number,
    o: { tone?: Tone; icon?: string; guarded?: boolean; off?: boolean; drag?: Hit['drag']; px?: number; version?: string } = {}): void {
    const { ctx, pal, state } = this
    const hot = state.hot === id
    const tone = this.tone(o.tone ?? 'plain')
    this.box(x, y, w, h, Math.min(22, h / 2), hot ? pal.edge : pal.raised, o.off ? pal.edge : hot ? tone : pal.edge)
    if (state.hold && state.hold.id === id && state.hold.p > 0) {
      ctx.save()
      ctx.beginPath()
      ctx.roundRect(x, y, w * state.hold.p, h, Math.min(22, h / 2))
      ctx.globalAlpha = 0.35
      ctx.fillStyle = tone
      ctx.fill()
      ctx.restore()
    }
    const px = o.px ?? 26
    this.font(px, { bold: true })
    const iconSize = o.icon ? px + 6 : 0
    const tw = Math.min(this.measure(label), w - 28 - iconSize)
    let tx = x + (w - tw - (iconSize ? iconSize + 10 : 0)) / 2
    if (o.icon) { this.icon(o.icon, tx, y + (h - iconSize) / 2, iconSize, o.off ? 0.5 : 1); tx += iconSize + 10 }
    if (label) this.text(label, tx, y + h / 2 + px * 0.35, o.off ? pal.faint : o.tone && o.tone !== 'plain' ? tone : pal.ink, w - 28 - iconSize)
    this.hit(id, x, y, w, h, { guarded: o.guarded, drag: o.drag, version: o.version })
  }

  /** Scroll window: content drawn inside is shifted up by `scroll` and cut to the window. */
  scrollBegin(y: number, h: number, scroll: number): void {
    const { ctx } = this
    ctx.save()
    ctx.beginPath()
    ctx.rect(0, y, this.w, h)
    ctx.clip()
    ctx.translate(0, -scroll)
    this.oy = scroll
    this.clip = { y, h }
  }

  scrollEnd(): void {
    this.ctx.restore()
    this.oy = 0
    this.clip = null
  }
}

/** The hit at a canvas point; later rectangles lie on top of earlier ones. */
export function hitAt(hits: readonly Hit[], x: number, y: number): Hit | null {
  for (let i = hits.length - 1; i >= 0; i -= 1) {
    const h = hits[i]
    if (x >= h.x && x <= h.x + h.w && y >= h.y && y <= h.y + h.h) return h
  }
  return null
}
