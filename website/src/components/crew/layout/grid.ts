/**
 * Pure grid geometry + recursive tree ops for the layout editor's edit model.
 *
 * No React, no DOM — the drag/drop editor (PR 2) calls these to decide where an
 * item may land and to transform the tree by id. The recursive by-id ops are
 * what make cross-canvas move, deeper nesting, and cycle-prevention fall out for
 * free rather than special-casing root vs one nested level.
 *
 * The editor works over an EDIT MODEL — a `GridItem` on a cell grid — which is a
 * richer, editing-time shape than the serialized `LayoutNode`. `editModel.ts`
 * converts between the two; this module is the geometry the editor manipulates
 * (RFC §5.4). It is never persisted and never crosses into the renderer.
 */
import { GROUP, type ElementKind, type ContainerKind } from './layoutTree'

export interface Rect {
  x: number
  y: number
  w: number
  h: number
}

/** An element placed on a cell grid, editing-time shape. A container carries a
 *  nested `grid` (one level of nesting in this pass) or `tabs` children. */
export interface GridItem extends Rect {
  id: string
  element: ElementKind | ContainerKind
  /** Children of a `tabs` container, one per tab (their x/y/w/h are unused). */
  tabs?: GridItem[]
  activeTab?: number
  /** Inner grid of a `group` container. */
  grid?: GridSpec
  /** Static per-cell config (the layout node's `config`). */
  config?: Record<string, unknown>
}

export interface GridSpec {
  cols: number
  rows: number
  items: GridItem[]
  /** fr track weights, carried through the edit model so a save→reopen keeps a
   *  non-equal split (e.g. the floor seed's `[3, 2]`) instead of resetting to
   *  equal tracks. Optional: absent means equal tracks. */
  colSizes?: number[]
  rowSizes?: number[]
}

export const clamp = (n: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, n))

/** Do two rectangles overlap (share any cell)? */
export function overlaps(a: Rect, b: Rect): boolean {
  return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h
}

/** Is a rectangle fully inside a cols×rows grid? */
export function inBounds(r: Rect, cols: number, rows: number): boolean {
  return r.x >= 0 && r.y >= 0 && r.x + r.w <= cols && r.y + r.h <= rows
}

/** May `rect` be placed among `items` — in bounds and overlapping nothing
 *  (except optionally the item being moved, named by `ignoreId`)? */
export function canPlace(
  items: GridItem[],
  rect: Rect,
  cols: number,
  rows: number,
  ignoreId?: string,
): boolean {
  if (!inBounds(rect, cols, rows)) return false
  return !items.some((it) => it.id !== ignoreId && overlaps(it, rect))
}

/** The first free 1×1 cell in row-major order, or null when the grid is full. */
export function firstFreeCell(
  items: GridItem[],
  cols: number,
  rows: number,
): { x: number; y: number } | null {
  for (let y = 0; y < rows; y++) {
    for (let x = 0; x < cols; x++) {
      if (canPlace(items, { x, y, w: 1, h: 1 }, cols, rows)) return { x, y }
    }
  }
  return null
}

/* ---------------------- recursive by-id tree ops ---------------------- */

/** Find an item anywhere in the tree (root items, tab children, nested grids). */
export function findItem(arr: GridItem[], id: string): GridItem | null {
  for (const it of arr) {
    if (it.id === id) return it
    if (it.tabs) {
      const t = findItem(it.tabs, id)
      if (t) return t
    }
    if (it.grid) {
      const g = findItem(it.grid.items, id)
      if (g) return g
    }
  }
  return null
}

/** Map an item by id anywhere in the tree, returning a new array. */
export function updateItems(arr: GridItem[], id: string, fn: (i: GridItem) => GridItem): GridItem[] {
  return arr.map((it) => {
    if (it.id === id) return fn(it)
    let next = it
    if (it.tabs) next = { ...next, tabs: updateItems(it.tabs, id, fn) }
    if (it.grid) next = { ...next, grid: { ...it.grid, items: updateItems(it.grid.items, id, fn) } }
    return next
  })
}

/** Remove an item by id anywhere in the tree. When the removed id is a tab of a
 *  `tabs` container, `activeTab` is recomputed so it keeps pointing at the SAME
 *  surviving tab (by identity, not index) and can never be left out of range —
 *  a stale index would otherwise serialize verbatim and reload unclamped,
 *  persisting an out-of-range active tab (the corruption class this model
 *  exists to close). */
export function removeItemById(arr: GridItem[], id: string): GridItem[] {
  return arr
    .filter((it) => it.id !== id)
    .map((it) => {
      let next = it
      if (it.tabs) {
        const prevActive = it.activeTab ?? 0
        const prevActiveId = it.tabs[prevActive]?.id
        const tabs = removeItemById(it.tabs, id)
        // Keep the previously-selected tab selected by id; if it was the one
        // removed (or there is none), clamp to a valid index (or undefined when
        // no tabs remain).
        let activeTab: number | undefined
        if (tabs.length === 0) {
          activeTab = undefined
        } else {
          const keptIdx = prevActiveId ? tabs.findIndex((t) => t.id === prevActiveId) : -1
          activeTab = keptIdx >= 0 ? keptIdx : clamp(prevActive, 0, tabs.length - 1)
        }
        next = { ...next, tabs, activeTab }
      }
      if (it.grid) next = { ...next, grid: { ...it.grid, items: removeItemById(it.grid.items, id) } }
      return next
    })
}

/** Does `item`'s subtree contain `id`? Guards a move from dropping a container
 *  into a canvas that lives inside itself. */
export function subtreeHas(item: GridItem, id: string): boolean {
  if (item.id === id) return true
  if (item.tabs?.some((t) => subtreeHas(t, id))) return true
  if (item.grid?.items.some((g) => subtreeHas(g, id))) return true
  return false
}

/** Which element kinds can hold a nested grid — a container. `group` nests;
 *  content cells and `tabs` do not open a child grid. */
export function canNest(element: GridItem['element']): boolean {
  return element === GROUP
}
