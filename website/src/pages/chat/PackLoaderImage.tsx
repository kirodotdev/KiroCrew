/**
 * One image of an installed pack's loader art, held on its first frame while the
 * user asks for reduced motion.
 *
 * Pack loader art may animate by itself: an animated WebP/APNG/GIF, or an SVG
 * carrying its own CSS or SMIL animation. That motion runs inside the image, where
 * nothing on the page reaches it. The global reduced-motion rule stops the page's
 * own animations, not an image's frames, and an SVG loaded through `<img>` does
 * not stop for the page's `prefers-reduced-motion` either: in Chromium it kept
 * animating under an emulated `reduce`, a reduced-motion media query inside the
 * SVG included. Theme overlays already stand down under reduced motion
 * (`ThemeExperienceLayer`); this is the loader's half of that rule.
 *
 * So under reduced motion a hidden `<img>` decodes the art and it is painted ONCE
 * onto a `<canvas>`, which never repaints it. A canvas paints an animated raster's
 * default (first) frame, and an SVG that is not displayed does not advance, so the
 * paint is the art at rest. The hidden `<img>` also carries the load-error path:
 * its `error` event reaches the footer's `onErrorCapture` the same way the visible
 * image's does. With motion allowed it renders the plain `<img>`.
 */
import { useRef, type CSSProperties, type SyntheticEvent } from 'react'
import { useReducedMotion } from '../../hooks/useReducedMotion'

/** Box edge used when the canvas has no laid-out size yet (a carousel slot is 14px). */
const FALLBACK_EDGE_PX = 14

/** Hides the decode-only `<img>`: inline, so the carousel's `.lyr>img` rule cannot show it. */
const DECODE_ONLY: CSSProperties = { display: 'none' }

export function PackLoaderImage({ src, className, style }: { src: string; className?: string; style?: CSSProperties }) {
  const reduced = useReducedMotion()
  const canvasRef = useRef<HTMLCanvasElement>(null)
  if (!reduced) {
    return <img className={className} src={src} alt="" aria-hidden="true" draggable={false} style={style} />
  }
  const paint = (e: SyntheticEvent<HTMLImageElement>) => paintStill(canvasRef.current, e.currentTarget)
  return (
    <>
      <canvas ref={canvasRef} className={className} aria-hidden="true" style={style} />
      <img src={src} alt="" aria-hidden="true" draggable={false} style={DECODE_ONLY} onLoad={paint} />
    </>
  )
}

/**
 * Paint `img` once into `canvas`, scaled to fit and centred the way
 * `object-fit: contain` places the visible image, at the display's pixel density.
 */
export function paintStill(canvas: HTMLCanvasElement | null, img: HTMLImageElement): void {
  const ctx = canvas?.getContext('2d')
  if (!canvas || !ctx) return
  const dpr = window.devicePixelRatio || 1
  const w = Math.max(1, Math.round((canvas.clientWidth || FALLBACK_EDGE_PX) * dpr))
  const h = Math.max(1, Math.round((canvas.clientHeight || FALLBACK_EDGE_PX) * dpr))
  // Assigning the bitmap size also clears it, so a new src never paints over the old art.
  canvas.width = w
  canvas.height = h
  const iw = img.naturalWidth
  const ih = img.naturalHeight
  // An image reporting no intrinsic size fills the box rather than vanishing.
  const scale = iw > 0 && ih > 0 ? Math.min(w / iw, h / ih) : 0
  const dw = scale ? iw * scale : w
  const dh = scale ? ih * scale : h
  try {
    ctx.drawImage(img, (w - dw) / 2, (h - dh) / 2, dw, dh)
  } catch {
    // An undecodable image leaves the canvas blank; the footer's label still says
    // "Thinking", which is how a failed loader image already reads.
  }
}
