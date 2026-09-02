/**
 * Shared canvas utilities for Worlds scenes.
 * Provides the canvas initialiser and the animation loop.
 */
import { useEffect } from 'react'
import { initTextCanvas } from './sceneText'

/** Shared canvas init: sets up pixel canvas + text overlay, returns bound draw-pixel fn */
export function initSceneCanvases(
  canvas: HTMLCanvasElement, textCanvas: HTMLCanvasElement,
  W: number, H: number, S: number,
) {
  const X = canvas.getContext('2d')!
  canvas.width = W * S; canvas.height = H * S; X.imageSmoothingEnabled = false
  const T = initTextCanvas(textCanvas, W, H, S)
  const d = (x: number, y: number, w: number, h: number, c: string) => {
    X.fillStyle = c; X.fillRect(x * S, y * S, w * S, h * S)
  }
  return { X, T, d }
}

/** Shared animation loop: runs update+draw on each frame when visible */
export function runSceneLoop(
  visibleRef: React.RefObject<boolean>,
  tickRef: React.MutableRefObject<number>,
  update: (t: number) => void,
  draw: (t: number) => void,
) {
  let raf: number
  const loop = () => {
    if (visibleRef.current) {
      tickRef.current++
      update(tickRef.current)
      draw(tickRef.current)
    }
    raf = requestAnimationFrame(loop)
  }
  loop()
  return () => cancelAnimationFrame(raf)
}

/** Sync visible prop to a ref for use in animation loops */
export function useVisibleSync(visibleRef: React.MutableRefObject<boolean>, visible: boolean) {
  useEffect(() => { visibleRef.current = visible }, [visible, visibleRef])
}
