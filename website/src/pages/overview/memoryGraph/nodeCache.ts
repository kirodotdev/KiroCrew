/**
 * The nodes the canvas has fetched, by tile. The layout is cut into one fixed
 * grid of square tiles (`gridStep`); each tile is read once, every page of it,
 * and kept until the cache passes its cap, oldest first.
 */
import type { GraphBbox, GraphNodeRow } from '../../../api/client/memory'

/** Most nodes kept on the client before the oldest tiles are dropped. */
export const CACHE_CAP = 60000

/** Tiles across the layout's longer side. The grid is fixed per layout, so
 *  panning and zooming reuse what is cached and never read a tile twice. */
export const GRID = 4
/** Rows one box page returns (the server's `PAGE_CAP`); a store this small is one read. */
export const PAGE_ROWS = 2000

/** The fixed grid step for a layout spanning `bounds`. */
export function gridStep(bounds: GraphBbox): number {
  return Math.max(bounds[2] - bounds[0], bounds[3] - bounds[1], 1e-6) / GRID
}

/** Keys and boxes of the tiles that cover `box` at grid step `step`. */
export function tilesFor(box: GraphBbox, step: number): { key: string; box: GraphBbox }[] {
  const out: { key: string; box: GraphBbox }[] = []
  const x0 = Math.floor(box[0] / step), y0 = Math.floor(box[1] / step)
  const x1 = Math.floor(box[2] / step), y1 = Math.floor(box[3] / step)
  for (let i = x0; i <= x1; i++) for (let j = y0; j <= y1; j++) {
    out.push({ key: `${step}:${i}:${j}`, box: [i * step, j * step, (i + 1) * step, (j + 1) * step] })
  }
  return out
}

export class NodeCache {
  private tiles = new Map<string, string[]>()
  private nodes = new Map<string, GraphNodeRow>()
  private refs = new Map<string, number>()
  /** Bumped on every change, so a render can tell a new node set from the last. */
  version = 0

  has(key: string) { return this.tiles.has(key) }

  add(key: string, rows: GraphNodeRow[]) {
    if (this.tiles.has(key)) return
    this.tiles.set(key, rows.map(r => r.id))
    for (const r of rows) {
      this.nodes.set(r.id, r)
      this.refs.set(r.id, (this.refs.get(r.id) || 0) + 1)
    }
    while (this.nodes.size > CACHE_CAP && this.tiles.size > 1) {
      const [oldest, ids] = this.tiles.entries().next().value as [string, string[]]
      this.tiles.delete(oldest)
      for (const id of ids) {
        const left = (this.refs.get(id) || 1) - 1
        if (left <= 0) { this.refs.delete(id); this.nodes.delete(id) } else this.refs.set(id, left)
      }
    }
    this.version++
  }

  all(): GraphNodeRow[] { return [...this.nodes.values()] }
  get(id: string) { return this.nodes.get(id) }
  get size() { return this.nodes.size }
  clear() { this.tiles.clear(); this.nodes.clear(); this.refs.clear(); this.version++ }
}
