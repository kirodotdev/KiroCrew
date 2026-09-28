/**
 * Convert between the serialized `LayoutTree` and the editor's cell-grid
 * `GridItem` model (RFC §5.4).
 *
 * The top level round-trips through a `grid` LayoutNode that stores cols/rows
 * and each cell's x/y/w/h VERBATIM, so a 2×3 arrangement (or any partial fill)
 * survives save → reopen without collapsing. The editor geometry IS the stored
 * geometry now — that is the whole point of the `grid` node (RFC decision 4).
 *
 * A legacy `group` root (the pre-grid seed) still converts IN, so an older
 * stored layout opens without crashing; it is written back OUT as a `grid`
 * (the one-time rebuild, RFC §5.5).
 */
import type { GridItem, GridSpec } from './grid'
import {
  LAYOUT_VERSION,
  GROUP,
  TABS,
  type ElementKind,
  type LayoutNode,
  type CellNode,
  type GroupNode,
  type TabsNode,
  type GridNode,
  type PlacedNode,
  type LayoutTree,
} from './layoutTree'

// A process-unique id. The `counter` restarts at 0 on every page load, so on its
// own it collides across reloads; a bare `Date.now()` suffix repeats every ~46.7 s
// (mod 46656 over 3 base-36 chars), so two drops in that window can also collide.
// Seeding `counter` from `Date.now()` once per load makes the counter itself
// monotonic and load-distinct, and the suffix only widens the space — so the
// stream is unique within a load, and `resolveId`'s seen-set is the backstop for
// a stored id that predates this load. `removeItemById` filters EVERY match, so a
// single collision silently deletes an unrelated pane; this closes the mint side.
let counter = Date.now()
const genId = () => `n${(counter++).toString(36)}${Date.now().toString(36).slice(-3)}`

/* --------------------------- LayoutTree → edit model --------------------------- */

/** Convert a LayoutTree into the editor's top-level GridSpec. */
export function toEditModel(tree: LayoutTree): GridSpec {
  const root = tree.root
  if (root.kind === 'grid') {
    return withGridSizes({ cols: root.cols, rows: root.rows, items: placeChildren(root.children) }, root)
  }
  if (root.kind === 'group') {
    // Legacy group root → a single row/col grid (one cell per child), carrying
    // the group's fr track weights so the split is not lost on rebuild.
    const horizontal = root.dir === 'row'
    const spec: GridSpec = {
      // An empty legacy group would give a 0-length dimension that `firstFreeCell`
      // can never satisfy and `gridDim` silently rewrites to 1 on the next parse
      // (a save→reopen geometry change); floor both to ≥ 1.
      cols: horizontal ? Math.max(1, root.children.length) : 1,
      rows: horizontal ? 1 : Math.max(1, root.children.length),
      items: root.children.map((child, i) => ({
        ...nodeToItem(child),
        x: horizontal ? i : 0,
        y: horizontal ? 0 : i,
        w: 1,
        h: 1,
      })),
    }
    applyGroupSizes(spec, root)
    return spec
  }
  // A bare cell or tabs root → a 1×1 grid holding it.
  return { cols: 1, rows: 1, items: [{ ...nodeToItem(root), x: 0, y: 0, w: 1, h: 1 }] }
}

/** Convert one LayoutNode into its editing-time GridItem. Dispatches on kind to
 *  a per-kind helper; the `switch` narrows so each helper takes a concrete node
 *  type with no cast. Placed at 1×1 by default — the caller overlays the rect. */
function nodeToItem(node: LayoutNode): GridItem {
  switch (node.kind) {
    case 'cell':
      return cellToItem(node)
    case 'tabs':
      return tabsToItem(node)
    case 'grid':
      return gridToItem(node)
    case 'group':
      return groupToItem(node) // legacy inbound → nested grid
  }
}

function cellToItem(node: CellNode): GridItem {
  return { id: node.id || genId(), element: node.element, x: 0, y: 0, w: 1, h: 1, config: node.config }
}

function tabsToItem(node: TabsNode): GridItem {
  return {
    id: node.id || genId(),
    element: TABS,
    x: 0,
    y: 0,
    w: 1,
    h: 1,
    activeTab: node.active,
    tabs: node.children.map(nodeToItem),
  }
}

function gridToItem(node: GridNode): GridItem {
  return {
    id: node.id || genId(),
    element: GROUP,
    x: 0,
    y: 0,
    w: 1,
    h: 1,
    grid: withGridSizes({ cols: node.cols, rows: node.rows, items: placeChildren(node.children) }, node),
  }
}

/** A legacy `group` root/child → a one-row (or one-column) nested grid, one cell
 *  per child. Kept so a pre-grid stored layout still opens (RFC §5.5). A group's
 *  `fr` track weights (`GroupNode.sizes`) map onto the grid's `colSizes` (row
 *  group) or `rowSizes` (col group) so the split survives the rebuild instead of
 *  resetting to equal tracks. */
function groupToItem(node: GroupNode): GridItem {
  const horizontal = node.dir === 'row'
  const grid: GridSpec = {
    // Floor both dimensions to ≥ 1 so an empty legacy group does not persist a
    // 0-length dimension (see the same guard in `toEditModel`).
    cols: horizontal ? Math.max(1, node.children.length) : 1,
    rows: horizontal ? 1 : Math.max(1, node.children.length),
    items: node.children.map((c, i) => ({
      ...nodeToItem(c),
      x: horizontal ? i : 0,
      y: horizontal ? 0 : i,
      w: 1,
      h: 1,
    })),
  }
  applyGroupSizes(grid, node)
  return { id: node.id || genId(), element: GROUP, x: 0, y: 0, w: 1, h: 1, grid }
}

/** Convert a grid's placed children into GridItems, carrying each rect over. */
function placeChildren(children: PlacedNode[]): GridItem[] {
  return children.map((p) => ({ ...nodeToItem(p.node), x: p.x, y: p.y, w: p.w, h: p.h }))
}

/** Copy a grid node's `colSizes`/`rowSizes` onto the edit-model spec, only when
 *  present, so track weights survive `toEditModel`→`toTree` instead of resetting
 *  to equal tracks (the silent-geometry-loss class this model exists to close). */
function withGridSizes(spec: GridSpec, node: GridNode): GridSpec {
  if (node.colSizes) spec.colSizes = node.colSizes
  if (node.rowSizes) spec.rowSizes = node.rowSizes
  return spec
}

/** Map a legacy `group`'s `fr` track weights onto the rebuilt grid spec: a `row`
 *  group lays its children across columns (→ `colSizes`), a `col` group down
 *  rows (→ `rowSizes`). One weight per child, which matches the one-cell-per-
 *  child grid, so the split survives the group→grid rebuild instead of
 *  resetting to equal tracks. */
function applyGroupSizes(spec: GridSpec, node: GroupNode): void {
  if (!node.sizes) return
  if (node.dir === 'row') spec.colSizes = node.sizes
  else spec.rowSizes = node.sizes
}

/* --------------------------- edit model → LayoutTree --------------------------- */

/** Convert the editor's top-level GridSpec back into a serializable LayoutTree,
 *  as a geometry-preserving `grid` node. */
export function toTree(spec: GridSpec): LayoutTree {
  return { version: LAYOUT_VERSION, root: specToGrid(spec, 'root') }
}

function specToGrid(spec: GridSpec, id: string): LayoutNode {
  const node: GridNode = {
    kind: 'grid',
    id,
    cols: spec.cols,
    rows: spec.rows,
    children: spec.items.map<PlacedNode>((it) => ({
      node: itemToNode(it),
      x: it.x,
      y: it.y,
      w: it.w,
      h: it.h,
    })),
  }
  if (spec.colSizes) node.colSizes = spec.colSizes
  if (spec.rowSizes) node.rowSizes = spec.rowSizes
  return node
}

/** Convert one editing-time GridItem back into a LayoutNode. The container tag
 *  lives in `item.element` (a `GROUP`/`TABS` sentinel) rather than a `kind`
 *  field, so dispatch reads that: a group with a nested grid → a `grid` node,
 *  `TABS` → a `tabs` node, anything else → a content `cell`. */
function itemToNode(item: GridItem): LayoutNode {
  if (item.element === GROUP && item.grid) return specToGrid(item.grid, item.id)
  if (item.element === TABS) return tabsItemToNode(item)
  return cellItemToNode(item)
}

function tabsItemToNode(item: GridItem): TabsNode {
  return { kind: 'tabs', id: item.id, children: (item.tabs ?? []).map(itemToNode), active: item.activeTab }
}

function cellItemToNode(item: GridItem): CellNode {
  const node: CellNode = { kind: 'cell', id: item.id, element: item.element as ElementKind }
  if (item.config) node.config = item.config
  return node
}

/** Mint a fresh GridItem for a palette drop. */
export function newItem(
  element: GridItem['element'],
  rect: { x: number; y: number; w: number; h: number },
): GridItem {
  const item: GridItem = { id: genId(), element, ...rect }
  if (element === GROUP) item.grid = { cols: 2, rows: 1, items: [] }
  if (element === TABS) {
    item.tabs = []
    item.activeTab = 0
  }
  return item
}

/** Fold a child into a `tabs` container, selecting the new tab. */
export function addChildToTabs(target: GridItem, child: GridItem): GridItem {
  const asTab = { ...child, x: 0, y: 0, w: 1, h: 1 }
  const tabs = [...(target.tabs ?? []), asTab]
  return { ...target, tabs, activeTab: tabs.length - 1 }
}
