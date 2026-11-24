/**
 * Where the readout's block stands in the wide view (`Readout.tsx`): in a corner of the room's picture that lies on
 * no page. `PageRects` reads, in the canvas, where each page is on the screen; `cornerFor` picks the first corner
 * (lower left, lower right, upper left, upper right) whose box covers none of them, else the one that covers least.
 */
import { useFrame, useThree } from '@react-three/fiber'
import { useRef, type MutableRefObject } from 'react'
import { Vector3 } from 'three'

/** A box on the screen, in CSS pixels of the window. */
export interface ScreenRect { x0: number; y0: number; x1: number; y1: number }

/** The corners the block may stand in, in the order they are tried. */
export const READOUT_CORNERS = ['lowerLeft', 'lowerRight', 'upperLeft', 'upperRight'] as const
export type ReadoutCorner = (typeof READOUT_CORNERS)[number]

/** How far the block stands from the picture's edges, in CSS pixels; how often the pages' boxes are read, in seconds. */
export const READOUT_PLACE = { insetPx: 8, everyS: 0.25 } as const

/** The block's box at a corner of `area`. */
export function boxAt(corner: ReadoutCorner, size: { w: number; h: number }, area: ScreenRect): ScreenRect {
  const i = READOUT_PLACE.insetPx
  const x0 = corner === 'lowerLeft' || corner === 'upperLeft' ? area.x0 + i : area.x1 - i - size.w
  const y0 = corner === 'upperLeft' || corner === 'upperRight' ? area.y0 + i : area.y1 - i - size.h
  return { x0, y0, x1: x0 + size.w, y1: y0 + size.h }
}

const overlap = (a: ScreenRect, b: ScreenRect) => Math.max(0, Math.min(a.x1, b.x1) - Math.max(a.x0, b.x0)) * Math.max(0, Math.min(a.y1, b.y1) - Math.max(a.y0, b.y0))

/** The first corner where the block covers no page; with none, the one where it covers the least (the earlier of two alike). */
export function cornerFor(size: { w: number; h: number }, area: ScreenRect, pages: readonly ScreenRect[]): ReadoutCorner {
  let best: ReadoutCorner = READOUT_CORNERS[0]
  let least = Infinity
  for (const corner of READOUT_CORNERS) {
    const box = boxAt(corner, size, area)
    const covered = pages.reduce((n, p) => n + overlap(box, p), 0)
    if (covered === 0) return corner
    if (covered < least) { least = covered; best = corner }
  }
  return best
}

const V = new Vector3()
const CORNERS = [[-0.5, -0.5], [0.5, -0.5], [0.5, 0.5], [-0.5, 0.5]] as const

/** In the canvas: four times a second, the screen box of every page that is drawn (its mesh carries `userData.xrPage`). */
export function PageRects({ into }: { into: MutableRefObject<ScreenRect[]> }) {
  const camera = useThree(s => s.camera)
  const gl = useThree(s => s.gl)
  const last = useRef(-1)
  useFrame(({ scene, clock }) => {
    if (clock.elapsedTime - last.current < READOUT_PLACE.everyS) return
    last.current = clock.elapsedTime
    const on = gl.domElement.getBoundingClientRect()
    const out: ScreenRect[] = []
    scene.traverse(o => {
      if (typeof o.userData.xrPage !== 'string' || !o.visible) return
      let x0 = Infinity; let y0 = Infinity; let x1 = -Infinity; let y1 = -Infinity; let ahead = false
      for (const [u, v] of CORNERS) {
        V.set(u, v, 0).applyMatrix4(o.matrixWorld).project(camera)
        if (V.z > 1) continue
        ahead = true
        const x = on.left + ((V.x + 1) / 2) * on.width
        const y = on.top + ((1 - V.y) / 2) * on.height
        x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y)
      }
      if (ahead) out.push({ x0, y0, x1, y1 })
    })
    into.current = out
  })
  return null
}
