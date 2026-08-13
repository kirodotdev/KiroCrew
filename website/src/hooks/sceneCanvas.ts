/**
 * Shared canvas utilities for Worlds scenes.
 * Provides the pixel-drawing helper and animation loop hook.
 */
import { useCallback, useEffect, useRef } from 'react'
import { initTextCanvas } from './sceneText'

/** Pixel-drawing helper: fills a scaled rectangle on the canvas */
export function createPixelDrawer(ctx: CanvasRenderingContext2D, scale: number) {
  return (x: number, y: number, w: number, h: number, c: string) => {
    ctx.fillStyle = c
    ctx.fillRect(x * scale, y * scale, w * scale, h * scale)
  }
}

/** Hook that returns a memoized dp (draw-pixel) function bound to a scale */
export function useDp(scale: number) {
  return useCallback((X: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, c: string) => {
    X.fillStyle = c; X.fillRect(x * scale, y * scale, w * scale, h * scale)
  }, [scale])
}

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

interface SceneLoopOpts {
  canvasRef: React.RefObject<HTMLCanvasElement | null>
  textRef: React.RefObject<HTMLCanvasElement | null>
  W: number
  H: number
  S: number
  visible: boolean
  setup: (ctx: CanvasRenderingContext2D, textCtx: CanvasRenderingContext2D, dp: ReturnType<typeof createPixelDrawer>) => {
    update: (tick: number) => void
    draw: (tick: number) => void
    cleanup?: () => void
  }
}

/** Hook that manages the canvas animation loop lifecycle */
export function useSceneLoop({ canvasRef, textRef, W, H, S, visible, setup }: SceneLoopOpts) {
  const visibleRef = useRef(visible)
  const tickRef = useRef(0)

  useEffect(() => { visibleRef.current = visible }, [visible])

  useEffect(() => {
    const C = canvasRef.current!
    const X = C.getContext('2d')!
    C.width = W * S; C.height = H * S; X.imageSmoothingEnabled = false
    const T = initTextCanvas(textRef.current!, W, H, S)
    const dp = createPixelDrawer(X, S)
    const { update, draw, cleanup } = setup(X, T, dp)

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
    return () => {
      cancelAnimationFrame(raf)
      cleanup?.()
    }
  }, [W, H, S]) // eslint-disable-line react-hooks/exhaustive-deps
}

/** Sync visible prop to a ref for use in animation loops */
export function useVisibleSync(visibleRef: React.MutableRefObject<boolean>, visible: boolean) {
  useEffect(() => { visibleRef.current = visible }, [visible, visibleRef])
}
