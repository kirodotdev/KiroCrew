/** Collision geometry for the sidebar's DndContext: which droppable a folder or
 *  session drag resolves to, and the header nest band that tells a re-parent from a
 *  reorder. */
import { type CollisionDetection, type Collision, type DroppableContainer, type ClientRect, closestCenter, pointerWithin } from '@dnd-kit/core'
import { pointerWithinDeepest, closestEdge } from '../../../components/dnd'

/**
 * Whether the pointer sits in the nest band of a collided folder's HEADER.
 *
 * Anchored to the MEASURED header height, not a constant. The droppable rect
 * spans the whole folder BLOCK (header + expanded body), so a fraction of
 * `rect.height` would balloon the nest zone on an expanded folder. The header is
 * the block's first child; its real height differs by layout — list headers
 * (text-sm py-1.5) are taller than board headers (text-[12px] py-1) — so a
 * single px constant that fit one layout mis-sized the other (the board
 * over-nest bug). Reading the header rect makes the middle-60% band correct for
 * both. Falls back to `FOLDER_HEADER_DROP_BAND`, clamped to the block, if the
 * node is unavailable (e.g. before first measure).
 *
 * A drag with no pointer coordinates (keyboard / synthetic activation) has no
 * band: there is no "where on the row" for it to answer, so it is never a nest.
 *
 * Shared by both folder branches of `sidebarCollision` so root and nested rows
 * disambiguate reorder from re-parent by the same rule — the asymmetry #10428
 * reported was a nested row having only one of the two gestures at all.
 */
function isFolderNestBandHit(args: Parameters<CollisionDetection>[0], collision: Collision): boolean {
  if (!args.pointerCoordinates) return false
  const rect = collision?.data?.droppableContainer?.rect?.current
  if (!rect) return false
  const headerRect = folderDropHeaderRect(collision?.data?.droppableContainer as DroppableContainer | undefined)
  const headerH = headerRect?.height || Math.min(rect.height, FOLDER_HEADER_DROP_BAND)
  // The band is measured from the header's LIVE top, not the block's: a sticky
  // header pinned to the lane sits below its block's top edge, so the block
  // offset would place the band over rows the header is not painted on.
  const headerTop = headerRect?.top ?? rect.top
  return isFolderNestBand(args.pointerCoordinates.y - headerTop, headerH)
}

/** The live rect of a `folder-drop` container's header row (the block's first
 *  child), or null when the node is unavailable or not yet laid out (a zero-size
 *  rect, e.g. before first measure or under jsdom). */
function folderDropHeaderRect(container: DroppableContainer | undefined): ClientRect | null {
  const node = container?.node?.current as HTMLElement | null | undefined
  const headerEl = node?.firstElementChild as HTMLElement | null | undefined
  if (!headerEl) return null
  const r = headerEl.getBoundingClientRect()
  if (!(r.width > 0) || !(r.height > 0)) return null
  return r
}

/**
 * The `folder-drop` whose HEADER ROW is painted under the pointer, or null.
 *
 * Folder headers are `position: sticky`, so a pinned header's painted position
 * is decoupled from its block's rect: with a descendant block scrolled up
 * underneath it, the pointer on the visible ancestor header is ALSO inside the
 * descendant's unclipped `folder-drop` rect, and a leaf-first hit-test on block
 * rects hands the drop to the descendant the user cannot see. The header is
 * what the user is aiming at, so it wins outright; a pointer on rows BELOW the
 * pinned header is in no header rect and keeps the block-rect resolution.
 *
 * Only real folders are considered (`folderId` set): the root lane and the
 * ungrouped bucket carry no header row as their first child. When two headers
 * both contain the pointer (a parent's header pushed out over its child's as
 * the block ends), the OUTERMOST wins, matching the paint order
 * (`FOLDER_ROW_STICKY_Z - depth`).
 */
function folderDropHeaderHit(args: Parameters<CollisionDetection>[0], containers: DroppableContainer[]): Collision | null {
  const p = args.pointerCoordinates
  if (!p) return null
  let hit: DroppableContainer | null = null
  for (const c of containers) {
    const d = c.data?.current as { type?: string; folderId?: string | null } | undefined
    if (d?.type !== 'folder-drop' || !d.folderId) continue
    const r = folderDropHeaderRect(c)
    if (!r) continue
    if (p.x < r.left || p.x > r.right || p.y < r.top || p.y > r.bottom) continue
    const node = c.node.current as HTMLElement | null
    const hitNode = hit?.node.current as HTMLElement | null | undefined
    if (!hit || (node && hitNode && node !== hitNode && node.contains(hitNode))) hit = c
  }
  return hit ? { id: hit.id, data: { droppableContainer: hit, value: 0 } } : null
}

/**
 * Folder reordering and session-to-folder assignment share one DndContext but
 * want different collision behavior:
 *  - Dragging a folder: restrict collisions to folder sortable containers so
 *    verticalListSortingStrategy animates cleanly and `over.id` is a folder id.
 *  - Dragging a session: prefer the innermost (DOM-deepest) droppable under
 *    the pointer (folder/root drop target), falling back to closest-edge.
 */
// Exported for a call-site unit test (ChatSidebar.folderNestBandCallSite.test.tsx):
// asserts the collision uses the MEASURED header height, not
// FOLDER_HEADER_DROP_BAND — the specific regression codex flagged in review.
export const sidebarCollision: CollisionDetection = (args) => {
  const activeData = args.active?.data?.current as {
    type?: string
    nested?: boolean
    subtree?: string[]
    siblings?: readonly string[]
    pinned?: boolean
    container?: string
  } | undefined
  const activeType = activeData?.type
  if (activeType === 'folder') {
    const subtree = new Set(activeData?.subtree ?? [])
    // The ring this drag may re-order within: the rows sharing its container.
    // Every folder row — root and nested — is a `folder` droppable, so without
    // this a closest-center fallback could resolve to a row in a DIFFERENT
    // container; that is a re-parent gesture, and routing it to the reorder path
    // renumbers nothing, which reads as the drag having been ignored. Absent
    // (drag data predating the field), every folder row stays eligible, which is
    // what the root lane did when it was the only sortable level.
    const siblings = activeData?.siblings
    const reorderContainers = args.droppableContainers.filter(c => {
      if ((c.data?.current as { type?: string } | undefined)?.type !== 'folder') return false
      return siblings ? siblings.includes(String(c.id)) : true
    })
    if (activeData?.nested) {
      // Nested subfolder drag: BOTH gestures, same as a root row. Target the
      // innermost folder-drop zone under the pointer (or the root lane to move to
      // top level), excluding the dragged folder's own subtree so it can never be
      // dropped into itself or a descendant.
      // Innermost = leaf-first by DOM containment: the root lane is every
      // folder's ancestor with a viewport-sized box, so pointerWithin's
      // box-size ranking would resolve a pointer on a tall expanded folder to
      // the LANE and silently un-nest the dragged subfolder instead of
      // re-parenting it.
      const dropContainers = args.droppableContainers.filter(c => {
        const d = c.data?.current as { type?: string; folderId?: string | null } | undefined
        return d?.type === 'folder-drop' && !(d.folderId && subtree.has(d.folderId))
      })
      const within = pointerWithinDeepest({ ...args, droppableContainers: dropContainers })
      // A pinned header painted over a descendant block wins outright; see
      // folderDropHeaderHit. It takes the innermost slot below so the sibling
      // thirds rule still applies to it.
      const headerHit = folderDropHeaderHit(args, dropContainers)
      const ranked = headerHit ? [headerHit, ...within.filter(c => c.id !== headerHit.id)] : within
      // The thirds rule, consulted only when the innermost zone under the pointer
      // belongs to a SIBLING: the middle band of that header re-parents INTO it,
      // its edges and everything below fall through to the sibling reorder. A
      // pointer on any other container's header is unambiguous — there is no
      // reorder to fall through to there — so it stays a re-parent at every
      // offset, exactly as it did before nested rows were reorderable.
      const innermost = ranked[0]
      const innermostId = (innermost?.data?.droppableContainer?.data?.current as { folderId?: string | null } | undefined)?.folderId
      if (innermost && innermostId && siblings?.includes(innermostId)
        && !isFolderNestBandHit(args, innermost)) {
        return closestCenter({ ...args, droppableContainers: reorderContainers })
      }
      // A pointer drag outside every drop zone deliberately has NO target
      // (releasing there keeps the current parent). A drag WITHOUT pointer
      // coordinates (keyboard / synthetic activation) has no such "outside", so
      // it degrades to closestCenter -- and it must degrade to the SIBLING RING,
      // not to the folder-drop zones. A keyboard drag has no "where on the row",
      // so it can never land in a nest band; resolving it against folder-drop
      // makes every keyboard drop a re-parent, which would leave a keyboard user
      // with exactly the harm #10428 reports (an order only an agent can set)
      // while the row walks its ring on screen. The root branch already lands
      // here for the same reason: its whole thirds block is gated on
      // `args.pointerCoordinates`, so a keyboard root drag falls straight to
      // `closestCenter(reorderContainers)`. This is that same line, one level
      // down. Without a sibling ring (drag data predating the field) there is no
      // reorder to offer, so it keeps the folder-drop resolution it had when
      // re-parent was a nested row's only gesture.
      if (ranked.length || args.pointerCoordinates) return ranked
      if (siblings) return closestCenter({ ...args, droppableContainers: reorderContainers })
      return closestCenter({ ...args, droppableContainers: dropContainers })
    }
    // Root folder drag: two gestures share the drag, disambiguated by where
    // the pointer sits on the target — the "thirds" pattern from VS Code /
    // Notion tree DnD. The MIDDLE band of another folder's header row
    // re-parents INTO it (folder-drop collision, ring highlight); the
    // header's top/bottom edges and everything below fall through to the
    // sortable reorder, so even a collapsed folder (whose whole block is
    // just the header) can still be reordered against at its edges.
    //
    // Band width is a DISCOVERABILITY lever: the original middle-50% (0.25–0.75)
    // of the header was easy to miss, so users concluded folder nesting did not
    // exist. Widening to the middle-60% (0.2–0.8, via FOLDER_HEADER_NEST_BAND_*)
    // makes the nest gesture — and its ring cue — easier to land, while the
    // top/bottom 20% of the header plus the entire folder BODY below it stay
    // reorder targets (closestCenter fallback), so reordering siblings is
    // preserved. The band is taken from the MEASURED header height below, not a
    // px constant, so it is correct for both the taller list header and the
    // shorter board header.
    if (args.pointerCoordinates) {
      const dropContainers = args.droppableContainers.filter(c => {
        const d = c.data?.current as { type?: string; folderId?: string | null } | undefined
        return d?.type === 'folder-drop' && !!d.folderId && !subtree.has(d.folderId)
      })
      const within = pointerWithin({ ...args, droppableContainers: dropContainers })
      // A pinned header painted over a descendant block is the target, not the
      // descendant whose smaller rect pointerWithin would rank first.
      const first = folderDropHeaderHit(args, dropContainers) ?? within[0]
      // Anchor the nest band to the MEASURED header height, not a constant —
      // isFolderNestBandHit owns that reasoning, and the nested branch above
      // reads it the same way.
      if (first && isFolderNestBandHit(args, first)) {
        return [first]
      }
    }
    return closestCenter({ ...args, droppableContainers: reorderContainers })
  }
  // Session drag. Containment first, leaf-first by DOM containment: the root
  // lane is the folders' ancestor but its border box is only viewport-sized
  // while an expanded folder block overflows it, so pointerWithin's box-size
  // ranking would resolve a pointer on a tall folder's own rows to the LANE —
  // no highlight on the folder, and the drop unfiles the session.
  //
  // Sidebar targets are consulted BEFORE the portaled chat-pane zone: the
  // pane's rect can geometrically overlap the sidebar in overlay layouts, and
  // with no DOM relation between the two trees containment cannot arbitrate —
  // a pointer inside any sidebar droppable belongs to the sidebar, and the
  // pane wins only when nothing in the sidebar contains the pointer.
  const sidebarContainers = args.droppableContainers.filter(c => {
    const d = c.data?.current as { type?: string; container?: string } | undefined
    if (d?.type === CHAT_PANE_DROP_TYPE) return false
    if (d?.type === 'pinned-session') {
      return activeData?.pinned === true && d.container === activeData.container
    }
    return true
  })
  // A sticky folder header pinned over a descendant block: the pointer on that
  // visible header is also inside the descendant's unclipped rect, and
  // leaf-first containment would file the session into the descendant. The
  // header the user sees wins (folderDropHeaderHit); rows below it are in no
  // header rect and keep the containment resolution.
  const headerHit = folderDropHeaderHit(args, sidebarContainers)
  if (headerHit) return [headerHit]
  const within = pointerWithinDeepest({ ...args, droppableContainers: sidebarContainers })
  if (within.length) return within
  const paneWithin = pointerWithinDeepest(args)
  if (paneWithin.length) return paneWithin
  // No pointer coordinates (keyboard / synthetic) and no sidebar droppable at
  // all: the pane is the only conceivable target, so degrade to closestCenter
  // over everything rather than resolving to nothing. Pointer drags never take
  // this path — the pane must not win by mere proximity.
  if (!args.pointerCoordinates && sidebarContainers.length === 0) return closestCenter(args)
  // Session drag that is inside no droppable: fall back to the nearest one, but
  // NEVER to the chat-pane zone. That zone is a pane-sized rect living outside
  // the sidebar, so by proximity it would routinely beat the folder row the
  // user was actually aiming at and steal near-miss drops. Nearness is
  // measured to the rect's EDGE (closestEdge), not its center: a pointer a
  // fraction of a px outside a tall expanded folder is half that folder's
  // height from its center, so closestCenter would hand the drop to a small
  // sibling instead.
  return closestEdge({ ...args, droppableContainers: sidebarContainers })
}

/** Droppable `type` for the chat-pane target that stages a session reference in
 *  the composer. Lives outside the sidebar's DOM (portaled into ChatPage's pane)
 *  but inside its DndContext, so React context reaches it while `useDroppable`
 *  measures its real on-screen rect. */
// Load-bearing invariant: the pane's portal host is never a DOM ancestor of
// the sidebar lane — that is what keeps containment re-ranking from ever
// arbitrating between the two trees (they always land in the "unrelated"
// group). Re-pointing chatDropTarget at a wrapper shared with the sidebar
// would break it.
export const CHAT_PANE_DROP_TYPE = 'chat-pane-ref'

/** Approximate height (px) of a folder header row. For root folder drags the
 *  MIDDLE 25%–75% of this band re-parents INTO the folder; the top/bottom
 *  edges (and everything below the header) stay sortable-reorder gestures —
 *  the VS Code / Notion "thirds" tree-DnD pattern. */
const FOLDER_HEADER_DROP_BAND = 34
/** Fraction of the MEASURED header height that re-parents INTO the folder (the
 *  nest zone). The middle 60% (0.2–0.8) is a modest widening of the original
 *  middle-50% — enough to make the nest gesture reliably hittable (its ring cue
 *  discoverable) without starving reorder: the top/bottom 20% of the header stay
 *  reorder edges, and the whole folder BODY below the header is always reorder.
 *  sidebarCollision multiplies these by the measured header height (not a px
 *  constant) so the same fractions are correct for both the taller list header
 *  and the shorter board header. */
const FOLDER_HEADER_NEST_BAND_LO = 0.2
const FOLDER_HEADER_NEST_BAND_HI = 0.8

/** True when a pointer at `offsetY` px below a folder header's top falls in the
 *  NEST band (re-parent INTO the folder); false means the top/bottom edge, which
 *  falls through to sortable REORDER. `headerH` is the MEASURED header height so
 *  the same fractions work for the taller list header and the shorter board
 *  header. Extracted + exported so the reorder-vs-nest boundary is unit-tested
 *  directly (the DOM-marker tests can't reach this math). */
export function isFolderNestBand(offsetY: number, headerH: number): boolean {
  return offsetY >= headerH * FOLDER_HEADER_NEST_BAND_LO && offsetY <= headerH * FOLDER_HEADER_NEST_BAND_HI
}
