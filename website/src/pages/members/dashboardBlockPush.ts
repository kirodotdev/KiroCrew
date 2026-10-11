/**
 * The browser's RECEIVE SIDE of the dashboard block patch.
 *
 * A crew-log fold advancing does not re-read the whole page any more. The
 * controller pushes only the blocks that SUBSCRIBE to the fold that moved, and
 * the Dashboard tab hands them to its sandboxed document with `postMessage` --
 * the only channel there is, because the frame sits on an opaque origin and its
 * DOM is unreachable from here. A gap, or a layout change, is not patched: it
 * triggers a full refetch, because the blocks a patch names only exist in the
 * layout it was composed against.
 *
 * ## WHERE THIS SHAPE COMES FROM
 *
 * The frame is the CONTROLLER LINE's (D2, chat-2622), relayed by conductor
 * chat-2620 and then read back out of D2's own module to be sure. The server
 * owns the type string, so this side adapts and never negotiates:
 *
 *     handlers/member_dashboard_push.py
 *       BLOCK_FRAME: Final[str] = "dashboard_block_patch"
 *       broadcast(BLOCK_FRAME, {slug, dashboard, version, layout,
 *                               fold, blocks, missing, refetch, reason})
 *
 * `dashboardBlockPush.test.ts` pins that constant's VALUE verbatim against the
 * python source, so a server-side rename reddens a test here instead of
 * silently turning the push path off -- which is the failure mode that matters,
 * because a frame nobody routes looks exactly like a fold that never advanced.
 *
 * ## THE TWO INDEPENDENT REFETCH TRIGGERS
 *
 * `version` is a per-live-page counter, +1 per frame ACTUALLY SENT. The server
 * spends no version when nothing subscribes to the advancing fold, so the only
 * sound comparison is against the last version this page RECEIVED -- not
 * against a fold count, a seq, or anything derived from the read.
 *
 * `layout` is the package artifact's own version, and it moves only when
 * `model` / `view` / `theme` do. It is checked separately and not folded into
 * the version check, because the two catch different things: a contiguous
 * version with a changed layout is the ordinary recompose, and a patch for a
 * view the page is not showing cannot be applied however well-numbered it is.
 */

/** The WS message type a patch arrives as. D2's `BLOCK_FRAME`, verbatim. */
export const DASHBOARD_BLOCK_PATCH_FRAME = 'dashboard_block_patch'

/** `reason` on a refetch frame: the package's layout moved. D2's `REASON_LAYOUT`. */
export const PATCH_REASON_LAYOUT = 'layout_changed'
/** `reason` on a refetch frame: the package is gone or bound elsewhere now.
 *  A rebind writes no version (the package line's ruling), so it is invisible to
 *  the `layout` check and arrives only as this. */
export const PATCH_REASON_UNBOUND = 'package_unbound'

/**
 * THE RENDERER'S OWN BLOCK PATCH, forwarded into the document verbatim.
 *
 * Built by `dashboard_package_render.block_patch()` and carried on the frame as
 * `patch`. It arrives with its OWN `type`
 * (`dashboard_package_render.BLOCK_PATCH_MESSAGE_TYPE`), which is why nothing on
 * this side constructs it, narrows it, formats it, or even names its type: the
 * frontend's whole job is to decide whether the frame belongs to the page on
 * screen and then hand this object over unchanged.
 *
 * Each block carries only the fields IT renders, so a patch cannot put a value
 * on a block the view never gave it, and `display` comes from the same
 * formatter the first render used -- which is what keeps a refilled cell and a
 * first-painted cell identical.
 *
 * Declared as a type only so the frame's shape is readable here. Nothing reads
 * past `type`: treating it as opaque is the property that keeps the narrowing,
 * the formatting and the message name all in Python.
 */
export interface DashboardBlockPatchPayload {
  /** The document's block-patch listener type. NOT the full-paint type. */
  type?: string
  /** Block id -> `{fields, display}`, each holding only that block's own fields. */
  blocks?: Record<string, { fields?: Record<string, unknown>; display?: Record<string, string> }>
  seq?: number
  stale?: boolean
  missing?: string[]
}

/**
 * A page's whole read, in the shape the DOCUMENT accepts for a FULL PAINT.
 *
 * `dashboard_frame.read_payload` builds it, and it is the same shape the first
 * paint's data island carries -- which is the point: a reader cannot be shown a
 * first paint and a refresh that describe the same read differently.
 *
 * This is the body's `read`, for a full repaint. It is NOT what a fold push
 * carries: a push forwards `patch` instead, and the difference is load-bearing
 * rather than cosmetic (see `PAGE_FULL_PAINT_MESSAGE_TYPE`).
 */
export interface DashboardReadPayload {
  /** Field name -> value, redacted. */
  fields?: Record<string, unknown>
  /** Field name -> the string to SHOW. Absent on a build with no renderer, and
   *  then the document falls back to a plain `String(value)` -- an unformatted
   *  number a reader can see is unformatted, rather than one formatted by a
   *  second set of rules that drifted. */
  display?: Record<string, string>
  /** Names the crewmate writes itself, rather than reading from a fold. */
  agentic?: string[]
  /** The highest fold sequence any of these values came from. */
  seq?: number
  /** A fold-backed cell did not resolve, which is a band. An agentic field the
   *  crewmate never wrote is ABSENT rather than stale, so it does not raise it. */
  stale?: boolean
  /** Names with neither a value nor a display string: the page DIMS those cells.
   *  Deliberately empty of both -- an empty string would render as a filled cell
   *  holding nothing, which is worse than dimmed. */
  missing?: string[]
  /** Agentic field -> when the crewmate wrote it. */
  written_at?: Record<string, string>
  /** The language the page renders its own words in. */
  locale?: string
}

/** One frame off the owner socket. */
export interface DashboardBlockPatch {
  /** The crewmate whose page this is. */
  slug: string
  /** The dashboard artifact's slug. */
  dashboard: string
  /** Per-live-page counter, +1 per frame sent. The gap detector. */
  version: number
  /** The package artifact's VERSION. A change means recompose. */
  layout: number
  /** The fold that advanced. `""` on a refetch frame. */
  fold: string
  /**
   * Block id -> the FIELD NAMES in it that moved. Only blocks subscribing to
   * `fold`; `{}` on a refetch frame.
   *
   * NAMES, NOT VALUES, and the distinction is the reason this type changed: a
   * value copy here would be a second home for the same number and an
   * UNFORMATTED one. So this says WHICH blocks moved and which of their fields,
   * and `read` says what everything is. Nothing may read a value from here,
   * because there is none to read.
   *
   * Safe as keys with no redaction: the package gate matches a block id and a
   * field name against `[a-z][a-z0-9_-]{0,63}`, so neither is agent free text.
   */
  blocks: Record<string, string[]>
  /** Field names whose path did not resolve. NAMED rather than carried as a
   *  value, because a null would render as a zero and the page dims the cell
   *  instead. */
  missing: string[]
  /**
   * THE PAYLOAD, forwarded into the document unchanged. `{}` on a refetch frame.
   *
   * The frame's `blocks` above is DERIVED from this by the server, so the frame
   * cannot name one set of blocks while the payload carries another. That is
   * also why `blocks` is safe to use for the fit check and this is not: one is
   * the frame's claim, the other is the thing the document will act on.
   */
  patch: DashboardBlockPatchPayload
  /** The page must read the whole thing again. Carries no values by design. */
  refetch: boolean
  /** `""`, or one of the two `PATCH_REASON_*` values when `refetch`. */
  reason: string
}

/**
 * The frame, or null for anything this build cannot read.
 *
 * Null rather than a partial: a patch missing its `layout` cannot be placed
 * against the view on screen, and applying it would write values into a layout
 * they may not belong to. Dropping it costs one update, which the finite
 * fallback refetch covers; applying it costs the reader a number under the
 * wrong label.
 */
export function readBlockPatchFrame(data: unknown): DashboardBlockPatch | null {
  if (!data || typeof data !== 'object') return null
  const frame = data as Record<string, unknown>
  const slug = typeof frame.slug === 'string' ? frame.slug : ''
  if (!slug) return null
  const num = (value: unknown) => (typeof value === 'number' && Number.isFinite(value) ? value : null)
  const version = num(frame.version)
  const layout = num(frame.layout)
  if (version === null || layout === null) return null
  const raw = frame.blocks
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const blocks: Record<string, string[]> = {}
  for (const [id, names] of Object.entries(raw as Record<string, unknown>)) {
    // AN ARRAY OF NAMES. A mapping here would be the old value-carrying shape,
    // which is refused rather than coerced: a frame from a gateway that still
    // sends values has no `read` to apply either, and reading its values would be
    // the second, unformatted home this shape exists to remove.
    if (!Array.isArray(names)) return null
    if (names.some(name => typeof name !== 'string')) return null
    blocks[id] = names as string[]
  }
  const patch = frame.patch
  if (!patch || typeof patch !== 'object' || Array.isArray(patch)) return null
  const missing = Array.isArray(frame.missing)
    ? frame.missing.filter((name): name is string => typeof name === 'string')
    : []
  return {
    slug,
    dashboard: typeof frame.dashboard === 'string' ? frame.dashboard : '',
    version,
    layout,
    fold: typeof frame.fold === 'string' ? frame.fold : '',
    blocks,
    missing,
    patch: patch as DashboardBlockPatchPayload,
    refetch: frame.refetch === true,
    reason: typeof frame.reason === 'string' ? frame.reason : '',
  }
}

/**
 * The FULL-PAINT message type, and the one a fold push must never use.
 *
 * `dashboard_frame.DATA_MESSAGE_TYPE`. The document's listener for it replaces
 * the whole frozen `read` and RE-INITIALISES every block, which is correct for a
 * first paint or a deliberate full refresh and wrong for a fold advancing: a
 * block that owns a canvas gets a second one, and two scenes animate over each
 * other on top of the cells they were drawing.
 *
 * Named for what it IS. It was previously called `BLOCK_PATCH_MESSAGE_TYPE` on
 * this side and posted on every patch, which is exactly that bug -- the name
 * made a full repaint read as the narrow thing it is not. A fold push now
 * forwards the renderer's own patch, which carries its own distinct type
 * (`dashboard_package_render.BLOCK_PATCH_MESSAGE_TYPE`,
 * `kirocrew-dashboard:block-patch`), and a test pins that the two differ.
 *
 * The server sends BOTH names in the read body (`page_message` and
 * `page_patch_message`) precisely so the frontend need hold neither. This
 * constant is kept as the fallback for a body that carries no `page_message`,
 * and as the thing the cross-language pin compares against.
 */
export const PAGE_FULL_PAINT_MESSAGE_TYPE = 'kirocrew-dashboard:data'

/**
 * What the document receives for a FULL PAINT: the whole read, and nothing else.
 *
 * One key, because the listener uses one -- `read = freeze(data.read)`, then
 * `fill()`. Not what a fold push sends; see `PAGE_FULL_PAINT_MESSAGE_TYPE`.
 */
export interface DashboardFullPaintMessage {
  type: string
  read: DashboardReadPayload
}

/** A full repaint message, with the type the SERVER named if it sent one. */
export function fullPaintMessage(
  read: DashboardReadPayload,
  pageMessage?: string,
): DashboardFullPaintMessage {
  return { type: pageMessage || PAGE_FULL_PAINT_MESSAGE_TYPE, read }
}

/**
 * What the page does with a patch, given where it stands.
 *
 * `refetch` is the fail-safe answer for every case where the patch cannot be
 * placed. The page then re-reads the whole dashboard, which is correct by
 * construction: the push protocol is an optimisation over that read and never
 * the only way a value arrives.
 */
export type PatchVerdict = 'apply' | 'refetch'

/** Where the page stands: the package version its document was composed at, and
 *  the highest patch version it has RECEIVED (`null` before the first one). */
export interface PatchPosition {
  layout: number
  lastVersion: number | null
}

export function verdictFor(patch: DashboardBlockPatch, at: PatchPosition): PatchVerdict {
  // THE SERVER ALREADY DECIDED. A refetch frame carries no values at all, so
  // there is nothing to apply even if every number below agreed.
  if (patch.refetch) return 'refetch'
  // NO PAYLOAD, SO NOTHING TO FORWARD. The server sends no frame at all when
  // nothing subscribes, so a non-refetch frame with an empty `patch` is a
  // gateway and a bundle that disagree rather than an ordinary quiet tick --
  // and forwarding `{}` would post a typeless message the document drops in
  // silence while this side spent a version on it. A re-read answers both.
  if (!patch.patch.type) return 'refetch'
  // A LAYOUT CHANGE, which is the one thing a block patch cannot express: the
  // blocks it names belong to the old view, and the new view may not have them.
  // Checked independently of the version, because a perfectly contiguous version
  // is exactly what a recompose produces.
  if (patch.layout !== at.layout) return 'refetch'
  // Nothing received yet and the read carried no counter, so there is no gap to
  // measure. Seeded from this frame rather than refused, so a tab is live
  // immediately; the read's own `push_version` is what makes even the first frame
  // checkable, and `seedVersion` reads it.
  if (at.lastVersion === null) return 'apply'
  // STRICT EQUALITY, and the strictness is the point.
  //
  // Only the very next frame can be applied. Everything else -- a gap forward, a
  // repeat, and in particular a LOWER version -- is answered by re-reading.
  //
  // A lower version is not a harmless replay, which is how this was first
  // written. The push counter lives in memory on the server, per live page, so a
  // GATEWAY RESTART arms a fresh page at 0 and the next frame arrives below
  // whatever the tab is holding. Treating that as a replay drops it, drops every
  // frame after it, and leaves the tab frozen on the values it had before the
  // restart with nothing on screen saying so -- until the counter happens to
  // climb back past the held number, at which point the page silently resumes
  // having missed everything in between.
  //
  // A refetch is the safe answer to all three cases: it re-reads the truth and
  // re-seeds `held` from the body. The cost is one extra read on a duplicate
  // frame, which is the cheaper mistake by a wide margin.
  if (patch.version !== at.lastVersion + 1) return 'refetch'
  return 'apply'
}

/**
 * The patch version a freshly-read page starts from.
 *
 * `push_version` is the controller's own statement of where the stream stood
 * when it composed this body, so the FIRST frame after a read is checkable for
 * a gap exactly like every later one. Null when the body does not carry it (an
 * older gateway, or the v2 template path), and then the first frame is trusted
 * -- see `verdictFor`.
 */
export function seedVersion(read: { push_version?: number } | null | undefined): number | null {
  const version = read?.push_version
  return typeof version === 'number' && Number.isFinite(version) ? version : null
}

/**
 * Whether every block a patch names is one the page is actually showing.
 *
 * The block ids come from the READ's own `blocks` map, which is every block's
 * values and therefore the whole view in data -- the browser never receives
 * `view.blocks` itself, because the document is composed server-side.
 *
 * A patch for an unknown block id is not a harmless extra: it means the page and
 * the controller disagree about the view while `layout` says they do not, which
 * is the one case the layout number cannot catch. So it is answered the same
 * way -- re-read.
 */
export function patchFitsPage(patch: DashboardBlockPatch, shownBlockIds: readonly string[]): boolean {
  const placed = new Set(shownBlockIds)
  return Object.keys(patch.blocks).every(id => placed.has(id))
}

/**
 * A frame router cannot reach into a mounted component, and a mounted component
 * is not on the socket. This is the whole bridge between them: the router
 * publishes, the open tab subscribes by slug.
 *
 * Deliberately NOT a react-query cache write. The patched values never enter the
 * read's cached body: the body holds the layout and the values AS COMPOSED, and
 * the layout did not change -- that is the premise of the whole push path.
 * Writing them in would make the cache a half-copy of the document's state, with
 * no reader for it.
 */
type PatchListener = (patch: DashboardBlockPatch) => void

const listeners = new Map<string, Set<PatchListener>>()

export function subscribeBlockPatch(slug: string, listener: PatchListener): () => void {
  const slot = listeners.get(slug) ?? new Set<PatchListener>()
  slot.add(listener)
  listeners.set(slug, slot)
  return () => {
    const live = listeners.get(slug)
    if (!live) return
    live.delete(listener)
    if (live.size === 0) listeners.delete(slug)
  }
}

/**
 * Hand a frame to whichever tab is open on that crewmate. Returns how many
 * listeners took it, so a caller can tell a patch for an open tab from one for
 * a crewmate nobody is looking at.
 *
 * EVERY LISTENER IS ISOLATED. One throwing must not cost the others their frame:
 * two tabs can be open on the same crewmate (the Members page's and a second
 * window's), and a listener whose component is mid-teardown can throw out of the
 * refetch it starts. Without the isolation, delivery stops at that one and every
 * later tab silently misses the update -- which looks like a fold that did not
 * advance, with nothing anywhere saying otherwise.
 *
 * A snapshot of the set is iterated, because the ordinary outcome of a frame is
 * a re-read that unmounts a tab and unsubscribes it mid-loop.
 */
export function publishBlockPatch(patch: DashboardBlockPatch): number {
  const slot = listeners.get(patch.slug)
  if (!slot) return 0
  const taking = [...slot]
  for (const listener of taking) {
    try {
      listener(patch)
    } catch {
      // Swallowed deliberately and not logged: the thrower is a component the
      // frame is no longer about, and a console line per frame per dead tab would
      // bury the one warning this module does raise.
    }
  }
  return taking.length
}

/** For tests: drop every subscription. A listener surviving a case would
 *  receive the next case's frames. */
export function __resetBlockPatchForTests(): void {
  listeners.clear()
}
