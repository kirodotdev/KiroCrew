import type { GridNode } from './useSessionGrid'
import { safeSetItem } from '../utils/safeStorage'

/**
 * splitLayoutStore — persistence for native split layouts, keyed by ANCHOR slot.
 *
 * A split is just a co-render of shared session slots; the slot is the source of
 * truth and is always openable as a single chat. The layout (which slots tile
 * together, their nesting + sizes) persists here so a split survives navigation /
 * refresh, and a member slot opened on its own shows a "return to split" hint.
 *
 * Keyed by anchor = the FIRST session leaf's slot of the tree (the session you
 * ⌘D'd from). This is per-slot, NOT one global blob — a single global blob would
 * replay an unrelated saved layout when entering a split. A layout only
 * counts as a real split when it holds >= 2 session panes; anything less dissolves.
 */

const KEY = 'mc-split-layouts'

type LayoutMap = Record<string, GridNode>

function readAll(): LayoutMap {
  try {
    const raw = localStorage.getItem(KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw)
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? (parsed as LayoutMap) : {}
  } catch {
    return {}
  }
}

function writeAll(map: LayoutMap): void {
  try {
    if (Object.keys(map).length === 0) localStorage.removeItem(KEY)
    else safeSetItem(KEY, JSON.stringify(map))
  } catch (e) {
    // The split still renders from memory after a failed write, so a quota or
    // serialization failure has no symptom at all until the layout fails to come
    // back after a refresh — by which time the exception is long gone.
    // eslint-disable-next-line no-console -- only trace of a swallowed persist
    console.warn('[splitLayoutStore] persist failed', e)
  }
}

/** Depth-first list of the session-slot keys held by a tree (placeholders excluded). */
export function sessionSlots(node: GridNode | null): string[] {
  if (!node) return []
  if (node.type === 'leaf') return node.kind === 'session' && node.slot ? [node.slot] : []
  return node.children.flatMap(sessionSlots)
}

/** Anchor = first session leaf's slot, depth-first (null if the tree has none). */
export function anchorOf(node: GridNode | null): string | null {
  return sessionSlots(node)[0] ?? null
}

/** A tree is a real, persistable split only when it tiles >= 2 distinct sessions. */
export function isRealSplit(node: GridNode | null): boolean {
  return new Set(sessionSlots(node)).size >= 2
}

/** Load the split layout anchored at `anchor` (the layout that slot owns). */
export function loadLayout(anchor: string | null): GridNode | null {
  if (!anchor) return null
  return readAll()[anchor] ?? null
}

/**
 * Persist `tree` under its anchor. Removes the `prevAnchor` entry when the anchor
 * moved (e.g. the first pane was closed) or when the tree no longer qualifies as a
 * real split (closed down to a single session → the split dissolves).
 */
export function saveLayout(prevAnchor: string | null, tree: GridNode | null): void {
  const map = readAll()
  if (prevAnchor) delete map[prevAnchor]
  const anchor = anchorOf(tree)
  if (anchor && isRealSplit(tree)) map[anchor] = tree as GridNode
  else if (anchor) delete map[anchor]
  writeAll(map)
}

/** The anchor of the first persisted layout that tiles `slot` as a session pane. */
export function anchorForSlot(slot: string | null): string | null {
  if (!slot) return null
  const map = readAll()
  for (const [anchor, tree] of Object.entries(map)) {
    if (sessionSlots(tree).includes(slot)) return anchor
  }
  return null
}

/**
 * Drop session leaves whose slot is not in `live`, collapse single-child splits,
 * renormalize sizes. Pure (no persistence). Returns null when nothing meaningful
 * remains. Used on entry to heal layouts that reference deleted/archived sessions.
 */
export function pruneToLive(node: GridNode | null, live: Set<string>): GridNode | null {
  if (!node) return null
  if (node.type === 'leaf') {
    if (node.kind === 'session' && node.slot && !live.has(node.slot)) return null
    return node
  }
  const kept: GridNode[] = []
  const keptSizes: number[] = []
  node.children.forEach((c, i) => {
    const r = pruneToLive(c, live)
    if (r) {
      kept.push(r)
      keptSizes.push(node.sizes[i] ?? 1 / node.children.length)
    }
  })
  if (kept.length === 0) return null
  if (kept.length === 1) return kept[0]
  const sum = keptSizes.reduce((a, b) => a + b, 0) || 1
  return { ...node, children: kept, sizes: keptSizes.map((s) => s / sum) }
}
