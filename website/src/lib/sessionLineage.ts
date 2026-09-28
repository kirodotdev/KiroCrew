/**
 * Session lineage — who opened whom, as a pure function of one payload.
 *
 * A session opened by another session through `session_create` carries the creator
 * its own crew log names. Two views nest on that edge: the System page's Sessions
 * table and the chat sidebar's conductor lane. They receive DIFFERENT payloads — the
 * table gets `dashboard:`-prefixed session keys from `/api/sessions/memory`, the
 * sidebar gets bare slot keys off the slots broadcast — so this module is written
 * against the one thing both payloads agree on: a row has a `key`, and its `parent`
 * names the creator's row IN THAT SAME PAYLOAD.
 *
 * That is why `nestsUnder` lives here rather than in either page. Two copies would
 * let the two views nest the same gateway differently, and a reader comparing them
 * would have no way to tell which one was right.
 *
 * Everything here is total: no throw, no I/O, no React. A payload this module cannot
 * make sense of yields a flat list of roots, never an exception — a sidebar that does
 * not nest is a far better failure than a sidebar that does not paint.
 */

/**
 * The least a row must carry to be placed. Deliberately structural, so the System
 * page's `SessionPayloadRow` and the sidebar's `Slot` both satisfy it without either
 * one importing the other's type.
 */
export interface LineageRow {
  key: string
  /**
   * The creator, or null/absent when nobody opened this session.
   *
   * `slot` is the child's own citation and survives everything — it is a fact from
   * the child's crew log. `key` is the creator's row key in this payload, and is null
   * when the creator is not running or the records formed a cycle. So a row can cite
   * a creator it cannot nest under, which is the orphan case.
   */
  parent?: { slot?: string; key?: string | null } | null
}

/** The tree, flattened into the two lookups a renderer actually walks. */
export interface Lineage {
  /** Top-level row keys, in the order they arrived. */
  roots: string[]
  /** Parent key -> its child keys, in the order they arrived. */
  children: Map<string, string[]>
  /** Child key -> the parent it was PLACED under (absent for a root). */
  parentOf: Map<string, string>
  /** Row key -> its depth, 0 for a root. */
  depth: Map<string, number>
}

/**
 * How a caller names rows in the tree, when its payload's `key` is not enough.
 *
 * The sidebar's list mixes LOCAL rows with rows federated from a peer gateway, and the
 * two do not share a slot-key namespace: a deterministic member key can be
 * byte-identical on both. Keyed by raw `key`, one of the pair silently replaces the
 * other -- a row vanishes and its twin renders twice. So that caller supplies
 * origin-qualified identities instead.
 *
 * `parentIdentityOf` exists for the same reason and is not derivable from
 * `identityOf`: a row's `parent.key` is a bare slot key in the key space of ITS OWN
 * gateway, so resolving it means finding the row with that key AND the same origin.
 * Composing the qualified form here instead would duplicate a format the server
 * already owns.
 *
 * Both default to the raw payload key, which is what the System page's single-origin
 * table wants.
 */
export interface LineageKeying<R> {
  /** The row's key in the tree. Defaults to `row.key`. */
  identityOf?: (row: R) => string
  /** The tree key of this row's creator, or null. Defaults to `row.parent?.key`. */
  parentIdentityOf?: (row: R) => string | null
}

/**
 * The row this one nests under, or null when it is top-level.
 *
 * The backend already resolved `parent.key` to a LIVE creator (null when the creator
 * is not running, or when the crew logs formed a cycle), so the edge is followed as
 * given. Two things are still refused here, because a view must never fail to paint
 * on a payload it did not produce: a key that names no row in this payload, and a
 * chain that returns to its own start. A member of such a chain becomes a top-level
 * row; a row that merely hangs off the chain keeps its edge, since its parent is now
 * a root.
 *
 * `byKey` is keyed in whatever space `keying` names -- raw keys by default.
 */
export function nestsUnder<R extends LineageRow>(
  row: R,
  byKey: Map<string, R>,
  keying: LineageKeying<R> = {},
): string | null {
  const identityOf = keying.identityOf ?? ((r: R) => r.key)
  const parentIdentityOf = keying.parentIdentityOf ?? ((r: R) => r.parent?.key ?? null)
  const self = identityOf(row)
  const parentKey = parentIdentityOf(row)
  if (parentKey == null || parentKey === self || !byKey.has(parentKey)) return null
  const seen = new Set<string>()
  let cursor: string | null = parentKey
  while (cursor != null) {
    if (cursor === self) return null
    // Some OTHER key repeats: the chain leads into a cycle this row is not on.
    // Its members detach themselves (each sees its own key come back), so this
    // row's parent is, or hangs off, a root -- the edge is safe to keep.
    if (seen.has(cursor)) break
    seen.add(cursor)
    const next: R | undefined = byKey.get(cursor)
    if (next === undefined) break
    const nextKey = parentIdentityOf(next)
    cursor = nextKey != null && nextKey !== identityOf(next) && byKey.has(nextKey) ? nextKey : null
  }
  return parentKey
}

/**
 * Rows -> the lineage tree, preserving input order at every level.
 *
 * Order is INHERITED, never decided here: the caller hands rows in the order its lane
 * already sorted them, so roots come out in that order and so do siblings. A
 * comparator of its own would make the conductor lane disagree with the flat lane
 * about the same two sessions, for no reason a user could see.
 *
 * Depth is computed by walking placed edges rather than by recursion over children, so
 * a payload whose edges are deeper than its rows (impossible from our backend, cheap
 * to be safe about) still terminates.
 *
 * Every key in the returned `Lineage` is in `keying`'s space, so a caller that passed
 * origin-qualified identities gets them back and must look rows up the same way.
 */
export function buildLineage<R extends LineageRow>(
  rows: readonly R[],
  keying: LineageKeying<R> = {},
): Lineage {
  const identityOf = keying.identityOf ?? ((r: R) => r.key)
  const byKey = new Map<string, R>()
  for (const row of rows) if (row.key) byKey.set(identityOf(row), row)

  const roots: string[] = []
  const children = new Map<string, string[]>()
  const parentOf = new Map<string, string>()

  for (const row of rows) {
    if (!row.key) continue
    const self = identityOf(row)
    const under = nestsUnder(row, byKey, keying)
    if (under == null) {
      roots.push(self)
      continue
    }
    parentOf.set(self, under)
    const siblings = children.get(under)
    if (siblings) siblings.push(self)
    else children.set(under, [self])
  }

  const depth = new Map<string, number>()
  for (const key of byKey.keys()) {
    let steps = 0
    let cursor = parentOf.get(key)
    // Bounded by the row count: `nestsUnder` already refused every cycle, so this
    // only guards against a future caller building `parentOf` some other way.
    while (cursor != null && steps <= byKey.size) {
      steps += 1
      cursor = parentOf.get(cursor)
    }
    depth.set(key, steps)
  }
  return { roots, children, parentOf, depth }
}

/**
 * The creator a row cites but could not be placed under, or null.
 *
 * This is the ORPHAN: the session that opened this one has closed, so the row is
 * top-level and still knows who opened it. Worth surfacing rather than dropping —
 * "this was opened by something that is gone" is exactly the context a reader scanning
 * a sidebar full of sessions is missing.
 */
export function orphanCitation<R extends LineageRow>(row: R, placedUnder: string | null): string | null {
  if (placedUnder != null) return null
  const slot = row.parent?.slot
  return typeof slot === 'string' && slot !== '' ? slot : null
}

/**
 * Every ancestor of *key*, nearest first. Empty for a root or an unknown key.
 *
 * What "reveal a nested session" needs: to show a row buried three levels down, each
 * ancestor on the way to it has to be expanded, and this is that list. Bounded the
 * same way `buildLineage` bounds depth.
 */
export function ancestorsOf(key: string, parentOf: Map<string, string>): string[] {
  const out: string[] = []
  let cursor = parentOf.get(key)
  while (cursor != null && out.length <= parentOf.size) {
    out.push(cursor)
    cursor = parentOf.get(cursor)
  }
  return out
}

/** Every key in *key*'s subtree, excluding itself. For aggregating a collapsed row. */
export function descendantsOf(key: string, children: Map<string, string[]>): string[] {
  const out: string[] = []
  const stack = [...(children.get(key) ?? [])]
  const seen = new Set<string>()
  while (stack.length > 0) {
    const next = stack.pop()!
    if (seen.has(next)) continue
    seen.add(next)
    out.push(next)
    for (const child of children.get(next) ?? []) stack.push(child)
  }
  return out
}
