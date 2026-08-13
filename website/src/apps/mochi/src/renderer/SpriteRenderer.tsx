/**
 * SpriteRenderer — renders a horizontal sprite strip as an animated loop.
 * Detects and skips empty trailing frames to avoid flicker.
 */
import React, { useEffect, useRef } from 'react'

interface SpriteRendererProps {
  src: string
  frameWidth: number
  frameHeight: number
  fps?: number
  displaySize?: number
  totalFrames?: number
}

const SpriteRendererInner: React.FC<SpriteRendererProps> = ({
  src, frameWidth, frameHeight, fps = 8, displaySize, totalFrames,
}) => {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const rafRef = useRef(0)
  const timerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const frameRef = useRef(0)
  const lastTimeRef = useRef(0)

  useEffect(() => {
    const img = new Image()
    img.src = src
    frameRef.current = 0
    lastTimeRef.current = 0

    const onLoad = () => {
      const canvas = canvasRef.current
      if (!canvas) return
      const ctx = canvas.getContext('2d')
      if (!ctx) return

      // Detect actual frame count (skip empty trailing frames)
      const maxFrames = totalFrames || Math.floor(img.naturalWidth / frameWidth)
      let frames = maxFrames
      if (!totalFrames) {
        const testCanvas = document.createElement('canvas')
        testCanvas.width = frameWidth
        testCanvas.height = frameHeight
        // `willReadFrequently`: this context exists ONLY to be read back with
        // getImageData in the loop below. Without the hint Chromium keeps it
        // GPU-backed and warns on every readback (and each one stalls on a
        // texture fetch), which is what filled the pet window's console.
        const tctx = testCanvas.getContext('2d', { willReadFrequently: true })!
        for (let i = maxFrames - 1; i > 0; i--) {
          tctx.clearRect(0, 0, frameWidth, frameHeight)
          tctx.drawImage(img, i * frameWidth, 0, frameWidth, frameHeight, 0, 0, frameWidth, frameHeight)
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
        ctx.drawImage(img, frameRef.current * frameWidth, 0, frameWidth, frameHeight, 0, 0, frameWidth, frameHeight)
        frameRef.current = (frameRef.current + 1) % frames
      }

      // Static sprite: draw once, no animation loop at all.
      if (frames === 1) {
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

    img.addEventListener('load', onLoad)
    return () => {
      img.removeEventListener('load', onLoad)
      cancelAnimationFrame(rafRef.current)
      clearTimeout(timerRef.current)
    }
  }, [src, frameWidth, frameHeight, fps, totalFrames])

  const dw = displaySize || frameWidth
  const dh = displaySize || frameHeight
  return <canvas ref={canvasRef} width={frameWidth} height={frameHeight} style={{ width: dw, height: dh, imageRendering: 'pixelated' }} />
}

export const SpriteRenderer = React.memo(SpriteRendererInner)
