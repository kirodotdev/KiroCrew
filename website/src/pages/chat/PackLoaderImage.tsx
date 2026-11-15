/**
 * One image of an installed pack's art (a loader image, or the brand mark on the
 * welcome screen, the rail or the narrow header), held on its first frame while
 * the user asks for reduced motion.
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
 * So under reduced motion a hidden `<img>` decodes the art and it is painted onto
 * a `<canvas>` when it loads, and again only when the canvas changes size (the
 * rail's mark grows when the rail collapses). A canvas paints an animated raster's
 * default (first) frame, and an SVG that is not displayed does not advance, so
 * every paint is the same art at rest. The hidden `<img>` also carries the
 * load-error path: its `error` event reaches the footer's `onErrorCapture` the same
 * way the visible image's does. With motion allowed it renders the plain `<img>`.
 * `onLoad` and `onError` fire from whichever `<img>` is decoding; under reduced
 * motion `onLoad` runs after the still frame is painted.
 */
import { useEffect, useRef, type CSSProperties, type SyntheticEvent } from 'react'
import { useReducedMotion } from '../../hooks/useReducedMotion'

/** Box edge used when the canvas has no laid-out size yet (a carousel slot is 14px). */
const FALLBACK_EDGE_PX = 14

/** Hides the decode-only `<img>`: inline, so the carousel's `.lyr>img` rule cannot show it. */
const DECODE_ONLY: CSSProperties = { display: 'none' }

export function PackLoaderImage({ src, className, style, onLoad, onError }: {
  src: string
  className?: string
  style?: CSSProperties
  onLoad?: () => void
  onError?: () => void
}) {
  const reduced = useReducedMotion()
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const decodeRef = useRef<HTMLImageElement>(null)
  // The canvas exists only under reduced motion, so this re-runs when it mounts.
  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const observer = new ResizeObserver(() => {
      if (decodeRef.current?.complete) paintStill(canvas, decodeRef.current)
    })
    observer.observe(canvas)
    return () => observer.disconnect()
  }, [reduced])
  if (!reduced) {
    return <img className={className} src={src} alt="" aria-hidden="true" draggable={false} style={style} onLoad={onLoad} onError={onError} />
  }
  const paint = (e: SyntheticEvent<HTMLImageElement>) => {
    paintStill(canvasRef.current, e.currentTarget)
    onLoad?.()
  }
  return (
    <>
      <canvas ref={canvasRef} className={className} aria-hidden="true" style={style} />
      <img ref={decodeRef} src={src} alt="" aria-hidden="true" draggable={false} style={DECODE_ONLY} onLoad={paint} onError={onError} />
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
  // A canvas under a hidden ancestor has no layout box but still has its CSS size.
  const css = getComputedStyle(canvas)
  const w = Math.max(1, Math.round((canvas.clientWidth || parseFloat(css.width) || FALLBACK_EDGE_PX) * dpr))
  const h = Math.max(1, Math.round((canvas.clientHeight || parseFloat(css.height) || FALLBACK_EDGE_PX) * dpr))
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
