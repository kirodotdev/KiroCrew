/**
 * A page source that is live and costs next to nothing: a ruled sheet that says its own size.
 *
 * It is what a panel shows when no page has been given to it, and it uses every part of the source contract
 * (`source.ts`) that a still cannot: asked for any size it lays itself out again at exactly that size, the page's
 * pointer is drawn where the hand points, a click leaves a mark, and a scroll moves the sheet. It has no words, only
 * numbers: the sheet's size in CSS pixels, and a rule every 100 of them.
 */
import { CanvasTexture, LinearFilter, SRGBColorSpace } from 'three'
import { XR_GL, css } from '../palette'
import type { PageFrame, PagePointer, PageScroll, PageSource } from './source'

/** Picture pixels for one CSS pixel: enough that the rules stay thin from close by. */
const DPR = 2

export class GridSource implements PageSource {
  /** What the panel handed over, for the test probe. */
  readonly seen = { moves: 0, downs: 0, ups: 0, cancels: 0, leaves: 0, scrolls: 0, scrolledPx: 0, last: null as PagePointer | null }
  private canvas = document.createElement('canvas')
  private texture = new CanvasTexture(this.canvas)
  /** The picture of the size before this one. A panel may still draw it for a frame, so it is freed one change later. */
  private before: CanvasTexture | null = null
  private readonly listeners = new Set<() => void>()
  private readonly pointers = new Map<number, { x: number; y: number }>()
  private clicks: { x: number; y: number }[] = []
  private scrollY = 0

  constructor(readonly id: string, private cssWidth: number, private cssHeight: number) {
    this.fresh()
    this.draw()
  }

  /** A texture has one size for its life: a sheet of another size is a new canvas and a new texture. */
  private fresh(): void {
    this.canvas = document.createElement('canvas')
    this.canvas.width = Math.round(this.cssWidth) * DPR
    this.canvas.height = Math.round(this.cssHeight) * DPR
    this.texture = new CanvasTexture(this.canvas)
    this.texture.colorSpace = SRGBColorSpace
    this.texture.minFilter = LinearFilter
    this.texture.generateMipmaps = false
  }

  private draw(): void {
    const { canvas } = this
    const w = Math.round(this.cssWidth)
    const h = Math.round(this.cssHeight)
    const ctx = canvas.getContext('2d')
    // A browser with no working 2D canvas leaves the sheet blank; the panel still stands.
    if (!ctx) return
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0)
    ctx.fillStyle = css(XR_GL.glass)
    ctx.fillRect(0, 0, w, h)
    ctx.font = '12px system-ui, sans-serif'
    ctx.textBaseline = 'top'
    for (let x = 0; x <= w; x += 100) {
      ctx.fillStyle = css(XR_GL.edge)
      ctx.fillRect(x, 0, 1, h)
      ctx.fillStyle = css(XR_GL.faint)
      ctx.fillText(String(x), x + 4, 4)
    }
    // The rules across move with the scroll; the numbers say how far down the sheet each one is.
    for (let y = -(this.scrollY % 100); y <= h; y += 100) {
      ctx.fillStyle = css(XR_GL.edge)
      ctx.fillRect(0, y, w, 1)
      ctx.fillStyle = css(XR_GL.faint)
      ctx.fillText(String(Math.round(y + this.scrollY)), 4, y + 4)
    }
    ctx.fillStyle = css(XR_GL.ink)
    ctx.font = '600 44px system-ui, sans-serif'
    ctx.textAlign = 'center'
    ctx.textBaseline = 'middle'
    ctx.fillText(`${w} × ${h}`, w / 2, h / 2)
    ctx.textAlign = 'start'
    ctx.strokeStyle = css(XR_GL.dim)
    for (const c of this.clicks) { ctx.beginPath(); ctx.arc(c.x, c.y - this.scrollY, 9, 0, Math.PI * 2); ctx.stroke() }
    ctx.fillStyle = css(XR_GL.ink)
    for (const p of this.pointers.values()) { ctx.fillRect(p.x - 10, p.y, 21, 1); ctx.fillRect(p.x, p.y - 10, 1, 21) }
    this.texture.needsUpdate = true
  }

  private changed(): void {
    this.draw()
    for (const fn of this.listeners) fn()
  }

  frame(): PageFrame { return { texture: this.texture, cssWidth: this.cssWidth, cssHeight: this.cssHeight } }

  subscribe(changed: () => void): () => void {
    this.listeners.add(changed)
    return () => { this.listeners.delete(changed) }
  }

  resize(cssWidth: number, cssHeight: number): void {
    if (Math.round(cssWidth) === Math.round(this.cssWidth) && Math.round(cssHeight) === Math.round(this.cssHeight)) return
    this.cssWidth = cssWidth
    this.cssHeight = cssHeight
    this.before?.dispose()
    this.before = this.texture
    this.fresh()
    this.changed()
  }

  pointer(event: PagePointer): void {
    const key = ({ move: 'moves', down: 'downs', up: 'ups', cancel: 'cancels', leave: 'leaves' } as const)[event.type]
    this.seen[key] += 1
    this.seen.last = event
    if (event.type === 'leave') this.pointers.delete(event.pointerId)
    else this.pointers.set(event.pointerId, { x: event.x, y: event.y })
    // A press and its release at the same place is a click: it leaves a mark on the sheet.
    if (event.type === 'up') this.clicks = [...this.clicks.slice(-11), { x: event.x, y: event.y + this.scrollY }]
    this.changed()
  }

  scroll(event: PageScroll): void {
    this.seen.scrolls += 1
    this.seen.scrolledPx += event.dy
    this.scrollY = Math.max(0, this.scrollY + event.dy)
    this.changed()
  }

  dispose(): void {
    this.listeners.clear()
    this.texture.dispose()
    this.before?.dispose()
  }
}
