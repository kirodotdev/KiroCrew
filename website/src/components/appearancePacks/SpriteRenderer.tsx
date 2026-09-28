/**
 * SpriteRenderer — renders one row of a sprite sheet as an animated loop.
 * Detects and skips empty trailing frames to avoid flicker.
 *
 * Core, not app-owned, for the same reason as `LottieRenderer`: a crew can wear
 * a sprite pack, and core never imports from `apps/`.
 */
import React, { useEffect, useRef } from 'react'

interface SpriteRendererProps {
  src: string
  frameWidth: number
  frameHeight: number
  fps?: number
  displaySize?: number
  totalFrames?: number
  /**
   * Which ROW of the sheet to step, zero-based. A pack's `sprite.rowAssignments`
   * maps a slot name to its row, so one sheet carries idle on row 0 and working
   * on row 1. Default 0 — a single-row strip is the same drawing it always was.
   */
  row?: number
  /**
   * Run the loop, or draw frame 0 once and stop. `false` is what a dense roster
   * renders (see `PackAvatar`); it costs one `drawImage` and no timers.
   */
  playing?: boolean
  /**
   * The sheet could not be decoded. Without it a missing or corrupt sheet left an
   * all-transparent canvas: the caller believed it had drawn a face, so no fallback
   * ran. Must be STABLE across renders (`useCallback`) -- it is an effect dependency.
   */
  onError?: () => void
}

/**
 * The most frames one row may hold before the sheet is refused. The empty-frame
 * scan below is a synchronous `getImageData` per candidate frame on the main
 * thread, and its length is `naturalWidth / frameWidth` — so a 1px frame over a
 * wide, cheaply compressed, fully transparent sheet (well under the bundle size
 * cap) is tens of thousands of readbacks that never early-exit. No shipped or
 * sample pack comes near this; a row that does is not a sprite strip.
 */
const MAX_FRAMES_PER_ROW = 512

const SpriteRendererInner: React.FC<SpriteRendererProps> = ({
  src, frameWidth, frameHeight, fps = 8, displaySize, totalFrames, row = 0, playing = true, onError,
}) => {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const rafRef = useRef(0)
  const timerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const frameRef = useRef(0)
  const lastTimeRef = useRef(0)

  useEffect(() => {
    // Geometry the sheet cannot be cut by is refused before anything is fetched:
    // the frame count is `naturalWidth / frameWidth`, so a width of `0` (or a
    // string that coerces to one, straight out of a hand-written manifest) is
    // `Infinity` frames and a trailing-frame scan that never returns. Reported
    // like a missing file, so the caller falls back rather than hangs.
    if (
      !(Number.isFinite(frameWidth) && frameWidth >= 1)
      || !(Number.isFinite(frameHeight) && frameHeight >= 1)
    ) {
      onError?.()
      return
    }
    const img = new Image()
    // `img` is an HTMLImageElement (decode-only, cannot execute script). `src` is
    // the same-origin per-slot route for a crew avatar (`packSlotUrl`), or the
    // Companion's own data:/blob: URL from a file the user picked — no externally
    // controlled input reaches the sink.
    // nosemgrep: semgrep.kirocrew.frontend-external-script-inject
    img.src = src
    frameRef.current = 0
    lastTimeRef.current = 0
    // Where this row starts in the sheet. Row 0 keeps the original `0` offset.
    const sy = Math.max(0, row) * frameHeight

    const onLoad = () => {
      // A row the sheet does not have samples off the image: `drawImage` draws
      // nothing, the sheet still decoded so `error` never fires, and the crew
      // shows a transparent tile. The map is third-party data, so check it
      // against the sheet that actually arrived and report like a missing file.
      if (sy + frameHeight > img.naturalHeight || frameWidth > img.naturalWidth) {
        onError?.()
        return
      }
      const canvas = canvasRef.current
      if (!canvas) return
      const ctx = canvas.getContext('2d')
      if (!ctx) return

      // Detect actual frame count (skip empty trailing frames)
      const maxFrames = totalFrames || Math.floor(img.naturalWidth / frameWidth)
      // Bounded, because the scan's cost is this number and the sheet is
      // third-party content: refused like any other geometry the renderer will
      // not cut, so the caller falls back rather than the tab hanging.
      if (maxFrames > MAX_FRAMES_PER_ROW) {
        onError?.()
        return
      }
      let frames = maxFrames
      if (!totalFrames) {
        const testCanvas = document.createElement('canvas')
        testCanvas.width = frameWidth
        testCanvas.height = frameHeight
        // This context exists only for repeated getImageData calls. The hint
        // avoids a GPU readback and Chromium warning for every sampled frame.
        const tctx = testCanvas.getContext('2d', { willReadFrequently: true })!
        for (let i = maxFrames - 1; i > 0; i--) {
          tctx.clearRect(0, 0, frameWidth, frameHeight)
          tctx.drawImage(img, i * frameWidth, sy, frameWidth, frameHeight, 0, 0, frameWidth, frameHeight)
          const data = tctx.getImageData(0, 0, frameWidth, frameHeight).data
          let hasContent = false
          for (let p = 3; p < data.length; p += 16) { // sample every 4th pixel alpha
            if (data[p] > 10) { hasContent = true; break }
          }
          if (hasContent) { frames = i + 1; break }
        }
      }
      if (frames < 1) frames = 1

      const interval = 1000 / fps
      const drawFrame = () => {
        ctx.clearRect(0, 0, frameWidth, frameHeight)
        ctx.drawImage(img, frameRef.current * frameWidth, sy, frameWidth, frameHeight, 0, 0, frameWidth, frameHeight)
        frameRef.current = (frameRef.current + 1) % frames
      }

      // Static sprite: draw once, no animation loop at all. Either the row holds
      // one frame, or the caller asked for a still (an avatar off screen).
      if (frames === 1 || !playing) {
        drawFrame()
        return
      }

      // Pace wakeups at the sprite's fps, not the display's refresh rate.
      // A bare rAF loop wakes the renderer at 60-120Hz to draw at ~8fps, and
      // (because an active rAF consumer keeps the compositor's BeginFrame
      // stream running) holds the GPU process busy too — this window runs with
      // backgroundThrottling disabled, so nothing ever throttles it. Instead,
      // sleep out the inter-frame gap with a timer and use rAF only to align
      // the actual draw with the next vsync.
      const animate = (time: number) => {
        rafRef.current = 0
        if (time - lastTimeRef.current >= interval) {
          lastTimeRef.current = time
          drawFrame()
        }
        const wait = Math.max(0, interval - (performance.now() - lastTimeRef.current))
        timerRef.current = setTimeout(() => {
          rafRef.current = requestAnimationFrame(animate)
        }, wait)
      }
      rafRef.current = requestAnimationFrame(animate)
    }

    const onFail = () => onError?.()
    img.addEventListener('load', onLoad)
    img.addEventListener('error', onFail)
    return () => {
      img.removeEventListener('load', onLoad)
      img.removeEventListener('error', onFail)
      cancelAnimationFrame(rafRef.current)
      clearTimeout(timerRef.current)
    }
  }, [src, frameWidth, frameHeight, fps, totalFrames, row, playing, onError])

  const dw = displaySize || frameWidth
  const dh = displaySize || frameHeight
  // eslint-disable-next-line jsx-a11y/control-has-associated-label -- decorative animation canvas, not interactive
  return <canvas ref={canvasRef} width={frameWidth} height={frameHeight} style={{ width: dw, height: dh, imageRendering: 'pixelated' }} />
}

export const SpriteRenderer = React.memo(SpriteRendererInner)
