import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { loadLayout, saveLayout, anchorOf, isRealSplit, pruneToLive } from './splitLayoutStore'

/** What a leaf pane holds:
 *  - placeholder: empty cell showing the session/terminal picker
 *  - session: a live ChatPane bound to `slot`
 *  - terminal: a TerminalPane bound to PTY session `termId` */
export type LeafKind = 'placeholder' | 'session' | 'terminal'
export type GridLeaf = { type: 'leaf'; id: string; kind: LeafKind; slot?: string; termId?: string }
/** A split node tiles its children along one axis.
 *  dir 'col' = children laid left→right (vertical dividers) = "split right".
 *  dir 'row' = children laid top→bottom (horizontal dividers) = "split down". */
export type GridSplit = { type: 'split'; id: string; dir: 'row' | 'col'; children: GridNode[]; sizes: number[] }
export type GridNode = GridLeaf | GridSplit
/** User-facing split direction; mapped to a split axis (right→col, down→row). */
export type SplitDir = 'right' | 'down'

const MIN_FRAC = 0.12 // a pane can't be shrunk below 12% of its split

const uid = () =>
  typeof crypto !== 'undefined' && crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2)

const newLeaf = (kind: LeafKind = 'placeholder', extra: Partial<GridLeaf> = {}): GridLeaf => ({
  type: 'leaf',
  id: uid(),
  kind,
  ...extra,
})

// ── immutable tree transforms ──────────────────────────────────────────────

/** Replace the leaf with id `targetId` by the node `make(leaf)` returns. */
function replaceLeaf(node: GridNode, targetId: string, make: (leaf: GridLeaf) => GridNode): GridNode {
  if (node.type === 'leaf') return node.id === targetId ? make(node) : node
  return { ...node, children: node.children.map((c) => replaceLeaf(c, targetId, make)) }
}

/** Apply `fn` to the split with id `splitId`. */
function transformSplit(node: GridNode, splitId: string, fn: (s: GridSplit) => GridSplit): GridNode {
  if (node.type === 'leaf') return node
  if (node.id === splitId) return fn(node)
  return { ...node, children: node.children.map((c) => transformSplit(c, splitId, fn)) }
}

/** Find the split that directly contains the leaf `childId` (null if it's the root leaf). */
function findParentSplit(node: GridNode, childId: string, parent: GridSplit | null = null): GridSplit | null {
  if (node.type === 'leaf') return node.id === childId ? parent : null
  for (const c of node.children) {
    const found = findParentSplit(c, childId, node)
    if (found) return found
  }
  return null
}

/** Remove leaf `id`; collapse single-child splits; renormalize sibling sizes.
 *  Returns null when the whole tree is emptied (closed the last pane). */
function removeLeaf(node: GridNode, id: string): GridNode | null {
  if (node.type === 'leaf') return node.id === id ? null : node
  const kept: GridNode[] = []
  const keptSizes: number[] = []
  node.children.forEach((c, i) => {
    const r = removeLeaf(c, id)
    if (r !== null) {
      kept.push(r)
      keptSizes.push(node.sizes[i] ?? 1 / node.children.length)
    }
  })
  if (kept.length === 0) return null
  if (kept.length === 1) return kept[0] // collapse: a split with one child becomes that child
  const sum = keptSizes.reduce((a, b) => a + b, 0) || 1
  return { ...node, children: kept, sizes: keptSizes.map((s) => s / sum) }
}

/** All leaves, depth-first. */
function leavesOf(node: GridNode | null): GridLeaf[] {
  if (!node) return []
  if (node.type === 'leaf') return [node]
  return node.children.flatMap(leavesOf)
}

/**
 * useSessionGrid — recursive split-tree state for the native "terminal split" mode.
 *
 * The grid is a binary/n-ary tree of split nodes and leaf panes (tmux/Warp model):
 * split the focused pane right (vertical divider) or down (horizontal divider),
 * nest arbitrarily, drag the per-split dividers to resize, and close a pane to let
 * its siblings reflow. There is NO base session — every leaf is equal and chosen by
 * the user (placeholder → pick a session / fork / terminal).
 *
 * Layouts persist per ANCHOR slot (splitLayoutStore), so a split survives navigation
 * and refresh: `useSessionGrid(anchorSlot)` restores that slot's saved layout on
 * entry, or seeds a fresh [anchor | placeholder] when there's none. A split is only
 * persisted once it tiles >= 2 sessions; closing back down to one dissolves it. This
 * is keyed per slot (NOT one global blob), so ⌘D restores the split of the session
 * you're looking at — never an unrelated saved layout.
 */
export function useSessionGrid(anchorSlot?: string | null) {
  // Restore this slot's persisted split (if any); otherwise start empty and let
  // SessionGridView seed [anchor | placeholder]. The tree is the live working copy;
  // the persist effect below mirrors every change back to splitLayoutStore.
  const [tree, setTree] = useState<GridNode | null>(() => loadLayout(anchorSlot ?? null))
  const [focusedId, setFocusedId] = useState<string | null>(null)

  // Persist the tree under its anchor on every change. Debounced 300ms so a divider
  // drag (setTree per mousemove frame) coalesces into one write. The DISSOLVE path
  // (tree no longer a real >=2-session split) writes SYNCHRONOUSLY and bypasses the
  // debounce: closing down to one pane unmounts the view, and a debounced write
  // would be cancelled by cleanup, leaving the just-closed layout restorable.
  const prevAnchorRef = useRef<string | null>(anchorOf(tree))
  // Latest tree, mirrored into a ref so the unmount-only flush below can persist
  // the final layout even when the last change is still inside the 300ms debounce
  // window at unmount.
  const treeRef = useRef(tree)
  useEffect(() => { treeRef.current = tree }, [tree])
  useEffect(() => {
    if (!isRealSplit(tree)) {
      saveLayout(prevAnchorRef.current, tree) // deletes the entry when < 2 sessions
      prevAnchorRef.current = anchorOf(tree)
      return
    }
    const id = setTimeout(() => {
      saveLayout(prevAnchorRef.current, tree)
      prevAnchorRef.current = anchorOf(tree)
    }, 300)
    return () => clearTimeout(id)
  }, [tree])
  // Unmount flush: the dissolve path saves synchronously, but a still-a-real-split
  // exit within the debounce window (clicking a sidebar session -> onSelectSlot ->
  // setSplitMode(false), or navigating away) clears the pending timer above and
  // would otherwise lose the user's latest layout edit on re-entry. Persist the
  // last real-split tree synchronously on unmount so EVERY exit is durable.
  useEffect(() => () => {
    const t = treeRef.current
    if (isRealSplit(t)) {
      saveLayout(prevAnchorRef.current, t)
      prevAnchorRef.current = anchorOf(t)
    }
  }, [])

  /** Seed the grid when entering split mode. With a current session, "split it
   *  in place": that session becomes the left pane and a fresh placeholder opens
   *  beside it (Warp/tmux ⌘D semantics). With no current session, seed a single
   *  empty placeholder. Called only when nothing was restored from persistence. */
  const seedFromSession = useCallback((slot: string | null) => {
    if (!slot) {
      const leaf = newLeaf()
      setTree(leaf)
      setFocusedId(leaf.id)
      return
    }
    const current = newLeaf('session', { slot })
    const blank = newLeaf()
    setTree({ type: 'split', id: uid(), dir: 'col', children: [current, blank], sizes: [0.5, 0.5] })
    setFocusedId(blank.id)
  }, [])

  /** Split the leaf `leafId` toward `dir`, adding a fresh placeholder beside it.
   *  If the leaf's parent split already runs along that axis, the new pane is
   *  inserted as a sibling (flattened, tmux-style) instead of nesting deeper. */
  const splitLeaf = useCallback((leafId: string, dir: SplitDir) => {
    const axis: 'row' | 'col' = dir === 'right' ? 'col' : 'row'
    const fresh = newLeaf()
    setTree((cur) => {
      if (!cur) return cur
      const parent = findParentSplit(cur, leafId)
      if (parent && parent.dir === axis) {
        return transformSplit(cur, parent.id, (s) => {
          const idx = s.children.findIndex((c) => c.type === 'leaf' && c.id === leafId)
          if (idx < 0) return s
          const children = [...s.children]
          children.splice(idx + 1, 0, fresh)
          const sizes = [...s.sizes]
          const half = (sizes[idx] ?? 1 / s.children.length) / 2
          sizes[idx] = half
          sizes.splice(idx + 1, 0, half)
          return { ...s, children, sizes }
        })
      }
      // Different axis (or root leaf): wrap the leaf in a new split.
      return replaceLeaf(cur, leafId, (leaf) => ({
        type: 'split',
        id: uid(),
        dir: axis,
        children: [leaf, fresh],
        sizes: [0.5, 0.5],
      }))
    })
    setFocusedId(fresh.id)
  }, [])

  /** Close the leaf `leafId`; siblings reflow, single-child splits collapse.
   *  Closing the last pane empties the tree (the view treats null as "exit"). */
  const closeLeaf = useCallback((leafId: string) => {
    setTree((cur) => (cur ? removeLeaf(cur, leafId) : cur))
    setFocusedId((cur) => (cur === leafId ? null : cur))
  }, [])

  /** Fill / change what a leaf holds (placeholder → session or terminal, etc). */
  const fillLeaf = useCallback((leafId: string, patch: Partial<Omit<GridLeaf, 'type' | 'id'>>) => {
    setTree((cur) => (cur ? replaceLeaf(cur, leafId, (leaf) => ({ ...leaf, ...patch })) : cur))
  }, [])

  /** Resize a split: shift `deltaFrac` of the split's extent from child `index+1`
   *  to child `index` (clamped so neither drops below MIN_FRAC). */
  const resize = useCallback((splitId: string, index: number, deltaFrac: number) => {
    setTree((cur) =>
      cur
        ? transformSplit(cur, splitId, (s) => {
            const sizes = [...s.sizes]
            let d = deltaFrac
            d = Math.min(d, (sizes[index + 1] ?? 0) - MIN_FRAC)
            d = Math.max(d, -((sizes[index] ?? 0) - MIN_FRAC))
            sizes[index] = (sizes[index] ?? 0) + d
            sizes[index + 1] = (sizes[index + 1] ?? 0) - d
            return { ...s, sizes }
          })
        : cur,
    )
  }, [])

  /** Heal the restored tree against the live session list: drop panes whose slot
   *  no longer exists (deleted/archived), collapse, renormalize. No-op when nothing
   *  changed. Called once by the view after the slot list loads. */
  const pruneAgainst = useCallback((liveSlotKeys: string[]) => {
    const live = new Set(liveSlotKeys)
    setTree((cur) => pruneToLive(cur, live))
  }, [])

  const leaves = useMemo(() => leavesOf(tree), [tree])
  /** Session slot keys currently pinned in a pane (to exclude from pickers). */
  const occupiedSlots = useMemo(
    () => leaves.filter((l) => l.kind === 'session' && l.slot).map((l) => l.slot as string),
    [leaves],
  )
  const paneCount = leaves.filter((l) => l.kind !== 'placeholder').length

  // Keep focus on a still-existing leaf: if the focused leaf was closed, fall
  // back to the first remaining leaf (or null when the tree is empty).
  useEffect(() => {
    setFocusedId((cur) => (cur && leaves.some((l) => l.id === cur) ? cur : leaves[0]?.id ?? null))
  }, [leaves])

  return {
    tree,
    focusedId,
    isEmpty: tree === null,
    leaves,
    occupiedSlots,
    paneCount,
    seedFromSession,
    splitLeaf,
    closeLeaf,
    fillLeaf,
    resize,
    pruneAgainst,
    setFocused: setFocusedId,
  }
}
