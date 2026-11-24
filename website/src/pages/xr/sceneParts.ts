/**
 * The pieces the XR scene is built from: a Bot's face art turned into
 * textures, and a canvas-backed surface that panels and cards are painted on.
 */
import { CanvasTexture, LinearFilter, LinearMipmapLinearFilter, Object3D, SRGBColorSpace, type Texture } from 'three'
import type { FaceArt, FaceLayerId } from './faces'
import { Painter, type Hit, type PaintState } from './painter'
import type { XrPalette } from './palette'

const FACE_PX = 256

export function loadImage(uri: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image()
    img.onload = () => resolve(img)
    img.onerror = () => reject(new Error('image failed to load'))
    img.src = uri
  })
}

function canvasOf(w: number, h: number): HTMLCanvasElement {
  const c = document.createElement('canvas')
  c.width = w
  c.height = h
  return c
}

function textureOf(canvas: HTMLCanvasElement): CanvasTexture {
  const t = new CanvasTexture(canvas)
  t.colorSpace = SRGBColorSpace
  t.anisotropy = 4
  t.minFilter = LinearMipmapLinearFilter
  t.magFilter = LinearFilter
  return t
}

/** A CSS colour as a GL number, read back from one painted pixel. */
export function toGl(color: string): number {
  const c = canvasOf(1, 1)
  const ctx = c.getContext('2d', { willReadFrequently: true })
  if (!ctx) return 0xffffff
  ctx.fillStyle = color
  ctx.fillRect(0, 0, 1, 1)
  const [r, g, b] = ctx.getImageData(0, 0, 1, 1).data
  return (r << 16) | (g << 8) | b
}

export interface FaceTextures {
  layers: { id: FaceLayerId; tex: Texture; depth: number }[]
  /** The whole face as one picture. */
  flat: Texture
  /** The round face as the dashboard shows it, for panel headers. */
  thumb: HTMLCanvasElement
}

/** Rasterise the dashboard's face art. A layer that fails to load is left out, never replaced. */
export async function loadFace(art: FaceArt): Promise<FaceTextures> {
  const thumb = canvasOf(FACE_PX, FACE_PX)
  const tctx = thumb.getContext('2d')
  const layers: FaceTextures['layers'] = []
  for (const l of art.layers) {
    const img = await loadImage(l.uri).catch(() => null)
    if (!img) continue
    const c = canvasOf(FACE_PX, FACE_PX)
    const ctx = c.getContext('2d')
    if (!ctx) continue
    // A face is round on every surface of the dashboard; the same cut applies here.
    ctx.beginPath()
    ctx.arc(FACE_PX / 2, FACE_PX / 2, FACE_PX / 2, 0, Math.PI * 2)
    ctx.clip()
    ctx.drawImage(img, 0, 0, FACE_PX, FACE_PX)
    tctx?.drawImage(c, 0, 0)
    layers.push({ id: l.id, tex: textureOf(c), depth: l.depth })
  }
  const flatCanvas = canvasOf(FACE_PX, FACE_PX)
  const flatImg = await loadImage(art.flat).catch(() => null)
  if (flatImg) flatCanvas.getContext('2d')?.drawImage(flatImg, 0, 0, FACE_PX, FACE_PX)
  const flat = textureOf(flatCanvas)
  return { layers, flat, thumb }
}

/**
 * A canvas that stands somewhere: a panel, a card, the hand strip. Painted from data; aimed at by rectangle.
 * It is data. `Panels.tsx` draws a plane with this canvas on it, where `at` says.
 */
export class Surface {
  readonly canvas: HTMLCanvasElement
  readonly tex: CanvasTexture
  /** Where it stands, which way it faces and how big it is: a plain transform, in the seat's space. It is not drawn. */
  readonly at = new Object3D()
  /** How see-through it is as a whole, from 0 to 1. */
  opacity = 1
  /** Its hit places stand apart, and a ray that goes between two of them goes on to what is behind. */
  gaps = false
  readonly painter: Painter
  readonly state: PaintState = { hot: '', hold: null }
  hits: Hit[] = []
  dirty = true
  scroll = 0
  maxScroll = 0

  constructor(readonly key: string, pxW: number, pxH: number, readonly width: number, pal: XrPalette, icons: ReadonlyMap<string, CanvasImageSource>, order = 20) {
    this.canvas = canvasOf(pxW, pxH)
    this.tex = textureOf(this.canvas)
    this.painter = new Painter(this.canvas, pal, icons, this.state)
    this.at.renderOrder = order
    this.at.visible = false
  }

  get height(): number { return (this.width * this.canvas.height) / this.canvas.width }

  paint(draw: (p: Painter) => void): void {
    draw(this.painter)
    this.hits = this.painter.hits
    this.tex.needsUpdate = true
    this.dirty = false
  }

  /** The canvas point of a plane hit given in the plane's own uv. */
  px(u: number, v: number): [number, number] {
    return [u * this.canvas.width, (1 - v) * this.canvas.height]
  }

  setHot(id: string): void {
    if (this.state.hot !== id) { this.state.hot = id; this.dirty = true }
  }

  dispose(): void {
    this.at.removeFromParent()
    this.tex.dispose()
  }
}
