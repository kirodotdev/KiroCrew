/**
 * Shared sprite-strip renderer for the Mochi and Crew Companion apps.
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

      const maxFrames = totalFrames || Math.floor(img.naturalWidth / frameWidth)
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
          tctx.drawImage(img, i * frameWidth, 0, frameWidth, frameHeight, 0, 0, frameWidth, frameHeight)
          const data = tctx.getImageData(0, 0, frameWidth, frameHeight).data
          let hasContent = false
          for (let p = 3; p < data.length; p += 16) {
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

      if (frames === 1) {
        drawFrame()
        return
      }

      // Sleep between frames and use rAF only to align the draw with vsync.
      // A bare rAF loop keeps both app windows waking at display refresh rate.
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
  // eslint-disable-next-line jsx-a11y/control-has-associated-label -- decorative animation canvas, not interactive
  return <canvas ref={canvasRef} width={frameWidth} height={frameHeight} style={{ width: dw, height: dh, imageRendering: 'pixelated' }} />
}

export const SpriteRenderer = React.memo(SpriteRendererInner)
