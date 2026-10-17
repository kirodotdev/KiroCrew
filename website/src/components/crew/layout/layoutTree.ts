/**
 * The layout tree — placement as data (RFC §5.1, §5.2, §5.5).
 *
 * A `LayoutTree` is the serializable config that describes WHERE cells sit.
 * Cells connect by placement, not by wiring, via the runtime scope (whose
 * pure-data value type lives in `scope.ts`). This module is pure data + guards,
 * React-free, so every rule is unit-testable.
 *
 * A node is one of:
 *  - `cell`   — a placed element (`ElementKind`) with static `config`.
 *  - `group`  — children laid out in a row or column, SHARING the parent scope
 *               (siblings see each other's selection). `sizes` are `fr` weights.
 *  - `tabs`   — children as tabs over one active child, sharing the parent scope.
 *  - `grid`   — an absolute cell grid, cols×rows, each child placed at x/y/w/h;
 *               the shape that stores geometry losslessly (RFC §5.1, decision 4).
 *
 * The stored value is versioned; an unknown version, an unknown node kind, or
 * an element kind that no longer exists drops that node and falls through,
 * never crashes (RFC §5.5).
 */

/** Every element the palette can place. Phase 1 ships a tiny set; this mirrors
 *  the existing panel-view vocabulary (`ViewKind`) so the palette is components
 *  the dashboard already has (RFC §2.1, §5.2). */
export type ElementKind =
  | 'chat' // the chat pane — transcript + composer, the center of gravity
  | 'sidePanel' // the whole session side panel (tab strip) as one cell
  // Individual views, each placeable as its own cell (wrap real components):
  | 'files' // FolderPanel — the file browser
  | 'git' // ActivityViewer view="git"
  | 'changes' // ActivityViewer view="changes"
  | 'subagents' // ActivityViewer view="subagents"
  | 'terminal' // CliPanel
  | 'notes' // CrewNotesTab — the crewmate's standing notes
  | 'workLog' // the crewmate's work-log / summary body

/** A placed element — the only leaf. Carries an `ElementKind` + static config. */
export interface CellNode {
  kind: 'cell'
  id: string
  element: ElementKind
  config?: Record<string, unknown>
}

/** Children laid out in a row or column, sharing the parent scope. `sizes` are
 *  `fr` weights. */
export interface GroupNode {
  kind: 'group'
  id: string
  dir: 'row' | 'col'
  children: LayoutNode[]
  sizes?: number[]
}

/** Children as tabs over one active child, sharing the parent scope. */
export interface TabsNode {
  kind: 'tabs'
  id: string
  children: LayoutNode[]
  active?: number
}

/** An absolute cell grid — cols×rows with each child placed at x/y/w/h. This is
 *  what the editor produces and what preserves a 2×3 arrangement verbatim across
 *  save/reopen (no collapse to a group). `colSizes`/`rowSizes` optionally carry
 *  the fr track weights the dividers set. */
export interface GridNode {
  kind: 'grid'
  id: string
  cols: number
  rows: number
  children: PlacedNode[]
  colSizes?: number[]
  rowSizes?: number[]
}

/** A node in the layout tree — one of the four named arms above. */
export type LayoutNode = CellNode | GroupNode | TabsNode | GridNode

/** A child of a `grid` node: any node plus its cell rectangle. */
export type PlacedNode = { node: LayoutNode; x: number; y: number; w: number; h: number }

/** The structural role of a layout node — how it arranges, not what it holds.
 *  Derived from the `LayoutNode` union so it can never drift from the actual
 *  arms. `cell` is the only leaf (it carries an `ElementKind`); the rest are
 *  containers. Distinct axis from `ElementKind`, which names cell CONTENT. */
export type NodeKind = LayoutNode['kind']

/** The container-only subset used as edit-model sentinels in `GridItem.element`
 *  (`grid.ts`), where a real `ElementKind` marks content and one of these marks
 *  a container. `grid` is excluded on purpose: the edit model carries a group's
 *  grid in its nested `grid` field, so an item is only ever tagged `group` or
 *  `tabs`, never `grid`. */
export type ContainerKind = Exclude<NodeKind, 'cell' | 'grid'>

/** The two `ContainerKind` values as named consts, so the edit model
 *  (`editModel.ts`, `grid.ts`) compares against a typed reference instead of a
 *  bare string literal — a rename is then a compile error, not a silent miss. */
export const GROUP: ContainerKind = 'group'
export const TABS: ContainerKind = 'tabs'

/** The current stored-envelope version. Bump when the shape changes
 *  incompatibly; `parseLayout` drops an unknown version to the floor. */
export const LAYOUT_VERSION = 1

/** A whole layout, wrapped in a versioned envelope for forward-compatible load. */
export interface LayoutTree {
  version: number
  root: LayoutNode
}

/** Element kinds a container node may hold — used to reject a malformed tree. */
const KNOWN_ELEMENTS: ReadonlySet<ElementKind> = new Set<ElementKind>([
  'chat',
  'sidePanel',
  'files',
  'git',
  'changes',
  'subagents',
  'terminal',
  'notes',
  'workLog',
])

export function isKnownElement(name: string): name is ElementKind {
  return KNOWN_ELEMENTS.has(name as ElementKind)
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

export type ParseResult = { ok: true; tree: LayoutTree } | { ok: false; error: string }

// ── normalize helpers ──────────────────────────────────────────────────────
// Small pure helpers shared by the per-kind normalizers below. Each guards ONE
// concern so the normalizers read as "what this kind needs", not bookkeeping.

/** A node's id, or a freshly minted one when the source omits, blanks, or
 *  DUPLICATES it. `seen` tracks every id already handed out in this parse so a
 *  duplicate stored id (which `updateItems`/`removeItemById` would then match on
 *  more than one pane, mutating or deleting an unrelated one) is replaced with a
 *  fresh unique id rather than preserved. Minting loops until it produces one
 *  not already in `seen`, so a fresh id can never collide either. */
function resolveId(raw: Record<string, unknown>, newId: () => string, seen: Set<string>): string {
  return typeof raw.id === 'string' && raw.id && !seen.has(raw.id) ? raw.id : mintUnique(newId, seen)
}

/** Mint a fresh id guaranteed not already in `seen`. */
function mintUnique(newId: () => string, seen: Set<string>): string {
  let id = newId()
  while (seen.has(id)) id = newId()
  return id
}

interface NormalizedChildren {
  children: LayoutNode[]
  retainedIndices: number[]
  sourceLength?: number
}

/** Normalize a container's `children`, dropping any child that fails to
 *  normalize while retaining its original index for positional metadata. */
function normalizeChildren(
  raw: unknown,
  newId: () => string,
  path: string,
  seen: Set<string>,
): NormalizedChildren {
  if (!Array.isArray(raw)) return { children: [], retainedIndices: [] }
  const children: LayoutNode[] = []
  const retainedIndices: number[] = []
  raw.forEach((child, index) => {
    const normalized = normalizeNode(child, newId, `${path}.children[${index}]`, seen)
    if (normalized) {
      children.push(normalized)
      retainedIndices.push(index)
    }
  })
  return { children, retainedIndices, sourceLength: raw.length }
}

/** Return `value` only if it is an array of FINITE numbers, else undefined — so
 *  an optional numeric-array field (`sizes`, `colSizes`, `rowSizes`) is either a
 *  clean array or absent, never a half-valid one (NaN/Infinity are rejected). */
function numberArray(value: unknown): number[] | undefined {
  return Array.isArray(value) && value.every((n) => typeof n === 'number' && Number.isFinite(n))
    ? (value as number[])
    : undefined
}

/** Clamp a grid dimension to a positive INTEGER count, defaulting to 1. */
function gridDim(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 1 ? Math.floor(value) : 1
}

// ── per-kind normalizers ─────────────────────────────────────────────────────
// One function per node kind, each returning the node or null to DROP it. The
// dispatch table below routes on `kind`; an unknown kind has no entry and drops.

function normalizeCell(
  raw: Record<string, unknown>,
  id: string,
): LayoutNode | null {
  if (typeof raw.element !== 'string' || !isKnownElement(raw.element)) return null // drop unknown
  const node: LayoutNode = { kind: 'cell', id, element: raw.element }
  if (isPlainObject(raw.config)) node.config = raw.config
  return node
}

function normalizeGroup(
  raw: Record<string, unknown>,
  id: string,
  newId: () => string,
  path: string,
  seen: Set<string>,
): LayoutNode | null {
  if (raw.dir !== 'row' && raw.dir !== 'col') return null
  const normalized = normalizeChildren(raw.children, newId, path, seen)
  const node: LayoutNode = { kind: 'group', id, dir: raw.dir, children: normalized.children }
  const sizes = numberArray(raw.sizes)
  if (sizes && sizes.length === normalized.sourceLength) {
    node.sizes = normalized.retainedIndices.map((index) => sizes[index])
  }
  return node
}

function normalizeTabs(
  raw: Record<string, unknown>,
  id: string,
  newId: () => string,
  path: string,
  seen: Set<string>,
): LayoutNode {
  const normalized = normalizeChildren(raw.children, newId, path, seen)
  const node: LayoutNode = { kind: 'tabs', id, children: normalized.children }
  // Resolve `active` against the original children, then map that child to its
  // surviving position. If normalization dropped the selected child, omit the
  // selection rather than silently selecting a different tab.
  if (normalized.sourceLength && typeof raw.active === 'number' && Number.isFinite(raw.active)) {
    const sourceActive = Math.min(Math.max(0, Math.floor(raw.active)), normalized.sourceLength - 1)
    const active = normalized.retainedIndices.indexOf(sourceActive)
    if (active >= 0) node.active = active
  }
  return node
}

function normalizeGrid(
  raw: Record<string, unknown>,
  id: string,
  newId: () => string,
  path: string,
  seen: Set<string>,
): LayoutNode {
  const children = Array.isArray(raw.children)
    ? raw.children
        .map((c, i) => normalizePlaced(c, newId, `${path}.children[${i}]`, seen))
        .filter((c): c is PlacedNode => c !== null)
    : []
  const node: LayoutNode = { kind: 'grid', id, cols: gridDim(raw.cols), rows: gridDim(raw.rows), children }
  const colSizes = numberArray(raw.colSizes)
  const rowSizes = numberArray(raw.rowSizes)
  if (colSizes) node.colSizes = colSizes
  if (rowSizes) node.rowSizes = rowSizes
  return node
}

/**
 * Validate + normalize one raw node. Returns the node, or null to DROP it (an
 * unknown kind, unknown element, or malformed shape falls through rather than
 * crashing, per RFC §5.5). `newId` mints a fresh id when the source omits one.
 */
function normalizeNode(
  raw: unknown,
  newId: () => string,
  path: string,
  seen: Set<string>,
): LayoutNode | null {
  if (!isPlainObject(raw)) return null
  const id = resolveId(raw, newId, seen)
  seen.add(id)
  let node: LayoutNode | null
  switch (raw.kind) {
    case 'cell':
      node = normalizeCell(raw, id)
      break
    case 'group':
      node = normalizeGroup(raw, id, newId, path, seen)
      break
    case 'tabs':
      node = normalizeTabs(raw, id, newId, path, seen)
      break
    case 'grid':
      node = normalizeGrid(raw, id, newId, path, seen)
      break
    default:
      node = null // unknown node kind → drop
  }
  if (!node) seen.delete(id)
  return node
}

/** Validate a placed child of a grid: a rect + a nested node. Drops if the
 *  nested node is malformed; a missing OR non-finite rect field defaults
 *  (x/y=0, w/h=1). Coordinates are floored to non-negative integers and
 *  dimensions to integers ≥ 1, because the placement helpers (`overlaps`,
 *  `firstFreeCell`, `canPlace`) assume finite integer rects the way `gridDim`
 *  already floors cols/rows — an `Infinity`, `NaN`, fractional, negative or
 *  zero-width rect slipping through corrupts overlap tests and free-cell scans. */
function normalizePlaced(
  raw: unknown,
  newId: () => string,
  path: string,
  seen: Set<string>,
): PlacedNode | null {
  if (!isPlainObject(raw)) return null
  const node = normalizeNode(raw.node, newId, `${path}.node`, seen)
  if (!node) return null
  // Non-negative integer coordinate, else the default.
  const coord = (v: unknown, d: number) =>
    typeof v === 'number' && Number.isFinite(v) ? Math.max(0, Math.floor(v)) : d
  // Positive integer dimension (≥ 1), else the default.
  const dim = (v: unknown, d: number) =>
    typeof v === 'number' && Number.isFinite(v) && v >= 1 ? Math.floor(v) : d
  return { node, x: coord(raw.x, 0), y: coord(raw.y, 0), w: dim(raw.w, 1), h: dim(raw.h, 1) }
}

/** Serialize a tree to stable pretty JSON (fixed key order, so diffs read cleanly). */
export function serializeLayout(t: LayoutTree): string {
  return JSON.stringify({ version: t.version, root: t.root }, null, 2)
}

/**
 * Parse stored JSON back into a tree. Guards: JSON must parse; the top level
 * must be an object; the version must be the current one (an unknown version
 * is refused so the caller falls through to the floor); the root must
 * normalize to a node. Pure and testable — `newId` is injected.
 */
export function parseLayout(raw: string, newId: () => string): ParseResult {
  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch (e) {
    return { ok: false, error: `Invalid JSON: ${(e as Error).message}` }
  }
  if (!isPlainObject(parsed)) return { ok: false, error: 'Top level must be a JSON object' }
  if (parsed.version !== LAYOUT_VERSION)
    return { ok: false, error: `Unknown layout version ${String(parsed.version)}` }
  const root = normalizeNode(parsed.root, newId, 'root', new Set<string>())
  if (!root) return { ok: false, error: 'root did not normalize to a valid node' }
  return { ok: true, tree: { version: LAYOUT_VERSION, root } }
}
