/** The conductor lane's session tree as rows: who opened whom, each row with the
 *  depth, chevron, child count and collapsed-subtree badges the session card draws
 *  (`ConductorRowView`). One builder, read by the two places that nest sessions by
 *  creator: the conductor lane (./listLanes) over every row, and a Project's body
 *  (./folderBlocks) over the rows filed in that Project. */
import { buildLineage, closedCreatorCitation, descendantsOf, orphanCitation, type Lineage } from '../../lib/sessionLineage'
import { citedCreatorOf } from './citedCreator'
import type { ConductorRowView } from './rows'
import { sessionRowIdentity } from './rowIdentity'
import type { Slot } from './types'

/** One row of the tree, in render order. */
export interface ConductorTreeRow extends ConductorRowView {
  /** The row's identity (`sessionRowIdentity`), the key the tree is built on. */
  id: string
  slot: Slot
}

/**
 * The lineage tree over *rows*, keyed by `sessionRowIdentity`.
 *
 * Keyed by identity, not raw slot key: the list mixes local rows with rows federated
 * from a peer, and the two namespaces collide on deterministic keys. Keyed raw, one
 * of a colliding pair replaces the other -- a session disappears and its twin renders
 * twice.
 *
 * A citation needs the same care from the other direction. `parent.key` is a bare
 * slot key in the key space of the CHILD's own gateway, so the creator is the row
 * carrying that key with the SAME origin. Resolving through this index rather than
 * composing the qualified form keeps that format the server's alone, and makes a peer
 * row unable to nest under a local row whose key merely matches.
 *
 * Nested by origin rather than keyed on one joined string: there is then no
 * separator, so no peer id or slot key containing it can be read as the wrong pair.
 */
export function rowLineage(rows: readonly Slot[]): Lineage {
  const byOrigin = new Map<string | undefined, Map<string, string>>()
  for (const s of rows) {
    let inOrigin = byOrigin.get(s.peer_id)
    if (inOrigin === undefined) {
      inOrigin = new Map<string, string>()
      byOrigin.set(s.peer_id, inOrigin)
    }
    inOrigin.set(s.key, sessionRowIdentity(s))
  }
  return buildLineage(rows as Slot[], {
    identityOf: sessionRowIdentity,
    parentIdentityOf: s => {
      const cited = citedCreatorOf(s)
      if (cited === null) return null
      return byOrigin.get(cited.origin)?.get(cited.key) ?? null
    },
  })
}

/** What the tree walk reads. */
export interface ConductorTreeInputs {
  tree: Lineage
  /** Row by identity, for every row the tree was built over. */
  byKey: ReadonlyMap<string, Slot>
  /** The identities that may render: every match plus the ancestors it hangs from. */
  kept: ReadonlySet<string>
  /** The identities the active filter ADMITS; a kept row outside it is an anchor. */
  matching: ReadonlySet<string>
  /** The open conductors (`mc-sidebar-conductor-expanded`). */
  expanded: ReadonlySet<string>
  /** `origin -> slot keys` over the unfiltered population (see `useConductorLane`). */
  citedCreatorExists: ReadonlyMap<string | undefined, ReadonlySet<string>>
  /** Does this row want the user? Counted into a collapsed conductor's badge. */
  wantsUser: (s: Slot) => boolean
  isRunning: (s: Slot) => boolean
  /** Root order. Defaults to the tree's own, which is the order it was built in. */
  roots?: readonly string[]
}

/** Does this row want the user? The same signals the row itself renders as a dot or
 *  a subtitle, so a collapsed conductor's badge and its children's badges can never
 *  disagree.
 *
 *  Both predicates are derived from the state the CHILD ROWS themselves render from,
 *  because the collapsed aggregate claims to be the same fact at a coarser zoom: a
 *  count that disagrees with the glyphs it stands for is worse than no count.
 *  `isRunning` is the widened signal -- own turn, a live workflow, or a loop -- so a
 *  workflow-active child is counted the way its own row is drawn, and a subagent
 *  awaiting approval is an ask even though the slot itself carries no
 *  `pending_approval`. */
export function wantsUserOf({ subagentApprovalCounts, unreadSet, isRunning }: {
  subagentApprovalCounts: Readonly<Record<string, number>>
  unreadSet: ReadonlySet<string>
  isRunning: (s: Slot) => boolean
}): (s: Slot) => boolean {
  return (s: Slot) =>
    !!(
      s.pending_approval
      || s.needs_input
      || (subagentApprovalCounts[s.key] || 0) > 0
      || (unreadSet.has(s.key) && !isRunning(s))
    )
}

/** The tree in render order: each kept root, then -- while it is open -- its kept
 *  children, depth first. A shut conductor carries its subtree's badges instead. */
export function conductorTreeRows(inputs: ConductorTreeInputs): ConductorTreeRow[] {
  const { tree, byKey, kept, matching, expanded: open, citedCreatorExists, wantsUser, isRunning } = inputs
  const keptKids = (key: string) => (tree.children.get(key) ?? []).filter(k => kept.has(k))
  const rows: ConductorTreeRow[] = []
  const emit = (key: string, depth: number) => {
    const slot = byKey.get(key)
    if (!slot) return
    const kids = keptKids(key)
    const expanded = open.has(key)
    const subtree = kids.length > 0 && !expanded
      ? descendantsOf(key, tree.children).filter(k => kept.has(k))
      : []
    // Which of the two citation glyphs this row earns. `orphanCitation` only
    // knows the row was placed under nothing; whether that is because the
    // creator closed or because the folder filter conceals it is decided
    // against the unfiltered population. A creator that is still there is
    // open and running, so saying it closed would be false.
    //
    // `closedCreatorCitation` is the row the backend re-parented: it IS
    // placed, under the nearest ancestor still open, and its own creator
    // closed. Same fact as an orphan's, so the same glyph, and the backend
    // said so outright -- which is why `ancestor` settles the reading here
    // rather than the population being consulted for it.
    const placedUnder = tree.parentOf.get(key) ?? null
    const cited = orphanCitation(slot, placedUnder)
      ?? closedCreatorCitation(slot, placedUnder)
    const creator = citedCreatorOf(slot)
    const creatorStillOpen = cited != null && slot.parent?.ancestor !== true
      && creator !== null
      && (citedCreatorExists.get(creator.origin)?.has(creator.key) ?? false)
    rows.push({
      id: key,
      slot,
      depth,
      childCount: kids.length,
      expanded,
      orphanOf: creatorStillOpen ? null : cited,
      citesParent: creatorStillOpen ? cited : null,
      anchorOnly: !matching.has(key),
      // Only a COLLAPSED conductor aggregates: while it is open its
      // children show their own badges, and showing both would count the
      // same session twice on one screen.
      aggregate: subtree.length > 0
        ? {
          needsYou: subtree.filter(k => { const c = byKey.get(k); return c ? wantsUser(c) : false }).length,
          running: subtree.filter(k => { const c = byKey.get(k); return c ? isRunning(c) : false }).length,
        }
        : null,
    })
    if (!expanded) return
    for (const kid of kids) emit(kid, depth + 1)
  }
  for (const key of inputs.roots ?? tree.roots) if (kept.has(key)) emit(key, 0)
  return rows
}

/** The search answer: every match at depth 0 in the order given, with no chevrons,
 *  each still citing the creator it would otherwise nest under. */
export function flattenedTreeRows(rows: readonly Slot[]): ConductorTreeRow[] {
  return rows.map(s => ({
    id: sessionRowIdentity(s), slot: s, depth: 0, childCount: 0, expanded: false,
    orphanOf: null, citesParent: s.parent?.slot ?? null, anchorOnly: false, aggregate: null,
  }))
}

/**
 * A Project's roots, top thread first.
 *
 * A Project is one conductor the person talks to plus the work it hands out, so the
 * thread the person STARTED -- a root that cites no creator -- leads, the oldest
 * first: that is the project's first chat, and a newer reply from one of its workers
 * never pushes it down. Roots that do cite a creator (a worker whose conductor has
 * closed) follow in the order the tree was built in, which is the lane's.
 */
export function topThreadFirst(tree: Lineage, byKey: ReadonlyMap<string, Slot>): string[] {
  const started: string[] = []
  const rest: string[] = []
  for (const id of tree.roots) (byKey.get(id)?.parent == null ? started : rest).push(id)
  const created = (id: string) => byKey.get(id)?.created ?? ''
  // Stable: a row with no `created` keeps its lane position after the dated ones.
  started.sort((a, b) => {
    const ca = created(a)
    const cb = created(b)
    if (ca === cb) return 0
    if (ca === '') return 1
    if (cb === '') return -1
    return ca < cb ? -1 : 1
  })
  return [...started, ...rest]
}
