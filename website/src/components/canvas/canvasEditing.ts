/**
 * Canvas edits behind the context menu, after FigJam's: stacking order, lock and
 * unlock, and copy and paste. Pure: each takes a canvas and returns the next one.
 *
 * Stacking order is the order of `canvas.shapes`, later on top. Sections always
 * draw under everything else (see `zOrdered`), so moving one only changes its
 * place among the other sections.
 */

import { newId, type Canvas, type Connector, type Pt, type Shape } from './canvasModel'

export type Restack = 'front' | 'forward' | 'backward' | 'back'

/** The canvas with the shapes in *ids* moved in the stacking order, keeping their order among themselves. */
export function restack(b: Canvas, ids: Set<string>, how: Restack): Canvas {
  const shapes = [...b.shapes]
  if (how === 'front' || how === 'back') {
    const moved = shapes.filter(s => ids.has(s.id))
    const rest = shapes.filter(s => !ids.has(s.id))
    return { ...b, shapes: how === 'front' ? [...rest, ...moved] : [...moved, ...rest] }
  }
  // One step: each moved shape swaps with the nearest unmoved shape it overlaps
  // on that side, as in FigJam, so a step is never wasted on a shape far away.
  const overlaps = (a: Shape, c: Shape) => a.x < c.x + c.w && c.x < a.x + a.w && a.y < c.y + c.h && c.y < a.y + a.h
  const order = how === 'forward' ? [...shapes.keys()].reverse() : [...shapes.keys()]
  for (const i of order) {
    const s = shapes[i]
    if (!ids.has(s.id)) continue
    const step = how === 'forward' ? 1 : -1
    let j = i + step
    while (j >= 0 && j < shapes.length && (ids.has(shapes[j].id) || !overlaps(s, shapes[j]))) j += step
    if (j < 0 || j >= shapes.length) continue
    shapes.splice(i, 1)
    shapes.splice(j, 0, s)
  }
  return { ...b, shapes }
}

/** The canvas with the shapes in *ids* locked or unlocked. */
export function setLocked(b: Canvas, ids: Set<string>, locked: boolean): Canvas {
  return {
    ...b,
    shapes: b.shapes.map(s => {
      if (!ids.has(s.id)) return s
      if (locked) return { ...s, locked: true }
      const { locked: _gone, ...rest } = s
      void _gone
      return rest
    }),
  }
}

export const unlockAll = (b: Canvas): Canvas => setLocked(b, new Set(b.shapes.map(s => s.id)), false)

/** What a copy carries: the shapes, and the connectors that run between them. */
export interface CanvasClip { shapes: Shape[]; connectors: Connector[] }

export function copyShapes(b: Canvas, ids: Set<string>): CanvasClip | null {
  const shapes = b.shapes.filter(s => ids.has(s.id))
  if (!shapes.length) return null
  return { shapes, connectors: b.connectors.filter(c => ids.has(c.from) && ids.has(c.to)) }
}

/**
 * The canvas with a copy pasted in, with fresh ids, and those ids. Placed with its
 * top-left corner at *at*, or 24px down and right of where it was copied from. A
 * pasted copy is never locked, so it can be moved at once.
 */
export function pasteClip(b: Canvas, clip: CanvasClip, at?: Pt): { canvas: Canvas; ids: string[] } {
  const left = Math.min(...clip.shapes.map(s => s.x))
  const top = Math.min(...clip.shapes.map(s => s.y))
  const dx = at ? at.x - left : 24
  const dy = at ? at.y - top : 24
  const idMap = new Map(clip.shapes.map(s => [s.id, newId()]))
  const shapes = clip.shapes.map(s => {
    const { locked: _l, ...rest } = s
    void _l
    return { ...rest, id: idMap.get(s.id)!, x: Math.round(s.x + dx), y: Math.round(s.y + dy) }
  })
  const connectors = clip.connectors.map(c => ({ ...c, id: newId('c'), from: idMap.get(c.from)!, to: idMap.get(c.to)!, route: c.route?.map(p => ({ x: p.x + dx, y: p.y + dy })) }))
    .map(c => (c.route ? c : (({ route: _r, ...rest }) => { void _r; return rest })(c)))
  return { canvas: { ...b, shapes: [...b.shapes, ...shapes], connectors: [...b.connectors, ...connectors] }, ids: shapes.map(s => s.id) }
}
